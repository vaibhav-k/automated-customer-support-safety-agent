# Automated Customer Support & Safety Agent

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

A production-shaped **Microsoft Foundry (Azure AI Foundry)** project built as a "catch-all" lab for the
**AI-103: Developing AI Apps and Agents on Azure** exam (*Azure AI Apps and Agents Developer Associate*).

One small, working system exercises the scenarios the exam tests most heavily:

- **RAG** — an agent grounded on a vector + semantic hybrid **Azure AI Search** index built with
  **text-embedding-3-small** (or **-3-large**) and an integrated vectorizer.
- **Agent tooling** — a Foundry prompt agent that calls a custom **OpenAPI tool** backed by an **Azure Function**
  (Python v2 programming model).
- **Content safety** — **Prompt Shields** (direct and indirect attacks) + harm-category moderation, in a client-side
  gate *and* in a server-side Foundry **Guardrail**.
- **Security** — keyless Entra ID auth everywhere, least-privilege **Azure RBAC**, managed identities, and a
  single secret held in a Foundry project connection.

> All company data is fictional (Contoso Ltd.). See [DEPLOYMENT.md](DEPLOYMENT.md) to build it and
> [tests/exam_verification.md](tests/exam_verification.md) to prove it works.

---

## Architecture

```
                         ┌──────────────────────────────┐
  [User Input] ────────► │  orchestrator/ (Python CLI)  │  DefaultAzureCredential (Entra ID, keyless)
                         └──────────────┬───────────────┘
                                        │ every turn
                                        ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│ 1. Azure AI Content Safety (Filter)                         orchestrator/safety.py │
│    • Prompt Shields  POST /contentsafety/text:shieldPrompt   ◄── catches prompt    │
│        userPromptAnalysis  → direct jailbreak / injection        injections        │
│        documentsAnalysis   → indirect attacks in attachments                       │
│    • Text moderation POST /contentsafety/text:analyze (Hate/Sexual/Violence/       │
│        SelfHarm, block at severity ≥ HARM_SEVERITY_THRESHOLD)                      │
│    • Fail-closed if the service is unreachable           RBAC: Cognitive Services  │
│                                                                User                │
└───────────────────────────────────────┬────────────────────────────────────────────┘
                                        │ (safe input only)
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ 2. Azure AI Foundry Agent (Core Logic)                 orchestrator/agent_client.py │
│    • Prompt agent "contoso-support-agent" (versioned) – scripts/provision_agent.py  │
│    • Instructions: agent/system_prompt.txt  ·  model: gpt-4.1-mini, temp 0.2        │
│    • Conversations API = server-side memory; Responses API + agent_reference        │
│    • Foundry Guardrail on model + agent: user prompt attacks, indirect attacks      │
│      (incl. TOOL RESPONSES), harm categories, protected material                    │
│                                                         RBAC: Azure AI User         │
└───────────┬───────────────────────────────────────────────────────┬─────────────────┘
            │ (Tool Call)                                           │ (Tool Call)
            ▼                                                       ▼
┌───────────────────────────────────────┐   ┌─────────────────────────────────────────┐
│ 3. Azure AI Search (RAG Knowledge Base)│   │ 4. Azure Function API (Action Executor)│
│  index: contoso-policy-index           │   │  src/function_app.py (Python v2 model) │
│  • content_vector 1536-d HNSW/cosine   │   │  GET /api/orders/{customerId}/status   │
│  • AOAI vectorizer → text-embedding-   │   │   → {"status": "Shipped", ...}         │
│    3-small (search MI, keyless)        │   │  • auth: function key in header        │
│  • semantic config + hybrid queries    │   │    x-functions-key                     │
│  • query_type vector_semantic_hybrid   │   │  • OpenAPI 3.0: agent/openapi_spec.json│
│  data: data/contoso_policy.md          │   │  • key stored in Foundry "Custom keys" │
│  RBAC (project MI): Search Index Data  │   │    project connection (never in code)  │
│   Reader (+ Search Service Contributor)│   │                                        │
│  RBAC (search MI → Foundry):           │   │                                        │
│   Cognitive Services OpenAI User       │   │                                        │
└────────────────────────────────────────┘   └────────────────────────────────────────┘
                                        │
                                        ▼
                    Output moderation (text:analyze) → reply to the user
```

