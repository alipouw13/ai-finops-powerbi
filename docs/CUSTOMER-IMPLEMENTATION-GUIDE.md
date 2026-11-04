# Enterprise Customer Implementation Guide - AI FinOps & Tokenomics Accelerator

This guide defines how an enterprise **Cloud Solution Architect**, **FinOps Practitioner**, and **Data & AI Engineer** takes this accelerator from the bundled FOCUS 1.2 + Multi-Platform PoC dataset to **100% live production data** across:

1. **Microsoft Cost Management FOCUS 1.2-preview** (Azure AI Foundry, Azure OpenAI PAYG & PTU Reservations, Foundry MaaS, Document Intelligence, Speech, Vision, Language, Content Safety, Azure ML, Supporting Infrastructure, and Copilot Studio / Cowork PAYG & P3 Commit Units)
2. **Azure API Management (APIM) AI Gateway** (Per-request tokenomics, caller identity, semantic caching, and token quota enforcement)
3. **Microsoft 365 Copilot & Agent 365** (Seat licensing, per-app activity, Cowork/Work IQ credits, and the Agent 365 Registry + Active Users)
4. **Microsoft Copilot Studio** (Dataverse `msdyn_aievents`, Prepaid Capacity Packs vs. P3 Commit vs. PAYG, M365 license zero-rating, and BYOM Foundry exclusion)
5. **GitHub Copilot Enterprise / Business** (Seat assignments, IDE activity, metered Premium Request overages, and engineering acceptance metrics)

---

## 1. End-to-End Reference Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                              ENTERPRISE AI TELEMETRY & BILLING SOURCES                          │
├──────────────────────────┬──────────────────────────┬────────────────────────┬──────────────────┤
│ Azure Cost Management    │ APIM AI Gateway          │ Microsoft 365 & PPAC   │ GitHub Enterprise│
│ • FOCUS 1.2-preview      │ • validate-azure-ad-token│ • Graph subscribedSkus │ • /copilot/seats │
│   Daily Export (Parquet) │ • llm-emit-token-metric  │ • Graph Copilot Usage  │ • /billing/usage │
│ • Covers AOAI, PTU, MaaS,│ • llm-semantic-cache     │ • Agent 365 Registry   │ • /copilot/metric│
│   AI Search, DocIntel,   │ • llm-token-limit        │ • Dataverse            │ • SCIM/Entra UPN │
│   AML, Fabric, Studio P3 │ • Diagnostic Logs -> LA  │   msdyn_aievents       │   Identity Map   │
└────────────┬─────────────┴────────────┬─────────────┴───────────┬────────────┴─────────┬────────┘
             │ OneLake Shortcut         │ Eventhouse / Export     │ Fabric Data Factory  │ REST
             ▼                          ▼                         ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│  MICROSOFT FABRIC LAKEHOUSE - MEDALLION ARCHITECTURE                                            │
│                                                                                                 │
│  [BRONZE] Append-Only Raw Fidelity (platform/medallion/bronze/)                                 │
│    • bronze_focus_cost              • bronze_m365_copilot_seats / _usage / _credits             │
│    • bronze_apim_gateway_logs       • bronze_studio_credits (msdyn_aievents)                    │
│    • bronze_azure_ai_metrics        • bronze_ghc_seats / _premium_usage / _metrics              │
│    • bronze_agent365_agents / _users• bronze_ref_identity_map / _app_inventory / _rate_card     │
│                                                                                                 │
│  [SILVER] Deduplication, Identity Graph & FOCUS Pro-Rata Allocation (platform/medallion/silver/)│
│    • Deduplicates FOCUS MTD partitions (latest _ingested_at wins per ChargePeriodStart+Resource)│
│    • Resolves UPN <-> Entra OID <-> GitHub Login <-> Service Principal <-> Entra Agent ID       │
│    • Allocates FOCUS Resource EffectiveCost across APIM callers by weighted token share         │
│    • Flags unlogged FOCUS AOAI spend as 'unattributed-gateway-bypass' (Shadow AI key detection) │
│    • Excludes Copilot Studio BYOM Foundry rows to prevent double-counting Foundry token spend   │
│                                                                                                 │
│  [GOLD] 16-Table Star Schema (platform/medallion/gold/ + agent365/ + persona/ + allocation/)    │
│    • fact_ai_usage (28 cols)        • fact_user_cost_allocation (DIRECT vs OWNER vs UNALLOCATED)│
│    • fact_agent_inventory           • fact_agent_activity                                       │
│    • 12 Conformed Dimensions & Bridges -> Direct Lake / Import Power BI Semantic Model (TMDL)   │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Source 1: Microsoft Cost Management - FOCUS 1.2-Preview Export

