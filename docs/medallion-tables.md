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

Every Bronze table also carries ingestion metadata columns:
`_source`, `_ingested_at`, `_watermark`, `_report_refresh_date`, `_raw_payload` (JSON where applicable).
🔒 = governed/PII sub-zone (restricted access + retention policy).

### Azure AI Foundry / Azure OpenAI
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_aoai_metrics` | Azure Monitor metrics | deployment × minute | metric_name, timestamp, deployment, model, value, aggregation |
| `bronze_aoai_requestresponse` 🔒 | Diagnostic log `RequestResponse` | per API call | TimeGenerated, OperationName, DurationMs, ResultSignature, CallerIPAddress, modelDeploymentName, modelName, apiVersion, streamType, caller_object_id |
| `bronze_aoai_audit` | Diagnostic log `Audit` | per event | TimeGenerated, OperationName, identity, result |
| `bronze_aoai_traces` 🔒 | App Insights GenAI spans | per span | operation, gen_ai.request.model, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens, latency, (prompt/response if logged) |
| `bronze_foundry_gateway` | APIM AI Gateway → Log Analytics | per request × identity | time_generated, oid, client_id, app_id, upn_or_app_name, business_unit_claim, cost_center_claim, model_name, model_version, prompt_tokens, completion_tokens, cached_prompt_tokens, requests, total_latency_ms, status_code, is_error — **the only per-user Foundry token attribution path** |
| `bronze_azure_cost_usage` | Cost Management `UsageDetails` | meter × resource × day | meterId, meterName, meterCategory, quantity, effectivePrice, costInBillingCurrency, resourceId, resourceGroup, tags, billingPeriod |
| `bronze_azure_cost_focus` | Cost Management FOCUS export (1.2-preview; tolerates 1.0) | charge × resource × day | BilledCost, EffectiveCost, ListCost, ContractedCost, ConsumedQuantity, ServiceCategory, ServiceName, ResourceName, SkuMeter (`x_SkuMeterName` in 1.0), InvoiceId, PricingCurrency, tags — preferred over `UsageDetails`, read only when present |
| `bronze_azure_price_sheet` | Cost Mgmt price sheet / retail prices | meter | meterId, unitPrice, unitOfMeasure, tierMinimumUnits, currency |

### Microsoft 365 Copilot
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_m365_copilot_usage` | `getMicrosoft365CopilotUsageUserDetail` | user × refresh day | UPN, Display Name, Last Activity Date, + per-app last-activity (Teams/Word/Excel/PowerPoint/Outlook/OneNote/Loop/Chat) |
| `bronze_m365_cowork_usage` | M365 admin center → Copilot → Cost Management / Cowork usage report (UI/CSV, no API) | user × day | user_id, user_principal_name, spending_policy_name, total_tasks, scheduled_tasks, user_initiated_tasks, **credits_consumed** (Copilot Credits @ \$0.01), last_activity_date — the Cowork consumptive add-on |
| `bronze_m365_copilot_interactions` 🔒 | `getAllEnterpriseInteractions` (aiInteraction) | per prompt/response | id, appClass, conversationType, interactionType, from, createdDateTime, sessionId, requestId, body, attachments, contexts, mentions, locale |
| `bronze_m365_subscribed_skus` | Graph `/subscribedSkus` | sku | skuId, skuPartNumber, prepaidUnits(enabled/suspended), consumedUnits |
| `bronze_m365_user_licenses` | Graph `/users?$select=assignedLicenses` | user × license | userId, UPN, skuId, assignedDateTime |
| `bronze_m365_purview_audit` 🔒 | Purview Unified Audit Log | per event | CreationTime, UserId, Operation, AppHost, AccessedResources, ClientIP |

