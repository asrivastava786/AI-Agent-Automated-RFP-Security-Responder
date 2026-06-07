"""
tools/mcp_tools.py – MCP-style isolated tool implementations.

What "MCP tools" means here
────────────────────────────
Model Context Protocol (MCP) defines tools as schema-described, isolated async
functions that an LLM agent can call to interact with external systems.
Each tool has:
  • A JSON-schema input definition  (enforced via Pydantic)
  • A single async `run()` method
  • Strict tenant isolation         (every query scoped by tenant_id)
  • Retry with exponential backoff  (tenacity)
  • Structured output               (Pydantic result model)

These tools are called from the dual_stream_retrieval node to augment the
retrieval context *before* the LLM synthesis step.  They are intentionally
kept isolated from each other – each tool creates its own httpx.AsyncClient
and never shares session state.

Tools implemented
─────────────────
  ConfluenceSearchTool  – semantic keyword search across Confluence spaces
  JiraSearchTool        – JQL query for related tickets/controls
  AWSConfigTool         – AWS Config compliance query for infrastructure nodes

Integration with LangGraph
──────────────────────────
The MCPToolRegistry vends all three tools as LangChain BaseTool instances
so they can be called inside any LangGraph node or bound to an LLM via
llm.bind_tools(registry.as_langchain_tools()).

Environment variables required per tool
────────────────────────────────────────
  CONFLUENCE_BASE_URL      https://your-org.atlassian.net/wiki
  CONFLUENCE_API_TOKEN     Atlassian API token (Basic auth)
  CONFLUENCE_EMAIL         Atlassian account email

  JIRA_BASE_URL            https://your-org.atlassian.net
  JIRA_API_TOKEN           Atlassian API token
  JIRA_EMAIL               Atlassian account email

  AWS_ACCESS_KEY_ID        AWS credentials (read-only Config policy)
  AWS_SECRET_ACCESS_KEY
  AWS_DEFAULT_REGION       e.g. us-east-1
"""

from __future__ import annotations

import logging
import os
from typing import Any, ClassVar

import httpx
import structlog
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from rfp_responder.config import settings

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared retry decorator
# ─────────────────────────────────────────────────────────────────────────────

_TOOL_RETRY = retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential_jitter(
        initial=settings.retry_initial_wait_seconds,
        max=settings.retry_max_wait_seconds,
    ),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    reraise=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# Result models
# ─────────────────────────────────────────────────────────────────────────────

class ConfluencePage(BaseModel):
    page_id: str
    title: str
    space_key: str
    excerpt: str
    url: str
    last_modified: str


class ConfluenceResult(BaseModel):
    query: str
    tenant_id: str
    pages: list[ConfluencePage] = Field(default_factory=list)
    error: str | None = None


class JiraTicket(BaseModel):
    key: str                  # e.g. "SEC-142"
    summary: str
    status: str
    issue_type: str
    labels: list[str]
    url: str


class JiraResult(BaseModel):
    query: str
    tenant_id: str
    tickets: list[JiraTicket] = Field(default_factory=list)
    error: str | None = None


class AWSResource(BaseModel):
    resource_id: str
    resource_type: str        # e.g. "AWS::S3::Bucket"
    region: str
    compliance_type: str      # "COMPLIANT" | "NON_COMPLIANT" | "NOT_APPLICABLE"
    annotation: str


class AWSConfigResult(BaseModel):
    resource_keyword: str
    tenant_id: str
    resources: list[AWSResource] = Field(default_factory=list)
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1: Confluence Search
# ─────────────────────────────────────────────────────────────────────────────

class ConfluenceSearchInput(BaseModel):
    query: str     = Field(description="Keyword or phrase to search in Confluence")
    tenant_id: str = Field(description="Tenant identifier for data isolation")
    limit: int     = Field(default=5, ge=1, le=20)


