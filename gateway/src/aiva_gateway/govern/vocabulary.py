"""Tenant vocabulary for DLP (ADR-0011): organisation, plan and bundle names and UI labels that are not
people.

Loaded from `config/policies/dlp/tenant_vocabulary.yaml`. It only narrows NER person findings:
- a PERSON span is dropped when every token belongs to the organisation, plan or bundle names, so mixed
  spans stay flagged;
- a PERSON span is dropped when it lies inside an occurrence of a UI label ("choose Bill history"). Labels
  match in context and case-sensitively ("Bill Pay" never matches "Can Bill pay"); their words never join
  the token vocabulary, so "Ask Bill" stays flagged.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

VOCABULARY_PATH = Path("policies") / "dlp" / "tenant_vocabulary.yaml"
_TOKEN = re.compile(r"[\w.+-]+")


class TenantVocabulary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    organisations: list[str] = Field(default_factory=list)
    plans: list[str] = Field(default_factory=list)
    bundles: list[str] = Field(default_factory=list)
    ui_labels: list[str] = Field(default_factory=list)

    @field_validator("organisations", "plans", "bundles", "ui_labels")
    @classmethod
    def _names_have_tokens(cls, names: list[str]) -> list[str]:
        empty = [n for n in names if not _TOKEN.findall(n)]
        if empty:
            raise ValueError(f"names without letters or digits: {empty}")
        return names

    @field_validator("ui_labels")
    @classmethod
    def _labels_have_two_words(cls, labels: list[str]) -> list[str]:
        # A one-word label ("Bill") would let that first name through everywhere.
        single = [label for label in labels if len(_TOKEN.findall(label)) < 2]
        if single:
            raise ValueError(f"UI labels need at least two words: {single}")
        return labels

    @property
    def terms(self) -> frozenset[str]:
        """Lower-cased tokens of every name, matched token by token against PERSON spans."""
        names = [*self.organisations, *self.plans, *self.bundles]
        return frozenset(token.lower() for name in names for token in _TOKEN.findall(name))

    def ui_label_pattern(self) -> re.Pattern[str] | None:
        """Whole-word, case-sensitive matcher for the UI labels (any whitespace between words)."""
        if not self.ui_labels:
            return None
        alternatives = sorted(
            (r"\s+".join(re.escape(t) for t in _TOKEN.findall(label)) for label in self.ui_labels),
            key=len,
            reverse=True,
        )
        return re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)")


def load_tenant_vocabulary(config_dir: Path) -> TenantVocabulary:
    with (config_dir / VOCABULARY_PATH).open(encoding="utf-8") as handle:
        return TenantVocabulary.model_validate(yaml.safe_load(handle))