### GitHub Copilot
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_ghc_seats` | `/orgs/{org}/copilot/billing/seats` | assigned user | assignee.login, assignee.id, created_at, last_activity_at, last_activity_editor, pending_cancellation_date, plan_type |
| `bronze_ghc_billing` | `/orgs/{org}/copilot/billing` | org × cycle | total, active_this_cycle, inactive_this_cycle, pending_invitation, pending_cancellation, seat_management_setting, plan_type |
| `bronze_ghc_usage_metrics` | `/copilot/metrics` + report exports | day (× lang/editor/model) | date, total_active_users, total_engaged_users, code suggestions/acceptances, lines suggested/accepted, chats, model usage per mode |
| `bronze_ghc_billing_usage` | Enhanced billing usage API | line item × day | date, product, sku, quantity, unitType, netAmount, grossAmount, discountAmount, repositoryName |
| `bronze_ghc_user_teams` | user-teams report | user × team × day | date, login, team (for team-level joins) |

### Copilot Studio
| Bronze table | Source | Grain | Notable raw columns |
|---|---|---|---|
| `bronze_studio_transcripts` 🔒 | Dataverse `conversationtranscript` | conversation | conversationtranscriptid, botid, conversationid, content(JSON activities), createdon, schematype |
| `bronze_studio_analytics` | Monitor / Dataverse analytics | agent × day | sessions, engagementRate, resolutionRate, escalationRate, abandonRate, csat, topicsTriggered, outcome |
| `bronze_studio_capacity` | Power Platform Admin | env/agent × day | messagesConsumed, generativeAnswers, aiBuilderCredits, environmentId |
| `bronze_studio_cost_usage` | Cost Management (PAYG meter) | meter × day | meterName, quantity, cost, resourceId |
| `bronze_studio_appinsights` | App Insights (optional) | event | conversationId, activityId, botId, latency, dependency calls |

### Reference / master data (customer inputs — mostly MOCK today)
| Bronze table | Source | Notable columns |
|---|---|---|
| `bronze_entra_users` | Graph `/users`, `/servicePrincipals` | id, UPN, displayName, type, department, manager |
| `bronze_org_hierarchy` 📝 | customer CSV | business_unit, division, cost_center, executive_owner, monthly_budget |
| `bronze_app_ownership` 📝 | customer CSV | app, owner_upn, owner_bu, service_principal_id, repo, agent_id, environment |
| `bronze_rate_card` 📝 | customer CSV / price sheet | platform, unit_type, model, unit_price_usd, effective_from, discount_pct |

---

## SILVER — conformed entities (the hard, high-value work)

Silver cleans, dedupes, resolves identity, normalizes taxonomy, and reconciles cost.
These are the intermediate conformed tables that feed Gold.

> **Status: implemented.** `platform/medallion/silver/10_conform_usage.py` writes
> these 15 tables, and `platform/validate/check_notebooks.py` fails the build if
> the list shrinks or if Gold reaches past Silver into Bronze. For a long time
> this table was a *specification* while the runnable notebook collapsed
> everything into a single `usage_conformed` table; that gap is now closed.

| Silver table | Built from (Bronze) | What it does |
|---|---|---|
| `silver_identity_resolved` | ghc_seats, m365 users, entra_users, aoai caller ids, studio botid | **Identity graph**: unify github_login ↔ Entra user ↔ UPN ↔ service principal ↔ agent into one `identity_key`; assign `principal_type`, `identity_class`, `is_human`, home BU |
| `silver_application_map` | app_ownership, tags, repo→app, agent→app | resolve every workload to an `application_key` + owner BU + default environment |
| `silver_model_map` | aoai deployments, ghc model usage, studio | normalize deployment/model names → canonical `model_key` (family, version, provider, modality) |
| `silver_org_hierarchy` | org_hierarchy | clean BU / division / cost center / budget / owner |
| `silver_rate_card` | rate_card, azure_price_sheet | one price per (platform, unit_type, model, date) incl. discount |
| `silver_usage_foundry` | foundry_gateway (preferred) **or** aoai_metrics (fallback) | per-call/per-day tokens+requests+latency; **prefers `bronze_foundry_gateway`** (per-identity, from the APIM gateway) and falls back to resource-grain `bronze_azure_ai_metrics` (identity = `unknown`) — never reads both, so tokens can't double-count |
| `silver_usage_m365` | m365_copilot_usage, user_licenses, subscribed_skus | per user/day: licensed?, active?, per-app activity; seat cost = seats × price |
| `silver_usage_m365_cowork` | m365_cowork_usage | per user/day: Microsoft 365 Copilot **Cowork** add-on as `unit_type = copilot_credit` (billed in Copilot Credits @ \$0.01, cost modelled); a consumptive add-on on top of the seat, so it lands in Variable, not Fixed, cost |
| `silver_usage_ghc` | ghc_seats, ghc_usage_metrics, ghc_billing_usage | per user/day: licensed?, last_activity, acceptance, model; seat + premium-req cost |
| `silver_usage_studio` | studio_analytics, studio_capacity, studio_cost_usage | per agent/day: sessions, resolution, messages consumed, billed $ |
| `silver_usage_unified` | the 5 `silver_usage_*` (m365, m365_cowork, ghc, studio, foundry) | **union to one grain**: date × platform × identity × application × model × cost; nulls where a source's grain doesn't reach; adds `cost_is_estimated` (modelled vs billed), `list_cost_usd` and `has_rate_card` (list/rate-card price per row, falling back to `cost_usd` when unpriced) |
| `silver_usage_azure` | azure_cost | REAL invoiced Azure meters, split `AzureAI` vs `AzureInfra` so AI spend is not overstated by the infrastructure it runs on |
| `silver_cost_reconciliation` | silver_usage_unified vs Cost Mgmt totals | modelled vs billed variance → drives `Cost Confidence %` |
| `silver_grain_audit` | every usage feed | cumulative-vs-delta verdict per feed; flags a `delta` feed behaving like a cumulative one before it double-counts |
| `silver_data_source_catalog` | ref_extractable_catalog | the extractable-signal catalog, passed through silver so Gold never reads Bronze |

**Silver grain rule:** `silver_usage_unified` is the coarsest common denominator —
**daily**. Per-call detail stays in the Foundry table for engineering drill-down;
everything rolls up to daily for the cross-platform fact.

---

## GOLD — the star the Power BI model binds to (exact columns)

This is **already built** in the repo (`data/*.csv` + TMDL). Gold = `silver_usage_unified`
projected into `fact_ai_usage` + conformed dimensions. Direct Lake (prod) or import (demo).

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
| `unit_type` | text | token / copilot_credit / premium_request / seat_day / prompt |
| `quantity` | decimal | native units consumed |
| `input_tokens` | int | 0 where N/A |
| `output_tokens` | int | 0 where N/A |
| `cached_tokens` | int | cache-hit tokens |
| `requests` | int | request/call count |
| `cost_usd` | decimal | modelled or billed cost |
| `list_cost_usd` | decimal | list / rate-card cost = quantity × `dim_rate_card` price (Azure rows take FOCUS `ListCost`); falls back to `cost_usd` when no rate exists, so never null |
| `cost_is_estimated` | bool | true=modelled, false=billed (Cost Mgmt) |
| `has_rate_card` | bool | true when a genuine list/rate-card price backed this row; false means `list_cost_usd` is the `cost_usd` fallback |
| `is_error` | bool | error flag |
| `latency_ms` | int | request latency |

### Dimensions (exact current columns)
| Dim | Columns |
|---|---|
| `dim_date` | date_key, year, quarter, month, month_name, day, day_name, is_weekday, year_month |
| `dim_platform` | platform_key, platform_name, billing_model, native_unit, **addon_unit, addon_billing_model**, has_token_telemetry, has_native_cost, is_variable_cost, data_source, enterprise_discount_pct |
| `dim_identity` | identity_key, display_name, principal_type, upn, github_login, team, business_unit, cost_center_key, **identity_class, is_human, home_business_unit_key** |
| `dim_model` | model_key, model_name, model_version, provider, modality |
| `dim_cost_center` | cost_center_key, cost_center_name, business_unit, owner_upn |
| `dim_business_unit` | business_unit_key, business_unit_name, division, monthly_budget_usd, executive_owner, is_mock_budget |
| `dim_application` | application_key, application_name, application_type, owner_business_unit_key, owner_upn, default_environment_key, criticality, is_mock |
| `dim_environment` | environment_key, environment_name, is_production, sla_tier |
| `dim_rate_card` | rate_key, platform, unit_type, model, unit_price_usd, effective_from, currency, source, note |
| `dim_data_source` | platform, signal_category, signal, source_api, grain, identity_granularity, cost_fidelity, retention, availability, notes *(disconnected catalog)* |

### The 57 measures live on `fact_ai_usage`
Cost (Total/Billed/Modelled/**Rate Card**/Discounted/Forecast/Chargeback/Budget variance),
usage (tokens, requests, cache hit, **Cowork Credits**), utilization (Licensed Seats,
Active Users, **Idle Licensed Users, Idle Seat Waste, Seat Utilisation %, Low-Use Licensed
Seats, Seat Action**), attribution (**Unattributed / Attributed Requests, Workload
Attribution %**), add-on (**Cowork Add-on Cost**), unit economics (Cost per 1K Tokens, Cost
per Active User), quality (Error Rate, Avg Latency), trend (Cost PM, MoM Delta, 30d
run-rate), and catalog counts (Extractable Signals). These are the numbers the 8 report
pages display.

