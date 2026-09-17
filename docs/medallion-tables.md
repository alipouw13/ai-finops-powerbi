# Medallion Table Inventory — what lives in Bronze, Silver, and Gold

Companion to `docs/extractable-fields.md` (field sources) and `docs/ARCHITECTURE.md`.
This is the **concrete table list** for each layer, plus **column-level Gold schemas**
grounded in what the semantic model already binds to (`AIFinOps.SemanticModel/data/*.csv`).

Naming: `bronze_<source>_<feed>` · `silver_<entity>` · Gold = the star table names the
report queries (`fact_ai_usage`, `dim_*`).

Flow: **Bronze** = one raw table per API/report feed (source-faithful) →
**Silver** = conformed/cleaned/identity-resolved/cost-reconciled →
**Gold** = the exact star the Power BI model consumes.

---

## BRONZE — raw landing (one table per source feed, source fidelity preserved)

Every Bronze table also carries lineage columns:
`_ingested_at`, `_source_system`, `_source_api`, `_watermark`, `_batch_id`, `_data_class`.
🔒 = governed/PII sub-zone (restricted access + retention policy).
**✅ = built and running** (`platform/fabric/gen_bronze_data.py` → `bronze_out/*.csv`,
landed by `platform/medallion/bronze/*.py`). Everything else is a design target.

### Cost — one contract for every Azure-billed service
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_focus_cost` ✅ | Cost Management Export — **FOCUS 1.0r2** (96 columns) | provider charge line (typically meter × resource × day) | BilledCost, EffectiveCost, ListCost, ConsumedQuantity, ConsumedUnit, PricingQuantity, PricingUnit, ResourceId, SubAccountId, Tags, x_ResourceGroupName, x_SkuMeterId, x_SkuMeterName, x_SkuDetails |
| `bronze_azure_price_sheet` | Cost Mgmt price sheet / retail prices | meter | meterId, unitPrice, unitOfMeasure, tierMinimumUnits, currency |

Azure OpenAI, the Copilot Studio PAYG credit meter, Fabric capacity and (when in scope)
Azure ML all arrive on this one export, separated by `ServiceName`. Do not build a
cost collector per service.

### Azure AI Foundry / Azure OpenAI — attribution
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_apim_gateway_requests` ✅ | APIM AI gateway → Log Analytics | per model request | ClientId, Oid, Appid, BackendId, BusinessUnitClaim, CostCenterClaim, PromptTokens, CompletionTokens, CachedPromptTokens, ModelName, ModelVersion, IsError, StatusCode, TotalLatencyMs, TimeGenerated *(all as strings)* |
| `bronze_apim_client_ownership` ✅ | Log Analytics `ApimClientOwnership_CL` | client | ClientId, AppName, BusinessUnit, CostCenter, Team, TenantId |
| `bronze_aoai_requestresponse` 🔒 | Diagnostic log `RequestResponse` | per API call | TimeGenerated, OperationName, DurationMs, ResultSignature, CallerIPAddress, modelDeploymentName, modelName, apiVersion, streamType, caller_object_id |
| `bronze_aoai_traces` 🔒 | App Insights GenAI spans | per span | operation, gen_ai.request.model, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens, latency |

Azure Monitor token metrics are deliberately **not** collected: they carry no identity
dimension, so they duplicate the gateway's token counts without its attribution.

### Microsoft 365 Copilot
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_m365_copilot_usage` ✅ | `getMicrosoft365CopilotUsageUserDetail` | user × refresh day | `Report Refresh Date`, `User Principal Name`, `Display Name`, `Last Activity Date`, + per-app last-activity (Teams/Word/Excel/PowerPoint/Outlook/OneNote/Loop/Chat) |
| `bronze_m365_copilot_seats` ✅ | Graph `/subscribedSkus` + `/users?$select=assignedLicenses` | user × SKU × day | snapshotDate, userPrincipalName, skuId, skuPartNumber, servicePlanName, capabilityStatus, assignedDateTime, prepaidUnitsEnabled, consumedUnits |
| `bronze_m365_copilot_credits` ✅ | M365 / Power Platform Copilot Credits billing | consumer × day | usageDate, consumerId, consumerType, capability, creditsConsumed, unitPriceUsd, costUsd |
| `bronze_m365_copilot_interactions` 🔒 | `getAllEnterpriseInteractions` (aiInteraction) | per prompt/response | id, appClass, conversationType, interactionType, from, createdDateTime, sessionId, body, contexts, locale |
| `bronze_m365_purview_audit` 🔒 | Purview Unified Audit Log | per event | CreationTime, UserId, Operation, AppHost, AccessedResources, ClientIP |

### GitHub Copilot
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_ghc_seats` ✅ | `/orgs/{org}/copilot/billing/seats` | assigned user × snapshot | assignee_login, assignee_id, assignee_type, assigning_team, created_at, last_activity_at, last_activity_editor, pending_cancellation_date, plan_type |
| `bronze_ghc_premium_usage` ✅ | Enhanced billing usage API | line item × day | date, product, sku, model, modelMultiplier, quantity, unitType, grossAmount, discountAmount, netAmount, repositoryName, username |
| `bronze_ghc_usage_metrics` | `/copilot/metrics` | day (× lang/editor/model) | total_active_users, total_engaged_users, suggestions/acceptances, chats (needs ≥5 active users) |
| `bronze_ghc_user_teams` | user-teams report | user × team × day | date, login, team |