### Why FOCUS 1.2 Is Mandatory
FOCUS (FinOps Open Cost and Usage Specification) combines **Actual (Billed)** and **Amortized (Effective)** costs in a single row, includes **ListCost** and **ContractedCost** natively, and exposes commitment utilization (`CommitmentDiscountStatus = 'Used' | 'Unused'`) for **Azure OpenAI PTU Reservations** and **Copilot Credit Pre-Purchase Plans (P3)**.

### Prerequisites & Permissions
- **Supported Billing Account Types:** Enterprise Agreement (EA), Microsoft Customer Agreement (MCA), or Microsoft Partner Agreement (MPA).
- **RBAC Role Required:** `Cost Management Reader` or `Cost Management Contributor` at the Billing Account, Billing Profile, or Subscription scope, plus `Storage Blob Data Contributor` on the destination ADLS Gen2 account.

### Step-by-Step Setup
1. **Create a Cost Management FOCUS Export:**
   - In Azure Portal → **Cost Management + Billing** → **Exports** → **+ Create**.
   - **Template:** `Cost and usage details (FOCUS)`
   - **Dataset version:** `1.2-preview` (or `1.0r2`)
   - **Frequency:** `Daily export of month-to-date costs`
   - **Format / Compression:** `Parquet` with `Snappy` (recommended for Fabric OneLake) or `CSV`
   - **File partitioning:** Enabled (`Overwrite data` = off so Bronze retains history, or deduplicate in Silver using `_ingested_at`).
2. **Mount in Microsoft Fabric via OneLake Shortcut:**
   - In `LH_tokenomics_bronze` → `Files` → **New shortcut** → **Azure Data Lake Storage Gen2** → point to the container/folder where Cost Management writes the FOCUS export.
3. **How FOCUS 1.2 Maps to `fact_ai_usage`:**

| FOCUS 1.2 Column | Gold `fact_ai_usage` Column | FinOps Meaning |
|---|---|---|
| `ChargePeriodStart` | `usage_date` | Usage date (`YYYY-MM-DD`) |
| `ServiceCategory` | `service_category` | `AI and Machine Learning`, `Databases`, `Integration`, `Analytics` |
| `ServiceName` | `service_name` | `Azure OpenAI`, `Azure AI Foundry`, `Azure AI Document Intelligence`, `Azure AI Speech`, `Azure AI Content Safety`, `Azure Machine Learning`, `Azure AI Search`, `API Management`, `Azure Cosmos DB`, `Azure Managed Redis`, `Microsoft Copilot Studio`, `Microsoft Fabric` |
| `PricingCategory` | `pricing_category` | `Standard` (PAYG), `Committed` (PTU Reservation / Copilot P3 Plan) |
| `CommitmentDiscountStatus` | `commitment_discount_status` | `Used` vs `Unused` (exposes unused PTU reservation waste!) |
| `ConsumedQuantity` | `quantity` | Native meter units (`1K Tokens`, `PTU-Hours`, `SU-Hours`, `1K Pages`, `Hours`) |
| `x_ListCostInUsd` | `list_cost_usd` | Public retail price before negotiated or commitment discounts |
| `x_ContractedCostInUsd` | `contracted_cost_usd` | Cost after EA/MCA negotiated discount |
| `x_EffectiveCostInUsd` | `effective_cost_usd` & `cost_usd` | **Primary FinOps measure** (includes amortized PTU & P3 reservations) |
| `x_BilledCostInUsd` | `billed_cost_usd` | Cash invoice amount in the billing period |
| `Tags` (`$.app`, `$.bu`, `$.env`, `$.agent_id`) | `application_key`, `business_unit_key`, `environment_key`, `agent_key` | Workload, BU, environment, and Agent 365 attribution |
| `x_CostCenter` | `cost_center_key` | Billing cost center |

