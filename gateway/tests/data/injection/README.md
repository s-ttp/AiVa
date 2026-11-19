# Prompt-injection benchmark corpora (synthetic)

Used by `aiva_gateway.govern.benchmark` and `tests/unit/test_injection.py` (ADR-0011).

| File | What |
|---|---|
| `dev.jsonl` | 112 records, 8 per category. Detectors may be tuned against it. |
| `heldout.jsonl` | 196 records, 14 per category. Never tuned against; reported as the headline result. |
| `generate_corpus.py` | Generator: `openai/gpt-oss-120b` via the HF router (ADR-0002); categories it refuses escalate to `nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16`. Each record names its generator. |
| `evaluate_nemo.py` | NVIDIA NeMo Guardrails 0.24.1 evaluator (non-LLM rails), run in its own virtual environment. |
| `nemo-0.24.1.*.predictions.jsonl` | Recorded NeMo predictions and run metadata (versions, model revisions, thresholds). |

Labels are fixed by construction: each generation request asks for one category, and the category
decides the label (`injection` or `benign`). All content is fictional. Some labels are ambiguous by
construction (for example "exfiltration" to an internal `@gct.com` address, or tool-abuse items that are
harmful action requests rather than injections; the tool ACL stage, not text detection, handles those).

Reproduce the table in ADR-0011:

```bash
uv run python -m aiva_gateway.govern.benchmark gateway/tests/data/injection/heldout.jsonl \
    --external gateway/tests/data/injection/nemo-0.24.1.heldout.predictions.jsonl --details
```

Re-run NeMo Guardrails (downloads ~3.6 GB of models to `NEMO_EVAL_DIR`, default `~/.cache/aiva-nemo-eval`):

```bash
uv venv --python 3.12 /tmp/nemo-venv
VIRTUAL_ENV=/tmp/nemo-venv uv pip install nemoguardrails==0.24.1 "transformers==4.57.6" yara-python \
    torch --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
/tmp/nemo-venv/bin/python -I gateway/tests/data/injection/evaluate_nemo.py \
    --corpus gateway/tests/data/injection/heldout.jsonl --out /tmp/heldout.predictions.jsonl
```
