# AI insight layer - Fabric Copilot, natural language, and RAG

Goal: let a stakeholder ask, in plain language, questions like  - 
- Which business unit spent the most last month?
- Which licenses should be reclaimed?
- Which applications increased spend?
- What are the highest-cost models?
- Where can we reduce AI spend?

 -  and get grounded, numeric answers. Three layers, each usable on its own.

---

## Layer 1 - Semantic model as the grounding contract
Fabric Copilot and Q&A are only as good as the model's metadata. The v2 model is
already shaped for this:

- **Descriptions** (`///` in TMDL) on every non-obvious measure become Copilot
  context. Keep writing them - they are prompt grounding, not just docs.
- **Conformed dims** give NL a vocabulary: *business unit, application,
  environment, principal type, model, platform*.
- **`discourageImplicitMeasures`** is on, so Copilot uses curated measures
  (`Total AI Cost`, `Chargeback Cost`, …) instead of ad-hoc column sums - answers
  stay consistent with the dashboards.
- **Synonyms** in `AIFinOps.SemanticModel/synonyms.linguistic.json` map business
  language ("wasted spend", "showback", "unused seats") to measures.

| Decision | Rationale | Tradeoff | Value | Effort |
|---|---|---|---|---|
| Curated measures over implicit | consistent, explainable NL answers | must author each measure | trust | done |
| Standalone synonym file (not wired in) | cannot break PBIP open | one manual apply step | safe iteration | S |
| Descriptions as grounding | free Copilot context | authoring discipline | better answers | ongoing |

**Apply the synonyms:** Tabular Editor → model culture `en-US` →
`linguisticMetadata` → paste the `Entities` block; or Desktop → Modeling → Q&A
setup. Kept out of `definition/` deliberately so a malformed edit can never stop
the model opening.

## Layer 2 - Fabric Copilot / Q&A (self-serve NL over the model)
Point Copilot in the Fabric workspace at the published semantic model. It answers
aggregate/slice questions directly ("spend by business unit last month",
"idle licensed users by BU") because those map to measures + dim columns.

- **Strategy.** Publish the semantic model to a Fabric workspace on a Copilot-
  enabled capacity; enable Copilot; ship the synonyms; add 6–10 verified Q&A
  example questions as "featured questions" to steer phrasing.
- **Tradeoff.** Copilot answers *within the model* - it cannot reason over
  external policy docs or explain *why* a cost moved. That is Layer 3.
- **Value.** Zero-BI-skill access for CFO/governance personas.
- **Effort.** M (capacity + publish + synonyms + featured questions).

## Layer 3 - RAG insight layer (the "why" and the recommendations)
For narrative/analytical questions ("*why* did Insurance's spend jump?", "draft a
reclaim plan"), combine model data with unstructured context.

```
User question
   │
   ▼
Orchestrator (Azure AI Foundry agent, via the APIM AI Gateway - same gateway that
   │           already gives per-user Foundry cost attribution & governance)
   ├─► Tool A: DAX/semantic query  → numbers from the Power BI model (grounded truth)
   ├─► Tool B: vector search        → FinOps policy, rate cards, prior optimization
   │                                   memos, MS pricing docs (Fabric/AI Search index)
   └─► Synthesis: LLM answers ONLY from retrieved numbers + docs, with citations
```

- **Rationale.** Numbers must come from the model (never hallucinated); commentary
  comes from retrieved documents. Separating the two keeps answers auditable.
- **Grounding truth.** Quantitative claims are executed as measure queries, not
  generated - the LLM composes prose around returned figures.
- **Governance.** Route the agent's own LLM calls through the APIM AI Gateway so
  the insight layer's cost is itself captured in this same FinOps model (recursive
  dogfooding, and per-user cost control).
- **Tradeoffs.** More moving parts (agent + vector index + eval harness); needs a
  golden-question eval set to prevent regressions.
- **Value.** Turns a dashboard into an analyst: explanations + recommended actions.
- **Effort.** L.

### Suggested build order
1. Ship synonyms + featured questions → **Fabric Copilot Q&A** (fast win). Effort S–M.
2. Index rate cards + FinOps policy in **Azure AI Search / Fabric**. Effort M.
3. Foundry agent with a **semantic-query tool + retrieval tool**, gated by the APIM
   gateway; add a golden-question eval set. Effort L.

## Example question → layer mapping
| Question | Layer | How it is answered |
|---|---|---|
| Which BU spent the most last month? | 2 | `Total AI Cost` by `dim_business_unit`, month filter |
| Which licenses should be reclaimed? | 2 | `Idle Licensed Users` / `Idle Seat Waste` by BU/identity |
| Which applications increased spend? | 2 | `MoM Cost Delta %` by `dim_application` |
| Highest-cost models? | 2 | `Total AI Cost` / `Cost per 1K Tokens` by `dim_model` |
| *Why* did spend grow and what do we cut? | 3 | semantic query + retrieved memos → cited narrative |
