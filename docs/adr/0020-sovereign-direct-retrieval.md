# ADR-0020: Knowledge-base retrieval on the sovereign-direct path

**Status.** Accepted by the owner, 2026-10-19. It changes an owner-approved path (LLD §6 step 7). It is built
default-off in a separate PR.

**Owners.**
- Optimize & Inference: retrieval, orchestrator, context profiles.
- Govern: classification ceiling, injection screening, policy.
- Scenarios: scenario expectations.

**Context.**

*What the LLD says today.* LLD §6 retrieves approved context only on the CIM-assisted path (step 9).
Confidential, restricted and sovereign-only traffic goes straight to GCT-Qwen with no retrieval (step 7). The
retrieval module assumes that traffic is below confidential.

*The consequence.* A confidential question gets no knowledge-base context unless the caller supplies it.
- S1 is declared `internal`. RLCD's sensitivity head raises it to `confidential` (0.9994), so it takes the
  sovereign path.
- Before launch, S1 answered a billing question without the passage that explains the bill. Scenarios now
  supplies that passage as the chat's sample evidence (`KB-TARIFF-020#0`).
- A real confidential question without supplied evidence still gets nothing from the KB.

*What is in place.*
- Retrieval runs in-process: BM25 in memory, pgvector in the gateway's Postgres, and the pinned ONNX embedder on
  CPU. No text leaves the gateway to be retrieved.
- On the sovereign path, retrieved passages would go only to the sovereign model, which is where confidential
  content already goes. That is consistent with Appendix B: never the CIM, never an external provider, no
  fallback.
- RLCD already predicts `context_need` (`sufficient`, `needs-retrieval`, `needs-packaging`). LLD §8 makes
  retrieval strategy part of RLCD's action.
