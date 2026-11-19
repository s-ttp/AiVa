"""Telecom recognizers and the dependency-free pattern DLP baseline.

`RECOGNIZERS` define subscriber identifiers, financial identifiers and secrets. The production scanner
(`presidio_dlp.PresidioDlpScanner`, ADR-0011) runs them as custom Presidio recognizers alongside NER;
`PatternDlpScanner` runs them alone and is kept as the benchmark baseline. Findings map to a
classification; the most restrictive finding sets the DLP-derived classification (LLD §7).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from aiva_gateway.core.enums import Classification
from aiva_gateway.govern.injection import InjectionDetector, PatternInjectionDetector
from aiva_gateway.pipeline.interfaces import DlpFinding, DlpResult


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_valid(raw: str) -> bool:
    """Luhn plus issuer prefix/length rules, so 15-digit IMEIs are not mistaken for cards."""
    digits = re.sub(r"\D", "", raw)
    length = len(digits)
    brand_ok = (
        (digits.startswith("4") and length in (13, 16, 19))
        or (digits[:2] in {"34", "37"} and length == 15)
        or (51 <= int(digits[:2]) <= 55 and length == 16)
        or ((digits.startswith("6011") or digits.startswith("65")) and 16 <= length <= 19)
        or (3528 <= int(digits[:4]) <= 3589 and 16 <= length <= 19)
        or (digits[:2] in {"36", "38"} and length == 14)
    )
    return brand_ok and _luhn_valid(digits)


def _iban_valid(raw: str) -> bool:
    iban = raw.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


@dataclass(frozen=True)
class Recognizer:
    type: str
    classification: Classification
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None
    group: int = 0


_GULF_MCC = "(?:424|420|427|426|422|419)"

RECOGNIZERS: tuple[Recognizer, ...] = (
    Recognizer(
        "PRIVATE_KEY",
        Classification.RESTRICTED,
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    Recognizer(
        "SECRET",
        Classification.RESTRICTED,
        re.compile(
            r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"
            r"|gh[pousr]_[A-Za-z0-9]{36,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})\b"
        ),
    ),
    Recognizer(
        "SECRET",
        Classification.RESTRICTED,
        re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|secret)\s*[:=]\s*\S{6,}"),
    ),
    Recognizer("IMSI", Classification.CONFIDENTIAL, re.compile(rf"(?<!\d){_GULF_MCC}\d{{12}}(?!\d)")),
    Recognizer("ICCID", Classification.CONFIDENTIAL, re.compile(r"(?<!\d)89\d{16,18}(?!\d)")),
    Recognizer(
        "PAYMENT_CARD",
        Classification.RESTRICTED,
        re.compile(r"(?<!\d)[3-6]\d{3}(?:[ -]?\d{4}){2}[ -]?\d{1,7}(?!\d)"),
        validator=_card_valid,
    ),
    Recognizer(
        "IMEI",
        Classification.CONFIDENTIAL,
        re.compile(r"(?<!\d)\d{15}(?!\d)"),
        validator=lambda match: _luhn_valid(match) and not re.match(_GULF_MCC, match),
    ),
    Recognizer(
        "MSISDN",
        Classification.CONFIDENTIAL,
        re.compile(r"(?<![\d+])(?:\+|00)(?:971|966|974|973|968|965)[ -]?\d(?:[ -]?\d){7,8}(?!\d)"),
    ),
    Recognizer("MSISDN", Classification.CONFIDENTIAL, re.compile(r"(?<!\d)05\d(?:[ -]?\d){7}(?!\d)")),
    Recognizer(
        "IBAN",
        Classification.RESTRICTED,
        re.compile(r"\b(?:AE|SA|QA|BH|KW|OM)\d{2}(?: ?[A-Z0-9]{4}){3,6}(?: ?[A-Z0-9]{1,4})?\b"),
        validator=_iban_valid,
    ),
    Recognizer("NATIONAL_ID", Classification.CONFIDENTIAL, re.compile(r"(?<!\d)784-?\d{4}-?\d{7}-?\d(?!\d)")),
    Recognizer(
        "NATIONAL_ID",
        Classification.CONFIDENTIAL,
        re.compile(r"(?i)(?:national\s*id|iqama|\bnin\b)\D{0,20}([12]\d{9})(?!\d)"),
        group=1,
    ),
    Recognizer(
        "EMAIL",
        Classification.CONFIDENTIAL,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
)


class PatternDlpScanner:
    """Deterministic, dependency-free scanner. Version is recorded with every decision."""

    def __init__(self, injection: InjectionDetector | None = None) -> None:
        self.injection = injection or PatternInjectionDetector()
        self.version = f"pattern-dlp-v1+{self.injection.version}"

    def scan(self, text: str, *, detect_injection: bool = True) -> DlpResult:
        findings: list[DlpFinding] = []
        claimed: list[tuple[int, int]] = []
        for recognizer in RECOGNIZERS:
            for match in recognizer.pattern.finditer(text):
                start, end = match.span(recognizer.group)
                value = match.group(recognizer.group)
                if any(start < c_end and end > c_start for c_start, c_end in claimed):
                    continue
                if recognizer.validator and not recognizer.validator(value):
                    continue
                claimed.append((start, end))
                findings.append(DlpFinding(recognizer.type, recognizer.classification, start, end))
        indicators = self.injection.detect(text) if detect_injection else ()
        findings.sort(key=lambda f: f.start)
        return DlpResult(
            findings=tuple(findings),
            injection_indicators=indicators,
            scanner_version=self.version,
            injection_detector=self.injection.version if detect_injection else "",
        )

    def redact(self, text: str, result: DlpResult) -> str:
        return redact(text, result)


def redact(text: str, result: DlpResult) -> str:
    """Replace each finding with its type, e.g. `[MSISDN]`. Findings never overlap."""
    redacted = text
    for finding in sorted(result.findings, key=lambda f: f.start, reverse=True):
        redacted = redacted[: finding.start] + f"[{finding.type}]" + redacted[finding.end :]
    return redacted
