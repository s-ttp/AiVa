# ADR-0011: Presidio DLP and prompt-injection detection

**Context.** LLD §3 names Presidio for AI-PEL DLP and Appendix A lists NeMo Guardrails as a candidate for
prompt/response controls. P0 shipped a regex-only scanner (`pattern-dlp-v1`) with five injection
patterns (`injection-patterns-v1`). The Govern brief: Presidio behind the existing `DlpScanner`
interface with an English NLP model, the telecom recognizers kept as custom Presidio recognizers, and an
injection detector benchmarked against the P0 baseline, adopting NeMo Guardrails only if it wins.

**Decision — DLP.**
- `govern/presidio_dlp.PresidioDlpScanner` runs Presidio `AnalyzerEngine` (presidio-analyzer 2.2.364)
  with spaCy `en_core_web_md` 3.8.0 (setting `AIVA_DLP_NLP_MODEL`). The telecom, financial and secret
  recognizers (`dlp.RECOGNIZERS`: MSISDN, IMSI, ICCID, IMEI, payment card, IBAN, national IDs, email,
  secrets, private keys) are registered as custom Presidio `EntityRecognizer`s with their validators.
  NER adds `PERSON`, classified **confidential** (personal data takes the sovereign path).
- `en_core_web_md` over `sm`/`lg`: on the ten scenario prompts and a name set, `sm` tagged "GCT Prepaid
  Plus" and "Traveller" (scenarios 1 and 8) as people, which would force those demos onto the sovereign
  path; `md` and `lg` had identical results; `md` is 33 MB against 400 MB (measured locally, 2026-10-14).
- False-positive controls: overlapping findings resolve by recognizer priority (patterns first, so an
  IMSI is never also an IMEI); a `PERSON` span is dropped when it is not shaped like a name (digits, line
  breaks, code punctuation, e.g. `ALM-7790\nsite_id`) or consists only of catalog/product vocabulary
  (model, application, tool and tenant names: "Claude", "Gemini", "Qwen"). A single capitalised word that
  opens a line, sentence or field value (where capitalisation carries no signal) is kept only if spaCy
  still tags it as a person once lower-cased: "Microwave hop MW-12 down" is dropped, "Mariam approved" and
  "Note: Sarah will call back" are kept (`ner-filter-v2`, added when Optimize's NOC alarm and knowledge
  base fixtures exposed the false positive).