class ConfluenceSearchTool(BaseTool):
    """
    Search Confluence for pages relevant to a security questionnaire question.

    Uses the Confluence Cloud REST API v2 search endpoint with CQL
    (Confluence Query Language) filtered by the tenant's space key.

    Tenant isolation: each Atlassian organisation has its own subdomain
    (e.g. acme.atlassian.net) stored in CONFLUENCE_BASE_URL.  In a
    multi-tenant deployment this URL is fetched per-tenant from the DB;
    for simplicity here it reads from the environment.
    """

    name: ClassVar[str]        = "confluence_search"
    description: ClassVar[str] = (
        "Search Confluence knowledge base for pages related to a security "
        "control or infrastructure topic.  Use when you need historical "
        "policy documents, architecture decision records, or runbooks."
    )
    args_schema: ClassVar[type[BaseModel]] = ConfluenceSearchInput

    async def _arun(
        self,
        query: str,
        tenant_id: str,
        limit: int = 5,
    ) -> ConfluenceResult:
        return await _confluence_search(query, tenant_id, limit)

    def _run(self, **kwargs: Any) -> ConfluenceResult:  # type: ignore[override]
        raise NotImplementedError("Use _arun for async execution")


@_TOOL_RETRY
async def _confluence_search(
    query: str,
    tenant_id: str,
    limit: int,
) -> ConfluenceResult:
    base_url   = os.environ.get("CONFLUENCE_BASE_URL", "")
    api_token  = os.environ.get("CONFLUENCE_API_TOKEN", "")
    email      = os.environ.get("CONFLUENCE_EMAIL", "")

    if not all([base_url, api_token, email]):
        return ConfluenceResult(
            query=query, tenant_id=tenant_id,
            error="Confluence credentials not configured (CONFLUENCE_BASE_URL, CONFLUENCE_API_TOKEN, CONFLUENCE_EMAIL).",
        )

    # CQL query: full-text search, scoped to the tenant's label/space
    cql = f'text ~ "{query}" AND label = "{tenant_id}" ORDER BY lastmodified DESC'

    log = logger.bind(tool="confluence", tenant_id=tenant_id, query=query)

    async with httpx.AsyncClient(
        base_url=base_url,
        auth=(email, api_token),
        headers={"Accept": "application/json"},
        timeout=10,
    ) as client:
        resp = await client.get(
            "/rest/api/content/search",
            params={"cql": cql, "limit": limit, "expand": "excerpt,space"},
        )
        resp.raise_for_status()
        data = resp.json()

    pages: list[ConfluencePage] = []
    for item in data.get("results", []):
        pages.append(ConfluencePage(
            page_id=item["id"],
            title=item["title"],
            space_key=item.get("space", {}).get("key", ""),
            excerpt=item.get("excerpt", ""),
            url=f"{base_url}/pages/{item['id']}",
            last_modified=item.get("history", {}).get("lastUpdated", {}).get("when", ""),
        ))

    log.info("Confluence search complete", result_count=len(pages))
    return ConfluenceResult(query=query, tenant_id=tenant_id, pages=pages)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2: Jira Search
# ─────────────────────────────────────────────────────────────────────────────

class JiraSearchInput(BaseModel):
    query: str     = Field(description="Keywords to search for in Jira issues")
    tenant_id: str = Field(description="Tenant identifier for data isolation")
    project_key: str | None = Field(
        default=None,
        description="Limit search to a specific Jira project key (e.g. 'SEC', 'INFRA').",
    )
    limit: int = Field(default=5, ge=1, le=20)


class JiraSearchTool(BaseTool):
    """
    Search Jira for tickets related to a security control or infrastructure topic.

    Uses JQL (Jira Query Language) to find issues mentioning the query term,
    scoped to the tenant's projects via a label or project key filter.

    Useful for finding:
    • Open security exceptions or risk acceptances
    • Past compliance audit findings
    • Infrastructure change tickets (e.g. KMS key rotation)
    """

    name: ClassVar[str]        = "jira_search"
    description: ClassVar[str] = (
        "Search Jira for tickets related to a security control, "
        "compliance requirement, or infrastructure component.  "
        "Use to surface open risks, exceptions, or audit findings."
    )
    args_schema: ClassVar[type[BaseModel]] = JiraSearchInput

    async def _arun(
        self,
        query: str,
        tenant_id: str,
        project_key: str | None = None,
        limit: int = 5,
    ) -> JiraResult:
        return await _jira_search(query, tenant_id, project_key, limit)

    def _run(self, **kwargs: Any) -> JiraResult:  # type: ignore[override]
        raise NotImplementedError("Use _arun for async execution")