### Request walkthrough

| Step | What happens | Code | Blocking stage |
|---|---|---|---|
| 1 | User text (and any attached documents) sent to **Prompt Shields**. Attack → refuse; agent never invoked. | `ContentSafetyGate.check_user_input` | `input_safety` |
| 2 | Same text scored by **text moderation**; any category ≥ threshold → refuse. | `ContentSafetyGate._moderate` | `input_safety` |
| 3 | Safe text (plus any attachments, wrapped as delimited *untrusted* data) appended to the **conversation**; `responses.create(... agent_reference ...)` runs the agent. | `SupportAgentClient.ask` | — |
| 4 | Foundry **Guardrail** checks user input, tool calls, tool responses, and output server-side. | Foundry service | `model_content_filter` |
| 5 | Agent calls **Azure AI Search** (policy questions) and/or the **OpenAPI tool** (order status). | Foundry service | — |
| 6 | Reply text, `url_citation` annotations, and tool-call trace are parsed. | `parse_response` | — |
| 7 | Reply re-moderated before display. | `ContentSafetyGate.check_model_output` | `output_safety` |

**Why two safety layers?** The client gate is explicit, testable, and works with any model; the Foundry guardrail
also sees **tool responses** (where indirect injection usually arrives) and protects every caller of the agent,
not just this client. Defence in depth is the exam-preferred answer.

---

## Repository structure

```
automated_customer_support_safety_agent/
├── README.md                     ← you are here: overview, architecture, AI-103 study map
├── DEPLOYMENT.md                 ← portal + CLI deployment, RBAC, Guardrails/Prompt Shields
├── .github/
│   ├── workflows/ci.yml          ← CI: ruff, mypy, pyright, complexity, pytest, dry runs, secret scan
│   └── dependabot.yml            ← weekly dependency / action update PRs
├── .env.example                  ← every setting the code reads (copy to .env)
├── .gitignore
├── LICENSE
├── pyproject.toml                ← pytest + ruff configuration
├── requirements.txt              ← orchestrator + scripts dependencies
├── requirements-dev.txt          ← + test/lint tooling + function deps
├── requirements-eval.txt         ← optional: azure-ai-evaluation for the AI judges
│
├── agent/                        ← agent & tool assets (provisioned by scripts/provision_agent.py)
│   ├── system_prompt.txt         ← persona, tool rules, grounding rules, fallbacks, guardrails
│   └── openapi_spec.json         ← OpenAPI 3.0.3 spec for the order-status tool
│
├── data/
│   └── contoso_policy.md         ← fictional policy handbook indexed into Azure AI Search
│
├── src/                          ← Azure Function App root (deploy this folder)
│   ├── function_app.py           ← Python v2 model: /api/orders/{customerId}/status, /api/health
│   ├── host.json
│   ├── requirements.txt
│   ├── local.settings.json.example
│   └── .funcignore
│
├── orchestrator/                 ← client pipeline: safety gate → agent → output check
│   ├── __init__.py
│   ├── __main__.py               ← CLI: python -m orchestrator [-q "..."] [--json] [--trace ...]
│   ├── auth.py                   ← shared DefaultAzureCredential factory (keyless)
│   ├── config.py                 ← validated settings from env/.env
│   ├── safety.py                 ← Prompt Shields + text moderation (keyless, retries, fail-closed)
│   ├── agent_client.py           ← Foundry agent via Conversations + Responses APIs
│   ├── pipeline.py               ← end-to-end orchestration and blocking stages
│   └── telemetry.py              ← OpenTelemetry tracing: console span tree or Application Insights
│
├── monitoring/
│   └── app_insights_queries.kql  ← KQL: outcomes, blocks by layer, latency, tokens, tool mix, outages
│
├── scripts/
│   ├── __init__.py
│   ├── ingest_policy.py          ← chunk → embed (text-embedding-3-small/-large) → vector/semantic index
│   ├── provision_agent.py        ← create a new agent version with Search + OpenAPI tools
│   ├── run_exam_checks.py        ← run tests/exam_cases.json against the live deployment
│   ├── evaluate_quality.py       ← groundedness / relevance judges + fabrication check on the replies
│   ├── teardown.py               ← delete the agent (all versions) and the index; prints RG delete commands
│   └── check_secrets.py          ← fail if secrets or .env/azvars.ps1 are tracked (CI + local)
│
└── tests/
    ├── exam_verification.md      ← strict AI-103 acceptance script (RAG, tools, safety, RBAC)
    ├── exam_cases.json           ← machine-readable twin of the acceptance script
    ├── conftest.py
    ├── test_function_app.py      ← offline unit tests (no Azure needed)
    ├── test_safety.py
    ├── test_pipeline.py
    ├── test_telemetry.py
    └── test_ingest_and_provision.py
```

