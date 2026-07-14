# AI Agent for Automated RFP & Security Questionnaire Responder
## Complete Project Documentation

---

## Table of Contents

1. [What the Product Does](#1-what-the-product-does)
2. [Who It Is For](#2-who-it-is-for)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Getting Started Locally](#17-getting-started-locally)


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

## 4. Getting Started Locally

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