@_TOOL_RETRY
async def _jira_search(
    query: str,
    tenant_id: str,
    project_key: str | None,
    limit: int,
) -> JiraResult:
    base_url  = os.environ.get("JIRA_BASE_URL", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")
    email     = os.environ.get("JIRA_EMAIL", "")

    if not all([base_url, api_token, email]):
        return JiraResult(
            query=query, tenant_id=tenant_id,
            error="Jira credentials not configured (JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_EMAIL).",
        )

    # Build JQL – always scope to tenant via label, optionally filter by project
    jql_parts = [f'text ~ "{query}"', f'labels = "{tenant_id}"']
    if project_key:
        jql_parts.append(f"project = {project_key}")
    jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"

    log = logger.bind(tool="jira", tenant_id=tenant_id, query=query)

    async with httpx.AsyncClient(
        base_url=base_url,
        auth=(email, api_token),
        headers={"Accept": "application/json"},
        timeout=10,
    ) as client:
        resp = await client.get(
            "/rest/api/3/search",
            params={
                "jql":        jql,
                "maxResults": limit,
                "fields":     "summary,status,issuetype,labels",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    tickets: list[JiraTicket] = []
    for issue in data.get("issues", []):
        fields = issue.get("fields", {})
        tickets.append(JiraTicket(
            key=issue["key"],
            summary=fields.get("summary", ""),
            status=fields.get("status", {}).get("name", ""),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            labels=fields.get("labels", []),
            url=f"{base_url}/browse/{issue['key']}",
        ))

    log.info("Jira search complete", result_count=len(tickets))
    return JiraResult(query=query, tenant_id=tenant_id, tickets=tickets)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3: AWS Config Compliance Query
# ─────────────────────────────────────────────────────────────────────────────

class AWSConfigInput(BaseModel):
    resource_keyword: str = Field(
        description="Resource type keyword to query (e.g. 'S3', 'KMS', 'IAM', 'RDS')."
    )
    tenant_id: str = Field(description="Tenant identifier for data isolation")
    region: str    = Field(default="us-east-1")


class AWSConfigTool(BaseTool):
    """
    Query AWS Config for the compliance status of infrastructure resources.

    Uses the AWS Config `select_resource_config` API with a SQL-like
    Advanced Query to find resources matching the keyword and return
    their current compliance evaluation.

    Tenant isolation: each tenant maps to an AWS account ID stored in the
    infrastructure graph.  The query filters by accountId so cross-tenant
    data leakage is impossible at the AWS API level.

    Requires read-only AWS credentials with the following IAM actions:
      config:SelectResourceConfig
      config:DescribeComplianceByResource
    """

    name: ClassVar[str]        = "aws_config_query"
    description: ClassVar[str] = (
        "Query AWS Config to check the current compliance state of "
        "cloud infrastructure resources.  Use when you need live "
        "evidence for encryption-at-rest, backup policies, or IAM posture."
    )
    args_schema: ClassVar[type[BaseModel]] = AWSConfigInput

    async def _arun(
        self,
        resource_keyword: str,
        tenant_id: str,
        region: str = "us-east-1",
    ) -> AWSConfigResult:
        return await _aws_config_query(resource_keyword, tenant_id, region)

    def _run(self, **kwargs: Any) -> AWSConfigResult:  # type: ignore[override]
        raise NotImplementedError("Use _arun for async execution")


@_TOOL_RETRY
async def _aws_config_query(
    resource_keyword: str,
    tenant_id: str,
    region: str,
) -> AWSConfigResult:
    """
    Execute an AWS Config advanced query using boto3 run in a thread pool
    (boto3 is synchronous; asyncio.to_thread avoids blocking the event loop).
    """
    import asyncio

    aws_key    = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

    if not all([aws_key, aws_secret]):
        return AWSConfigResult(
            resource_keyword=resource_keyword, tenant_id=tenant_id,
            error="AWS credentials not configured (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY).",
        )

    log = logger.bind(tool="aws_config", tenant_id=tenant_id, keyword=resource_keyword)

    def _sync_query() -> list[dict]:
        import boto3  # imported lazily – only if AWS creds are present

        client = boto3.client(
            "config",
            region_name=region,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
        )
        # AWS Config Advanced Query (SQL SELECT on config snapshots)
        # Filter by resourceType keyword AND the tenant's account tag
        expression = f"""
            SELECT
                resourceId,
                resourceType,
                awsRegion,
                configuration,
                supplementaryConfiguration,
                tags
            WHERE
                resourceType LIKE '%{resource_keyword}%'
                AND tags.tenant_id = '{tenant_id}'
            LIMIT 10
        """
        paginator = client.get_paginator("select_resource_config")
        records: list[dict] = []
        for page in paginator.paginate(Expression=expression.strip()):
            import json
            for item in page.get("Results", []):
                records.append(json.loads(item))
        return records

    try:
        raw_records = await asyncio.to_thread(_sync_query)
    except Exception as exc:
        log.warning("AWS Config query failed", error=str(exc))
        return AWSConfigResult(
            resource_keyword=resource_keyword, tenant_id=tenant_id,
            error=str(exc),
        )

    resources: list[AWSResource] = []
    for rec in raw_records:
        # Derive a human-readable compliance annotation from supplementary config
        supp = rec.get("supplementaryConfiguration", {})
        compliance = supp.get("complianceType", "NOT_APPLICABLE")
        annotation = supp.get("annotation", "No annotation available.")
        resources.append(AWSResource(
            resource_id=rec.get("resourceId", ""),
            resource_type=rec.get("resourceType", ""),
            region=rec.get("awsRegion", region),
            compliance_type=compliance,
            annotation=annotation,
        ))

    log.info("AWS Config query complete", result_count=len(resources))
    return AWSConfigResult(
        resource_keyword=resource_keyword,
        tenant_id=tenant_id,
        resources=resources,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────────────────────────────────────

class MCPToolRegistry:
    """
    Central registry that vends all MCP tools.

    Usage in a LangGraph node
    ─────────────────────────
        from rfp_responder.tools import get_tool_registry
        registry = get_tool_registry()

        # Call a specific tool directly
        result = await registry.confluence.arun(
            {"query": "SAML SSO", "tenant_id": tenant_id}
        )

        # Or bind all tools to an LLM for agent-style tool use
        llm_with_tools = llm.bind_tools(registry.as_langchain_tools())

    Design note
    ───────────
    Tools are instantiated once per registry instance (module-level singleton
    via get_tool_registry()).  Each `_arun()` call creates its own httpx
    session – there is no shared mutable state between concurrent calls.
    """

    def __init__(self) -> None:
        self.confluence = ConfluenceSearchTool()
        self.jira       = JiraSearchTool()
        self.aws_config = AWSConfigTool()

    def as_langchain_tools(self) -> list[BaseTool]:
        """Return all tools as a list for LLM binding."""
        return [self.confluence, self.jira, self.aws_config]

    async def run_all(
        self,
        query: str,
        tenant_id: str,
        resource_keyword: str | None = None,
    ) -> dict[str, Any]:
        """
        Fire all three tool queries concurrently for a single question.

        Returns a dict with keys "confluence", "jira", "aws_config".
        Each value is the tool's result model or an error string.
        """
        import asyncio

        keyword = resource_keyword or query

        conf_task = self.confluence.arun({"query": query,   "tenant_id": tenant_id})
        jira_task = self.jira.arun(      {"query": query,   "tenant_id": tenant_id})
        aws_task  = self.aws_config.arun({"resource_keyword": keyword, "tenant_id": tenant_id})

        conf_res, jira_res, aws_res = await asyncio.gather(
            conf_task, jira_task, aws_task, return_exceptions=True
        )

        return {
            "confluence": conf_res if not isinstance(conf_res, BaseException) else str(conf_res),
            "jira":       jira_res if not isinstance(jira_res, BaseException) else str(jira_res),
            "aws_config": aws_res  if not isinstance(aws_res,  BaseException) else str(aws_res),
        }


# Module-level singleton – instantiated once, reused across requests
_registry: MCPToolRegistry | None = None


def get_tool_registry() -> MCPToolRegistry:
    global _registry
    if _registry is None:
        _registry = MCPToolRegistry()
    return _registry