---

## One-screen mental model

```
 SOURCES (APIs/reports)          BRONZE (raw, 1:1)         SILVER (conformed)            GOLD (star → Power BI)
 ─────────────────────           ─────────────────         ──────────────────            ──────────────────────
 AOAI metrics/logs/cost   ─┐     bronze_aoai_*        ─┐    silver_usage_foundry  ─┐
 M365 usage/interactions  ─┤ ──▶ bronze_m365_*        ─┼──▶ silver_usage_m365,    ─┤
 GitHub seats/metrics/$   ─┤     bronze_ghc_*         ─┤    silver_usage_m365_cowork┼─▶ silver_usage_unified ─▶ fact_ai_usage
 Copilot Studio dataverse ─┘     bronze_studio_*      ─┘    silver_usage_ghc/studio─┘        + dim_* (10)     ─▶ 8 report pages
 Entra / org / rate card  ─────▶ bronze_*(reference)  ────▶ silver_identity_resolved,
                                                            silver_application_map,
                                                            silver_model_map,
                                                            silver_rate_card
```

**Bottom line:** Bronze = ~26 raw feed tables (source-faithful, incremental, PII zone for
content). Silver = ~16 conformed tables (identity + app + model + cost normalization, then
one unified daily usage table). Gold = **1 fact + 10 dims** — the exact star already in this
repo, which the 8 Power BI pages and 57 measures consume.
