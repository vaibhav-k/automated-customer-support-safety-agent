# Deployment Guide

End-to-end, click-by-click deployment of the **Automated Customer Support & Safety Agent**. Portal steps use the
current Foundry portal (`https://ai.azure.com`, branded **Microsoft Foundry**, formerly Azure AI Foundry) and Azure
portal terminology. Where the classic Foundry portal uses a different label, it is shown in *(classic: …)*.

> **Auth model:** everything is **keyless** (Microsoft Entra ID + managed identities) except one secret — the Azure
> Function key — which lives only inside a Foundry **Custom keys** connection, never in code.

**Estimated time:** 60–90 minutes. **Estimated cost:** a few US dollars per day of active study (AI Search Basic
is the largest line item; delete the resource group when you are done — see [Step 14](#step-14--clean-up)).

---

## Contents

1. [Prerequisites](#step-1--prerequisites)
2. [Resource group and naming](#step-2--resource-group-and-naming)
3. [Foundry resource, project, and model deployments](#step-3--foundry-resource-project-and-model-deployments)
4. [Azure AI Search](#step-4--azure-ai-search)
5. [Azure AI Content Safety](#step-5--azure-ai-content-safety)
6. [RBAC role assignments](#step-6--rbac-role-assignments)
7. [Connect AI Search to the Foundry project](#step-7--connect-ai-search-to-the-foundry-project)
8. [Build and populate the vector index](#step-8--build-and-populate-the-vector-index)
9. [Deploy the Azure Function (order API)](#step-9--deploy-the-azure-function-order-api)
10. [Store the function key in a Custom keys connection](#step-10--store-the-function-key-in-a-custom-keys-connection)
11. [Configure Guardrails and Prompt Shields](#step-11--configure-guardrails-and-prompt-shields)
12. [Provision the agent](#step-12--provision-the-agent)
13. [Run and verify](#step-13--run-and-verify)
14. [Clean up](#step-14--clean-up)
15. [Troubleshooting](#troubleshooting)

---

## Step 1 — Prerequisites

| Tool | Version | Check |
|---|---|---|
| Azure subscription | Owner, or Contributor + **User Access Administrator** / **Role Based Access Control Administrator** (needed to assign roles) | — |
| Python | 3.11 (3.10–3.12 supported) | `python --version` |
| Azure CLI | 2.60+ | `az version` |
| Azure Functions Core Tools | v4 | `func --version` |
| Git | any | `git --version` |

Set up the local workspace (PowerShell):

```powershell
cd C:\Users\VaibhavKulshrestha\development\automated_customer_support_safety_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest  # all offline tests must pass before you deploy anything
Copy-Item .env.example .env
az login
az account set --subscription "<your-subscription-id>"
```

## Step 2 — Resource group and naming

Pick one region that offers `gpt-4.1-mini` and `text-embedding-3-small` (Global Standard), the AI Search semantic
ranker, and Content Safety Prompt Shields — **East US 2** or **Sweden Central** are safe choices.

```powershell
$RG       = "RG-Vaibhav-POC-002"
$LOCATION = "canadacentral"
$SEARCH   = "automated-customer-support-safety-agent"
$SAFETY   = "automated-customer-support"
$FUNCAPP  = "func-contoso-orders-vk4904"
az group create --name $RG --location $LOCATION
```

## Step 3 — Foundry resource, project, and model deployments

1. Go to **https://ai.azure.com** → **Create new** → choose **Microsoft Foundry resource** (the *Foundry project*
   option, not a hub-based project).
2. **Project name:** `contoso-support`. Expand **Advanced options**: subscription, resource group `rg-ai103-support-agent`,
   region **East US 2**, **Foundry resource** name e.g. `fdy-contoso-<suffix>`. Select **Create**.
3. When the project opens, copy from the project **Overview**:
   - **Project endpoint** → `.env` `FOUNDRY_PROJECT_ENDPOINT`
     (`https://<foundry-resource>.services.ai.azure.com/api/projects/contoso-support`)
   - **Azure OpenAI endpoint** → `.env` `AZURE_OPENAI_ENDPOINT` (`https://<foundry-resource>.openai.azure.com`)
4. Deploy the chat model: **Build** → **Models** *(classic: **Models + endpoints**)* → **Deploy model** →
   **Deploy base model** → `gpt-4.1-mini` → **Confirm**.
   - Deployment name: `gpt-4.1-mini` (→ `FOUNDRY_MODEL_DEPLOYMENT_NAME`)
   - Deployment type: **Global Standard**; **Tokens per Minute Rate Limit**: 50K is plenty.
5. Deploy the embedding model the same way: `text-embedding-3-small`, deployment name `text-embedding-3-small`
   (→ `EMBEDDING_DEPLOYMENT_NAME`), Global Standard / Standard, 50K TPM.
6. Confirm the project has a **system-assigned managed identity**: Azure portal → your Foundry resource →
   **Resource Management → Projects** → `contoso-support` → **Identity** → *System assigned* = **On**.
   Copy its **Object (principal) ID** — you need it in Step 6.

> **Exam note:** text-embedding-3-small produces **1536-dimensional** vectors; text-embedding-3-large produces
> **3072** (both accept a shorter `dimensions` value; ada-002 is fixed at 1536). The index vector field dimension
> **must** match the embedding output or uploads fail. If you deploy 3-large, set all three together in `.env`:
> `EMBEDDING_DEPLOYMENT_NAME`, `EMBEDDING_MODEL_NAME=text-embedding-3-large`, `EMBEDDING_DIMENSIONS=3072` —
> `ingest_policy.py` refuses mismatched combinations before creating anything.

## Step 4 — Azure AI Search

1. Azure portal → **Create a resource** → **Azure AI Search** → **Create**.
   - Service name: `$SEARCH`; region: same as Foundry; **Pricing tier: Basic** (Free tier lacks managed identity
     and is limited for semantic ranker scenarios).
2. After deployment, open the service and configure:
   - **Settings → Semantic ranker** → select **Free** plan → **Save**.
   - **Settings → Keys → API access control** → select **Both** (switch to **Role-based access control** after
     verification to disable API keys entirely) → **Save**.
   - **Settings → Identity → System assigned** → **Status: On** → **Save**. Copy the **Object (principal) ID**.
3. Copy the **Url** from **Overview** → `.env` `AZURE_SEARCH_ENDPOINT` (`https://<search>.search.windows.net`).

> **Exam note:** with **API keys** only, RBAC data-plane roles are ignored; with **Role-based access control**
> only, API keys are rejected. **Both** accepts either — useful for migration.

## Step 5 — Azure AI Content Safety

1. Azure portal → **Create a resource** → **Content Safety** → **Create**.
   - Name `$SAFETY`, same region, **Pricing tier: Standard S0**.
2. Open the resource → **Resource Management → Keys and Endpoint** → copy **Endpoint** →
   `.env` `CONTENT_SAFETY_ENDPOINT`. **Do not copy the keys** — this project authenticates with Entra ID.

> **Lab fallback — no permission to assign roles:** if you cannot be granted **Cognitive Services User** on this
> resource, copy **KEY 1** from **Keys and Endpoint** into `.env` as `CONTENT_SAFETY_API_KEY`. The gate then sends it
> in the `Ocp-Apim-Subscription-Key` header instead of an Entra token and logs a warning on every start. Remove it once
> the role is granted. There is **no** key fallback for the Foundry agent itself: invoking and provisioning the agent
> always needs **Azure AI User** on the project (check the project's **Access control (IAM)** first — the creator is
> sometimes granted it automatically).

> The Foundry resource itself can also serve Content Safety APIs. A dedicated resource is used here so that the
> RBAC boundary (who may call moderation) is explicit, which mirrors exam scenarios.

## Step 6 — RBAC role assignments

This is the most-tested part of the build. Assign **exactly** these roles.

| # | Principal (who) | Role | Scope (on what) | Why |
|---|---|---|---|---|
| 1 | **You** (developer) | **Azure AI User** | Foundry project | Create agent versions, create conversations, invoke the agent |
| 2 | **You** | **Cognitive Services OpenAI User** | Foundry resource | Call `text-embedding-3-small` during ingestion |
| 3 | **You** | **Cognitive Services User** | Content Safety resource | Call Prompt Shields (`text:shieldPrompt`) and `text:analyze` |
| 4 | **You** | **Search Service Contributor** | AI Search service | Create/update the index definition |
| 5 | **You** | **Search Index Data Contributor** | AI Search service | Upload documents |
| 6 | **Foundry project managed identity** | **Search Index Data Reader** | AI Search service | Agent's Azure AI Search tool queries the index (least privilege) |
| 7 | **Foundry project managed identity** | **Search Service Contributor** | AI Search service | Lets the tool read the index schema/vectorizer (listed by current Foundry docs) |
| 8 | **AI Search system-assigned managed identity** | **Cognitive Services OpenAI User** | Foundry resource | Integrated vectorizer embeds queries at runtime |
| 9 | **You** *(optional, tracing)* | **Log Analytics Reader** | Application Insights resource | Read traces in Foundry **Tracing** and App Insights (Step 13.1) |

> **About row 6:** *Search Index Data Reader* is the least-privilege, read-only data-plane role and is the answer
> to "the agent only needs to query the index" exam questions. The current Foundry Azure AI Search tool doc lists
> **Search Index Data Contributor** + **Search Service Contributor** for the project identity. If the agent's search
> calls return 403 with Reader, add **Search Index Data Contributor** to the project identity.

**Portal method (repeat per row):** open the target resource → **Access control (IAM)** → **Add** → **Add role
assignment** → **Role** tab: search the role name → **Next** → **Members** tab: *Assign access to* =
**User, group, or service principal** (rows 1–5) or **Managed identity** (rows 6–8; pick the Foundry project or the
search service) → **Select members** → **Review + assign**.

**CLI method (PowerShell):**

```powershell
$ME          = az ad signed-in-user show --query id -o tsv
$SUB         = az account show --query id -o tsv
$FOUNDRY     = "<foundry-resource-name>"
$PROJECT     = "contoso-support"
$FOUNDRY_ID  = "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY"
$PROJECT_ID  = "$FOUNDRY_ID/projects/$PROJECT"
$SEARCH_ID   = az search service show -g $RG -n $SEARCH --query id -o tsv
$SAFETY_ID   = az cognitiveservices account show -g $RG -n $SAFETY --query id -o tsv
$PROJECT_MI  = az resource show --ids $PROJECT_ID --api-version 2025-06-01 --query identity.principalId -o tsv   # or copy it from Step 3.6
$SEARCH_MI   = az search service show -g $RG -n $SEARCH --query identity.principalId -o tsv

function Grant($principal, $type, $role, $scope) {
  az role assignment create --assignee-object-id $principal --assignee-principal-type $type `
    --role $role --scope $scope --output none
  Write-Host "Granted '$role' -> $type $principal"
}

Grant $ME         User             "Azure AI User"                   $PROJECT_ID
Grant $ME         User             "Cognitive Services OpenAI User"  $FOUNDRY_ID
Grant $ME         User             "Cognitive Services User"         $SAFETY_ID
Grant $ME         User             "Search Service Contributor"      $SEARCH_ID
Grant $ME         User             "Search Index Data Contributor"   $SEARCH_ID
Grant $PROJECT_MI ServicePrincipal "Search Index Data Reader"        $SEARCH_ID
Grant $PROJECT_MI ServicePrincipal "Search Service Contributor"      $SEARCH_ID
Grant $SEARCH_MI  ServicePrincipal "Cognitive Services OpenAI User"  $FOUNDRY_ID
```

Role assignments can take **up to 10 minutes** to propagate. Run `az account get-access-token` again (or
`az logout; az login`) if you see 401/403 right after assigning.

> **Exam traps:** *Cognitive Services OpenAI **User*** can call inference but cannot deploy models;
> *Cognitive Services OpenAI **Contributor*** can also manage deployments. *Search Index Data **Reader*** can query
> but not write; *Search **Service** Contributor* manages the service and index *definitions* but grants **no data
> access**. *Reader* (ARM) never grants data-plane access.

## Step 7 — Connect AI Search to the Foundry project

1. Foundry portal → your project → **Manage** *(classic: **Management center**)* → **Project details** →
   **Connected resources** tab → **Add connection**.
2. Select **Azure AI Search** → browse to `$SEARCH` → **Authentication: Microsoft Entra ID** (keyless) →
   **Add connection**.
3. Copy the connection **Name** → `.env` `AZURE_SEARCH_CONNECTION_NAME`.
   (`provision_agent.py` resolves it to the full connection resource ID at runtime.)

## Step 8 — Build and populate the vector index

The script creates `contoso-policy-index` with:

- Fields `id`, `title`, `section`, `content`, `source`, `url`, `chunk_index`, and `content_vector`
  (`Collection(Edm.Single)`, `EMBEDDING_DIMENSIONS` dims — 1536 for 3-small, 3072 for 3-large — HNSW / cosine).
- An **Azure OpenAI vectorizer** (`aoai-embedding`) bound to your embedding deployment that authenticates with the **search service's
  managed identity** (no API key) — this is what lets the agent issue *vector* and *hybrid* queries with plain text.
- A **semantic configuration** (`title` / `content` / `section`) for the semantic ranker.

```powershell
python -m scripts.ingest_policy --dry-run     # inspect heading-aware chunks (no Azure calls)
python -m scripts.ingest_policy               # create index + embed + upload
python -m scripts.ingest_policy --recreate    # after renaming/removing policy headings (avoids stale chunks)
```

Verify: Azure portal → AI Search → **Search management → Indexes** → `contoso-policy-index` → document count = 31 (one per policy subsection).
In **Search explorer**, choose **View → JSON view** and run:

```json
{ "search": "free shipping Alaska", "queryType": "semantic", "semanticConfiguration": "contoso-semantic-config",
  "vectorQueries": [{ "kind": "text", "text": "free shipping Alaska", "fields": "content_vector", "k": 5 }],
  "select": "title,url" }
```

The top hit must be *4. Shipping Policy > 4.2 Shipping exemptions…*. A `text` vector query only works because the
vectorizer is configured — if it errors, revisit Step 6 row 8.

> **Lab fallback — no permission to assign roles:** if **Access control (IAM) → Add role assignment** is greyed
> out, you are Contributor but not Owner/User Access Administrator. Until an admin assigns the roles, set in `.env`:
> `AZURE_SEARCH_API_KEY` = an **admin key** (AI Search → **Settings → Keys**; API access control must be **API keys**
> or **Both**) and `AZURE_OPENAI_VECTORIZER_API_KEY` = a Foundry resource key (**Keys and Endpoint**), then run
> `python -m scripts.ingest_policy --recreate`. In Step 7, create the Foundry Search connection with
> **Authentication: API key** instead of Microsoft Entra ID. Remove both keys and switch back to keyless once roles
> are granted — key auth is the anti-pattern the exam expects you to replace with managed identity + RBAC.

> **Portal alternative (exam-relevant):** AI Search → **Import data (new)** *(also shown as **Import and vectorize
> data**)* → data source (Blob Storage containing `contoso_policy.md`) → **RAG** scenario → vectorize with your
> Foundry `text-embedding-3-small` deployment using **System assigned identity** → enable **semantic ranker**. This
> creates a data source, skillset (chunking via *Text Split* + *Azure OpenAI Embedding* skills), index, and indexer.
> If you use it, set `AZURE_SEARCH_INDEX_NAME` to the generated index name.

## Step 9 — Deploy the Azure Function (order API)

**Run locally first:**

```powershell
cd src
Copy-Item local.settings.json.example local.settings.json
func start
# in another terminal:
Invoke-RestMethod http://localhost:7071/api/orders/CUST-10001/status   # -> status : Shipped
cd ..
```

**Create the Function App:** Azure portal → **Create a resource** → **Function App** → **Flex Consumption** →

- Function App name: `$FUNCAPP`; **Runtime stack: Python**, **Version: 3.11**; region: same as above.
- **Storage:** create new. **Monitoring:** enable **Application Insights**.
- **Review + create** → **Create**.

**Publish the code** (from the `src` folder — it is the Function App root):

```powershell
cd src
func azure functionapp publish $FUNCAPP
cd ..
az functionapp config appsettings set -g $RG -n $FUNCAPP --settings FUNCTION_AUTH_LEVEL=function
```

**Verify the key requirement:**

```powershell
$KEY = az functionapp function keys list -g $RG -n $FUNCAPP --function-name GetOrderStatus --query default -o tsv
# New Function Apps get a unique default hostname (e.g. <name>-<hash>.<region>-01.azurewebsites.net),
# so always read it from Azure instead of assuming <name>.azurewebsites.net:
$HOST_NAME = az functionapp show -g $RG -n $FUNCAPP --query defaultHostName -o tsv
$BASE = "https://$HOST_NAME/api"
Invoke-RestMethod "$BASE/health"                                                   # 200, anonymous
try { Invoke-RestMethod "$BASE/orders/CUST-10001/status" } catch { $_.Exception.Response.StatusCode }  # 401
Invoke-RestMethod "$BASE/orders/CUST-10001/status" -Headers @{ "x-functions-key" = $KEY }        # Shipped
```

Set `.env` `ORDER_API_BASE_URL` to the value of `$BASE` (run `echo $BASE`). It is the Function App's **Default domain** from the portal **Overview** page plus `https://` and `/api`.

## Step 10 — Store the function key in a Custom keys connection

1. Foundry portal → project → **Manage** → **Project details** → **Connected resources** → **Add connection** →
   **Custom keys**.
2. **Key:** `x-functions-key` (must exactly match `components.securitySchemes.functionKey.name` in
   `agent/openapi_spec.json`). **Value:** the function key from Step 9. Mark it as **secret**.
3. **Connection name:** `contoso-order-api-key` → `.env` `ORDER_API_CONNECTION_NAME`. **Add connection**.

> **Exam note:** the OpenAPI tool supports three auth modes — **Anonymous**, **API key via project connection**,
> and **Managed identity** (audience-scoped token, e.g. for an Entra-protected API). Leave
> `ORDER_API_CONNECTION_NAME` empty and set `FUNCTION_AUTH_LEVEL=anonymous` to try anonymous mode.

## Step 11 — Configure Guardrails and Prompt Shields

Two independent safety layers are configured. The **client gate** (`orchestrator/safety.py`) calls Content Safety
directly; the **Foundry guardrail** protects the model and agent server-side, including tool calls and tool
responses that the client never sees.

1. Foundry portal → project → **Build** → **Guardrails** *(classic: **Guardrails + controls** → **Content
   filters**)* → **Create Guardrail**.
2. **Step 1: Add controls** — add each row below (select the risk, tick the intervention points, choose the action,
   **Add control**):

| Risk | Intervention points | Action | Severity |
|---|---|---|---|
| **User prompt attacks** (Prompt Shields for jailbreak) | User input | **Annotate and block** | — |
| **Indirect attacks** (Prompt Shields for documents) | User input, **Tool response** | **Annotate and block** | — |
| Hate | User input, Output | Block | **Medium** |
| Sexual | User input, Output | Block | **Medium** |
| Violence | User input, Output | Block | **Medium** |
| Self-harm | User input, Output | Block | **Medium** |
| Protected material text | Output | Annotate and block | — |
| Protected material code | Output | Annotate | — |
| **Personally identifiable information (PII)** | Output | **Annotate and block** (or mask, if offered) | — |

3. **Next** → **Step 2: Assign** → **Add models** → `gpt-4.1-mini`; after Step 12, return and **Add agents** →
   `contoso-support-agent` → **Save**.
4. **Next** → **Step 3: Review** → name `contoso-support-guardrail` → **Create**.
5. Test: select the guardrail → **Try in Playground** → send S1 from `tests/exam_verification.md`; you must see a
   blocked message naming the risk and the intervention point.

*Classic portal equivalent:* **Create content filter** → **Input filter**: set *Prompt shields for jailbreak
attacks* = **Annotate and block**, *Prompt shields for indirect attacks* = **Annotate and block**, harm categories
threshold **Medium** → **Output filter**: harm categories **Medium**, *Protected material for text* = **Annotate
and block** → **Apply to deployment** `gpt-4.1-mini`.

> **Exam note:** *User prompt attacks* (jailbreaks) come from the **user**; *indirect attacks* (XPIA) arrive via
> **documents, emails, or tool responses**. Annotate-only returns detection details without blocking — useful for
> monitoring before enforcing. A severity threshold of *Medium* blocks Medium **and** High.

## Step 12 — Provision the agent

```powershell
python -m scripts.provision_agent --dry-run   # review the exact payload sent to Foundry
python -m scripts.provision_agent             # creates contoso-support-agent version N
```

What it sends:

- **Model:** `FOUNDRY_MODEL_DEPLOYMENT_NAME`, **temperature** 0.2 (factual support answers).
- **Instructions:** `agent/system_prompt.txt`.
- **Azure AI Search tool:** your project connection + `contoso-policy-index`, `query_type =
  vector_semantic_hybrid`, `top_k = 5`.
- **OpenAPI tool** `contoso_order_status`: `agent/openapi_spec.json` with `servers[0].url` replaced by
  `ORDER_API_BASE_URL`, auth = project connection `contoso-order-api-key`.

Re-running creates a **new version** of the same agent — that is how you roll out (and roll back) prompt and tool
changes. Then finish Step 11.3 (assign the guardrail to the agent).

**Portal alternative:** **Build** → **Agents** → **Create agent** → name `contoso-support-agent`, model
`gpt-4.1-mini`, paste `system_prompt.txt` into **Instructions** → **Tools** → **Add** → **Azure AI Search** (select
the connection, index `contoso-policy-index`, search type **Hybrid (vector + keyword) with semantic ranking**) →
**Add** → **OpenAPI** (paste the spec with your Function URL in `servers`, authentication **Connection** →
`contoso-order-api-key`) → **Save**. Test in the agent **Playground**.

## Step 13 — Run and verify

```powershell
python -m orchestrator                                   # interactive chat
python -m orchestrator -q "Status for CUST-10001?" --json # one-shot, full pipeline JSON
python -m scripts.run_exam_checks --report exam_report.json
```

Then complete the sign-off table in `tests/exam_verification.md`.

**Quality evaluation (groundedness, relevance, fabrication):** score the replies captured above without calling
the agent again.

```powershell
pip install -r requirements-eval.txt                # once: azure-ai-evaluation (pulls in pandas, nltk)
python -m scripts.evaluate_quality --offline        # deterministic fabricated-fact check only
python -m scripts.evaluate_quality                  # + GroundednessEvaluator and RelevanceEvaluator (1-5)
```

The AI judges call a chat deployment (`EVAL_MODEL_DEPLOYMENT_NAME`, default `FOUNDRY_MODEL_DEPLOYMENT_NAME`) on
`AZURE_OPENAI_ENDPOINT` with your Entra ID, so you need **Cognitive Services OpenAI User** on the Foundry resource
(Step 6 row 2). Reasoning-model judges (o-series, gpt-5, `*-chat-latest`) are detected from the name; override with
`EVAL_IS_REASONING_MODEL`. Results go to `evaluation_report.json` (git-ignored).

### 13.1 Tracing with Application Insights

The orchestrator emits OpenTelemetry traces (`orchestrator/telemetry.py`). One trace per customer turn:

```
contoso.pipeline.turn            stage, blocked, tools, citations, response id
├── contoso.safety.input         Prompt Shields + harm categories verdict
├── invoke_agent contoso-support-agent   gen_ai.* attributes: model, token usage, tool calls
└── contoso.safety.output        output moderation verdict
```

The agent span comes from `AIProjectInstrumentor` (azure-ai-projects, **preview**): it instruments the
Conversations and Responses calls and propagates the W3C trace context to the Agent Service, so the server-side
steps (model, Azure AI Search, OpenAPI calls) join the same trace in the portal.

**1. Try it locally first (no Azure resources):**

```powershell
python -m orchestrator --trace console -q "What's the return window for a laptop with Contoso Plus?"
```

A span tree with durations and token counts prints to stderr.

**2. Connect Application Insights to the project:** Foundry portal → your project → **Tracing**
*(new portal: **Operate** → **Tracing**)* → **Connect** → choose the Application Insights resource created with the
Function App in Step 9 (or **Create new**) → **Connect**. Needs **Contributor** (or Owner) on the Foundry project.

**3. Export traces:**

```powershell
python -m orchestrator --trace azure_monitor -q "Status for CUST-10001?"
python -m scripts.run_exam_checks --trace azure_monitor     # one trace per case; failing cases print their trace id
```

Or set `TRACING_MODE=azure_monitor` in `.env`. The client reads the connection string from the project
(`project.telemetry.get_application_insights_connection_string()`). If that fails with 403 (your role cannot read
connection secrets), copy **Application Insights → Overview → Connection String** into
`APPLICATIONINSIGHTS_CONNECTION_STRING` (it embeds the ingestion key, so treat it as a secret).

**4. View them** (allow 2–5 minutes):

- **Foundry portal → Tracing**: per-run view of model calls, tool calls, tokens and latency. The CLI sends the
  agent version id in `agent_reference` when tracing is on, which links traces to the agent.
- **Application Insights → Transaction search**: paste the `trace=` id the CLI prints (it is the `operation_Id`).
- **Application Insights → Logs**: run the KQL in [`monitoring/app_insights_queries.kql`](monitoring/app_insights_queries.kql)
  (turn outcomes, block rate by layer, p95 latency, token usage, tool mix, failing exam cases).
- Viewing needs **Log Analytics Reader** on the Application Insights resource (or its workspace) — Step 6 row 9.

**Message content is not recorded by default** (prompts and replies can hold personal data). For a debugging
session only, set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`; the CLI warns while it is on.

> **Exam notes:** tracing is client-side OpenTelemetry exported to **Application Insights**; the Foundry portal
> reads the *connected* Application Insights resource. Content recording is opt-in. Content-filter blocks show up
> as span events (`contoso.content_filter_block`) and as `stage=model_content_filter`, separate from client-side
> Prompt Shields blocks (`stage=input_safety`), so you can tell which layer stopped an attack.

The Function App already sends its own request logs to the same Application Insights resource (Step 9 →
**Monitoring**), so `requests` shows every order lookup made by the OpenAPI tool.

**Hardening for production:** set AI Search **API access control** to **Role-based access control**; set
`SAFETY_FAIL_CLOSED=true`; host the orchestrator on App Service or Azure Container Apps with a **user-assigned
managed identity** (set `AZURE_CLIENT_ID` so `DefaultAzureCredential` selects it) holding rows 1 and 3 of the
RBAC table; consider private endpoints for Foundry, Search, and the Function App.

## Step 14 — Clean up

First remove what the scripts created inside the services, then delete the Azure resources:

```powershell
python -m scripts.teardown            # dry run: lists the agent (all versions) and the index
python -m scripts.teardown --yes      # deletes them; --keep-agent / --keep-index to skip one
```

Conversations are deleted by the CLI and `run_exam_checks` at the end of every run (the API has no "list"
operation to find orphans). The script finishes by printing the `az group delete` commands — set
`TEARDOWN_RESOURCE_GROUPS=rg-one,rg-two` in `.env` to fill in your names. Delete **every** group you created, e.g.
the main one and the Function App's auto-created `<funcapp>_group`:

```powershell
az group delete --name $RG --yes --no-wait
```

Foundry resources are soft-deleted; purge them if you want to reuse the same name:
Azure portal → **Azure AI Foundry** *(or **Microsoft Foundry**)* → **Manage deleted resources** → **Purge**.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Missing required configuration: …` | `.env` incomplete | Fill the listed variables (see `.env.example`) |
| `403` on document upload (`docs/search.index`) but the index was created | You have Search Service Contributor but not **Search Index Data Contributor**, and **Add role assignment** is greyed out for you | Ask an Owner to assign the role. Lab fallback: set `AZURE_SEARCH_API_KEY` (admin key) and optionally `AZURE_OPENAI_VECTORIZER_API_KEY` in `.env`, then re-run with `--recreate` (see Step 8) |
| `403` from ingestion on `create_or_update_index` | Search still on **API keys** only, or role not propagated | Step 4.2 → **Both**; wait 10 min; check Step 6 rows 4–5 |
| Uploads fail: *vector dimension mismatch* | `EMBEDDING_DIMENSIONS` ≠ field dimension | 1536 for 3-small, 3072 for 3-large; re-run with `--recreate` after any change |
| Search explorer `text` vector query errors | Vectorizer cannot reach the embedding deployment | Step 6 row 8; check `AZURE_OPENAI_ENDPOINT` is the `*.openai.azure.com` endpoint |
| Agent answers policy questions without citations | Index lacks retrievable `url`/`title`, or search tool failing | Inspect the trace; confirm Step 6 rows 6–7 |
| OpenAPI tool returns 401 | Key name mismatch | Connection **Key** must be `x-functions-key` exactly |
| OpenAPI tool not called | Spec/description unclear or customerId missing | The agent asks for the ID by design (T2); include `CUST-xxxxx` |
| Every request blocked with `safety_service_error` | Missing **Cognitive Services User** on Content Safety (fail-closed); run with `-v` to see the HTTP 401/403 hint | Step 6 row 3, or the `CONTENT_SAFETY_API_KEY` lab fallback (Step 5) |
| `HTTP 403 ... 'Azure AI User'` from `provision_agent` or `Could not create conversation` | Missing **Azure AI User** on the Foundry project | Step 6 row 1 — must come from an admin (no key fallback) |
| Request blocked at `model_content_filter` | Foundry guardrail fired (expected for S1–S3) | Check the guardrail annotations in the trace |
| `TRACING_MODE=azure_monitor ... No Application Insights resource is connected` | Tracing not connected for the project | Step 13.1 item 2, or set `APPLICATIONINSIGHTS_CONNECTION_STRING` |
| `Could not read the project's Application Insights connection (403)` | Your role can't read connection secrets | Set `APPLICATIONINSIGHTS_CONNECTION_STRING` from App Insights → Overview |
| Traces sent but Foundry **Tracing** is empty | Ingestion delay, or no **Log Analytics Reader** | Wait 2–5 min; Step 6 row 9; check App Insights → Transaction search for the printed `trace=` id |
| `ModuleNotFoundError: azure.functions` locally | Function venv missing deps | `pip install -r src/requirements.txt` |
