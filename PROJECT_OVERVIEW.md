# Automated RFP & Security Questionnaire Responder
## Complete Project Documentation

---

## Table of Contents

1. [What the Product Does](#1-what-the-product-does)
2. [Who It Is For](#2-who-it-is-for)
3. [High-Level Architecture](#3-high-level-architecture)
4. [The AI Workflow — Step by Step](#4-the-ai-workflow--step-by-step)
5. [Data Model & State](#5-data-model--state)
6. [Backend — Python / FastAPI](#6-backend--python--fastapi)
7. [Frontend — Next.js 15](#7-frontend--nextjs-15)
8. [Authentication & Enterprise SSO](#8-authentication--enterprise-sso)
9. [Background Job Queue (arq + Redis)](#9-background-job-queue-arq--redis)
10. [Multi-Tenancy & Security](#10-multi-tenancy--security)
11. [External Integrations (MCP Tools)](#11-external-integrations-mcp-tools)
12. [Observability & Monitoring](#12-observability--monitoring)
13. [Database Strategy](#13-database-strategy)
14. [Infrastructure & DevOps](#14-infrastructure--devops)
15. [Testing Strategy](#15-testing-strategy)
16. [Project File Map](#16-project-file-map)
17. [Getting Started Locally](#17-getting-started-locally)
18. [Key Design Decisions](#18-key-design-decisions)

---

## 1. What the Product Does

Security teams and procurement departments receive **RFP (Request for Proposal)** questionnaires from enterprise customers on a regular basis. These are documents — sometimes hundreds of questions long — that ask a vendor to describe their security controls, compliance posture, infrastructure configuration, and business practices. Examples:

- "Is all data encrypted at rest using AES-256?"
- "Do you support SAML 2.0 SSO?"
- "Are your S3 buckets protected by a KMS-managed key?"
- "Have you achieved SOC 2 Type II certification?"

Answering these **manually is slow, expensive, and inconsistent**. A security engineer must look up past responses, check whether the answer is still accurate against the current infrastructure, and write a compliant response — for every question.

**This product automates that entire process:**

1. A security team member uploads the questionnaire (JSON or Excel format).
2. The AI pipeline retrieves relevant past answers from a knowledge base and verifies them against the live infrastructure graph.
3. An LLM drafts a high-confidence answer for each question.
4. Questions the AI is confident about are auto-approved; uncertain ones are flagged for human review.
5. Reviewers see only the flagged questions, approve or correct them, and submit.
6. The system exports a completed, formatted Excel/JSON document ready to send to the customer.

The result: **what took 2-3 days of engineering time takes 20-30 minutes**, with a full audit trail.

---

## 2. Who It Is For

| Persona | Role in the Product |
|---|---|
| **Security Engineer** | Uploads the questionnaire, reviews flagged questions, downloads the final export |
| **Compliance Manager** | Monitors approval rates, reviews audit logs in LangSmith/Grafana |
| **IT Administrator** | Configures enterprise SAML SSO for their organisation |
| **SaaS Operator** | Manages the deployment, monitors Grafana dashboards, responds to alerts |

The product is **multi-tenant B2B SaaS**: each customer organisation (tenant) has their own isolated knowledge base, infrastructure graph, and workflow history. Tenant `acme` can never see or modify tenant `globex`'s data.

---

## 3. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         BROWSER (React)                              │
│  Next.js 15  ·  TanStack Query  ·  React Hook Form  ·  Tailwind CSS │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HTTPS  (rewrites via next.config)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    FASTAPI  (Python 3.11)                             │
│  POST /api/v1/rfp/ingest  →  enqueue arq job  →  202 immediately    │
│  GET  /api/v1/rfp/threads/{id}/status  →  poll checkpoint           │
│  GET  /api/v1/rfp/threads/{id}/review  →  flagged questions         │
│  POST /api/v1/rfp/threads/{id}/resume  →  inject decisions + resume │
│  GET  /health  (liveness)  ·  GET /ready  (readiness)               │
│  GET  /metrics (Prometheus)                                          │
└──────────┬───────────────────────────────────────────────────────────┘
           │  enqueue_job("run_workflow")
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ARQ WORKER  (Python 3.11)                          │
│  Picks up jobs from Redis and runs the full LangGraph pipeline       │
│                                                                      │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │              LANGGRAPH  StateGraph                           │   │
│   │                                                             │   │
│   │  parse_questionnaire                                        │   │
│   │       │                                                     │   │
│   │  dual_stream_retrieval  ◄── Qdrant (Vector DB)             │   │
│   │       │                 ◄── Neo4j  (Graph DB)              │   │
│   │       │                                                     │   │
│   │  draft_response  ◄──── GPT-4o / Claude-3.5-Sonnet          │   │
│   │       │                                                     │   │
│   │  [confident?]──No──► human_review_wait  ◄── INTERRUPT      │   │
│   │       │Yes                   │resume                       │   │
│   │       └──────────────────────┘                             │   │
│   │               │                                            │   │
│   │  compile_and_export  ──► JSON + Excel files               │   │
│   │               │         LangSmith metrics                  │   │
│   └───────────────┼─────────────────────────────────────────────┘  │
│                   │ checkpoint writes                               │
└───────────────────┼─────────────────────────────────────────────────┘
                    │
       ┌────────────┼──────────────────────┐
       ▼            ▼                      ▼
  PostgreSQL     Redis              LangSmith
  (checkpoint)  (job queue)        (LLM traces)
```

---

## 4. The AI Workflow — Step by Step

The entire AI pipeline is built using **LangGraph**, a framework for building stateful, multi-step agent workflows with built-in interruption and resumption capabilities.

### Node 1: `parse_questionnaire`

**Input:** Raw POST body (JSON or base64-encoded Excel `.xlsx`)

**What it does:**
- Detects format (`format: "json"` or `format: "excel"`)
- For JSON: reads the `questions` array directly
- For Excel: decodes the base64 file, opens it with `openpyxl`, reads columns: Question, Category, Control ID, Notes
- Skips blank rows with a warning log
- Creates a `QuestionItem` for each row, stamping **deterministic UUID5 IDs** derived from `(thread_id, row_index)` — this means re-running the same file for the same thread produces identical question IDs, making every downstream write idempotent
- Sets all questions to `status: PENDING`

**Output:** A list of structured `QuestionItem` objects stored in state

---

### Node 2: `dual_stream_retrieval`

**Input:** All PENDING questions from state

**What it does (for each question, concurrently):**

**Stream 1 — Vector Search (Qdrant)**
- Embeds the question text using OpenAI `text-embedding-3-small` (1536 dimensions)
- Queries Qdrant with a **must-filter on `tenant_id`** to enforce tenant isolation
- Returns the top-matching past answer with a cosine similarity score

**Stream 2 — Graph Query (Neo4j)**
- Extracts an infrastructure keyword from the question text (e.g. `"SAML"`, `"KMS"`, `"S3"`, `"TLS"`)
- Runs a parameterised Cypher query against the infrastructure knowledge graph
- Returns the component status, compliance frameworks, and whether it is currently active and compliant

Both streams are wrapped in **tenacity retry** (exponential backoff + jitter, 3 attempts). If one stream fails after all retries, the bundle carries a `retrieval_error` flag and the workflow continues — a single failing leg does not abort the pipeline.

Fan-out across all questions uses `asyncio.gather`, so 100 questions trigger 200 concurrent async tasks (100 vector + 100 graph), completing in parallel rather than serially.

**Output:** A `RetrievalBundle` per question, stored in a dict keyed by `question_id`

---

### Node 3: `draft_response`

**Input:** Questions + their retrieval bundles

**What it does:**
- For each question, calls GPT-4o (with Claude-3.5-Sonnet as fallback) using **structured output** (`with_structured_output(SynthesisOutput)`)
- The LLM receives: the question text, the historical vector answer, the current infrastructure graph state, the cosine similarity score, and a system prompt defining it as a security engineer persona
- The LLM returns: `proposed_answer`, `confidence` (0–1), `graph_verified` (boolean), `discrepancy_detected` (boolean), `reasoning_trace`
- The node caps the reported confidence at the actual vector similarity score — the LLM cannot be more confident than the retrieval

**Auto-approve routing decision:**
- `vector_confidence >= 0.92` **AND** `graph_verified == True` → **AUTO_APPROVED** (no human needed)
- Anything else → **REVIEW_REQUIRED** (flag for human)
- `discrepancy_detected == True` → always **REVIEW_REQUIRED** regardless of confidence

After processing all questions, the node sets:
- `review_required: True/False`
- `review_question_ids: [list of question_ids needing review]`

---

### Conditional Branch: `evaluation_router`

After `draft_response`, the graph checks:

```
review_required == False  →  compile_and_export  (fast path, no human needed)
review_required == True   →  human_review_wait   (INTERRUPT fires here)
```

---

### Node 4: `human_review_wait` *(Interrupt Point)*

This is where **LangGraph's Human-in-the-Loop** mechanism activates.

When the router sends execution here, LangGraph:
1. Serialises the **entire current state** to Postgres (the durable checkpoint)
2. Returns control to the caller — the arq worker job completes
3. The API responds that the thread is in `awaiting_review` state

The thread is **frozen in Postgres**. It will stay there until the reviewer submits their decisions.

**On resume** (when `POST /resume` is called):
- The FastAPI handler patches the Postgres checkpoint with `human_decisions`
- The arq worker picks up a `resume_workflow` job
- The graph continues from this node using the patched state
- For each decision: `approved=True, no override` → `HUMAN_APPROVED`; `approved=True, override text` → `HUMAN_OVERRIDDEN`; `approved=False` → `REVIEW_REQUIRED` (excluded from export)

---

### Node 5: `compile_and_export`

**Input:** All final answers (auto-approved + human-reviewed)

**What it does:**
1. Walks every question and resolves the effective final answer text
2. Serialises to **JSON** (machine-readable, returned in API response)
3. Serialises to **Excel** (styled `.xlsx` with colour-coded rows: green=auto-approved, yellow=human-overridden, red=rejected)
4. Pushes `AuditMetrics` to **LangSmith** as run feedback (auto-approve ratio, total tokens, processing duration)
5. Emits **Prometheus metrics** (business metrics: workflow completion, auto-approve rate, vector confidence distribution, human override rate)
6. Sets `workflow_status: COMPLETE`

---

## 5. Data Model & State

### The `RFPState` TypedDict

LangGraph passes a single state dict through every node. The state is designed so multiple concurrent nodes can each write to their own section without colliding:

```
RFPState:
  tenant_id             string       — injected at API layer; never changed
  thread_id             string       — LangGraph checkpoint key
  questionnaire_id      string       — stable ID for source document
  raw_payload           dict         — original POST body preserved for audit

  questions             list[dict]   — QuestionItem per row
  retrieval_bundles     dict[id→dict] — merge reducer: each question writes its own key
  drafted_answers       dict[id→dict] — merge reducer
  human_decisions       dict[id→dict] — merge reducer
  final_answers         dict[id→dict] — merge reducer

  review_required       bool
  review_question_ids   list[str]
  workflow_status       WorkflowStatus enum
  error_message         string|None
  audit_metrics         dict|None
  messages              list          — add_messages reducer (append-only)
```

### Why merge reducers?

When `dual_stream_retrieval` fans out over 10 questions concurrently, each async task returns `{"q-abc": RetrievalBundle(...)}`. LangGraph uses the `_merge_dicts` reducer to combine all 10 partial returns into one `retrieval_bundles` dict. Without this, concurrent writes would clobber each other.

### Idempotency via UUID5

Every `QuestionItem` gets two deterministic IDs:
```python
question_id  = UUID5(NAMESPACE_OID, f"{thread_id}:{row_index}")
execution_id = UUID5(NAMESPACE_OID, f"{thread_id}:{question_id}:exec")
```

Re-ingesting the same file for the same thread produces identical IDs. This means all downstream database writes (Qdrant upserts, Neo4j MERGE statements) are safe to replay after transient failures.

---

## 6. Backend — Python / FastAPI

### Module Structure

```
rfp_responder/
├── state.py              Pydantic models + TypedDict state + reducers
├── graph.py              LangGraph topology + conditional router
├── config.py             Pydantic Settings (env vars + .env file)
├── clients.py            Lazy singletons: Qdrant, Neo4j, OpenAI + health checks
├── rate_limit.py         slowapi Limiter (per-tenant rate limiting)
├── metrics.py            Prometheus custom business metrics
│
├── nodes/
│   ├── parse_questionnaire.py       Node 1
│   ├── dual_stream_retrieval.py     Node 2 (concurrent fan-out)
│   ├── draft_response.py            Node 3 (LLM synthesis)
│   ├── human_review_wait.py         Node 4 (HITL merge)
│   └── compile_and_export.py        Node 5 (Excel/JSON export)
│
├── api/
│   ├── schemas.py                   Pydantic v2 request/response models
│   └── routes.py                    FastAPI route handlers
│
├── app/
│   ├── lifespan.py                  Startup/shutdown: DB pools, graph, arq pool
│   └── main.py                      FastAPI app: middleware, rate limiting, /health, /ready
│
├── worker/
│   ├── tasks.py                     arq task functions (run_workflow, resume_workflow)
│   └── main.py                      WorkerSettings + process entrypoint
│
└── tools/
    └── mcp_tools.py                 Confluence, Jira, AWS Config integrations
```

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/rfp/ingest` | Enqueue new workflow. Returns 202 + `thread_id` immediately |
| `GET` | `/api/v1/rfp/threads/{id}/status` | Poll current `WorkflowStatus`, metrics, next node |
| `GET` | `/api/v1/rfp/threads/{id}/review` | Fetch flagged questions for review (409 if not interrupted) |
| `POST` | `/api/v1/rfp/threads/{id}/resume` | Submit review decisions, enqueue continuation |
| `GET` | `/health` | Liveness: always 200 if process is alive |
| `GET` | `/ready` | Readiness: 200 only if Postgres + Qdrant + Neo4j all respond |
| `GET` | `/metrics` | Prometheus metrics scrape endpoint |

### Rate Limiting

The `POST /ingest` endpoint is rate-limited at **20 requests per hour per tenant** using `slowapi`. The key function uses the `X-Tenant-ID` header so limits are per-tenant, not per-IP. This prevents a single tenant from generating runaway LLM costs.

### Startup Sequence

When the FastAPI process starts (via `lifespan.py`):
1. Opens `AsyncConnectionPool` to Postgres (2 min / 10 max connections)
2. Initialises `AsyncPostgresSaver` and runs `setup()` (creates checkpoint tables if absent)
3. Compiles the production LangGraph with `AsyncPostgresSaver` as checkpointer
4. Ensures the Qdrant collection exists (idempotent)
5. Verifies Neo4j driver connectivity
6. Opens arq Redis pool for job enqueueing

---

## 7. Frontend — Next.js 15

### Pages

| Route | Purpose |
|---|---|
| `/login` | Two-tab: email/password credentials + Enterprise SSO tab |
| `/` (dashboard) | List of recent threads with status badges |
| `/upload` | Questionnaire upload form (JSON paste or file upload) |
| `/threads/[threadId]` | Thread status page: live step timeline, metrics, export links |
| `/threads/[threadId]/review` | Human review UI: question cards with approve/override/reject |
| `/admin/sso` | Admin: configure SAML SSO for the organisation |

### Real-Time Status Polling

The thread status page uses **TanStack Query** with adaptive polling:

```typescript
refetchInterval: (query) =>
  isProcessing(query.data?.workflow_status) ? 3_000 : false
```

While the workflow is running, the UI polls every 3 seconds. Once it reaches `awaiting_review` or `complete`, polling stops. When status becomes `awaiting_review`, the page automatically navigates to `/review`. When `complete`, it navigates back to the dashboard with export download links visible.

### Review Interface

The review page fetches all flagged questions and presents each as a card showing:
- The original question text and compliance framework reference
- The LLM's proposed answer
- A confidence bar (colour-coded: green ≥ 0.92, yellow 0.7–0.92, red < 0.7)
- Whether the infrastructure graph verified the answer
- Whether a discrepancy was detected
- The LLM's reasoning trace
- Three action buttons: **Approve**, **Override** (opens text editor), **Reject**

Decisions are held in local state. A sticky footer shows progress ("7 of 12 reviewed") and activates the Submit button only when all questions have a decision.

### Next.js API Proxy

All `/api/rfp/*` requests from the browser are proxied to the FastAPI backend via `next.config.ts` rewrites. The `X-Tenant-ID` header is injected server-side (from the JWT session) so it never has to be managed by the browser.

---

## 8. Authentication & Enterprise SSO

### Provider Stack

Authentication uses **NextAuth v5** (Auth.js) with two providers:

**1. Credentials Provider (email + password)**
- For small teams and early adopters
- Password verification is pluggable (`mockVerifyCredentials` in dev, real DB check in prod)
- Uses JWT session strategy (no server-side session storage)

**2. BoxyHQ SAML Provider (Enterprise SSO)**
- BoxyHQ SAML Jackson acts as a **SAML → OAuth 2.0 proxy**
- Enterprise customers configure their IdP (Okta, Azure AD, Google Workspace) once in the admin panel
- Users then sign in via their company's identity provider
- The `tenant_id` is extracted from the SAML assertion's `profile.requested.tenant` field and embedded in the JWT
- This means enterprise users get SSO and the system automatically knows which tenant they belong to

### Session Structure

After login, every session JWT contains:
```json
{
  "email": "alice@acme.com",
  "tenantId": "acme",
  "provider": "boxyhq-saml"
}
```

### Middleware (Route Protection)

`frontend/src/middleware.ts` uses NextAuth's middleware to protect all routes:
- `/api/auth/*` and `/login` are public
- All other routes require a valid session, redirecting to `/login?callbackUrl=...` if absent

### SAML Admin Secret Protection

The admin panel posts SAML metadata to `/api/sso/configure`. This Next.js API route is a **server-side proxy** that adds the `Authorization: Bearer ${BOXYHQ_SAML_ADMIN_SECRET}` header before forwarding to BoxyHQ Jackson. The secret never reaches the browser.

---

## 9. Background Job Queue (arq + Redis)

### Why Async Jobs?

Processing a 100-question RFP involves:
- 100 vector embeddings
- 100 Neo4j queries
- 100 LLM completions
- 1 Excel generation

This can take **5–15 minutes**. Holding an HTTP connection open for that long would cause timeouts, exhaust API server threads, and break load balancer keep-alive limits.

**Solution: enqueue and poll.** `POST /ingest` takes < 50ms. The browser polls `/status` every 3 seconds.

### How It Works

**API side:**
```python
await arq_pool.enqueue_job(
    "run_workflow",
    thread_id=thread_id,
    tenant_id=tenant_id,
    payload=body.payload,
    _job_id=f"workflow:{thread_id}",   # idempotency key
)
# Returns 202 immediately
```

**Worker side** (separate Python process):
```python
async def run_workflow(ctx, *, thread_id, tenant_id, payload):
    await graph.ainvoke(initial_state, config=thread_config(thread_id, tenant_id))
    # Checkpoints state to Postgres at every node
```

**Status tracking:** The arq worker writes workflow progress directly to the Postgres checkpoint via LangGraph's `AsyncPostgresSaver`. The API reads from the same checkpoint via `graph.aget_state()`. No shared in-memory state between processes.

### Worker Configuration

```python
class WorkerSettings:
    functions = [run_workflow, resume_workflow]
    max_jobs = 10          # concurrent jobs per worker process
    job_timeout = 1800     # 30 minutes max per job
    max_tries = 3          # retry on failure
    keep_result = 86_400   # keep job result in Redis for 24h
```

---

## 10. Multi-Tenancy & Security

Tenant isolation is enforced at **every layer**:

### Vector DB (Qdrant)
Every search query includes a mandatory `must` filter:
```python
Filter(must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))])
```
A Qdrant query without this filter will never return results from another tenant, even if the collection is shared.

### Graph DB (Neo4j)
Every Cypher query uses a parameterised `$tenant_id` argument:
```cypher
MATCH (c:Component {tenant_id: $tenant_id, name: $keyword})
RETURN c.name, c.status, c.is_compliant, c.frameworks
```
Parameterised queries also prevent Cypher injection.

### Checkpoint Store (Postgres)
The `thread_config()` helper embeds `tenant_id` in the LangGraph metadata. Every route that reads a checkpoint cross-checks:
```python
if state_values.get("tenant_id") != tenant_id:
    raise HTTPException(status_code=403, detail="Access denied.")
```
A tenant cannot read or resume another tenant's thread even if they guess the `thread_id`.

### API Layer
- `X-Tenant-ID` header is validated and required on every request
- In production, this header is injected by an API gateway after JWT verification — the service never receives raw credentials
- Rate limits are per-tenant (20 ingests/hour), preventing cost-based denial of service

---

## 11. External Integrations (MCP Tools)

The `rfp_responder/tools/mcp_tools.py` module provides three LangChain-compatible tools that nodes can call to enrich context:

### ConfluenceSearchTool
- Queries Confluence REST API v2 using CQL (Confluence Query Language)
- Filters by tenant label: `label = "tenant-{tenant_id}"`
- Returns page titles, excerpts, and URLs
- Used when the vector DB lacks coverage for a specific question

### JiraSearchTool
- Queries Jira issues using JQL
- Filters by tenant label: `labels = "{tenant_id}"`
- Useful for retrieving past security incident reports or remediation evidence

### AWSConfigTool
- Calls `boto3.client("config").select_resource_config()`
- Queries AWS Config rules by tenant tag: `tags.tenant_id = '{tenant_id}'`
- Returns compliance status of AWS resources in real time
- Runs via `asyncio.to_thread` to avoid blocking the async event loop

All tools share a tenacity retry decorator and can be run concurrently via `MCPToolRegistry.run_all()`.

---

## 12. Observability & Monitoring

The system has three distinct observability layers:

### LangSmith (LLM Traces)
- Automatically activated when `LANGCHAIN_TRACING_V2=true`
- Every `graph.ainvoke()` call produces a LangSmith run with full node-by-node traces
- Nodes, their inputs/outputs, token counts, latencies, and errors are all visible
- `compile_and_export` pushes structured feedback: `auto_approved_ratio`, `total_tokens`, `processing_duration_seconds`
- Enables per-tenant filtering of runs via the `tenant:{id}` tag

### Prometheus + Grafana (Infrastructure & Business Metrics)

**HTTP metrics** (auto-instrumented by `prometheus-fastapi-instrumentator`):
- Request rate, error rate, p50/p99 latency per endpoint

**Custom business metrics** (`rfp_responder/metrics.py`):

| Metric | Type | What it measures |
|---|---|---|
| `rfp_workflow_completions_total` | Counter | Completed/failed workflows per tenant |
| `rfp_workflow_duration_seconds` | Histogram | End-to-end processing time |
| `rfp_questions_auto_approved_total` | Counter | Questions auto-approved (retrieval quality signal) |
| `rfp_questions_human_reviewed_total` | Counter | Questions needing human review |
| `rfp_questions_human_overridden_total` | Counter | Questions where reviewer changed the answer |
| `rfp_vector_confidence` | Histogram | Distribution of cosine similarity scores |
| `rfp_llm_tokens_total` | Counter | Prompt + completion tokens per model |
| `rfp_job_queue_depth` | Gauge | arq Redis queue backlog |
| `rfp_job_processing_seconds` | Histogram | Worker job duration |

**Grafana Dashboard** (`monitoring/grafana/dashboards/rfp_overview.json`): 11 panels auto-provisioned on startup.

### Alertmanager (Alerting)

10 alert rules defined in `monitoring/prometheus/rules/rfp_alerts.yml`:

| Alert | Threshold | Severity |
|---|---|---|
| High error rate | > 5% for 2 minutes | Critical |
| High p99 latency | > 10s for 5 minutes | Warning |
| API down | Scrape fails for 1 minute | Critical |
| Low auto-approve rate | < 60% for 30 minutes | Warning |
| High human override rate | > 50% for 1 hour | Warning |
| Workflow failures | > 0.1/minute | Critical |
| Job queue backlog | > 50 jobs for 5 minutes | Warning |
| Slow job processing | p95 > 10 minutes | Warning |
| Postgres down | Exporter unreachable | Critical |
| Redis down | Exporter unreachable | Critical |

Alerts route to Slack: critical alerts go to `#rfp-oncall`, warnings go to `#rfp-alerts`.

### Structured Logging

`structlog` is configured throughout:
- **Development**: colourised console output with key-value pairs
- **Production**: JSON lines, one object per event, with `timestamp`, `level`, `logger`, and all bound context (tenant_id, thread_id, etc.)
- Every request emits an access log line with method, path, status code, and duration

---

## 13. Database Strategy

### PostgreSQL — Workflow State (LangGraph Checkpoints)

**Purpose:** Durable storage of the entire `RFPState` at every node transition. Survives process restarts. Enables graph resumption after human review.

**Tables** (managed by Alembic migration `0001`):
- `checkpoints` — one row per node transition, keyed by `(thread_id, checkpoint_ns, checkpoint_id)`
- `checkpoint_blobs` — binary payloads for large state fields
- `checkpoint_writes` — in-progress node write batches

**Indexes:**
- `idx_checkpoints_thread_created` — fast latest-checkpoint lookup per thread
- `idx_checkpoints_tenant` — GIN index on `metadata->>'tenant_id'` for tenant-scoped queries
- `idx_checkpoints_workflow_status` — for dashboard/monitoring queries

**Connection:** `psycopg v3` async driver with `AsyncConnectionPool` (min=2, max=10). `autocommit=True` and `prepare_threshold=0` required for compatibility with PgBouncer transaction-mode pooling.

### Qdrant — Past Answer Knowledge Base (Vector DB)

**Purpose:** Store historical question-answer pairs from past RFPs. Retrieval by semantic similarity (cosine distance).

**Collection schema:**
- Vectors: 1536-dimensional (OpenAI `text-embedding-3-small`)
- Payload fields: `question_text`, `answer_text`, `source_document_id`, `tenant_id`, `created_at`
- Tenant isolation: every search query includes a `must` filter on `tenant_id`

### Neo4j — Infrastructure Knowledge Graph (Graph DB)

**Purpose:** Model the live infrastructure topology so the LLM can verify whether a security control is currently active and compliant.

**Node types:** `Component`, `KMSKey`, `S3Bucket`, `IAMPolicy`, `SSO`, `Database`, `Certificate`

**Relationship types:** `PROTECTS`, `ENCRYPTS`, `GOVERNS`, `USES`

**Example query:**
```cypher
MATCH (c:Component {tenant_id: $tenant_id, name: $keyword})
RETURN c.name, c.status, c.is_compliant, c.frameworks
```

### Redis — Job Queue

**Purpose:** arq job queue. Holds pending jobs, retry metadata, and job results (kept 24h for debugging).

**Configuration:** `maxmemory: 256mb`, `allkeys-lru` eviction, append-only persistence.

---

## 14. Infrastructure & DevOps

### Docker Compose Stack

Running `docker compose up -d` starts all 10 services:

```
api         FastAPI backend (uvicorn, 2 workers)
worker      arq background job worker
frontend    Next.js 15 (standalone mode)
postgres    PostgreSQL 16 (checkpoint store)
qdrant      Qdrant 1.13 (vector DB)
neo4j       Neo4j 5.27 Community (graph DB)
redis       Redis 7 (job queue)
jackson     BoxyHQ SAML Jackson (SSO proxy)
prometheus  Prometheus 2.55 (metrics)
grafana     Grafana 11.3 (dashboards)
```

Services declare health checks. The `api` service only starts after `postgres`, `qdrant`, `neo4j`, and `redis` are all healthy. The startup command runs `alembic upgrade head` before starting uvicorn, ensuring the schema is always up to date.

### GitHub Actions CI

Five jobs on every push/PR to `main`:

1. **Ruff lint** — code style and import ordering
2. **Mypy type check** — strict mode, no `Any` leakage
3. **pytest** — full test suite with `--cov-fail-under=75` coverage gate
4. **Next.js ESLint + TypeScript** — frontend lint and type check
5. **Docker build** — verifies both images build successfully

On merge to `main`, Docker images are pushed to GitHub Container Registry (`ghcr.io`) tagged with both `latest` and the commit SHA.

### Alembic Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Generate a new migration after schema change
alembic revision --autogenerate -m "add_new_table"

# Roll back one step
alembic downgrade -1
```

---

## 15. Testing Strategy

### Unit Tests

**`tests/test_state.py`** — 27 tests
- UUID5 determinism: same `(thread_id, row_index)` always produces the same ID
- Initial state factory: all fields populated correctly
- `_merge_dicts` reducer: concurrent writes, no mutation
- Model round-trips: `model_dump()` → `model_validate()` for all Pydantic models

**`tests/test_graph.py`** — 15 tests
- `evaluation_router`: all routing conditions (review required, all approved, empty drafts)
- `AUTO_APPROVE_VECTOR_THRESHOLD` is `0.92`
- `build_graph`: compiles, has correct nodes, respects `interrupt_before`
- `thread_config`: correct configurable dict, tenant tag, metadata

### Integration Tests (Mocked External Systems)

**`tests/test_nodes/test_parse_questionnaire.py`** — 11 tests
- JSON format: parsing, idempotency, blank row skipping, error cases
- Excel format: real `.xlsx` generation with `openpyxl`, blank row handling, missing file error

**`tests/test_nodes/test_dual_stream_retrieval.py`** — 9 tests
- `_extract_infra_keyword`: SAML, KMS, S3, TLS, WAF, SIEM
- Bundles created for all questions with mocked Qdrant + Neo4j
- Qdrant filter contains tenant_id (isolation proof)
- Vector failure → bundle carries `retrieval_error`, no exception raised
- Empty questions → returns early

**`tests/test_api/test_routes.py`** — 24 tests
- `POST /ingest`: 202 response, job enqueued, correct state
- `GET /status`: fields, cross-tenant 403, 404
- `GET /review`: items, sort order, 409 when not interrupted
- `POST /resume`: 202 accepted, `aupdate_state` called before `ainvoke`, `None` sentinel passed, cross-tenant 403

### Test Infrastructure

All tests run without real network connections. The `autouse` fixture `mock_settings_env` injects dummy env vars so `Settings()` never fails. The `app_client` fixture overrides the `get_graph` FastAPI dependency with a `MagicMock` that has `AsyncMock` for async methods.

Run the suite:
```bash
pytest --cov=rfp_responder --cov-report=term-missing
```

---

## 16. Project File Map

```
SB2B/
├── Dockerfile                    Backend multi-stage build
├── docker-compose.yml            All 10 services
├── .env.example                  All required environment variables documented
├── alembic.ini                   Migration config
├── pyproject.toml                All Python deps + ruff + mypy + pytest config
│
├── rfp_responder/                Python package (backend)
│   ├── state.py                  Data contracts, TypedDict, reducers
│   ├── graph.py                  LangGraph topology
│   ├── config.py                 Settings (env vars)
│   ├── clients.py                DB/API client singletons
│   ├── rate_limit.py             slowapi limiter
│   ├── metrics.py                Prometheus business metrics
│   ├── nodes/
│   │   ├── parse_questionnaire.py
│   │   ├── dual_stream_retrieval.py
│   │   ├── draft_response.py
│   │   ├── human_review_wait.py
│   │   └── compile_and_export.py
│   ├── api/
│   │   ├── schemas.py
│   │   └── routes.py
│   ├── app/
│   │   ├── lifespan.py
│   │   └── main.py
│   ├── worker/
│   │   ├── tasks.py
│   │   └── main.py
│   └── tools/
│       └── mcp_tools.py
│
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 0001_langgraph_checkpoints.py
│
├── tests/
│   ├── conftest.py
│   ├── test_state.py
│   ├── test_graph.py
│   ├── test_nodes/
│   │   ├── test_parse_questionnaire.py
│   │   └── test_dual_stream_retrieval.py
│   └── test_api/
│       └── test_routes.py
│
├── frontend/                     Next.js 15 application
│   ├── Dockerfile
│   ├── src/
│   │   ├── app/
│   │   │   ├── (auth)/login/page.tsx
│   │   │   ├── (dashboard)/
│   │   │   │   ├── page.tsx               Dashboard
│   │   │   │   ├── upload/page.tsx
│   │   │   │   ├── threads/[threadId]/
│   │   │   │   │   ├── page.tsx           Thread status
│   │   │   │   │   └── review/page.tsx    Human review UI
│   │   │   │   └── admin/sso/page.tsx
│   │   │   └── api/
│   │   │       ├── auth/[...nextauth]/route.ts
│   │   │       └── sso/configure/route.ts
│   │   ├── lib/
│   │   │   ├── auth.ts                    NextAuth config
│   │   │   └── api.ts                     Axios client
│   │   ├── hooks/
│   │   │   └── use-thread-status.ts       Adaptive polling hook
│   │   ├── components/
│   │   │   ├── confidence-bar.tsx
│   │   │   ├── review-item-card.tsx
│   │   │   └── thread-status-badge.tsx
│   │   ├── types/api.ts                   TypeScript mirrors of backend schemas
│   │   └── middleware.ts                  NextAuth route protection
│   └── tailwind.config.ts
│
├── monitoring/
│   ├── prometheus/
│   │   ├── prometheus.yml
│   │   └── rules/rfp_alerts.yml           10 alert rules
│   ├── alertmanager/
│   │   └── alertmanager.yml               Slack routing
│   └── grafana/
│       ├── provisioning/                  Auto-provision datasources + dashboards
│       └── dashboards/rfp_overview.json   11-panel overview dashboard
│
└── .github/
    └── workflows/
        └── ci.yml                         5-job CI pipeline
```

---

## 17. Getting Started Locally

### Prerequisites

- Docker + Docker Compose
- Python 3.11+ (for running tests outside Docker)
- Node.js 20+ (for frontend development outside Docker)

### Full Stack with Docker

```bash
# 1. Clone and configure
git clone <repo>
cd SB2B
cp .env.example .env
# Edit .env: add OPENAI_API_KEY, ANTHROPIC_API_KEY, POSTGRES_PASSWORD, etc.

# 2. Start everything
docker compose up -d

# 3. Check all services are healthy
docker compose ps

# 4. Access the application
open http://localhost:3000      # Frontend
open http://localhost:8000/docs # FastAPI (dev mode)
open http://localhost:3001      # Grafana (admin/changeme-in-prod)
open http://localhost:9090      # Prometheus
```

### Backend Only (for development)

```bash
cd SB2B
pip install -e ".[dev]"

# Start infrastructure only
docker compose up postgres qdrant neo4j redis -d

# Run database migrations
alembic upgrade head

# Start the API
uvicorn rfp_responder.app.main:app --reload --port 8000

# Start the arq worker (separate terminal)
python -m rfp_responder.worker.main
```

### Frontend Only

```bash
cd SB2B/frontend
npm install
cp .env.example .env.local
# Set BACKEND_URL=http://localhost:8000
npm run dev
```

### Running Tests

```bash
cd SB2B
pytest -v
# With coverage report:
pytest --cov=rfp_responder --cov-report=html
```

---

## 18. Key Design Decisions

### Why LangGraph instead of a custom state machine?

LangGraph provides three things that are hard to build from scratch:
1. **Durable checkpointing** — the full state is serialised to Postgres at every node transition, enabling crash recovery without any extra code
2. **Interrupt + resume** — the `interrupt_before` mechanism pauses execution and resumes it exactly where it left off, with human-injected state, without any polling or callback infrastructure
3. **Concurrency-safe state** — the reducer annotation system handles concurrent writes from fan-out tasks cleanly

### Why two retrieval streams (Vector + Graph)?

Neither source alone is sufficient:
- **Vector search** gives high recall on semantically similar past answers but can be stale if the infrastructure has changed
- **Graph query** gives current ground truth about infrastructure but only for things that are modelled in the graph

Together, they catch discrepancies: if the vector DB says "Yes, KMS encryption is enabled" but the Neo4j graph shows the KMS key was deleted last week, the `discrepancy_detected` flag fires and the question goes to human review.

### Why `interrupt_before` instead of a webhook callback?

Webhooks require the caller to have a stable HTTPS endpoint and add deployment complexity. `interrupt_before` stores the pause point in Postgres, so the reviewer can take hours or days to respond without keeping any process alive. When they do respond, any API instance can resume the workflow because the state is in the shared database.

### Why arq instead of Celery?

arq is asyncio-native: task functions are `async def` and share the same event loop. LangGraph's `graph.ainvoke()` is an async coroutine. Celery's execution model is synchronous workers — running async code in Celery requires greenlet patches or thread-pool wrappers, adding complexity and performance overhead.

### Why UUID5 for question IDs instead of UUID4?

UUID4 is random — re-ingesting the same questionnaire would produce different IDs, making deduplication impossible. UUID5 is deterministic (derived from a namespace + name), so re-running the same file for the same thread always produces the same IDs. This makes every downstream Qdrant upsert and Neo4j `MERGE` statement idempotent.

### Why Alembic when AsyncPostgresSaver already calls `setup()`?

`setup()` is `CREATE TABLE IF NOT EXISTS` — it works but is not versioned. In production, you need to:
- Know exactly when schema changes were applied
- Roll back a migration if a deployment goes wrong
- Run the same migration safely across multiple replica instances starting simultaneously
- Track schema history alongside code history in git

Alembic provides all of this. The `setup()` call is kept as a safety net in development but the canonical schema is now in `migrations/`.

---

*Built with LangGraph · FastAPI · Next.js 15 · Qdrant · Neo4j · PostgreSQL · arq · Prometheus · Grafana*
