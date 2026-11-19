"""Presidio DLP (LLD §3 AI-PEL, §7; ADR-0011).

Microsoft Presidio's AnalyzerEngine runs two kinds of recognizer over the text:
- the telecom, financial and secret patterns from `dlp.RECOGNIZERS`, registered as custom Presidio
  recognizers with their validators (Luhn, IBAN checksum, card issuer rules);
- spaCy English NER (`en_core_web_md` by default) for person names, the PII the patterns cannot see.

Overlapping findings resolve by recognizer priority (patterns before NER, in `RECOGNIZERS` order), so a
15-digit IMSI is never also reported as an IMEI. Person-name hits are dropped when they are made only
of product, model or brand vocabulary (e.g. "Claude", "Gemini", "GCT"), of network vocabulary
("Microwave", "Jitter") or of the configured tenant
vocabulary (organisation, plan and bundle names such as "GCT Oman", "Prepaid Smart", "Hajj"), when
they lie inside a tenant UI label in the text ("choose Bill history"), or when they are not shaped like a
name (digits, line breaks, code punctuation). A single capitalised word
opening a line, sentence or field is kept only if spaCy still reads it as a person when lower-cased
("Microwave hop down" is not a person). Text longer than the scan limit is refused, never passed on
unscanned (fail closed).
"""

import re
from collections.abc import Iterable
from functools import cache
from importlib.metadata import version as package_version
from typing import Any

from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngineProvider
from presidio_analyzer.predefined_recognizers import SpacyRecognizer

from aiva_gateway.catalog import Catalog
from aiva_gateway.core.enums import Classification
from aiva_gateway.core.errors import AivaError
from aiva_gateway.govern.dlp import RECOGNIZERS, Recognizer, redact
from aiva_gateway.govern.injection import HeuristicInjectionDetector, InjectionDetector
from aiva_gateway.govern.vocabulary import TenantVocabulary
from aiva_gateway.pipeline.interfaces import DlpFinding, DlpResult

LANGUAGE = "en"
PERSON = "PERSON"
# Personal names are personal data: a detected name sends the request down the sovereign path.
NER_CLASSIFICATION = {PERSON: Classification.CONFIDENTIAL}
NER_RECOGNIZER = "aiva-ner-person"
NER_FILTER_VERSION = "ner-filter-v4"
IGNORED_NER_LABELS = [
    "CARDINAL",
    "DATE",
    "EVENT",
    "FAC",
    "GPE",
    "LANGUAGE",
    "LAW",
    "LOC",
    "MONEY",
    "NORP",
    "ORDINAL",
    "ORG",
    "PERCENT",
    "PRODUCT",
    "QUANTITY",
    "TIME",
    "WORK_OF_ART",
]
WINDOW_CHARS = 20_000
WINDOW_OVERLAP = 256
UI_LABEL_REACH = 100  # characters around a PERSON span searched for a tenant UI label
_TOKEN = re.compile(r"[\w.+-]+")
_NOT_A_NAME = re.compile(r"[\d\n_@/\\:#=<>{}\[\]|]")

# Vocabulary that spaCy may tag as a person but that names products, models or vendors.
STATIC_ALLOW_TERMS = frozenset(
    {
        "aiva",
        "presidio",
        "anthropic",
        "google",
        "nvidia",
        "openai",
        "meta",
        "llama",
        "mistral",
        "gpt",
        "chatgpt",
        "copilot",
        "siri",
        "alexa",
        "cortana",
        "bard",
        "hugging",
        "face",
        "huggingface",
    }
)
# Network vocabulary that spaCy tags as a person when capitalised mid-sentence or opening a quoted or
# bracketed alarm title (`- ALM-7790 (major) - "Microwave hop MW-12 down"`, "Check the Jitter alarm"), where
# the lower-cased re-check does not run. Each was observed misread (ner-filter-v4, ADR-0011) and none is a
# personal name; a span made only of them is dropped. Words that are also names are never added here.
NETWORK_TERMS = frozenset({"microwave", "baseband", "latency", "jitter"})


class DlpScanLimitError(AivaError):
    status_code = 413
    code = "DLP_SCAN_LIMIT_EXCEEDED"