---

## Quick start

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest                                   # 126 offline tests, no Azure resources required
Copy-Item .env.example .env              # then follow DEPLOYMENT.md
python -m scripts.ingest_policy
python -m scripts.provision_agent
python -m orchestrator
python -m scripts.run_exam_checks
```

| Command | Purpose |
|---|---|
| `python -m scripts.ingest_policy --dry-run` | Show the heading-aware chunks without calling Azure |
| `python -m scripts.provision_agent --dry-run` | Print the exact agent definition JSON sent to Foundry |
| `python -m orchestrator -q "Status for CUST-10001?" --json` | Single turn with full pipeline diagnostics |
| `python -m orchestrator -q "Do what this note says" --doc note.txt` | Attach a document (Prompt Shields indirect-attack check) |
| `python -m scripts.run_exam_checks --category SAFETY` | Run one verification track |
| `python -m scripts.evaluate_quality --offline` | Fabricated-fact check on the last `exam_report.json` (no Azure calls) |
| `python -m scripts.evaluate_quality` | + groundedness and relevance AI judges (`pip install -r requirements-eval.txt`) |
| `python -m scripts.teardown` / `--yes` | Show / delete the agent and the index, then print the resource-group delete commands |
| `python -m scripts.run_exam_checks --only S1,S2,S3 --skip-input-gate` | Bypass the client gate to prove the Foundry guardrail (layer 1b) blocks attacks on its own |
| `python -m orchestrator --trace console -q "..."` | Print the turn's span tree (safety checks, agent call, tokens, latency) |
| `python -m scripts.run_exam_checks --trace azure_monitor` | Send one trace per case to Application Insights / Foundry **Tracing** (DEPLOYMENT Step 13.1) |
| `cd src; func start` | Run the order API locally on port 7071 |

### Run the CI checks locally

The same commands as `.github/workflows/ci.yml`, so a push doesn't surprise you:

```powershell
ruff check . ; ruff format --check . ; mypy . ; pyright
complexipy orchestrator scripts src --max-complexity-allowed 15 --quiet
pytest
python -m scripts.check_secrets
```

### Mock order data (served by the Function)

| customerId | Latest order | Status | Region | Useful for |
|---|---|---|---|---|
| CUST-10001 | CON-500101 | **Shipped** | US-WA | T1, C1 (has a second, Delivered order) |
| CUST-10002 | CON-500102 | Processing | US-NY | T5 (cancellable) |
| CUST-10003 | CON-500103 | Delivered | CA-ON | S7 (privacy) |
| CUST-10004 | CON-500104 | Shipped | US-AK | C2 (Alaska exemptions) |
| CUST-10005 | CON-500105 | Cancelled | GB-LND | |
| CUST-10006 | CON-500106 | ReturnInitiated | DE-BE | |
| CUST-10007 | CON-500107 | Refunded | US-CA | |
| CUST-99999 | — | 200 `found: false` (OrderNotFound) | — | T4 (not found) |

---

## AI-103 study guide — how this build maps to the exam

Skill-area weights are from the official AI-103 study guide. ✅ = implemented and exercised in this repo,
📘 = covered as documented exam notes/alternatives, ➖ = outside this project's scope.

### 1. Plan and manage an Azure AI solution (25–30%)

| Objective (official wording, abridged) | Where in this repo | What to be able to explain |
|---|---|---|
| ✅ Choose appropriate Foundry services for generative tasks, grounding, vector search, agent workflows | Architecture; `provision_agent.py` | Why AI Search (not file search) for a governed corporate KB; why an OpenAPI tool vs. a function tool |
| ✅ Choose an appropriate method for retrieval and indexing | `ingest_policy.py` | Heading-aware chunking, push API vs. indexer; integrated vectorizer |
| ✅ Choose memory, tool, and knowledge integration services for agents | `agent_client.py` | Conversations = server-side memory; tools = Search + OpenAPI |
| ✅ Configure model and agent deployments | DEPLOYMENT Steps 3, 12 | Global Standard deployments, TPM, agent **versions** |
| ✅ Configure security: managed identity, keyless credentials, role policies | DEPLOYMENT Step 6; every client uses `DefaultAzureCredential` | Exact roles, scopes, which identity needs what, API-key vs. RBAC modes on Search |
| 📘 Private networking | DEPLOYMENT Step 13 hardening | Private endpoints; key-based search auth isn't supported over private networking for the agent tool |
| ✅ Configure safety filters, guardrails, risk detection, content moderation | `safety.py`; DEPLOYMENT Step 11 | Prompt Shields user vs. document attacks; severity thresholds; intervention points; annotate vs. block |
| ✅ Govern agent behavior with constraints and tool-access controls | `system_prompt.txt`; OpenAPI `pattern`s | Parameter validation, "only the ID given in this conversation", no write operations exposed |
| 📘 Monitor safety events and grounding quality; manage quotas/cost | DEPLOYMENT Step 13 observability | Tracing via Application Insights; token usage captured in `AgentReply.usage` |
| ✅ Integrate Foundry projects with CI/CD pipelines | `.github/workflows/ci.yml`, `dependabot.yml` | Every push/PR runs lint, types, complexity, 100+ offline tests, agent-definition and ingestion dry runs, and a secret scan, with no Azure credentials needed. A deploy stage would use OIDC federated credentials (no stored secrets) to run `provision_agent.py`, which creates a new agent **version** |

### 2. Implement generative AI and agentic solutions (30–35%)

| Objective | Where | What to be able to explain |
|---|---|---|
| ✅ Implement RAG in an application | Search tool + `system_prompt.txt` grounding rules | Retrieval → grounding → citation; fallback when nothing is retrieved |
| ✅ Design tool-augmented flows and multistep reasoning | Test C1/C2 | Order tool result feeds a policy lookup |
| ✅ Evaluate apps, including detecting fabrications and safety | `run_exam_checks.py`, `evaluate_quality.py` | Assertion-based evals; `GroundednessEvaluator` / `RelevanceEvaluator` (1-5, AI-assisted) vs. a deterministic fabricated-fact check; fallback cases F1/F2 |
| ✅ Integrate generative workflows with Foundry SDKs; connect an app to a Foundry project | `agent_client.py` | `AIProjectClient(endpoint, credential)` → `get_openai_client()` |
| ✅ Define agent roles, goals, conversation tracking, tool schemas | `system_prompt.txt`, `openapi_spec.json` | Persona + scope; `operationId`; descriptions drive tool selection |
| ✅ Build agents integrating retrieval, function-calling, and memory | `provision_agent.py` | `PromptAgentDefinition(tools=[AzureAISearchTool, OpenApiTool])` |
| ✅ Integrate agent tools: APIs, search, custom functions | `src/function_app.py` | OpenAPI auth modes: anonymous / project connection / managed identity |
| ✅ Build workflows with safeguards | `pipeline.py` | Fail-closed gate, output moderation, blocked-stage reporting |
| ✅ Tune generation behavior (prompt engineering, parameters) | `AGENT_TEMPERATURE`, prompt | Low temperature for factual support; explicit fallback phrasing |
| ✅ Observability: tracing, token analytics, safety signals, latency | `telemetry.py`, `monitoring/*.kql`, DEPLOYMENT 13.1 | OpenTelemetry → Application Insights connected to the project; `AIProjectInstrumentor` GenAI spans; content recording off by default; which layer blocked |
| ➖ Multi-agent orchestration, reflection loops | — | Extension idea: add a "refund approval" agent with human approval |

### 3–4. Computer vision (10–15%) and text analysis / speech (10–15%)

| Objective | Where | Notes |
|---|---|---|
| ✅ Configure detection of safety issues and sensitive content | `safety.py`, test S6 | Harm categories; PII handling rules in the prompt |
| 📘 Detect indirect prompt injection (including embedded text) | Test S3 | Same Prompt Shields `documents` mechanism applies to OCR'd image text |
| ➖ Image/video generation, Content Understanding, speech, translation | — | Not covered by this project — study separately |

### 5. Implement information extraction solutions (10–15%)

| Objective | Where | What to be able to explain |
|---|---|---|
| ✅ Ingest and index content | `ingest_policy.py` | Index schema, key field, retrievable vs. searchable, `stored=False` for vectors |
| ✅ Configure semantic, hybrid, and vector search for grounding | `build_index`, `query_type` | HNSW profile → vectorizer; semantic config; `vector_semantic_hybrid` |
| 📘 Enrichment with built-in skills; RAG ingestion flow | DEPLOYMENT Step 8 portal alternative | *Import data (new)* creates Text Split + Azure OpenAI Embedding skills and an indexer |
| ✅ Connect retrieval pipelines to agent tools | `AISearchIndexResource` | Project connection ID + index name + query type + `top_k` |

### Exam traps this project makes concrete

1. **Embedding dimensions** — text-embedding-3-small = 1536, text-embedding-3-large = 3072 (both can be shortened, ada-002 cannot); the index field must match, and changing it requires rebuilding the index.
2. **Vector queries with plain text** require an **integrated vectorizer** on the index; the *search service's*
   identity needs **Cognitive Services OpenAI User** on the embedding resource.
3. **Search RBAC needs RBAC enabled** — on *API keys only* mode, role assignments are ignored.
4. **Search Service Contributor ≠ data access.** Data plane = *Search Index Data Reader/Contributor*.
5. **Cognitive Services OpenAI User vs. Contributor** — inference only vs. inference + deployment management.
6. **Prompt Shields:** `userPromptAnalysis` (jailbreak from the user) vs. `documentsAnalysis` (indirect attack
   from grounding data/tool output). Guardrails can intervene on **tool responses**.
7. **Annotate vs. block** — annotate surfaces detections without filtering (good for monitoring first).
8. **OpenAPI tool + API key** — the project connection **key name** must equal the spec's `securitySchemes.name`.
9. **Grounded fallback** — "I couldn't find that…" beats a plausible fabrication; it is a design requirement.
10. **Agent changes are versions** — new instructions/tools = new agent version; callers reference the name.

---

## Design decisions

- **Keyless by default.** Every client uses `DefaultAzureCredential`; the only secret (function key) lives in a
  Foundry *Custom keys* connection. Swap the OpenAPI auth to managed identity + Entra-protected Function for zero
  secrets.
- **Observable, privacy-first.** OpenTelemetry spans for every turn (`--trace console|azure_monitor`); outcomes,
  tokens and latency are recorded, message content only when `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.
- **Fail closed.** If Content Safety is unreachable the request is blocked (`SAFETY_FAIL_CLOSED=true`).
- **Deterministic mock backend.** The Function serves fixed data so verification runs are repeatable.
- **Server-side memory.** Conversations live in Foundry; the client holds only the conversation ID.
- **Pure, tested core.** Response parsing, chunking, spec preparation, and scoring are pure functions covered by
  offline tests; Azure calls sit behind thin, injectable clients.
- **No containers.** The Function deploys with Core Tools; the orchestrator runs as a Python CLI (or on App
  Service / Container Apps with a managed identity).

## SDK versions

Built against the GA Foundry `v1` APIs: `azure-ai-projects` 2.x (prompt agents, `create_version`,
Conversations/Responses with `agent_reference`), `azure-search-documents` 11.6+, `azure-ai-contentsafety` 1.0,
Content Safety Prompt Shields REST `2024-09-01`, and `azure-functions` (Python v2 model). Tested offline with
`azure-ai-projects` 2.0.0 and 2.7.0.

## License

[MIT](LICENSE) — for study use. Contoso is a fictitious company.
