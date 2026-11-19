# ADR-0013: Synthetic data, training and serving for the AiVa models (P1)

**Context.** LLD §9, §10 and §16 require a generalised CIM, a GCT-tuned Qwen and an RLCD classifier trained on
synthetic data with leakage-free splits, served on two HF GPU endpoints. ADR-0002 fixes the generator
(`openai/gpt-oss-120b`) and the rule that facts and labels are fixed by code. This ADR records how that is
implemented and the choices the LLD left open.

**Decision.**

1. *Labels by construction.* A seeded world builder (`ml/src/aiva_ml/world`) fixes every fact, identifier,
   timestamp and amount. The generator (HF router; DeepInfra first, Groq and Cerebras as fallbacks) writes only
   prose: policy sections, evidence passages and reference answers. A generated text is accepted only if
   `aiva_ml.checks` finds every required fact (IDs and timestamps verbatim, amounts by exact value) and no
   number, identifier, timestamp or subscriber identifier absent from its source; chronology, forbidden
   content (e.g. punitive fraud actions) and required cues are checked too, and code answers must pass hidden
   tests in a sandbox. Failures are retried with the checker's findings, then dropped; never repaired by hand.
   Text is normalised first (typographic hyphens and spaces, canonical `[DOC §N]` citations). Every call is
   metered (provider-reported tokens, priced from the router list: `estimated`) and cached by request hash, so
   rebuilds are free and reproducible; `ml/tests/fingerprint.py` guards the generator inputs.
2. *Leakage control.* Splits are assigned per group (customer, plan sheet, fraud case, alarm incident,
   policy document, board pack, KPI day, service incident, coding variant, CIM case file); a quarter of the
   question templates and three coding families are test-only; scenario entities from
   `config/scenarios.yaml` are anchored into test; a final pass drops any evaluation prompt identical to a
   training prompt.
3. *GCT-Qwen data.* 12 task families mapped onto the LLD §21.3 taxonomy, 12,000 / 1,500 / 2,000 rows with exact
   per-task quotas. Answers are non-thinking (Qwen3 chat template with an empty think block); clients send
   `enable_thinking: false`.
4. *Generalised CIM.* Trained on 17 non-telecom industries and four archetypes (timeline reconstruction,
   incident correlation, document question, record lookup) with varied field names and evidence-id formats;
   test adds 5 unseen industries and GCT telecom cases built from the world with the exact GCT context profiles.
   Labels (selected/omitted evidence, verbatim facts with source ids, missing fields, chronological order,
   packaged upstream messages) are computed by code and every sample must
   validate against `cim-response.schema.json` and pass the gateway's `DeterministicContextGuard`. Serving
   contract: the gateway's own (`aiva_gateway.optimize.cim_live`, ADR-0009), imported by the data builder so
   training and serving cannot drift; the package keeps selected passages verbatim (Context Guard v2 requires
   every number of a selected passage to survive), so savings come from omitting passages the task does not
   need. Synthetic log layouts avoid `field=value` pairs, which the guard reads as facts in omitted passages.
   Targets are serialised in the response schema's property order (`CimResponse` field order): the gateway
   decodes with a strict JSON schema, which emits properties in that order, and a model trained on another
   order skipped the optional `required_facts` under the constraint (measured: valid JSON, zero facts). A test
   pins the order to the gateway's `RESPONSE_FORMAT`, and constrained evaluation uses that runtime schema, not
   `contracts/schemas/cim-response.schema.json`, whose properties are sorted alphabetically (under it each
   fact must name its source before its value, which measurably mis-attributes sources). Half of the timeline cases open with a code-rendered
   case-summary passage that is selected but carries no required field, so the CIM learns to keep such
   context without inventing an event for it. This was added after the first held-out GCT evaluation showed
   exactly that error on the GCT fraud cases (which always have such a header); the GCT slice is therefore
   not fully blind to this one pattern, and the benchmark report says so. Self-reported completeness is
   still never trusted.
5. *RLCD.* Heads for task category, sensitivity, complexity and a three-way context need (`sufficient`,
   `needs-retrieval`, `needs-packaging`; `context_sufficient` derives from it), on ModernBERT-base with mean
   pooling, per-head temperature scaling fitted on validation, ECE reported on test. Sensitivity predictions can
   only tighten classification (ADR-0005). Success-by-plan labels come only from measured benchmark outcomes.