---

## 3. Source 2: Azure API Management (APIM) AI Gateway

### Why APIM AI Gateway Is Required
Azure Cost Management (FOCUS) and Azure Monitor metrics have **zero caller identity** - they only know which Azure OpenAI resource was billed. Routing all LLM and agent traffic through **Azure API Management (Standard v2 / Premium v2)** with Entra JWT validation and `llm-emit-token-metric` is the **only way** to attribute tokens and FOCUS dollars to specific users, service principals, and agents.

### Production APIM Policy XML (`inbound` / `outbound`)
Apply this policy on your APIM AI Gateway APIs fronting Azure AI Foundry / Azure OpenAI:

```xml
<policies>
    <inbound>
        <base />
        <!-- 1. Validate Entra ID JWT and extract caller identity + FinOps claims -->
        <validate-azure-ad-token tenant-id="{{entra-tenant-id}}" failed-validation-httpcode="401">
            <output-token-variable-name>jwt</output-token-variable-name>
        </validate-azure-ad-token>
        <set-variable name="callerOid" value="@(((Jwt)context.Variables["jwt"]).Claims.GetValueOrDefault("oid", "unknown"))" />
        <set-variable name="clientAppId" value="@(((Jwt)context.Variables["jwt"]).Claims.GetValueOrDefault("appid", "unknown"))" />
        <set-variable name="callerUpn" value="@(((Jwt)context.Variables["jwt"]).Claims.GetValueOrDefault("upn", ""))" />
        <set-variable name="costCenter" value="@(context.Request.Headers.GetValueOrDefault("x-cost-center", ((Jwt)context.Variables["jwt"]).Claims.GetValueOrDefault("cc", "CC-NA")))" />
        <set-variable name="businessUnit" value="@(context.Request.Headers.GetValueOrDefault("x-business-unit", ((Jwt)context.Variables["jwt"]).Claims.GetValueOrDefault("bu", "BU-UNALLOC")))" />
        <set-variable name="agentId" value="@(context.Request.Headers.GetValueOrDefault("x-agent-id", "NO-AGENT"))" />

        <!-- 2. Enforce Prompt Shield & Content Safety via Azure AI Content Safety -->
        <llm-content-safety backend-id="acs-apim-guardrail-prod" shield-prompt="true" />

        <!-- 3. Semantic Cache Lookup (Azure Managed Redis Enterprise E10) -->
        <llm-semantic-cache-lookup score-threshold="0.15" embeddings-backend-id="aoai-embedding-backend" embeddings-backend-auth="system-assigned" />

        <!-- 4. Enforce Per-Application Token Quota & TPM Rate Limit -->
        <llm-token-limit counter-key="@((string)context.Variables["clientAppId"])"
                         tokens-per-minute="150000"
                         token-quota="50000000"
                         token-quota-period="Monthly"
                         estimate-prompt-tokens="false" />

        <!-- 5. Emit LLM Token Metrics (Prompt, Completion, Cached, Reasoning) with FinOps Dimensions -->
        <llm-emit-token-metric namespace="ai-finops-llm-metrics">
            <dimension name="ClientId" value="@((string)context.Variables["clientAppId"])" />
            <dimension name="Oid" value="@((string)context.Variables["callerOid"])" />
            <dimension name="Upn" value="@((string)context.Variables["callerUpn"])" />
            <dimension name="AgentId" value="@((string)context.Variables["agentId"])" />
            <dimension name="CostCenterClaim" value="@((string)context.Variables["costCenter"])" />
            <dimension name="BusinessUnitClaim" value="@((string)context.Variables["businessUnit"])" />
            <dimension name="ModelName" value="@(context.Request.MatchedParameters.GetValueOrDefault("deployment-id", "unknown"))" />
        </llm-emit-token-metric>

        <!-- Authenticate to backend Azure AI Foundry / OpenAI using APIM Managed Identity -->
        <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    </inbound>
    <backend>
        <base />
    </backend>
    <outbound>
        <base />
        <!-- Store completion in Redis Semantic Cache for 1 hour -->
        <llm-semantic-cache-store duration="3600" />
    </outbound>
</policies>
```

