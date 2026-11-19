# ADR-0018: The CIM returns selection and facts; the gateway builds the package

**Status.** Proposed (Wave 3 / P2). Not implemented; owners: Optimize & Inference (contract, `cim_live`, Context
Guard, orchestrator), ML & Data (dataset, training, evaluation).

**Context.** Under the current contract (ADR-0009, ADR-0013) the CIM returns `upstream_messages`: one system
message with the evidence preamble and every selected passage verbatim in `evidence_block`, followed by the
original conversation. The gateway could build that package itself from the selected ids, so the CIM spends
most of its output copying text it was given. Measured with the Qwen3-4B tokenizer on the gold replies of the
held-out CIM test set and on scenario 5 (`gateway/tests/fixtures/scenarios/network-operations.json`):

| Slice | Rows | Output tokens today, p50 / p95 | Without `upstream_messages`, p50 / p95 | Mean ratio |
|---|---|---|---|---|
| Seen industries | 500 | 684 / 1,175 | 287 / 538 | 0.44 |
| Unseen industries | 1,000 | 675 / 1,189 | 286 / 536 | 0.44 |
| GCT telecom profiles | 471 | 1,044 / 1,645 | 538 / 905 | 0.49 |
| Scenario 5 (60 `field: value` alarms, keep-all) | 1 | 6,260 | 1,343 | 0.21 |

The copy also creates failure modes the Context Guard must catch (citations lost, wrapper integrity, numbers
altered, values not carried into the payload), and it is what pushes scenario 5 over the gateway's CIM output
cap (5,424) and the 60 s CIM timeout. Endpoint A decodes at about 29 tokens/s per stream without speculation
and about 70-75 tokens/s with n-gram speculation (measured, `ml/reports/endpoint-cim-l4-*`); CIM latency is
decode-bound, so it falls roughly in proportion to output tokens (estimated: p50 about 24 s to about 10 s
without speculation; speculation helps less once there is little verbatim copying, so the gain over today's
9.6 s p50 is smaller, about 1.5-2x, to be measured).

**Decision (proposed).**

1. *Contract.* `CimResponse` v2 drops `upstream_messages`; the CIM returns `intent`, `required_facts`,
   `selected_evidence_ids`, `omitted_evidence_ids`, `missing_fields` and `self_reported_complete` (in that
   schema order, which constrained decoding follows, ADR-0013). `CIM_SYSTEM_PROMPT` stops describing the
   package.
2. *Package.* The gateway builds it deterministically: one system message `EVIDENCE_PREAMBLE` plus
   `evidence_block(id, text)` for each selected id in evidence order, then the original messages unchanged.
   This is exactly what the CIM is trained to copy today, so the answering model sees the same input.
3. *Guard.* The Context Guard runs unchanged on the built package. Citation, wrapper, payload-preservation
   and copy-related numeric checks become invariants that hold by construction; the checks that judge the
   CIM still do the work: required fields, verbatim facts, omitted-facts (context loss), chronology and the
   token budget (a selection whose package exceeds the budget fails as today and falls back to full context).
4. *Output cap.* The cap becomes a function of the evidence count and required fields rather than of the
   package budget, and is counted in real tokens (the current cap mixes `estimate_tokens` units with real
   `max_tokens`; record lists run 1.7-2.0 real tokens per estimated token).

**Training-target change.** Labels are unchanged; only the serialised target drops `upstream_messages`
(`aiva_ml.cim.prompt.response_json`). The dataset rebuilds from the generator cache with no paid calls and
retrains with the same recipe (1 epoch, about USD 5 on 1xH200; shorter targets make it faster). The next CIM
retrain also folds in the structured-record cases (`aiva_ml.cim.records`) and mixes task categories into
`CimRequest.task`, which the orchestrator sets to the task category rather than a sentence. About half of the
document cases also take the gateway's KB chunk ids, `{document_id}#{ordinal}` (`aiva_ml.cim.cases.add_chunk_ids`).
On the recorded scenario 4 request, the CIM reported the chunk id `GCT-POL-118#3` as `document_id`; no
training id had contained the document id before. A fifth of the samples are also sent as an application
without a profile sends them (`general`, no required fields, the task category): on such requests the first
CIM kept no evidence (scenario 6). About a third of the document cases also carry other operating units'
versions of the same policy (`aiva_ml.cim.cases.add_unit_versions`): when the question names no unit, the
answering section of every version is kept alongside the target's, never instead of it (scenario 4). With the
owner's go, about half of them also get a decoy section that uses the question's words without answering it
(`aiva_ml.cim.decoys`; generator prose, about USD 0.08 expected, hard cap USD 1.50); the eval job's timeout is
1.5 h so the retrain, eval, live check and decoys stay within USD 18.70 at worst.

