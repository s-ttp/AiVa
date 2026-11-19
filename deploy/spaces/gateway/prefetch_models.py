"""Fetch the gateway's pinned models ahead of time (HF Space 2).

    python prefetch_models.py onnx         # BAAI ONNX embedder + reranker into HF_HOME (image build)
    python prefetch_models.py rlcd <dir>   # private ModernBERT RLCD into <dir> (boot, needs HF_TOKEN)

The pins come from the gateway package, so this script cannot drift from what the gateway loads.
Exit code 0 on success, 1 on failure; it never prints a token.
"""

import os
import sys
from pathlib import Path


def onnx() -> None:
    from huggingface_hub import hf_hub_download

    from aiva_gateway.optimize.embeddings import BGE_BASE, BGE_RERANKER

    for pin in (BGE_BASE, BGE_RERANKER):
        for filename in (pin.model_file, pin.tokenizer_file):
            hf_hub_download(pin.repo_id, filename, revision=pin.revision)
        print(f"prefetch: {pin.repo_id}@{pin.revision[:12]}")


def rlcd(target: Path) -> None:
    from huggingface_hub import snapshot_download

    from aiva_gateway.optimize.rlcd_model import RLCD_REPO, RLCD_REVISION

    token = os.environ.get("HF_TOKEN") or None
    if token is None:
        raise SystemExit("prefetch: HF_TOKEN is not set; the RLCD model is private")
    snapshot_download(RLCD_REPO, revision=RLCD_REVISION, token=token, local_dir=target)
    if not (target / "model.safetensors").is_file():
        raise SystemExit(f"prefetch: {RLCD_REPO} snapshot has no model.safetensors")
    print(f"prefetch: {RLCD_REPO}@{RLCD_REVISION[:12]} -> {target}")


def main(argv: list[str]) -> None:
    if argv[:1] == ["onnx"] and len(argv) == 1:
        onnx()
    elif argv[:1] == ["rlcd"] and len(argv) == 2:
        rlcd(Path(argv[1]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
