#!/usr/bin/env python
"""Run NVIDIA NeMo Guardrails' locally runnable, non-LLM jailbreak/injection rails over a JSONL corpus.

Usage:
    python evaluate_nemo.py --corpus corpus.jsonl --out predictions.jsonl [--detectors heuristics,model,yara]

Each corpus line is a JSON object with at least ``id`` and ``text``. Each output line is
``{"id", "detectors": {name: {"flagged": bool, "scores": {...}}}, "latency_ms": {name: float}}``.
A ``<out>.meta.json`` file records versions, model ids/revisions, thresholds, device and skipped detectors.

Detectors (library code paths, library default thresholds, no reimplementation):
  nemo_heuristics   nemoguardrails.library.jailbreak_detection.heuristics.checks
                    check_jailbreak_length_per_perplexity + check_jailbreak_prefix_suffix_perplexity,
                    combined with any(...) exactly as the in-process
                    `jailbreak_detection_heuristics` action does.
  nemo_model_based  nemoguardrails.library.jailbreak_detection.model_based.checks.check_jailbreak
                    (NemoGuard JailbreakDetect random forest on snowflake-arctic-embed-m-long embeddings).
                    Needs transformers 4.x plus a one-kwarg load shim; see _safe_serialization_kwarg_shim.
  nemo_yara_injection
                    nemoguardrails.library.injection_detection.actions.injection_detection (YARA rules,
                    action=reject, all four built-in rule sets). Designed as an OUTPUT rail for code/SQL/
                    template/XSS payloads; run here on the input text.

Latency is wall-clock time of the library call(s) only, measured after models are loaded and warmed up.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import platform
import statistics
import sys
import time
import traceback
from collections.abc import Callable
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

# Run this in its own virtual environment (see README.md), never in the gateway's. Model downloads
# (~3.6 GB) go to NEMO_EVAL_DIR, outside the repository, unless the caller overrides them.
HERE = Path(os.environ.get("NEMO_EVAL_DIR", Path.home() / ".cache" / "aiva-nemo-eval")).resolve()
HERE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(HERE / "hf_cache"))
# Both NeMo modules read this; heuristics default to cpu, model_based would pick cuda if present.
os.environ.setdefault("JAILBREAK_CHECK_DEVICE", "cpu")
# model_based.checks.initialize_model() downloads snowflake.onnx here when it is missing.
os.environ.setdefault("EMBEDDING_CLASSIFIER_PATH", str(HERE / "models" / "nemoguard_jailbreakdetect"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

log = logging.getLogger("evaluate_nemo")

ALL_DETECTORS = ("heuristics", "model", "yara")
DETECTOR_NAMES = {
    "heuristics": "nemo_heuristics",
    "model": "nemo_model_based",
    "yara": "nemo_yara_injection",
}
YARA_RULE_SETS = ["code", "sqli", "template", "xss"]
WARMUP_TEXT = "Hello, what are the opening hours of your nearest store?"


def _pkg_version(name: str) -> str | None:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _jsonable(value: Any) -> Any:
    """Make values strict-JSON safe (non-finite floats become strings)."""
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _hf_snapshot_revision(repo_id: str, filename: str = "config.json") -> str | None:
    """Commit sha of the cached snapshot that from_pretrained resolved, if present in the HF cache."""
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(repo_id, filename)
        if isinstance(path, str):
            parts = Path(path).parts
            if "snapshots" in parts:
                return parts[parts.index("snapshots") + 1]
    except Exception:
        pass
    return None


def _local_dir_revision(local_dir: Path, filename: str) -> str | None:
    """Commit sha recorded by hf_hub_download(local_dir=...) in its .cache metadata file."""
    meta = local_dir / ".cache" / "huggingface" / "download" / f"{filename}.metadata"
    try:
        return meta.read_text().splitlines()[0].strip() or None
    except (OSError, IndexError):
        return None


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# --------------------------------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------------------------------


class HeuristicsDetector:
    """NeMo jailbreak heuristics: length/perplexity and prefix/suffix perplexity (GPT-2 large)."""

    name = DETECTOR_NAMES["heuristics"]

    def __init__(self) -> None:
        from nemoguardrails.library.jailbreak_detection.rail_config import JailbreakDetectionConfig

        defaults = JailbreakDetectionConfig()
        self.lp_threshold = defaults.length_per_perplexity_threshold
        self.ps_threshold = defaults.prefix_suffix_perplexity_threshold

        t0 = time.perf_counter()
        # Importing the module loads GPT-2 large (module-level from_pretrained), as the library does.
        from nemoguardrails.library.jailbreak_detection.heuristics import checks as hc

        self.load_seconds = time.perf_counter() - t0
        self._hc = hc

        # Record the perplexities the library computes, without recomputing them: the check functions
        # look up the module-global get_perplexity at call time.
        self._ppl_log: list[float] = []
        original = hc.get_perplexity

        def recording_get_perplexity(input_string: str) -> float:
            value = original(input_string)
            self._ppl_log.append(value)
            return value

        hc.get_perplexity = recording_get_perplexity

    def meta(self) -> dict[str, Any]:
        hc = self._hc
        model_id = hc.model_id
        return {
            "library_functions": [
                "nemoguardrails.library.jailbreak_detection.heuristics.checks.check_jailbreak_length_per_perplexity",
                "nemoguardrails.library.jailbreak_detection.heuristics.checks.check_jailbreak_prefix_suffix_perplexity",
            ],
            "combination": "flagged = any([lp_check['jailbreak'], ps_ppl_check['jailbreak']]) "
            "(same as actions.jailbreak_detection_heuristics in-process path)",
            "thresholds": {
                "length_per_perplexity_threshold": self.lp_threshold,
                "prefix_suffix_perplexity_threshold": self.ps_threshold,
                "source": "nemoguardrails.library.jailbreak_detection.rail_config"
                ".JailbreakDetectionConfig defaults",
            },
            "rule_notes": {
                "length_per_perplexity": "flag if len(text_chars) / perplexity(text) >= threshold",
                "prefix_suffix_perplexity": "only evaluated when text has >= 20 whitespace words; flag if "
                "perplexity of first 19 words or of words[-20:-1] >= threshold",
            },
            "model": {
                "id": model_id,
                "class": f"{type(hc.model).__name__} / {type(hc.tokenizer).__name__}",
                "revision": _hf_snapshot_revision(model_id),
                "revision_note": "library calls from_pretrained without a pinned revision; this is the "
                "cached snapshot commit that was resolved",
                "device": str(next(hc.model.parameters()).device),
                "dtype": str(next(hc.model.parameters()).dtype),
                "n_positions": int(hc.model.config.n_positions),
                "stride": 512,
            },
            "load_seconds": round(self.load_seconds, 3),
            "score_fields": {
                "perplexity": "full-text perplexity computed by the library (captured, not recomputed)",
                "length_per_perplexity": "len(text) / perplexity (derived from the captured value)",
                "lp_flagged": "check_jailbreak_length_per_perplexity result",
                "ps_evaluated": "False when text has < 20 words (library returns not-jailbreak)",
                "prefix_perplexity / suffix_perplexity": "captured from the library call when evaluated",
                "ps_flagged": "check_jailbreak_prefix_suffix_perplexity result",
            },
        }

    def __call__(self, text: str) -> tuple[bool, dict[str, Any], float]:
        hc = self._hc
        self._ppl_log.clear()
        t0 = time.perf_counter()
        lp_check = hc.check_jailbreak_length_per_perplexity(text, self.lp_threshold)
        lp_ppl = list(self._ppl_log)
        self._ppl_log.clear()
        ps_check = hc.check_jailbreak_prefix_suffix_perplexity(text, self.ps_threshold)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ps_ppl = list(self._ppl_log)
        self._ppl_log.clear()

        flagged = any([lp_check["jailbreak"], ps_check["jailbreak"]])
        perplexity = lp_ppl[0] if lp_ppl else None
        scores: dict[str, Any] = {
            "perplexity": perplexity,
            "length_per_perplexity": (len(text) / perplexity) if perplexity else None,
            "lp_flagged": bool(lp_check["jailbreak"]),
            "ps_evaluated": len(ps_ppl) == 2,
            # check_jailbreak_prefix_suffix_perplexity computes the suffix first, then the prefix.
            "suffix_perplexity": ps_ppl[0] if len(ps_ppl) == 2 else None,
            "prefix_perplexity": ps_ppl[1] if len(ps_ppl) == 2 else None,
            "ps_flagged": bool(ps_check["jailbreak"]),
        }
        return bool(flagged), scores, elapsed_ms


@contextlib.contextmanager
def _safe_serialization_kwarg_shim() -> Any:
    """Compat shim for NeMo 0.24.1 model_based/models.py::SnowflakeEmbed.

    The library calls AutoModel.from_pretrained(..., use_safetensors=True), but the repo's remote
    NomicBertModel.from_pretrained (trust_remote_code) only reads ``safe_serialization`` and otherwise looks
    for pytorch_model.bin, which the Snowflake repo does not ship -> OSError "Model name ... was not found".
    While the library initialises, forward ``use_safetensors=True`` as ``safe_serialization=True`` too.
    Nothing else about the model, weights or inference path changes.
    """
    import transformers

    original = transformers.AutoModel.from_pretrained

    def patched(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("use_safetensors") and "safe_serialization" not in kwargs:
            kwargs["safe_serialization"] = True
        return original(*args, **kwargs)

    transformers.AutoModel.from_pretrained = patched
    try:
        yield
    finally:
        transformers.AutoModel.from_pretrained = original


class ModelBasedDetector:
    """NemoGuard JailbreakDetect: random forest (ONNX) on snowflake-arctic-embed-m-long embeddings."""

    name = DETECTOR_NAMES["model"]

    def __init__(self) -> None:
        import transformers

        if int(transformers.__version__.split(".")[0]) >= 5:
            raise RuntimeError(
                f"transformers {transformers.__version__}: the Snowflake remote NomicBert code calls "
                "PreTrainedModel.get_extended_attention_mask, which transformers 5.x removed (AttributeError "
                "at inference). Use transformers 4.x (tested 4.57.6)."
            )
        from nemoguardrails.library.jailbreak_detection.model_based import checks as mc
        from nemoguardrails.library.jailbreak_detection.model_based import models as mm

        self._mc = mc
        self._mm = mm
        t0 = time.perf_counter()
        with _safe_serialization_kwarg_shim():
            classifier = mc.initialize_model()
        self.load_seconds = time.perf_counter() - t0
        if classifier is None:
            raise RuntimeError("initialize_model() returned None (EMBEDDING_CLASSIFIER_PATH not set)")
        self._classifier = classifier

    def meta(self) -> dict[str, Any]:
        mc, mm = self._mc, self._mm
        classifier_dir = Path(os.environ["EMBEDDING_CLASSIFIER_PATH"])
        onnx_path = classifier_dir / mc.MODEL_FILENAME
        embed = self._classifier.embed
        return {
            "library_functions": [
                "nemoguardrails.library.jailbreak_detection.model_based.checks.initialize_model",
                "nemoguardrails.library.jailbreak_detection.model_based.checks.check_jailbreak",
            ],
            "thresholds": {
                "decision": "classifier label from the ONNX random forest (argmax of class probabilities); "
                "the library exposes no configurable threshold",
            },
            "classifier": {
                "repo_id": mc.MODEL_REPO_ID,
                "file": mc.MODEL_FILENAME,
                "revision": _local_dir_revision(classifier_dir, mc.MODEL_FILENAME),
                "sha256": _sha256(onnx_path),
                "local_path": str(onnx_path),
                "runtime": "onnxruntime CPUExecutionProvider",
                "licence": "NVIDIA Open Model License (model card); repo not gated",
            },
            "embedding_model": {
                "id": mm.SNOWFLAKE_MODEL_ID,
                "revision": _hf_snapshot_revision(mm.SNOWFLAKE_MODEL_ID),
                "revision_note": "library calls from_pretrained(trust_remote_code=True) without a pinned "
                "revision; this is the cached snapshot commit that was resolved",
                "trust_remote_code": True,
                "device": str(embed.device),
                "max_length": 2048,
            },
            "compat_shims": [
                "During initialize_model() only, AutoModel.from_pretrained(use_safetensors=True) also "
                "receives "
                "safe_serialization=True. Without it NeMo 0.24.1's own in-process path fails with "
                "'OSError: Model name Snowflake/snowflake-arctic-embed-m-long was not found.' because the "
                "remote "
                "NomicBert loader ignores use_safetensors and the repo has no pytorch_model.bin.",
                "transformers pinned to 4.x: under transformers 5.19.0 the remote NomicBert forward raises "
                "AttributeError: 'NomicBertModel' object has no attribute 'get_extended_attention_mask'.",
            ],
            "load_seconds": round(self.load_seconds, 3),
            "score_fields": {
                "score": "library value: +P(jailbreak) when flagged, -P(benign) when not flagged",
                "p_jailbreak": "derived: score if flagged else 1 + score (binary RF probabilities sum to 1)",
            },
        }

    def __call__(self, text: str) -> tuple[bool, dict[str, Any], float]:
        t0 = time.perf_counter()
        result = self._mc.check_jailbreak(prompt=text, classifier=self._classifier)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        flagged = bool(result["jailbreak"])
        score = float(result["score"])
        p_jailbreak = score if flagged else 1.0 + score
        return flagged, {"score": score, "p_jailbreak": p_jailbreak}, elapsed_ms


class YaraInjectionDetector:
    """NeMo injection detection rail (YARA): code / SQL / template / markdown-XSS payloads."""

    name = DETECTOR_NAMES["yara"]

    def __init__(self) -> None:
        from nemoguardrails import RailsConfig
        from nemoguardrails.library.injection_detection import actions as ia

        ia._check_yara_available()
        self._ia = ia
        yaml_content = (
            "rails:\n  config:\n    injection_detection:\n"
            f"      injections: {json.dumps(YARA_RULE_SETS)}\n"
            "      action: reject\n"
        )
        self._config = RailsConfig.from_content(yaml_content=yaml_content)
        self._loop = asyncio.new_event_loop()
        self.load_seconds = 0.0

    def meta(self) -> dict[str, Any]:
        ia = self._ia
        return {
            "library_functions": ["nemoguardrails.library.injection_detection.actions.injection_detection"],
            "config": {"injections": YARA_RULE_SETS, "action": "reject", "yara_path": str(ia.YARA_DIR)},
            "thresholds": {"decision": "flag if any YARA rule matches (no numeric threshold)"},
            "notes": [
                "Library default config has injections=[] (checks nothing); all four built-in rule sets "
                "were enabled here.",
                "Rail is designed as an output rail ($bot_message) targeting code (Python os/subprocess/"
                "networking imports), SQL injection, Jinja template injection and Markdown XSS. It does not "
                "target natural-language prompt injection; here it is applied to the input text.",
                "The library action recompiles the YARA rules on every call; latency includes that.",
            ],
            "rule_files": sorted(p.name for p in ia.YARA_DIR.glob("*.yara")),
        }

    def __call__(self, text: str) -> tuple[bool, dict[str, Any], float]:
        t0 = time.perf_counter()
        outcome = self._loop.run_until_complete(self._ia.injection_detection(text=text, config=self._config))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        md = outcome.metadata
        detections = list(md.get("detections", []))
        flagged = bool(outcome.is_blocked)
        return (
            flagged,
            {"is_injection": bool(md.get("is_injection")), "matched_rules": detections},
            elapsed_ms,
        )


FACTORIES: dict[str, Callable[[], Any]] = {
    "heuristics": HeuristicsDetector,
    "model": ModelBasedDetector,
    "yara": YaraInjectionDetector,
}

NOT_INCLUDED_RAILS = {
    "self_check_input / content_safety / topic_safety / llama_guard": "LLM-based rails (need an LLM).",
    "jailbreak_detection NIM path (nim_base_url)": "Same NemoGuard JailbreakDetect model served by an NVIDIA "
    "NIM; not needed because the model runs in-process here.",
    "context_bloat_detection": "Non-LLM, local; targets padded/oversized/repetitive context-manipulation "
    "(token-budget exhaustion), not prompt-injection semantics. Not included.",
    "regex": "Non-LLM, local, but ships no default patterns (user-supplied regexes only). Not included.",
    "hf_classifier": "Generic local HF text-classification rail with no default model; using it means "
    "picking a third-party classifier, not a NeMo detector. Not included.",
    "guardrails_ai detect_jailbreak": "Wrapper around a third-party Guardrails AI hub validator "
    "(hub://guardrails/detect_jailbreak); requires the separate guardrails-ai package and hub install. "
    "Not included.",
    "activefence / prompt_security / pangea / crowdstrike_aidr / f5 / trend_micro / ai_defense / fiddler / "
    "autoalign / clavata / cleanlab / privateai / patronusai / polygraf / policyai / gcp_moderate_text": (
        "Rails that call hosted third-party services (API key / external endpoint); not run to keep spend at "
        "zero and because they are not NeMo's own detectors."
    ),
}


# --------------------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------------------


def _read_corpus(path: Path, limit: int | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "id" not in obj or "text" not in obj:
                raise ValueError(f"{path}:{lineno}: record must have 'id' and 'text'")
            if not isinstance(obj["text"], str):
                raise ValueError(f"{path}:{lineno}: 'text' must be a string")
            records.append(obj)
            if limit is not None and len(records) >= limit:
                break
    return records


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pct(p: float) -> float:
        k = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
        return round(ordered[k], 3)

    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": pct(0.95),
        "max_ms": round(ordered[-1], 3),
        "min_ms": round(ordered[0], 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", required=True, type=Path, help="JSONL with at least 'id' and 'text'")
    parser.add_argument("--out", required=True, type=Path, help="predictions JSONL to write")
    parser.add_argument(
        "--detectors",
        default=",".join(ALL_DETECTORS),
        help=f"comma-separated subset of {','.join(ALL_DETECTORS)} (default: all)",
    )
    parser.add_argument("--limit", type=int, default=None, help="only evaluate the first N records")
    parser.add_argument("--no-warmup", action="store_true", help="skip the warm-up call per detector")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The library warns on every in-process call ("NOT RECOMMENDED FOR PRODUCTION"); keep stderr readable.
    if not args.verbose:
        logging.getLogger("nemoguardrails").setLevel(logging.ERROR)
        # The Snowflake remote code logs "<All keys matched successfully>" at WARNING level.
        logging.getLogger("transformers_modules").setLevel(logging.ERROR)
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

    wanted = [d.strip() for d in args.detectors.split(",") if d.strip()]
    unknown = [d for d in wanted if d not in FACTORIES]
    if unknown:
        parser.error(f"unknown detector(s): {unknown}; choose from {list(FACTORIES)}")

    records = _read_corpus(args.corpus, args.limit)

    import torch

    torch.set_grad_enabled(False)

    detectors: list[Any] = []
    skipped: dict[str, dict[str, str]] = {}
    for key in wanted:
        name = DETECTOR_NAMES[key]
        print(f"[evaluate_nemo] loading {name} ...", file=sys.stderr, flush=True)
        try:
            det = FACTORIES[key]()
            if not args.no_warmup:
                det(WARMUP_TEXT)
        except Exception as exc:
            skipped[name] = {
                "reason": "failed to initialise",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback_tail": "".join(traceback.format_exception(exc)[-3:]),
            }
            print(f"[evaluate_nemo] SKIPPED {name}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        detectors.append(det)
        print(f"[evaluate_nemo] ready {name} (load {det.load_seconds:.1f}s)", file=sys.stderr, flush=True)

    for key in ALL_DETECTORS:
        if key not in wanted:
            skipped.setdefault(DETECTOR_NAMES[key], {"reason": "not selected via --detectors"})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    latencies: dict[str, list[float]] = {d.name: [] for d in detectors}
    flagged_counts: dict[str, int] = {d.name: 0 for d in detectors}
    error_counts: dict[str, int] = {d.name: 0 for d in detectors}
    started = time.time()
    with args.out.open("w", encoding="utf-8") as out:
        for i, rec in enumerate(records, 1):
            row: dict[str, Any] = {"id": rec["id"], "detectors": {}, "latency_ms": {}}
            for det in detectors:
                try:
                    flagged, scores, elapsed_ms = det(rec["text"])
                except Exception as exc:
                    error_counts[det.name] += 1
                    row["detectors"][det.name] = {
                        "flagged": None,
                        "scores": {},
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    row["latency_ms"][det.name] = None
                    continue
                row["detectors"][det.name] = {"flagged": flagged, "scores": scores}
                row["latency_ms"][det.name] = round(elapsed_ms, 3)
                latencies[det.name].append(elapsed_ms)
                flagged_counts[det.name] += int(flagged)
            out.write(json.dumps(_jsonable(row), ensure_ascii=False) + "\n")
            out.flush()
            if i % 25 == 0 or i == len(records):
                print(f"[evaluate_nemo] {i}/{len(records)} records", file=sys.stderr, flush=True)

    import onnxruntime  # noqa: F401 - imported for version reporting only
    import transformers

    meta: dict[str, Any] = {
        "corpus": str(args.corpus.resolve()),
        "predictions": str(args.out.resolve()),
        "n_records": len(records),
        "run_started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "run_wall_seconds": round(time.time() - started, 3),
        "provenance": "measured (local CPU run of the NeMo Guardrails library)",
        "versions": {
            "python": platform.python_version(),
            "nemoguardrails": _pkg_version("nemoguardrails"),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "huggingface_hub": _pkg_version("huggingface-hub"),
            "onnxruntime": _pkg_version("onnxruntime"),
            "yara-python": _pkg_version("yara-python"),
            "numpy": _pkg_version("numpy"),
        },
        "device": {
            "requested": os.environ.get("JAILBREAK_CHECK_DEVICE"),
            "cuda_available": bool(torch.cuda.is_available()),
            "torch_num_threads": torch.get_num_threads(),
            "cpu_count": os.cpu_count(),
            "platform": platform.platform(),
            "processor": platform.processor() or platform.machine(),
        },
        "warmup": None if args.no_warmup else {"calls_per_detector": 1, "text": WARMUP_TEXT},
        "latency_definition": "wall-clock ms of the library call(s) for one record, models already loaded",
        "detectors": {d.name: d.meta() for d in detectors},
        "skipped_detectors": skipped,
        "other_rails_not_included": NOT_INCLUDED_RAILS,
        "summary": {
            d.name: {
                "flagged": flagged_counts[d.name],
                "errors": error_counts[d.name],
                "latency": _latency_summary(latencies[d.name]),
            }
            for d in detectors
        },
    }
    meta_path = Path(str(args.out) + ".meta.json")
    meta_path.write_text(json.dumps(_jsonable(meta), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[evaluate_nemo] wrote {args.out} and {meta_path}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