**v2 prompt and schema (as trained for v6, 2026-10-18).** `aiva_ml.cim.prompt.CIM_SYSTEM_PROMPT_V2` is the
gateway's `CIM_SYSTEM_PROMPT` without the `upstream_messages` bullet; the budget sentence moves to the selection:
"- selected_evidence_ids: the passages needed to answer, including every passage that supplies a required fact.
The gateway sends exactly these passages to the answering model: keep them within target_budget_tokens."
`RESPONSE_SCHEMA_V2` is `CimResponse`'s schema without `upstream_messages` (same property order), with all six
properties required: in v1 the required `upstream_messages` was the package, in v2 `selected_evidence_ids` is,
so guided decoding must not be free to omit it. The gateway's v2 path must send that prompt and constrain with
that schema, byte for byte, or the v6 weights see a prompt they were not trained on. v6 trains into `s-ttp/aiva-cim-qwen3-4b-v6-lora` / `-v6`; the v1 repos and
Endpoint A's pin are untouched until the swap is approved. The offline eval (`cim-v6-eval`) runs v1 (v1 prompt
and schema) and v6 (v2) on the same rows in one job.

*v6 result and v7 (2026-10-18).* v6 failed the gate (`ml/reports/cim-v6-test-v1-vs-v6.md`):
- On scenario 4 it read "GCT enterprise" as the unit GCT Enterprise.
- On scenario 5 with retrieval on it reported alarm facts the policy passages do not hold.
- On the GCT alarm slice it omitted other sites' records (55 of 150).

v7 changes only data, with no new generator calls:
- Document samples sent under a profile whose fields no passage holds: every field reported missing, no
  facts, never an empty selection.
- Unit names that are also segment words, which questions use lowercase for the customer segment without
  naming a unit.
- Short record lists (6–14, as the GCT alarm profile sends) with a kept topology note.

The scenario slice adds two controls derived from the recorded scenario 4 request. Both are part of the gate:
- Naming GCT Enterprise keeps only GCT-POL-118.
- A segment-word question keeps GCT-POL-014 §3 alongside GCT-POL-118's.

Scenario 5 with retrieval on must report every field missing with no facts.

**Migration.**

1. Add `CimResponse` v2 next to v1 and a per-model `cim_contract: v1 | v2` in `config/models.yaml`; the gateway
   builds the package for v2 and keeps today's path for v1.
2. Build the v2 dataset and train; evaluate v1 and v2 offline on the same held-out rows under the gateway
   schema (guard pass, fact recall, output tokens), plus the scenario fixtures. The GCT slice uses the
   gateway's production shape (KB chunks as retrieved, configured context profiles). The recorded replay-set
   requests run as a scenario slice (`aiva_ml.cim.scenarios`) with per-step expectations aligned with the
   Scenarios recorder. The gate before any deploy to Endpoint A: the expectations are met, no step that passes
   on v1 fails on v2, and scenario 4 passes (GCT-POL-014 §3 kept and cited, whatever retrieval ranked above it).
3. Deploy the v2 weights to Endpoint A under a new pinned revision and set `cim_contract: v2`; measure live
   latency and guard pass with `aiva-ml bench` and `aiva-ml live`.
4. After a soak, remove the v1 path. Rollback is the flag plus the previous revision pin.

