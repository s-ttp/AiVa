"""Prompt-injection detector benchmark (ADR-0011).

    uv run python -m aiva_gateway.govern.benchmark gateway/tests/data/injection/heldout.jsonl \
        [--external nemo=predictions.jsonl] [--json results.json]

Corpus records: {"id", "text", "label": "injection"|"benign", "category"}. External predictions (e.g. the
NeMo Guardrails evaluator) are JSON lines {"id", "detectors": {name: {"flagged": bool|null}}}. Results are
measured on synthetic data and are labelled as such.
"""

import argparse
import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiva_gateway.govern.injection import (
    HeuristicInjectionDetector,
    InjectionDetector,
    PatternInjectionDetector,
)

POSITIVE = "injection"


@dataclass
class Score:
    detector: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    errors: int = 0
    latency_ms: list[float] = field(default_factory=list)
    by_category: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    missed: list[str] = field(default_factory=list)
    false_alarms: list[str] = field(default_factory=list)

    def add(self, record: dict[str, Any], flagged: bool | None) -> None:
        if flagged is None:
            self.errors += 1
            flagged = False  # an error is a miss for injections and not a false alarm for benign text
        positive = record["label"] == POSITIVE
        bucket = self.by_category[record["category"]]
        bucket[1] += 1
        if flagged:
            bucket[0] += 1
        if positive and flagged:
            self.tp += 1
        elif positive:
            self.fn += 1
            self.missed.append(record["id"])
        elif flagged:
            self.fp += 1
            self.false_alarms.append(record["id"])
        else:
            self.tn += 1

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def false_positive_rate(self) -> float:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0

    def summary(self) -> dict[str, Any]:
        latency = sorted(self.latency_ms)
        return {
            "detector": self.detector,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "errors": self.errors,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "false_positive_rate": round(self.false_positive_rate, 3),
            "latency_ms_mean": round(statistics.fmean(latency), 3) if latency else None,
            "latency_ms_p95": round(latency[int(0.95 * (len(latency) - 1))], 3) if latency else None,
            "flag_rate_by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(self.by_category.items())},
            "missed": self.missed,
            "false_alarms": self.false_alarms,
        }


def load_corpus(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def score_detector(detector: InjectionDetector, corpus: list[dict[str, Any]]) -> Score:
    score = Score(detector.version)
    for record in corpus:
        started = time.perf_counter()
        flagged = bool(detector.detect(record["text"]))
        score.latency_ms.append((time.perf_counter() - started) * 1000)
        score.add(record, flagged)
    return score


def score_external(path: Path, corpus: list[dict[str, Any]]) -> list[Score]:
    predictions = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            predictions[item["id"]] = item
    names = sorted({name for item in predictions.values() for name in item["detectors"]})
    scores = []
    for name in names:
        score = Score(name)
        for record in corpus:
            item = predictions.get(record["id"], {})
            result = item.get("detectors", {}).get(name, {})
            score.add(record, result.get("flagged"))
            if (latency := item.get("latency_ms", {}).get(name)) is not None:
                score.latency_ms.append(float(latency))
        scores.append(score)
    return scores


def run(
    corpus_path: Path, external: list[Path], exclude: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    corpus = [r for r in load_corpus(corpus_path) if r["category"] not in exclude]
    scores = [score_detector(d, corpus) for d in (PatternInjectionDetector(), HeuristicInjectionDetector())]
    for path in external:
        scores.extend(score_external(path, corpus))
    return [s.summary() for s in scores]


def markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "| Detector | Precision | Recall | F1 | FPR | TP | FP | FN | TN | Errors | Mean ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['detector']} | {r['precision']} | {r['recall']} | {r['f1']} | {r['false_positive_rate']} "
            f"| {r['tp']} | {r['fp']} | {r['fn']} | {r['tn']} | {r['errors']} | {r['latency_ms_mean']} "
            f"| {r['latency_ms_p95']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--external", type=Path, action="append", default=[])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--exclude-category", action="append", default=[], help="Leave a category out")
    parser.add_argument("--details", action="store_true", help="Print per-category rates and misses")
    args = parser.parse_args()
    results = run(args.corpus, args.external, frozenset(args.exclude_category))
    print(f"Corpus: {args.corpus} (synthetic; provenance of these figures: measured on synthetic data)\n")
    print(markdown(results))
    if args.details:
        for r in results:
            print(f"\n{r['detector']}: {json.dumps(r['flag_rate_by_category'])}")
            print(f"  missed: {r['missed']}\n  false alarms: {r['false_alarms']}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