class TelecomPatternRecognizer(EntityRecognizer):
    """One `dlp.Recognizer` (regex + optional capture group + validator) as a Presidio recognizer."""

    def __init__(self, definition: Recognizer, index: int) -> None:
        self.definition = definition
        self.recognizer_name = f"aiva-{definition.type.lower()}-{index}"
        super().__init__(
            supported_entities=[definition.type],
            name=self.recognizer_name,
            supported_language=LANGUAGE,
            version="telecom-v1",
        )

    def load(self) -> None:
        return None

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        results = []
        definition = self.definition
        for match in definition.pattern.finditer(text):
            value = match.group(definition.group)
            if definition.validator and not definition.validator(value):
                continue
            start, end = match.span(definition.group)
            results.append(
                RecognizerResult(
                    entity_type=definition.type,
                    start=start,
                    end=end,
                    score=1.0,
                    recognition_metadata={
                        RecognizerResult.RECOGNIZER_NAME_KEY: self.recognizer_name,
                        RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                    },
                )
            )
        return results


@cache
def _engine(model: str) -> tuple[AnalyzerEngine, dict[str, int]]:
    """Load the NLP model once per process; return the engine and recognizer priorities by name."""
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": LANGUAGE, "model_name": model}],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": {"PERSON": PERSON, "PER": PERSON},
                "labels_to_ignore": IGNORED_NER_LABELS,
            },
        }
    )
    nlp_engine = provider.create_engine()
    # The dependency parser is not used by Presidio's NER or context enhancement; skipping it halves
    # latency with identical findings (measured on the scenario prompts, ADR-0011).
    spacy_model: Any = nlp_engine.nlp[LANGUAGE]  # type: ignore[attr-defined]
    if "parser" in spacy_model.pipe_names:
        spacy_model.disable_pipe("parser")
    registry = RecognizerRegistry(supported_languages=[LANGUAGE])
    priorities: dict[str, int] = {}
    for index, definition in enumerate(RECOGNIZERS):
        recognizer = TelecomPatternRecognizer(definition, index)
        registry.add_recognizer(recognizer)
        priorities[recognizer.recognizer_name] = index
    registry.add_recognizer(
        SpacyRecognizer(supported_language=LANGUAGE, supported_entities=[PERSON], name=NER_RECOGNIZER)
    )
    priorities[NER_RECOGNIZER] = len(RECOGNIZERS)
    engine = AnalyzerEngine(nlp_engine=nlp_engine, registry=registry, supported_languages=[LANGUAGE])
    return engine, priorities


def catalog_allow_terms(catalog: Catalog) -> frozenset[str]:
    """Product, model, application and tenant vocabulary from the catalog (lower-cased tokens)."""
    names: list[str] = [catalog.tenant.tenant.name]
    names += [m.display_name for m in catalog.models.models]
    names += [m.provider_model_id.replace("/", " ") for m in catalog.models.models]
    names += [a.name for a in catalog.tenant.applications]
    names += [t.display_name for t in catalog.tools]
    return frozenset(token.lower() for name in names for token in _TOKEN.findall(name))


