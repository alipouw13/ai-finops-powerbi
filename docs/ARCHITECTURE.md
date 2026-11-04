# AI FinOps platform - architecture & decision record

Scope: evolve the PoC into a reusable enterprise **AI Cost Management** accelerator
on Microsoft Fabric + Power BI, giving one view of AI usage, licensing, governance,
chargeback and optimization across Azure AI Foundry, Azure OpenAI, APIM AI Gateway,
M365 Copilot, GitHub Copilot, Copilot Studio, and future platforms.

Every decision below carries **rationale / tradeoffs / business value / effort**
(effort: S ≈ hours, M ≈ days, L ≈ weeks).

---

## 1. Medallion on Fabric (bronze/silver/gold)
See `platform/medallion/`. Bronze = raw append-only history; silver = conformed
grain (USD + `unit_type`); gold = the semantic star.

- **Rationale.** AI telemetry is fragmented and lossy (Azure Monitor 93-day
  retention; Cost-Management MTD *replaces*; GitHub metrics 1-yr). A retained
  bronze layer is the only way to answer year-over-year cost questions.
- **Tradeoffs.** Three physical copies of data vs one. Justified: cheap Delta
  storage, and silver/gold are always rebuildable from bronze.
- **Value.** Auditability + historical trend + a clean contract to the model.
- **Effort.** M for Foundry (connector exists); M each for the mock platforms.

## 2. Cost is the only conformed measure; `unit_type` is a dimension
- **Rationale.** Only Foundry exposes tokens. A literal cross-platform
  "tokenomics" view is impossible; USD with a `unit_type` (`token · copilot_credit
  · premium_request · seat_day · prompt`) is the only reconciliation.
- **Tradeoffs.** Loses a single physical activity unit; gains a coherent total.
- **Value.** A defensible headline number Finance can reconcile to invoices.
- **Effort.** Already implemented (`fact_ai_usage`).

## 3. Conformed dimensions added in v2
`dim_business_unit`, `dim_application`, `dim_environment`, plus universal identity
on `dim_identity`. Each new dim has a **single** relationship to the fact (clean
star, no ambiguous paths); BU is reached through the fact key, not a second hop
through identity.

| Dim | Rationale | Value | Effort |
|---|---|---|---|
| dim_business_unit | identity and cost_center carried *divergent* BU labels - conform once | chargeback, budget variance, BU allocation | S (done) |
| dim_application | "spend by application" is the App-Owner persona's core question | optimization targeting, showback | S (done) |
| dim_environment | dev/test/prod split exposes non-prod waste | reclaimable spend | S (done) |
| universal identity | not every request is a person (SP/MI/agent) | correct attribution across platforms | S (done) |

## 4. Universal identity model
`identity_class ∈ {Human, ServicePrincipal, ManagedIdentity, Agent, Application}`
with `is_human`. Foundry attribution *requires* the APIM gateway because Azure
Monitor token metrics have no identity dimension at all.

- **Rationale.** Agents and service principals now drive material AI spend with no
  human in the loop; per-user assumptions silently misattribute cost.
- **Tradeoffs.** Some platforms (M365) only emit human UPNs; the vocabulary is
  wider than today's data exercises - intentional, so future rows conform without
  a schema change.
- **Value.** Honest attribution; enables "spend with no human owner" governance.
- **Effort.** S (done); ManagedIdentity/Agent rows arrive when those sources land.

## 5. Cost model: actual / discounted / forecast / chargeback
Measures in `fact_ai_usage.tmdl`. Discount is a per-platform attribute
(`dim_platform.enterprise_discount_pct`, MOCK per contract). Chargeback grosses up
unallocated spend pro-rata. Budget lives on `dim_business_unit` (MOCK).

- **Rationale.** Finance needs list→net→forecast→showback, not just a total.
- **Tradeoffs.** Forecast is straight-line MTD projection - simple and explainable;
  swap for a time-series model (Fabric AutoML) when history is deep enough.
- **Value.** Budget variance and chargeback are the CFO's two headline asks.
- **Effort.** M (done); AutoML forecast is a later M.

## 6. Rate card and provenance as first-class data
Every price lives only in `dim_rate_card` (disconnected input). Real-vs-mock is
`dim_platform.data_source` + `fact.cost_is_estimated`, surfaced as `Cost
Confidence %` on page 1.

- **Rationale.** Nobody pays list; a FinOps programme loses credibility the first
  time modelled dollars are mistaken for billed ones.
- **Value.** One-CSV customer onboarding; honesty in the demo.
- **Effort.** S (done).

## 7. Persona reporting
Five pages (CFO, Governance, Engineering, App Owner, License Optimization) over the
same model - see `build_personas.py`. Report pages are additive/idempotent so the
model and pages 1–4 are never at risk.

## Target-state diagram
```
 Foundry/AOAI ─┐
 APIM Gateway ─┤   Fabric: Bronze ─► Silver ─► Gold ─► Power BI semantic model ─► Persona reports
 M365 Copilot ─┤   (Delta, OneLake)          (star)         (import today /            + Fabric Copilot Q&A
 GitHub Copilot┤                                             DirectLake later)          + RAG insight layer
 Copilot Studio┘
```

## Roadmap (not yet built)
| Item | Value | Effort |
|---|---|---|
| Live connectors for the 3 mock platforms | REAL coverage | M each |
| DirectLake gold + scheduled bronze ingest | live cost, no refresh | M |
| AutoML forecast replacing straight-line | tighter budget calls | M |
| Anomaly detection (cost spikes) + alerts | proactive FinOps | M |
| Fabric Copilot Q&A + RAG insight layer | NL self-serve | see `ai-insight-layer.md` |