### Detecting Direct Resource Access That Bypassed APIM (`Bypassed Gateway Spend`)
In Silver (`10_conform_usage.py` / `build_data.py`), we compare each Azure OpenAI resource's daily FOCUS `EffectiveCost` against the token volume logged in APIM for that resource. Any AOAI resource or spend in FOCUS with zero matching APIM gateway logs is flagged with `gateway_routed = FALSE` and `identity_key = 'unattributed-gateway-bypass'`. **Best practice:** disable local key authentication (`disableLocalAuth: true`) on all Azure OpenAI/Foundry resources and allow only the APIM Managed Identity on the data plane.

---

## 4. Source 3: Microsoft 365 Copilot & Agent 365 Integration

### Required Entra App Registration Permissions (Microsoft Graph)
Create an Entra Service Principal for the Fabric ingestion pipeline with the following **Application** or **Delegated** permissions (Admin Consent required):
- `Reports.Read.All` & `ReportSettings.ReadWrite.All` - Calls `GET https://graph.microsoft.com/beta/reports/getMicrosoft365CopilotUsageUserDetail(period='D28')` (note: v2 requires `D28`, not `D30`)
- `Directory.Read.All`, `Organization.Read.All`, & `User.Read.All` - Calls `GET /v1.0/subscribedSkus` and `GET /v1.0/users?$select=id,userPrincipalName,displayName,department,assignedLicenses`
- `CopilotPackages.Read.All` - Calls `GET https://graph.microsoft.com/v1.0/copilot/admin/catalog/packages` and `GET /v1.0/copilot/admin/catalog/packages/{id}` (live Agent 365 Catalog & Package Detail API, returning `activeUsers`, `totalSessions`, `ownerId`, `agentIdentityId`, and `elementDetails` including `SourceIds.AzureResourceId`)
- `AgentIdentity.Read.All` & `AgentIdentityBlueprint.Read.All` - Calls `GET https://graph.microsoft.com/beta/servicePrincipals/microsoft.graph.agentIdentity` and `GET /beta/applications/microsoft.graph.agentIdentityBlueprint`
- `IdentityRiskyAgent.Read.All` - Calls `GET https://graph.microsoft.com/beta/identityProtection/riskyAgents`
- *(Optional Phase 2)* `AiEnterpriseInteraction.Read.All` - Calls `GET /v1.0/copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions`

### Critical Tenant Admin Setting (Unmasking User Principal Names)
By default, Microsoft 365 Admin Center anonymizes `User Principal Name` in usage and Agent 365 active-user exports (`is_identity_resolved = 0` in `fact_agent_activity`).
To enable per-user attribution and idle-license reclamation (either in the UI or programmatically):
- **Via Microsoft Graph API:** `PATCH https://graph.microsoft.com/beta/admin/reportSettings` with body `{"displayConcealedNames": false}` (requires `ReportSettings.ReadWrite.All`).
- **Via UI:** Go to **Microsoft 365 admin center** (`https://admin.microsoft.com`) → **Settings** → **Org settings** → **Services** → **Reports** → Uncheck *"Display concealed user, group, and site names in all reports"*.