- The spaCy dependency parser is disabled (unused by Presidio's NER and context enhancement): identical
  findings, about half the latency. Long text is scanned in 20k-character overlapping windows. Text above
  `AIVA_DLP_MAX_SCAN_CHARS` (default 250,000, more than the sovereign model's 32k-token window) is refused
  with `413 DLP_SCAN_LIMIT_EXCEEDED` before any model call: unscanned text never passes.
- Measured locally (4 vCPU container): ~4–7 ms per short prompt, ~1.5 s for 70k characters, ~1.5 s
  model load at startup. `PatternDlpScanner` stays as the dependency-free baseline; unit tests run every
  recognizer case against both scanners.

**Decision — prompt injection.** Adopt `injection-heuristics-v2` (`govern/injection.py`): indicator
patterns evaluated over normalised views of the text (NFKC, zero-width removal, Cyrillic/Greek
look-alikes, dashes and quotes, spaced-out letters, leetspeak, reversed text, decoded base64). NeMo
Guardrails is **not** adopted. Prompt indicators feed `injection_detected` (GCT-INJ-003 denies); evidence
indicators feed `evidence_injection_detected`, which only restricts (GCT-TOOL-006 blocks tool side
effects). Evidence is wrapped so its text cannot open or close an evidence block and its ID cannot break
out of the attribute.

**Benchmark** (`aiva_gateway.govern.benchmark`, data in `gateway/tests/data/injection/`; provenance:
measured on synthetic data). Corpora were written by `openai/gpt-oss-120b` via the HF router (ADR-0002),
with refused categories escalated to `nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16`; labels are fixed by
category (9 injection, 5 benign categories, including hard negatives such as "Android developer mode" and
"list every roaming bundle"). About 45k generator tokens in total (estimated cost well under $1).
v2 was tuned on the dev split only and frozen before the held-out split was first scored. NeMo Guardrails
0.24.1 rails that run locally without an LLM were evaluated with library code and default thresholds:
jailbreak heuristics (GPT-2-large perplexity), NemoGuard JailbreakDetect (random forest on
snowflake-arctic-embed-m-long) and YARA injection rules. LLM-based rails (`self_check_input` and others)
were excluded by design: they would send unclassified prompt text to a model before the policy decision
and budget reservation, and confidential text to a non-sovereign model.

Held-out split (196 records, 126 injection / 70 benign):

| Detector | Precision | Recall | F1 | FPR | Mean latency |
|---|---|---|---|---|---|
| injection-patterns-v1 (P0 baseline) | 0.750 | 0.024 | 0.046 | 0.014 | 0.02 ms |
| **injection-heuristics-v2 (adopted)** | **0.983** | **0.460** | **0.627** | 0.014 | 0.17 ms |
| NeMo jailbreak heuristics | 0.000 | 0.000 | 0.000 | 0.000 | 477 ms |
| NeMo NemoGuard JailbreakDetect | 1.000 | 0.016 | 0.031 | 0.000 | 56 ms |
| NeMo YARA injection rules | 0.667 | 0.016 | 0.031 | 0.014 | 2.1 ms |

Dev split (112 records, tuned on): v1 F1 0.105, v2 F1 0.971 (precision 1.0, recall 0.944); every NeMo
rail had recall ≤ 0.056. Without the tool-abuse category (harmful action requests, which the tool ACL
stage handles rather than text detection), held-out v2 recall is 0.509 (F1 0.671). After the held-out
run one parity fix restored the `print`/`display` verbs v1 already had; held-out figures were unchanged.

**Consequences.**
- v2 recall on held-out data (0.46) is far below its dev recall (0.94): hand-written patterns generalise
  poorly to new paraphrases (system-prompt extraction 3/14, persona role-play 4/14, Unicode-styled
  obfuscation 2/14). Policy denial on a false positive blocks a legitimate request; held-out FPR is 1.4%
  (1 of 70: "forget the last temporary routing rule").
- Next step (ML & Data, P2): a trained injection head (e.g. alongside the RLCD ModernBERT classifier) on
  generator data with labels by construction, scored on a fresh held-out split with this harness. Any
  detector change must re-run the benchmark and update this ADR; `tests/unit/test_injection.py` pins the
  held-out confusion matrix.
- The gateway image gains spaCy and the 33 MB English model; the Space loads it once at startup.
- Labels are by construction and some are ambiguous (e.g. "exfiltration" to an internal `@gct.com`
  address); per-category rates are printed by `--details` for review.

**Amendment (2026-10-14): tenant vocabulary.** ML & Data's synthetic knowledge base (514 documents) had
53 sections in 11 documents withheld by `kb-ingest` because NER tagged tenant names as people: "Hajj"
(16), "Prepaid Smart" (13), "GCT Qatar" (12), "GCT Oman" (12), "GCT Kuwait" (4), "GCT Bahrain" (3) and
"GCT Wholesale" (1). Failing closed was safe but degraded retrieval. `config/policies/dlp/
tenant_vocabulary.yaml` now lists the tenant's organisations, plan families and roaming bundles (from
the synthetic GCT world). A PERSON span is dropped only when every token belongs to that vocabulary or to
the catalog vocabulary; mixed spans such as "Hajj Omar" and names outside it ("Mariam approved") are
still confidential. With it, the same knowledge base has 0 withheld sections (measured locally on the
PR #7 data file). The vocabulary version is part of the scanner version recorded with every decision
(`…+tenant-vocabulary-v1+…`). Adding a name that can also be a personal name widens what is let through,
so changes to this file are reviewed like policy changes.

**Amendment (2026-10-14): tenant UI labels (`ner-filter-v3`, `tenant-vocabulary-v2`).** Scenarios found
"Bill" in "choose Bill history" tagged as a person, which moved a harmless self-care question onto the
sovereign path. Probing the app's labels showed NER tags "Bill" in "Bill history", "Bill summary", "Bill
details", "Bill payment", "Bill Pay", "Bill shock" and "Bill and Payments". It did not tag "Plan",
"Balance" or "Top Up" labels, which are listed anyway. The vocabulary gains `ui_labels`: multi-word
phrases matched in the text around a PERSON span. The span is dropped only when it lies inside a label
occurrence. Labels match case-sensitively, exactly as written, and each casing the app uses is listed.
In a label with a first name, a word that can also be read as a verb is capitalised ("Bill Pay", "Bill
Details", "Bill Shock"). Without that, "Can Bill pay the invoice?", "Did Bill shock the team?" and "In his
note, Bill details the fix" would match and be dropped; review found this in the first draft.
Text in another casing stays confidential, which is the safe side. Label words never join the token
vocabulary, so "Ask Bill about the invoice", "Can you pay Bill for the repair?" and "Bill Ahmed called
about his SIM" stay confidential, and "Bill history for Bill Ahmed" still reports "Bill Ahmed". A one-word
label is rejected at load, because it would let that first name through everywhere. Labels that read as an
action on a person ("Pay Bill") are never added. The knowledge base still has 0 withheld sections. Known
limitation, unchanged from `ner-filter-v2`: a lone first name that is also a common noun and opens a
sentence ("Bill called about his SIM") is not reported, because the lower-cased re-check reads it as a
noun. A full name or any mid-sentence mention is reported.

**Amendment (2026-10-18): network vocabulary (`ner-filter-v4`).** In scenario 6's re-record (run 3) the
analyse answer, stored as `INC-7781/analysis` evidence, cited the alarm it diagnosed:
`- ALM-7790 (major) - "Microwave hop MW-12 down"`. NER tagged "Microwave" as a person. That raised the text
to confidential above the run's internal, so the same phase's `ticket.create` was denied before OPA. The
lower-cased re-check (`ner-filter-v2`) never ran, because a word after an opening quote does not open a line,
sentence or field.

- **Rejected: treating an opening quote or bracket as a unit start.** This was proposed first. It clears the
  alarm, but it also stops reporting `Approved by 'Layla' yesterday.` and `Ticket owner (Ahmed) confirmed.`.
  A narrower form only counted a quoted phrase that goes on past the word, which keeps those two. Even so,
  on 64 synthetic sentences (16 Gulf first names × 4 phrasings) opening a quoted phrase, it missed 21
  names against 10 today. The lower-cased re-check is weak on first names, so extending it widens what
  passes.
- **Decision.** The scanner gains `NETWORK_TERMS` ("microwave", "baseband", "latency", "jitter"). A PERSON
  span made only of these words is dropped, like product vocabulary. A mixed span ("Microwave Omar") and a
  name next to the word stay reported.
  - Each term was observed misread, capitalised mid-sentence or opening a quoted title ("The Microwave hop
    MW-12 is down", "Check the Jitter alarm").
  - None is a personal name. Words that are also names are never added: "Tower" was misread too, but it is
    a surname, so it is left out.
  - Across 48 capitalised phrases from the synthetic alarm, incident, runbook and scenario fixtures (each
    in three quoted shapes), only "Microwave" was misread.
- **Effect.** S6's analysis evidence has no findings, so `ticket.create` reaches OPA again. Text that was
  confidential only because of these words is now classified by its other findings. A name NER misses in
  that text is no longer covered by accident: "Ask Layla about the Microwave alarm" was confidential only
  through "Microwave".
- **Known limitation, measured.** This corrects the `ner-filter-v3` note, which said any mid-sentence
  mention is reported. `en_core_web_md` misses many Gulf first names on its own, and the lower-cased
  re-check adds misses at line starts. On the same 64 sentences on `ner-filter-v3`, it missed:
  - 13 mid-sentence ("Yesterday Khalid approved it");
  - 8 after a sentence;
  - 22 at the start of the text;
  - 32 after a line break.

  Raising name recall, for example with a synthetic first-name list, would move more text onto the
  sovereign path and change recordings, so it is a separate decision. This amendment does not make it.

**Amendment (2026-10-18): knowledge-base chunks are screened for prompt injection.** LLD §7 says to scan
retrieved text. Caller evidence was scanned with injection detection, but knowledge-base chunks were scanned
at ingest with detection off and not at all at retrieval, so a poisoned chunk could have reached the CIM and
the answering model with no indicator recorded.

- Ingest now scans each chunk with detection on. It stores the indicator names in
  `kb_chunk.injection_indicators` (names only, never text) and the detector version that produced them in
  `kb_chunk.injection_detector`. It counts flagged chunks in `IngestReport`.
- Retrieval treats an injection-flagged chunk like a DLP-flagged one: it is stored but never eligible.
- A chunk is screened only for the running detector version (`injection-heuristics-v2` today). A chunk
  with no result (NULL, stored before this change) or with a result from another detector version is
  never eligible either. It stays out until its document is re-ingested; the gateway Space re-ingests its
  boot knowledge base on every start. The check uses the injection detector's version only, not the full
  DLP scanner string, so an NER or vocabulary change never empties retrieval.
- A retrieval that holds chunks back for screening records the count on its span,
  `aiva.retrieval.injection_held_back`: a count only, set only when it is above zero.
- The scanner version is part of each document's content hash. Re-ingesting with a different scanner
  therefore changes the knowledge-base version, which rebuilds the retrieval index and changes the
  response-cache keys.
- Retrieved text stays untrusted evidence, never instructions.

Measured before the change with `…+ner-filter-v4+tenant-vocabulary-v2+injection-heuristics-v2`, each file
rendered and chunked exactly as ingest does:
- the deployed knowledge base (`data/synthetic/kb/gct_kb.jsonl`: 93 documents, 514 chunks) has 0 flagged
  chunks;
- the test seed (7 documents, 9 chunks) has 0 flagged chunks.

So no retrieval result and no recorded request body changes. For ADR-0020: exclusion, the version-current
scan and the held-back count together are the precondition for retrieval on the sovereign path.