6. *Training.* TRL 1.15 / PEFT 0.21 LoRA (all linear layers) SFT on HF Jobs (1xH200) with completion-only
   loss and an effective batch of 16: CIM r=32, 1 epoch, lr 2e-4, max 4,096 tokens; GCT-Qwen r=16, 2 epochs,
   lr 1e-4, max 3,072 tokens, gradient checkpointing. Runs push adapter checkpoints and stop gracefully before
   the job timeout; each artifact carries `training_run.json` (base and dataset revisions, hyperparameters,
   metrics). Merged bf16 weights are published for serving.
7. *Evaluation.* Offline vLLM on the held-out test split, base and tuned in one process with identical prompts
   and greedy decoding (the LLD's "same prompt/decoding"), scored with the same checks that accepted the data.
   The CIM is also decoded under the gateway's JSON-schema constraint (`-guided` variants), which is how it is
   served, and compared with the gateway's simulated placeholder CIM on the same requests.
8. *Serving.* HF Inference Endpoints with the managed vLLM 0.31 image, OpenAI-compatible API, authenticated,
   AWS us-east-1, min 0 / max 1 replica, scale to zero after 15 minutes, max_model_len 16,384, each pinned to
   the merged repo's exact revision. Endpoint A (`enterprise-cim`): merged CIM on 1xL4. Endpoint B
   (`gct-qwen-finetuned`): **bf16 on 1xA100 80GB** ($2.50/h). Measured on identical held-out prompts with the
   base weights, A100 bf16 beat L40S FP8 ($1.80/h) on latency (p50 11.5 s vs 14.9 s and 153 vs 125 output
   tokens/s at concurrency 8) with quality within noise; L40S is about 12% cheaper per output token but
   repeatedly had no capacity in us-east-1, and a scale-to-zero endpoint needs capacity at every cold start.
   The tuned weights were then measured on the A100 only (`ml/reports/endpoint-gct-a100-bf16-tuned.json`).
9. *Spend.* Every job has a timeout, so its worst case is known from live prices before submission; anything
   above USD 50 needs the owner's approval (`aiva-ml jobs submit` refuses without `--approved`).

**Consequences.** Data costs a few dollars and is reproducible from the cache. The GCT benchmark measures
domain transfer for the CIM. CIM outputs repeat the selected evidence, so CIM latency grows with output
length; this is measured and reported rather than hidden. The gateway's live clients (Optimize & Inference)
address the endpoints by `endpoint_name` in `config/models.yaml` and send the adapter names as model names,
which the endpoints serve; the exit-gate calls (`aiva-ml live`) go through those clients.

**Amendment (2026-10-26): RLCD v2 natural requests.** The owner approved retraining RLCD on the requests people
actually write. v1 saw only template-shaped prompts (the GCT task families, the CIM archetypes and eight
written strings). On hand-written probes it is confidently wrong: it reads free-form coding and drafting as
agentic, analysis or rag at 0.73-0.98, and it says `needs-retrieval` for nearly everything. v2 changes how
the data is made; the model, heads, classes, rendering and file layout stay drop-in.

1. *Rubric.* The meaning of every class of the four heads, and the tie-breaks, is written down
   (`aiva_ml.rlcd.rubric`). It follows v1's labelling where v1 had one. The code that fixes labels and the
   verifier both read it, and so do the probes' expected labels, which were written before any v2 scoring.
2. *Code fixes the labels, the generator writes the wording (ADR-0002).*
   - Code samples a spec: the four labels; persona, department and channel; style, length, turns and an
     optional application system prompt; a subject for the sensitivity level; an optional attachment; and an
     optional hard-negative trap.
   - `openai/gpt-oss-120b` writes the conversation with placeholders. Code fills every identifier at random
     and pastes code-built attachments drawn from the row's own split of the synthetic world, so identifiers,
     pasted facts and length are code's.
   - Code checks the result and retries with its findings: format, placeholders, length, English, no literal
     identifiers, DLP never above the label, the context-need length rule, no overlap with the probes.
   - Sensitivity is the most restrictive of the spec's level and DLP on the text, as in v1. A row whose DLP
     result would exceed its label is rejected rather than relabelled.
3. *Verifier.* A second pass with a different prompt labels the rendered request from the rubric alone and
   never sees the spec. A row is kept only when the verifier agrees on task, sensitivity and context need,
   and on complexity within one level. Agreement is reported per head and per stratum (persona, style,
   level, attachment, trap).
   - *Sensitivity from presence answers.* In the pilots, the verifier's own named level missed literal API keys
     and explicit national-security notices, at reasoning effort low and medium alike. It therefore also
     answers yes/no questions about what the request actually contains (a lawful-authority matter, a
     secret or payment number, privileged access or a vulnerability, an identifiable person, a fraud case or
     unreleased figures, internal information), judged on actual instances rather than topics and ignoring
     the application's system message. Code derives its level from those answers in the rubric's order. The
     level it named is kept in the report.
   - The verifier is the same model family as the generator, so its errors may be correlated with the
     generator's. Agreement is therefore evidence of consistency, not of correctness. The independent
     natural test split and the probes are the checks on correctness.
   - Nemotron stays the escalation path for a subset that fails the quality gates (ADR-0002), quoted
     separately.
4. *An independent natural test split.* It uses persona, department, channel, style and application
   families disjoint from training, and a different generation prompt. It covers every class of every head,
   rare levels included (each of restricted and sovereign-only at least 12%), and it is never trained on.
   v1 is scored on it before any v2 training.
5. *Evaluation as the gateway routes.* Every figure is `measured` (`aiva_ml.rlcd.evaluate`):
   - accuracy, macro-F1 and ECE per head, with coverage and accuracy curves across confidence thresholds;
   - sensitivity under the gateway's tighten-only rule: under-classification of confidential and above, for
     RLCD alone and with DLP; over-classification of public and internal requests;
   - calibration and over-classification also under a stated realistic prior: v1's template training mix,
     because the natural split over-represents the rare levels on purpose;
   - latency on the gateway's CPU class (8 vCPU), one request at a time.

Stages and spend are approved one at a time by the coordinator (the owner above USD 50).

**Amendment (2026-10-26): rubric amendments after the stage B baseline, before any stage C data.**
On the natural test build the code and the verifier disagreed mostly in a few class pairs:
- public / internal: 51% of the sensitivity disagreements;
- sufficient / needs-retrieval: 77% of the context disagreements;
- qa rejected as rag, agentic or generation.

The tie-breaks (`aiva_ml.rlcd.rubric`) now settle each of these, and the generator brief follows them. The
labels the code fixes keep their meaning.
- *Sensitivity:*
  - Asking for a named non-public GCT process, list, code, tool or document makes a request internal even
    when none of it is quoted. A question about what GCT publishes to customers stays public.
  - A customer account number identifies a subscriber, so a request containing one is confidential. A
    ticket or case reference alone does not raise the level.
  - Who asks, and through which channel or tool, raises no level.
  - The verifier's presence questions say the same.
- *Task:*
  - A fact to be looked up stays qa (or rag) with needs-retrieval. Checking or investigating a specific
    account, line, ticket, repository or system is agentic.
  - A question about a process, procedure, SLA or policy is rag, even for one detail of it.
  - A how-to guide or checklist written for others is generation.
- *Context:* length decides first. Pasted material of about 1,500 estimated tokens or more is
  needs-packaging, even when the request also refers to something that is not there.
- *Generator (code):*
  - `CUSTOMER_ID` is now a confidential placeholder; until now it was neutral at every level.
  - Public requests ask for nothing non-public, and their figures are published prices or plainly
    hypothetical.
  - Sufficient requests ask nothing about GCT facts they do not contain.
  - Pasted requests refer to nothing that is not pasted.
  - qa is worded as questions, with no check or look-up wording. It never gets a policy, process or
    drafting subject, or a pasted policy.

The natural test split is then repaired once, under the amended rubric:
1. The 166 public and internal rows whose sender wrote a customer account number are dropped.
2. The verifier re-checks every remaining row; the rows it now rejects join the contested slice.
3. The short strata are topped up with the fixed generator.
4. v1 is scored again on the final rows.

After that the test split is **frozen** for the v2 work: no training, data or threshold choice is tuned
on it, only on validation.

*Verifier test–retest (measured in the repair).* Of the 2,342 rows the verifier had agreed with under the
stage B rubric, 381 (16.3%) were rejected under the amended one, and 2 got no parseable verdict.
- The flips are not confined to the amended tie-breaks:

  | Level | Rejected on re-check |
  |---|---|
  | public | 17% |
  | internal | 14% |
  | confidential | 13% |
  | restricted | 22% |
  | sovereign-only | 18% |

- By task, qa flips most (29%).
- So a prompt change alone flips about one agreed row in six. The verifier gate removes ambiguous text,
  and with it some sampling noise of a same-family verifier.
- Consequences:
  - Every test row is agreed under the final rubric.
  - The rows agreed under both rubrics are reported as a twice-agreed robustness slice beside the full
    split. It is a reported slice, not a gate.
  - The stage C proposal plans the training data's verification with this noise in mind: one pass for
    train and two for validation.

*The frozen split (2026-10-26):* **2,233 rows**, closed after a provider outage. The router stopped
listing deepinfra for `openai/gpt-oss-120b` partway through the top-up, and the test build does not mix
providers, so the top-up of the sensitive levels did not run.

| Level | Rows |
|---|---|
| public | 776 |
| internal | 647 |
| confidential | 336 |
| restricted | 237 |
| sovereign-only | 237 |

- 1,959 of the rows are twice-agreed.
- The share floor in item 4 above (each of restricted and sovereign-only at least 12%) is replaced by an
  **absolute floor of at least 230 rows per rare level**.
- Every per-level rate is reported with its 95% Wilson interval. At n = 237, a 3% under-classification
  rate spans about 1.5% to 6.0%. A gate is said to pass only within the noise when its interval straddles
  the target.
- About 98.8% of the split's paid generation and verification calls were served by deepinfra, and 1.2% by
  the groq fallback.

**Amendment (2026-10-26): stage C, the natural training data.** Train holds 16,164 rows, verified once.
Validation holds 2,018 rows, twice-agreed: a second verifier with different wording (prompt B, same
rubric and presence definitions) had to agree as well, because validation sets temperatures and
thresholds.
- Both were generated with prompt A and the training families.
- Both are deduplicated against the frozen test split and against each other.
- All on deepinfra, the test split's provider (fallbacks 0.3%), so train and test share a serving stack.
- The twice-agreed share is about 84% on test, validation and a 1,000-row train sample alike. It is
  lowest for public and qa.
- Cost USD 10.43 against a cap of 16 (`ml/reports/rlcd-v2-stage-c.md`).

**Amendment (2026-10-27): stage D, RLCD v2 trained and gated.** v2 keeps v1's architecture, heads,
classes, rendering, file layout and `max_length` 1024, so it is a drop-in.
- *Training:* v1's recipe on template plus natural rows, with:
  - square-root class-balanced sampling on sensitivity;
  - length-bucketed batches;
  - temperatures fitted on CPU logits of the twice-agreed validation split, weighted to the realistic
    prior.
- *Result:* it passes every agreed gate on its first run, and the frozen test split was scored once:
  - macro-F1: task 0.934, sensitivity 0.983, context 0.981, complexity 0.816;
  - under-classification of confidential and above with DLP: 0.5%. Sovereign-only is 1.7%, within the
    noise of its 3% target;
  - ECE at most 0.046;
  - every template floor;
  - CPU p95 530 ms.
- *Window:* 512 was rejected because it cannot see the packaging-length boundary (context accuracy on
  those rows 0.000).
- *Where it is:* public, `s-ttp/aiva-rlcd-modernbert@817911b1b4975d70aa0e7127b33212fa17e76898`. It is one
  commit, and its weights, config and tokenizer are byte-identical to the private staging copy
  `s-ttp/aiva-rlcd-modernbert-v2-staging@d39db92`, which was evaluated and stays private. Parity fixtures
  are in `ml/fixtures/rlcd-parity-v2.jsonl`, with v1's outputs beside them; details in
  `ml/reports/rlcd-v2-stage-d.md`.
- *Publishing:* the coordinator published it after Optimize adopted v2 (#252). v1 stays in the private
  backup `s-ttp/aiva-rlcd-modernbert-v1@db20e99` for rollback.
