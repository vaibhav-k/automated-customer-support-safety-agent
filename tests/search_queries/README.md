# Azure AI Search verification queries

Paste any file into **Azure portal → AI Search → Search management → Indexes → `contoso-policy-index` →
Search explorer → View → JSON view**, then select **Search**. Each query is hybrid: BM25 keyword search, a
`text` vector query that the index's vectorizer embeds, and semantic reranking with extractive captions and
answers.

| File | Checks | Pass criteria |
|---|---|---|
| `01_index_health_wildcard.json` | Index health | `@odata.count` = **31**, no errors (proves the vectorizer can reach the embedding deployment). Scores are flat and there is no reranking, as expected for `*`. |
| `02_alaska_free_shipping.json` | Regional exemption (exam case R4) | Top hit **4.2 Shipping exemptions (POL-SHP-002)**; answer/caption mentions **$19.99** and **7 to 12 business days** |
| `03_laptop_contoso_plus.json` | Category override (R3) | Top hit **2.3 Category-specific windows (POL-RET-003)** |
| `04_germany_tablet_return.json` | Regional override (R5) | **5.3 United Kingdom and European Union (POL-REG-003)** in the top 2 |
| `05_cancel_shipped_order.json` | Cancellation rules (C1) | Top hit **7.1 Cancelling an order (POL-ORD-001)** |
| `06_trade_in_not_in_policy.json` | No-answer case (F1) | Low `@search.rerankerScore` (roughly < 1.5) and no answers; nothing really matches, so the agent should use its "I couldn't find that…" fallback |

`@search.rerankerScore` runs from 0 to 4. A strong match is usually above 2.5.

**REST alternative** (key fallback; use `Authorization: Bearer` with an Entra token once RBAC is granted):

```powershell
# Values from your .env (PowerShell does not read .env automatically)
$env:AZURE_SEARCH_ENDPOINT = "https://automated-customer-support-safety-agent.search.windows.net"
$env:AZURE_SEARCH_API_KEY  = "<admin key>"

$body = Get-Content tests\search_queries\02_alaska_free_shipping.json -Raw
Invoke-RestMethod -Method Post `
  -Uri "$env:AZURE_SEARCH_ENDPOINT/indexes/contoso-policy-index/docs/search?api-version=2026-04-01" `
  -Headers @{ "api-key" = $env:AZURE_SEARCH_API_KEY } -ContentType "application/json" -Body $body
```