*Gateway v2 path (2026-10-18, migration step 1).* `cim_contract` on the CIM model selects the contract (default
`v1`; enterprise-cim stays on v1 until the v6 swap is approved). `cim_live.CIM_SYSTEM_PROMPT_V2` and
`RESPONSE_SCHEMA_V2` are the canonical v2 text, pinned by sha256 to what v6 was trained on (ML & Data import
them and hold the same hashes); the gateway's `build_package` is the one ML scores v6 with. A v2 reply is parsed as the six-field selection (a reply carrying a package is a schema
violation) and becomes the `CimResponse` the Context Guard, recall floor and recorder already take. The
packaging-time bypass applies to both: a v1 CIM must emit the kept passages, a v2 CIM every required fact the
evidence holds, at `FACT_ENTRY_TOKENS` (40; the gold v2 per-row maximum is 38.9, v7's measured maximum 33.2)
each, at `AIVA_CIM_V2_DECODE_TOKENS_PER_S` (40 tok/s, measured: v7 on Endpoint A's L4 image and args,
per-stream p5 41, p50 62, ML & Data 2026-10-18, `ml/reports/endpoint-cim-v2-enterprise-cim-v7-bench.md`; it was
29, the L4 without speculation, until measured); scenario 5 with retrieval
on (240 facts, about 260 s at 40 tok/s) is bypassed under v2 too. The v2 output cap is computed
before the call: 128 + Σ over ids of (the id's JSON bytes + 1) + 40 × evidence × max(1, required fields),
within the context the prompt leaves. On the gold v2 targets of the v6 dataset no row of 6,750 exceeds it (at
least 1.66 times headroom); v6's own measured output is checked against it in `cim-v6-eval`. Traces record
`aiva.cim.contract`.

*Trained-task bypass (2026-10-18, with the v7 swap).* `cim_trained_tasks` on the CIM model lists the taxonomy
`task_category` ids its weights were trained on (null: no restriction; the loader rejects unknown ids). A request
of any other category bypasses the CIM before the call, with the full context used and the reason
`task-outside-cim-training` ("CIM bypassed: task outside the CIM's trained categories; full context used"):
its reply would be out of distribution. Unlike the size bypasses, it applies with or without evidence. v6 and
v7 are trained on `qa`, `rag`, `agentic`, `summarization` and `analysis`, never on `coding` or `generation`
(ML & Data); the configured list is the one ML confirms for the pinned weights.

*Small v2 requests (2026-10-18).* A CIM call is refused for lack of context (`CIM_CONTEXT_EXCEEDED`) when the
reply cap the endpoint's context leaves is under 256 tokens, room for a useful v1 package. A v2 selection over a
few passages needs less than that (one passage and one required field: about 180 tokens), so under v2 the
minimum is the smaller of 256 and the selection bound (`min_cim_output_tokens`).

*Runaway guard (2026-10-18).* In ML & Data's v7 evaluation, 4 of 2,000 replies (0.2%) looped to the token
limit, inventing ids past the end of 43–52-row timeline lists. With `AIVA_CIM_V2_CONSTRAIN_IDS`, each v2 call's
guided-decoding schema (`constrained_response_format`) narrows `selected_evidence_ids` and `omitted_evidence_ids`
to an `enum` of the ids sent, with `maxItems` = the number of passages. It is a per-request copy that only adds
those two constraints. `RESPONSE_SCHEMA_V2` stays pinned by sha256 and is what the weights were trained on, and a
test checks that removing the two keys gives back the pinned schema. It is on by default. ML & Data's L4 bench
of v7 on Endpoint A's image and args (2026-10-18) found:
- vLLM honours `enum` on array items and `maxItems`.
- 30 of 30 replies that did not loop are byte-identical with and without it (median latency ratio 0.98).
- The two invented-id loops stop: 3,998 tokens (106 s) fall to 540 (9.6 s), and 5,718 (156 s) to 624 (10.8 s).
  Both then pass the Context Guard.

Two other replies loop by repeating one row's facts; narrowing the ids does not stop them. ML measured that a
`source_id` enum plus `required_facts.maxItems` = evidence × max(1, required fields) ends them, at 3,467 and
4,455 tokens. All four looping rows are bypassed before any call (nothing to omit, or too large to build in time).
The facts constraint is therefore a measured follow-up, not applied until its effect on rows that do not loop is
measured. Either way,
the Context Guard still fails a selection that names an unknown id.

*Evidence-id shape (2026-10-19, owner decision).* The owner approved section-aligned KB evidence ids:
`DOC#n` with `n` from the passage header `[DOC §n]`, `DOC#n.j` for each part of a split section. They replace
`{document_id}#{ordinal}`, whose number is one less than the section's. ML & Data measured v7 on requests
remapped to the new ids (`ml/reports/cim-v7-idshape.md`):
- its selection matched its replies to the unchanged requests on 78.8% of 165 rows, against a 98% floor;
- Guard pass on the GCT enterprise-rag slice fell from 150 to 146.

The id shape is part of the CIM's input distribution. The gateway therefore keeps the ordinal ids until a CIM
retrain trains, evaluates and gates on the new ids together (`ml/README.md`). The gateway's id change ships in
the same change as that Endpoint A swap.

**Consequences.** Roughly half the CIM output tokens (a fifth on large record lists) and correspondingly lower
CIM latency and GPU time; fewer guard failures caused by copying; scenario 5 no longer hits the output cap.
The CIM can no longer rewrite or trim the conversation, which it was never allowed to do. Evidence order in the
package is fixed by the gateway. Until this lands, ADR-0009's contract stands, and Optimize's deterministic
"nothing omittable" bypass covers inputs like scenario 5 where no passage can be omitted.