- KB chunks are screened for prompt injection at ingest, and flagged, unscreened or stale-detector chunks are
  never retrieved (Govern, #127). Supplied evidence ids win over KB chunk ids (#126).

**Decision.**

1. **When it runs.** Retrieval runs on the sovereign-direct path only when all three hold:
   - the run's context profile opts in (`sovereign_retrieval_k`, default 0 = off);
   - the caller supplied no evidence;
   - RLCD does not confidently predict `context_need: sufficient`. When the heuristic serves, the profile
     decides.

   *Why "no supplied evidence":* with no CIM to prune on this path, retrieved passages would stack on top of the
   caller's evidence unfiltered. Retrieval fills the gap only where the caller gave no context.

   Local-fast is unchanged.
2. **Relevance floor.** A passage is added only if its cosine similarity to the query is at least the profile's
   floor.
   - The similarity uses the pinned bge-base embedding that retrieval already computes. The floor defaults to
     0.62 (`sovereign_retrieval_min_cosine`).
   - The top `k` fused passages that clear it are added in rank order; `k` is not filled with passages under the
     floor.
   - When none clears it, no passage is added. The retrieval span records `aiva.retrieval.retrieved = 0` and
     `aiva.retrieval.below_floor`, the number of candidates held back.
   - Calibration ($0, 2026-10-18, deployed KB, pinned embedder):
     - on-topic: 549 gold chunks of ML & Data's 150 v6 enterprise-rag test questions; cosine median 0.722,
       p5 0.618;
     - off-topic: 300 test questions from other profiles whose answers are not in the KB; best-chunk cosine
       median 0.551, p95 0.626.

     | Floor | Gold kept | Off-topic questions with any chunk added |
     |---|---|---|
     | 0.60 | 96.9% | 13.7% |
     | **0.62** | **94.5%** | **7.3%** |
     | 0.64 | 88.2% | 3.3% |
     | 0.66 | 78.5% | 2.0% |
3. **Access control.** The same as on the CIM-assisted path: tenant, application grant, validity dates and
   DLP-clean chunks. The classification ceiling stays at **public and internal** (Govern, answer 1 below).
   Supplied ids win over KB chunk ids (#126).
4. **Injection-clean chunks only.** As on the CIM-assisted path (#127):
   - KB chunks are scanned with the injection detector at ingest, and the detector version is stored;
   - chunks that are flagged, unscanned, or scanned by an older detector version are never retrieved;
   - the retrieval span records how many chunks were held back (`aiva.retrieval.injection_held_back`).

   Retrieved passages therefore carry no injection indicators by construction. **Rule:** any other source added
   to a sovereign run, such as live tool output or documents not ingested into the KB, must go through the same
   `evidence_dlp` scan as caller evidence, with its indicators in the policy input.
5. **No CIM and no package.** The passages join the full context. Retrieval runs before the reservation, so the
   envelope covers the retrieved text.
   - The added passages must fit the profile's `target_budget_tokens`, which stays well inside Endpoint B's
     16,384-token window.
   - Preflight still refuses a payload over the window.
   - The egress policy check runs before the upstream call, as for any payload.
   - The answer-citation check resolves citations of retrieved passages like any other evidence.
6. **The baseline and external models never see them.**
   - A confidential run's allowed models contain no external model, so the opt-in Claude baseline is neither
     reserved nor called. It reports `denied-by-policy` (`_reserve` includes the baseline only when the frontier
     model is allowed, and `_baseline` evaluates egress first).
   - That holds today and is unchanged. Two tests cover it:
     - `test_baseline_denied_for_confidential`: zero baseline cost, and the run's span records
       `aiva.baseline.status = denied-by-policy`;
     - `test_confidential_request_stays_sovereign_and_never_reroutes`: no external model is called.
   - New invariant tests add retrieval to that case: a confidential, restricted or sovereign-only run with
     retrieved passages never calls the CIM, the baseline or any external model, and the sovereign path never
     falls back.
7. **Trace and UI.**
   - The retrieval span runs on the sovereign path with `aiva.retrieval.path = sovereign-direct`. Span
     attributes stay `aiva.*` and pass `redact_attributes`; no passage text is recorded.
   - The CIM stays "CIM bypassed by mandatory sovereignty policy" (LLD §22.8).
   - The execution graph shows retrieval → (CIM bypassed) → inference.

**S1, measured ($0, local).**
- Retrieval for S1's frozen prompt against the deployed KB (93 documents; 394 chunks granted to
  `gct-support-chatbot`):
  - The top fused passages are Prepaid Plus plan price lines (`KB-TARIFF-007` §1–§11), cosine 0.741–0.759.
  - All clear the 0.62 floor. They are on topic, but none explains the 15 AED increase.
  - The deployed KB has no passage about the add-on fee; only S1's supplied evidence (`KB-TARIFF-020#0`) has it.
- With the "no supplied evidence" rule, S1's default request carries its sample evidence, so it does not
  retrieve. **S1's Endpoint B body is unchanged, and no re-record is needed.** S3 (fraud) and S10 (document
  copilot) are not opted in and are unchanged too.
- Without that rule, S1 would add three price-line passages. That would change its B body and add unpruned,
  non-answering context to its answer.
- The first profile to opt in is a new `customer-support` profile (`sovereign_retrieval_k: 3`,
  `sovereign_retrieval_min_cosine: 0.62`). It is used when a confidential support question arrives without
  evidence, for example S1's prompt edited in the Test Console.

**Alternatives considered.**
- **Keep today's design** (caller-supplied evidence only). Nothing changes, and the gap stays for real use.
- **Retrieve and package with the CIM.** Not allowed: the CIM must never see confidential content (LLD §3,
  Appendix B).
- **Retrieve on every opted-in sovereign run, even with supplied evidence.** This adds unpruned passages on top
  of the caller's, as S1 shows, and changes recorded bodies.
- **Add `KB-TARIFF-020` to the KB so S1 can drop its sample evidence.** Rejected:
  - it changes the KB version, and with it every response-cache key and S4's BM25 statistics, which means an S4
    re-record;
  - S1's supplied evidence already wins over a KB chunk with the same id.

**Consequences.**
- **Request bodies:** none of the default scenario set's bodies change. The coordinator's replay gate must show
  zero body changes before the build merges.
- **Cost:** retrieval adds about 0.1–0.3 s of CPU time on runs that retrieve. Up to three short passages add a
  little prefill on the sovereign model; GPU time is estimated as today.
- **Response cache:** the KB version is already part of every cache key.
- **Invariants:** Appendix B holds. New invariant tests are added (Decision 6).

**Answers (Govern, 2026-10-18).**
1. **Confidential KB documents stay out.** The ceiling stays at public and internal.
   - The KB knows only application grants, not per-user or per-department ACLs.
   - So any user of an application whose run goes sovereign would receive confidential documents. That
     includes a public visitor who declares a request confidential: visitors hold `chat:live`, and a
     declared-confidential run takes the sovereign path.
   - Confidential retrieval would need per-principal document ACLs plus an OPA rule deciding retrieval
     eligibility. That is a later design.
2. **Excluding flagged chunks is enough,** given two preconditions, both met by #127:
   - a version-current scan, where unscanned or stale chunks are excluded;
   - a held-back count on the retrieval span.

   Plus the rule in Decision 4 for any other source on the sovereign path. Govern measured the deployed KB:
   0 of 514 chunks flagged.
3. **Keep S1's sample evidence.** Supplied evidence wins over KB chunks, and adding the passage to the KB is the
   rejected alternative above.

**Implementation (2026-10-19).**
- **Off by default.** `ContextProfile.sovereign_retrieval_k` defaults to 0 and `sovereign_retrieval_min_cosine`
  to 0.62. The `customer-support` profile (k 3, floor 0.62) is scenario 1's, and it is the only one that opts in.
- **The stage.** The `retrieval` stage runs on the sovereign path only when the conditions in Decision 1 hold.
  Otherwise it is skipped, with these reasons:
  - not opted in: "CIM bypassed by mandatory sovereignty policy", as before;
  - evidence supplied: "Knowledge base not searched: the request supplied its evidence";
  - RLCD confidently predicts sufficient: "Knowledge base not searched: RLCD predicts the context is sufficient".
- **The floor.** The retriever takes `min_cosine` and compares it with each fused candidate's cosine to the
  query embedding (1 − pgvector cosine distance). Candidates under the floor are dropped, and the limit is never
  filled with them.
- **The token budget.** Passages that would take the context past the profile's `target_budget_tokens` are not
  added.
- **The span.** It records `aiva.retrieval.path = sovereign-direct`, `aiva.retrieval.retrieved`,
  `aiva.retrieval.below_floor` and `aiva.retrieval.over_budget`. It holds counts only, never passage text.
- **The tests.**
  - `tests/integration/test_sovereign_retrieval.py`.
  - `tests/invariants/test_sovereign_retrieval.py`: confidential, restricted and sovereign-only runs with a
    retrieved passage call only the sovereign model, never the CIM, the baseline or an external model, and an
    outage of the sovereign endpoint still never falls back.