def _windows(text: str) -> Iterable[tuple[int, str]]:
    """Overlapping windows, cut at whitespace where possible, so long text never exceeds spaCy limits."""
    start = 0
    while start < len(text):
        end = min(len(text), start + WINDOW_CHARS)
        if end < len(text):
            cut = max(
                text.rfind("\n", start + WINDOW_CHARS // 2, end),
                text.rfind(" ", start + WINDOW_CHARS // 2, end),
            )
            end = cut if cut > start else end
        yield start, text[start:end]
        if end >= len(text):
            break
        start = max(end - WINDOW_OVERLAP, start + 1)


class PresidioDlpScanner:
    def __init__(
        self,
        *,
        model: str = "en_core_web_md",
        allow_terms: frozenset[str] = frozenset(),
        vocabulary: TenantVocabulary | None = None,
        max_chars: int = 250_000,
        injection: InjectionDetector | None = None,
    ) -> None:
        self.model = model
        self.allow_terms = (
            STATIC_ALLOW_TERMS
            | NETWORK_TERMS
            | allow_terms
            | (vocabulary.terms if vocabulary else frozenset())
        )
        self._ui_labels = vocabulary.ui_label_pattern() if vocabulary else None
        self.max_chars = max_chars
        self.injection = injection or HeuristicInjectionDetector()
        self._engine, self._priorities = _engine(model)
        self._spacy: Any = self._engine.nlp_engine.nlp[LANGUAGE]  # type: ignore[attr-defined]
        self.version = (
            f"presidio-{package_version('presidio-analyzer')}+{model}-{package_version(model)}"
            f"+telecom-v1+{NER_FILTER_VERSION}"
            + (f"+{vocabulary.version}" if vocabulary else "")
            + f"+{self.injection.version}"
        )

    def scan(self, text: str, *, detect_injection: bool = True) -> DlpResult:
        if len(text) > self.max_chars:
            raise DlpScanLimitError(
                f"The request has {len(text)} characters; the DLP scan limit is {self.max_chars}.",
                remediation="Split the request or send less evidence. Nothing was sent to any model.",
            )
        candidates: dict[tuple[str, int, int], tuple[int, RecognizerResult]] = {}
        for offset, window in _windows(text):
            for result in self._engine.analyze(window, language=LANGUAGE):
                start, end = result.start + offset, result.end + offset
                if result.entity_type == PERSON and not self._is_person(text, start, end):
                    continue
                name = (result.recognition_metadata or {}).get(RecognizerResult.RECOGNIZER_NAME_KEY, "")
                priority = self._priorities.get(name, len(self._priorities))
                candidates[(result.entity_type, start, end)] = (priority, result)
        findings = self._resolve(candidates)
        indicators = self.injection.detect(text) if detect_injection else ()
        return DlpResult(
            findings=findings,
            injection_indicators=indicators,
            scanner_version=self.version,
            injection_detector=self.injection.version if detect_injection else "",
        )

    def redact(self, text: str, result: DlpResult) -> str:
        return redact(text, result)

    def _is_person(self, text: str, start: int, end: int) -> bool:
        """NER is noisy on IDs, key/value data and capitalised technical words; keep only spans shaped
        like a personal name that are not entirely product, model or vendor vocabulary or part of a UI
        label."""
        span = text[start:end]
        if _NOT_A_NAME.search(span):
            return False
        tokens = _TOKEN.findall(span)
        if not any(_name_like(t) for t in tokens):
            return False
        if all(t.lower() in self.allow_terms for t in tokens):
            return False
        if self._inside_ui_label(text, start, end):
            return False
        if len(tokens) > 1 or not _starts_unit(text, start):
            return True
        # A single capitalised word opening a line, sentence or field ("Microwave hop down",
        # "title: Microwave link runbook") carries no capitalisation signal: keep it only if spaCy still
        # reads it as a person once lower-cased. First names keep that reading; common nouns do not.
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line = text[line_start : len(text) if line_end == -1 else line_end][:2000]
        offset_start, offset_end = start - line_start, end - line_start
        if offset_end > len(line):
            return True
        lowered = line[:offset_start] + line[offset_start:offset_end].lower() + line[offset_end:]
        return any(
            ent.label_ == "PERSON" and ent.start_char < offset_end and ent.end_char > offset_start
            for ent in self._spacy(lowered).ents
        )

    def _inside_ui_label(self, text: str, start: int, end: int) -> bool:
        """Whether the span lies inside a UI label occurrence ("choose Bill history" covers "Bill")."""
        if self._ui_labels is None:
            return False
        lo, hi = max(0, start - UI_LABEL_REACH), min(len(text), end + UI_LABEL_REACH)
        return any(m.start() <= start and m.end() >= end for m in self._ui_labels.finditer(text, lo, hi))

    def _resolve(
        self, candidates: dict[tuple[str, int, int], tuple[int, RecognizerResult]]
    ) -> tuple[DlpFinding, ...]:
        """Keep the highest-priority, then longest, finding for each overlapping region."""
        ordered = sorted(
            candidates.items(), key=lambda item: (item[1][0], item[0][1] - item[0][2], item[0][1])
        )
        claimed: list[tuple[int, int]] = []
        findings = []
        for (entity, start, end), _ in ordered:
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            findings.append(DlpFinding(entity, _classification(entity), start, end))
        return tuple(sorted(findings, key=lambda f: f.start))


def _starts_unit(text: str, start: int) -> bool:
    """Whether `start` opens the text, a line, a sentence or a `field:` value."""
    before = text[:start].rstrip(" \t")
    return not before or before[-1] in "\n.!?;:"


def _name_like(token: str) -> bool:
    """A capitalised word ("Layla"), or a word in a script without case (Arabic-ready)."""
    if not token.isalpha():
        return False
    return (token[0].isupper() and any(c.islower() for c in token)) or (
        not token.isascii() and token.lower() == token.upper()
    )


def _classification(entity: str) -> Classification:
    if entity in NER_CLASSIFICATION:
        return NER_CLASSIFICATION[entity]
    return next(r.classification for r in RECOGNIZERS if r.type == entity)