### Copilot Studio
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_dataverse_msdyn_aievent` ✅ | Dataverse `msdyn_aievents` (OData, per environment) | billed event | msdyn_aieventid, msdyn_eventtimestamp, msdyn_eventtype, msdyn_billingtype, **msdyn_creditconsumed**, msdyn_ismeteredevent, _msdyn_botid_value (+ OData FormattedValue), _msdyn_environmentid_value, msdyn_channel, msdyn_outcome |
| `bronze_studio_transcripts` 🔒 | Dataverse `conversationtranscript` | conversation | conversationtranscriptid, botid, conversationid, content(JSON activities), createdon |
| `bronze_studio_analytics` | Monitor / Dataverse analytics | agent × day | sessions, engagementRate, resolutionRate, escalationRate, csat, outcome |

Studio **dollars** come from the PAYG meter on `bronze_focus_cost`; this feed supplies
the credits that say which agent burned them.

### Reference / master data (customer inputs — MOCK today)
| Bronze table | Source | Notable columns |
|---|---|---|
| `bronze_ref_identity_map` ✅ | Graph `/users`, `/servicePrincipals` + manual map | identity_key, upn, github_login, principal_type, is_human, home_business_unit_key, cost_center_key |
| `bronze_ref_app_inventory` ✅ 📝 | CMDB / resource tags | application_key, application_type, owner_business_unit_key, default_environment_key, gateway_app_name, gateway_client_id, azure_resource_name |
| `bronze_ref_business_hierarchy` ✅ 📝 | customer CSV | business_unit_key, business_unit_name, division, monthly_budget_usd, executive_owner |
| `bronze_ref_agent_inventory` ✅ 📝 | Copilot Studio env inventory | agent_key, agent_name, bot_id, environment_id, owner_business_unit_key, cost_center_key |
| `bronze_ref_rate_card` ✅ 📝 | customer CSV / price sheet | platform, unit_type, model, unit_price_usd, effective_from, source |

---

## SILVER — conformed entities (the hard, high-value work)

Silver types, cleans, resolves identity, normalizes taxonomy, and **allocates billed
cost onto identities**. ✅ = built and running in both engines
(`platform/medallion/silver/10_conform_usage.py` and `platform/data-store/build_store.py`).

**The central rule: the provider's bill is the only source of dollars.** FOCUS carries
cost but no identity; the gateway and Dataverse carry identity but no cost. Silver
splits the billed amount by each identity's share of the billed unit, so Gold
reconciles to the invoice instead of re-deriving spend from a rate card.

| Silver table | Built from (Bronze) | What it does |
|---|---|---|
| `silver_cost_charge` ✅ | focus_cost | Type the 96 FOCUS columns, decode `Tags` and `x_SkuDetails` JSON → application/BU/env keys, model, token direction; classify `ServiceName` → platform_key; expose billed vs list (the realised discount) |
| `silver_gateway_request` ✅ | apim_gateway_requests, apim_client_ownership, ref_identity_map, ref_app_inventory | Cast Log Analytics strings (`""`/`"None"` → 0), resolve `ClientId` → identity + class, `BackendId` → application, and BU via identity → client registry → app owner; unresolvable callers become `unknown`/`BU-UNALLOC` rather than vanishing |
| `silver_studio_event` ✅ | dataverse_msdyn_aievent, ref_agent_inventory | Type credits and resolve `_msdyn_botid_value` → agent + owning BU |
| `silver_foundry_allocation` ✅ | silver_cost_charge, silver_gateway_request | **Split billed AOAI cost across identities by token share, per direction** (input/output/cached are priced differently) |
| `silver_studio_allocation` ✅ | silver_cost_charge, silver_studio_event | Split the billed PAYG credit meter across agents by credit share |
| `silver_usage_conformed` ✅ | the allocations + m365/github feeds | **Union to one daily grain**: date × platform × identity × application × model × unit_type, with `cost_is_estimated` separating invoiced from modelled and `cost_method` recording how each dollar was derived |
| `silver_identity_resolved` | ghc_seats, m365 users, entra_users, studio botid | Fuller identity graph (currently resolved inline from `ref_identity_map`) |
| `silver_cost_reconciliation` | silver_usage_conformed vs FOCUS totals | Modelled vs billed variance → drives `Cost Confidence %` *(the build asserts the allocation ties to the invoice today)* |

**cost_method values:** `focus_token_allocation`, `focus_credit_allocation`,
`provider_billed` (the feed carries its own dollars: GitHub `netAmount`, M365 credits),
`rate_card` (nothing is billed — seats), `usage_signal` (activity proof, zero cost).

**Silver grain rule:** `silver_usage_conformed` is the coarsest common denominator —
**daily**. Per-request detail stays in `silver_gateway_request` for engineering
drill-down; everything rolls up to daily for the cross-platform fact.

---

## GOLD — the star the Power BI model binds to (exact columns)

This is **already built** in the repo (`data/*.csv` + TMDL) and materialized by
`platform/medallion/gold/20_build_star.py` (Spark) and `build_store.py` (SQLite).
Gold = `silver_usage_conformed` projected into `fact_ai_usage` + conformed dimensions.
Direct Lake (prod) or import (demo). Both builds **assert** the column contract and
that every fact key resolves to a dimension, so drift fails loudly.

Fabric capacity is excluded from `fact_ai_usage` on purpose: it is real FOCUS content
but platform self-cost, not AI consumption, so it is reported from
`silver_cost_charge`.

### `fact_ai_usage` (grain: date × platform × identity × model × application × environment × BU × cost center)
| Column | Type | Notes |
|---|---|---|
| `usage_date` | date | → dim_date.date_key |
| `platform_key` | text FK | → dim_platform |
| `identity_key` | text FK | → dim_identity |
| `model_key` | text FK | → dim_model |
| `cost_center_key` | text FK | → dim_cost_center |
| `application_key` | text FK | → dim_application |
| `environment_key` | text FK | → dim_environment |
| `business_unit_key` | text FK | → dim_business_unit |
| `unit_type` | text | token / seat_day / copilot_credit / premium_request / active_day |
| `quantity` | decimal | native units consumed |
| `input_tokens` | int | 0 where N/A |
| `output_tokens` | int | 0 where N/A |
| `cached_tokens` | int | cache-hit tokens |
| `requests` | int | request/call count |
| `cost_usd` | decimal | allocated-billed or modelled cost |
| `cost_is_estimated` | bool | true=modelled (rate card), false=billed (provider invoice) |
| `is_error` | bool | error flag |
| `latency_ms` | int | request latency |

### Dimensions (exact current columns)
| Dim | Columns |
|---|---|
| `dim_date` | date_key, year, quarter, month, month_name, day, day_name, is_weekday, year_month |
| `dim_platform` | platform_key, platform_name, billing_model, native_unit, has_token_telemetry, has_native_cost, is_variable_cost, data_source, enterprise_discount_pct |
| `dim_identity` | identity_key, display_name, principal_type, upn, github_login, team, business_unit, cost_center_key, **identity_class, is_human, home_business_unit_key** |
| `dim_model` | model_key, model_name, model_version, provider, modality |
| `dim_cost_center` | cost_center_key, cost_center_name, business_unit, owner_upn |
| `dim_business_unit` | business_unit_key, business_unit_name, division, monthly_budget_usd, executive_owner, is_mock_budget |
| `dim_application` | application_key, application_name, application_type, owner_business_unit_key, owner_upn, default_environment_key, criticality, is_mock |
| `dim_environment` | environment_key, environment_name, is_production, sla_tier |
| `dim_rate_card` | rate_key, platform, unit_type, model, unit_price_usd, effective_from, currency, source, note |
| `dim_data_source` | platform, signal_category, signal, source_api, grain, identity_granularity, cost_fidelity, retention, availability, notes *(disconnected catalog)* |

### The 42 measures live on `fact_ai_usage`
Cost (Total/Billed/Modelled/Discounted/Forecast/Chargeback/Budget variance), usage
(tokens, requests, cache hit), utilization (Licensed Seats, Active Users, **Idle Licensed
Users, Idle Seat Waste**), unit economics (Cost per 1K Tokens, Cost per Active User),
quality (Error Rate, Avg Latency), trend (Cost PM, MoM Delta, 30d run-rate), and catalog
counts (Extractable Signals). These are the numbers the 10 report pages display.

---

## One-screen mental model

```
 SOURCES (APIs/reports)          BRONZE (raw, 1:1)              SILVER (conformed)          GOLD (star → Power BI)
 ─────────────────────           ─────────────────              ──────────────────          ──────────────────────
 Cost Mgmt FOCUS 1.0r2 ────┐     bronze_focus_cost         ─┐   silver_cost_charge ──┐
   (the only $ authority)  │                                │                        ├─▶ foundry_allocation  ─┐
 APIM gateway → LA        ─┤ ──▶ bronze_apim_*             ─┼──▶ silver_gateway_request            │           │
 Dataverse msdyn_aievent  ─┤     bronze_dataverse_*        ─┤   silver_studio_event ─┴─▶ studio_allocation ────┼─▶ silver_usage_conformed ─▶ fact_ai_usage
 M365 Graph + credits     ─┤     bronze_m365_*             ─┤                                                  │        + dim_* (9)        ─▶ 10 report pages
 GitHub seats + billing   ─┘     bronze_ghc_*              ─┘                                                  │
 Entra / org / rate card  ─────▶ bronze_ref_* (5)          ─────▶ (identity, application, agent, BU resolution)┘
```

**Bottom line:** Bronze = one table per source **endpoint**, source-faithful and
append-only — with a single cost contract (FOCUS) covering every Azure-billed service.
Silver = 6 conformed tables whose central job is **allocating billed dollars onto
identities** by token/credit share. Gold = **1 fact + 9 dims** — the exact star already
in this repo, which the 10 Power BI pages and 42 measures consume.