### Agent 365 Registry & Spend Attribution (100% Live Graph API)
`platform/fabric/extract_m365_graph.py` automatically queries `GET /v1.0/copilot/admin/catalog/packages`, enriches non-third-party packages via `GET /v1.0/copilot/admin/catalog/packages/{id}`, and queries `GET /beta/servicePrincipals/microsoft.graph.agentIdentity` to write `bronze_agent365_agents.csv` and `bronze_entra_agent_identities.csv` directly into `platform/fabric/bronze_real/` - no manual CSV export required!
- **How Agent 365 Connects to Spend (`rel_usage_agent`):**
  Agent 365 itself does not expose dollar cost per agent, but `GET /v1.0/copilot/admin/catalog/packages/{id}` exposes `elementDetails` (`AgentMetadatas` & `Bots`) which we link to `fact_ai_usage` via three deterministic bridges:
  1. **Direct Azure AI Foundry Resource Link (`foundry_resource_id`):** Extracted from `elementDetails -> AgentMetadatas -> SourceIds.AzureResourceId` (`/subscriptions/.../Microsoft.CognitiveServices/accounts/.../projects/.../agents/...`), joining directly to Azure Cost Management FOCUS `ResourceId`.
  2. **Entra Agent Identity (`entra_agent_id`):** Extracted from `agentIdentityId` and `/beta/servicePrincipals/microsoft.graph.agentIdentity`, matching the `Oid` / `ClientId` logged by APIM AI Gateway.
  3. **Copilot Studio Bot ID (`bot_id`):** Extracted from `elementDetails -> Bots -> botId`, matching `msdyn_aievents` / `bots` in Dataverse.

---

## 5. Source 4: Microsoft Copilot Studio & M365 Usage-Based Billing (Copilot Credits)

### The Three Billing Channels for Copilot Credits
Per Microsoft's official billing architecture, Copilot Credits are the shared currency across **Copilot Studio**, **M365 Copilot Cowork / Autopilot**, and **Work IQ APIs**, and can be purchased three ways:

| Purchasing Model | Where Cost Appears | Where Usage Detail Appears | How This Repo Models It |
|---|---|---|---|
| **1. Prepaid Capacity Packs** ($200 / 25,000 credits/mo) | M365 invoice (fixed subscription line, **NOT in Azure Cost Mgmt**) | Power Platform Admin Center (PPAC) & Dataverse `msdyn_aievents` | `pricing_category = 'CreditPack'`, `cost_is_estimated = TRUE`, valued at `$0.008/credit` via `dim_rate_card` |
| **2. Copilot Credit Pre-Purchase Plan (P3)** (1-year commit units) | **Azure Cost Management FOCUS** (`ServiceName = 'Microsoft Copilot Studio'`, `CommitmentDiscountType = 'Reservation'`) | PPAC & M365 Admin Center > Copilot > Cost Management | `pricing_category = 'Committed'`, `commitment_discount_status = 'Used' | 'Unused'`, `cost_is_estimated = FALSE` (`EffectiveCost`) |
| **3. Pay-As-You-Go (PAYG) Meter** | **Azure Cost Management FOCUS** (`ServiceName = 'Microsoft Copilot Studio'`, `PricingCategory = 'Standard'`) | PPAC & M365 Admin Center > Copilot > Cost Management | `pricing_category = 'Standard'`, `cost_is_estimated = FALSE` (`BilledCost` / `EffectiveCost`) |

### Two Mandatory Engineering Guardrails Implemented in Silver
1. **Never Multiply Activity Counts by Credit Rates:** Users holding a `$30/mo` M365 Copilot license are **zero-rated** for classic answers, generative answers, tenant graph grounding, and agent flows invoked interactively. Always read `msdyn_creditconsumed` from Dataverse `msdyn_aievents` (which is already net of M365 license zero-rating).
2. **Exclude BYOM (Bring Your Own Model) Foundry Agents from Credit Cost:** Copilot Studio agents connected to your own Azure AI Foundry deployment (`is_byom_foundry = TRUE`) bill their tokens directly in Azure Cost Management under `Azure OpenAI` / `Azure AI Foundry`. Silver filters out BYOM rows from `CopilotStudio` credit cost so they are never double-counted.

---

## 6. Source 5: GitHub Copilot Enterprise / Business

### Required GitHub Permissions & Endpoints
Create a GitHub App or Classic/Fine-Grained PAT with `manage_billing:copilot`, `read:org`, and `read:enterprise` scopes, and enable the **"Copilot usage metrics" policy** in GitHub Enterprise settings:
1. **Seat Assignments & Idle Detection:**
   `GET https://api.github.com/orgs/{org}/copilot/billing/seats`
   → Lands in `bronze_ghc_seats` (`assignee.login`, `plan_type`, `last_activity_at`, `last_activity_editor`). Seats with `last_activity_at` older than 28 days or `NULL` surface in `Idle Licensed Users` and `Idle Seat Waste (monthly)`.
