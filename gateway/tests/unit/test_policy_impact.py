"""A grant that lets an application take key calls from the internet widens access, so publishing it needs
a second administrator (ADR-0012, ADR-0027); taking it away only narrows."""

import copy
from typing import Any

from aiva_gateway.catalog.loader import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.govern.bundle import build_bundle
from aiva_gateway.govern.impact import bundle_changes


def _bundle() -> dict[str, Any]:
    catalog = load_catalog(REPO_ROOT / "config")
    return build_bundle(catalog, catalog.policies, version="1", allow_simulated_ingress=True)


def _with_flag(bundle: dict[str, Any], app_id: str, value: bool) -> dict[str, Any]:
    changed = copy.deepcopy(bundle)
    changed["applications"][app_id]["internet_callers"] = value
    return changed


def test_granting_internet_callers_is_high_impact() -> None:
    current = _bundle()
    assert current["applications"]["gct-support-chatbot"]["internet_callers"] is False
    changes = bundle_changes(current, _with_flag(current, "gct-support-chatbot", True))
    assert [(c.target_id, c.high_impact, c.summary) for c in changes] == [
        ("gct-support-chatbot", True, ["Accepts key calls from the internet (any source)."])
    ]


def test_withdrawing_internet_callers_only_narrows() -> None:
    current = _bundle()
    assert current["applications"]["gct-api-sandbox"]["internet_callers"] is True
    changes = bundle_changes(current, _with_flag(current, "gct-api-sandbox", False))
    assert [(c.target_id, c.high_impact, c.summary) for c in changes] == [
        ("gct-api-sandbox", False, ["Grants narrowed."])
    ]
