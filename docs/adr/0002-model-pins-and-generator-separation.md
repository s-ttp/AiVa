# ADR-0002: Model pins and synthetic-data generator separation

**Context.** LLD §1 names GCT-Qwen, Gemini Flash-Lite, DeepSeek-V3.2 and a pinned Claude flagship as
routing targets. On 2026-10-14 no Hugging Face inference provider served DeepSeek-V3.2 (all mappings in
error state) while the MIT-licensed DeepSeek V4 family was live. Training data must also be generated
without breaching provider terms: Google's Gemini API terms and Anthropic's commercial terms restrict
using their outputs to develop competing models.

**Decision.**
- Routing targets: `gct-qwen-finetuned` (Qwen3-32B + LoRA), `gemini-3.5-flash-lite`, `claude-opus-5-5`,
  `deepseek-ai/DeepSeek-V4.1-Flash` (replacing V3.2) via HF Inference Providers.
- Synthetic data generator: `openai/gpt-oss-120b` (Apache 2.0), escalating to
  `nvidia/NVIDIA-Nemotron-3-Ultra` (OpenMDW) only for subsets that fail quality gates. The generator must
  come from a family that is neither a routing target nor the GCT-Qwen student, so benchmark data and RLCD
  outcome labels are not biased towards one model. The P2 grader follows the same rule.
- Facts and labels are fixed by code ("labels by construction"); the generator only writes surrounding
  prose, and automatic checks reject samples that alter a fact.
- Exact revisions live in `config/models.yaml`; prices in `config/rate_cards.yaml` with source and date.

**Consequences.** Data generation cost drops to tens of dollars. Proprietary models are used only as
routing targets (their intended use), never to produce training data, unless the owner's legal review
says otherwise.