2. **Metered Premium Request Overages (Enhanced Billing Platform):**
   `GET https://api.github.com/enterprises/{enterprise}/settings/billing/usage?product=copilot`
   → Lands in `bronze_ghc_premium_usage` (`date`, `login`, `sku`, `quantity`, `model`, `model_multiplier`, `gross_amount`, `discount_amount`, `net_amount`, `repository_name`).
3. **Developer Productivity & Acceptance Metrics:**
   `GET https://api.github.com/orgs/{org}/copilot/metrics`
   → Lands in `bronze_ghc_metrics` (`total_active_users`, `total_engaged_users`, `suggestions_count`, `acceptances_count`, `chat_turns_count`).
4. **GitHub Login ↔ Entra UPN Identity Resolution:**
   For GitHub Enterprise Managed Users (EMU), `github_login` maps deterministically via SCIM `externalId` / `userName` in Entra ID (`GET /servicePrincipals/{github-enterprise-sp-id}/synchronization/jobs`). For non-EMU orgs, maintain `bronze_ref_identity_map.csv` linking `github_login` to `upn`.

---

## 7. Financial Services (FSI) 100% Real-Data Deployment (`--real-only`)

For regulated enterprise environments (banks, insurers, capital markets) deploying with **zero mock data** and unattended Service Principal authentication:

### 7.1 Unattended Service Principal & Azure Key Vault Configuration
Both `platform/fabric/extract_real_bronze.py` and `platform/fabric/extract_m365_graph.py` support unattended OAuth2 `client_credentials` authentication via environment variables or Fabric `notebookutils.credentials.getSecret`:
```bash
export AZURE_TENANT_ID="<entra-tenant-guid>"
export AZURE_CLIENT_ID="<spn-app-id>"
# Option A: Direct secret in CI/CD or Container Job
export AZURE_CLIENT_SECRET="<spn-client-secret>"
# Option B: Inside a Fabric Notebook backed by Azure Key Vault
export AZURE_KEY_VAULT_URI="https://<vault-name>.vault.azure.net/"
export AZURE_CLIENT_SECRET_NAME="finops-spn-secret"
```

### 7.2 Extracting & Seeding Reference Tables (`bronze_real/`)
Run the real extractors to pull live Azure Cost Management, Azure Monitor, Entra ID, M365 Copilot, Power Platform / Dataverse, and Copilot Studio telemetry into `platform/fabric/bronze_real/`:
```bash
python platform/fabric/extract_real_bronze.py --i-understand-this-is-real-tenant-data
python platform/fabric/extract_m365_graph.py --i-understand-this-is-real-tenant-data
```
On first run, `extract_real_bronze.py` automatically seeds three customer-editable reference CSVs in `platform/fabric/bronze_real/` (and preserves your edits on subsequent runs):
- `bronze_ref_business_hierarchy.csv` - seeded from your live Entra `department` values (`BU-<DEPT>`). Add your monthly budgets (`monthly_budget_usd`) and executive owners.
- `bronze_ref_rate_card.csv` - seeded with Microsoft & GitHub published list prices. Overwrite `unit_price_usd` with your EA/MCA contract rates.
- `bronze_ref_persona_map.csv` - maps your Entra `department` / team names to the 5 AI FinOps personas (`PERS-KNOW`, `PERS-MAKER`, `PERS-DEV`, `PERS-AIDS`, `PERS-PLAT`) without editing PySpark code.

### 7.3 Deploying in `--real-only` Mode
Pass `--real-only` to `platform/deploy/fabric_deploy.py` to skip the mock lakehouse (`00_load_bronze_csv`) and run the Medallion pipeline exclusively against `bronze_real`:
```bash
python platform/deploy/fabric_deploy.py \
  --real-only \
  --workspace "<workspace-name-or-guid>" \
  --lakehouse-bronze-real "LH_tokenomics_bronze_real" \
  --lakehouse-silver "LH_tokenomics_silver" \
  --lakehouse-gold "LH_tokenomics_gold"
```

