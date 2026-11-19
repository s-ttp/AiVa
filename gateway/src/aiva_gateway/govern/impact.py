"""Compare a candidate policy bundle with the published one (LLD §22.6: review before publish).

A change is high-impact when it relaxes a constraint (a policy removed, a deny or approval dropped, a
model allowlist widened, a grant or network widened) or blocks every request. High-impact drafts need a
second administrator to publish them (ADR-0012).
"""

from typing import Any

from aiva_gateway.contracts.policy import PolicyChange
from aiva_gateway.core.enums import Classification

_ACTION_RANK = {None: 0, "approval_required": 1, "deny": 2}
_RESTRICTIONS = {
    "execution_path": "No longer forces the sovereign direct route.",
    "cim": "No longer bypasses CIM.",
    "external_providers": "No longer denies external providers.",
    "output_dlp": "No longer requires output DLP.",
}


def bundle_changes(current: dict[str, Any] | None, candidate: dict[str, Any]) -> list[PolicyChange]:
    if current is None:
        return [
            PolicyChange(target="policy", target_id=p["policy_id"], change="added", high_impact=False)
            for p in candidate["policies"]
        ]
    changes = _policy_changes(current["policies"], candidate["policies"])
    changes += _grant_changes(current["applications"], candidate["applications"])
    changes += _registry_changes("model", current["models"], candidate["models"], _model_relaxations)
    changes += _registry_changes(
        "tool", current.get("tools", {}), candidate.get("tools", {}), _tool_relaxations
    )
    changes += _registry_changes("network", current["networks"], candidate["networks"], _network_relaxations)
    if current["meta"].get("allow_simulated_ingress") is False and candidate["meta"].get(
        "allow_simulated_ingress"
    ):
        changes.append(
            PolicyChange(
                target="settings",
                target_id="allow_simulated_ingress",
                change="modified",
                high_impact=True,
                summary=["Simulated test identities become accepted."],
            )
        )
    return changes


def _policy_changes(current: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[PolicyChange]:
    old = {p["policy_id"]: p for p in current}
    new = {p["policy_id"]: p for p in candidate}
    changes = []
    for pid in sorted(old.keys() - new.keys()):
        changes.append(
            PolicyChange(
                target="policy",
                target_id=pid,
                change="removed",
                high_impact=bool(old[pid]["enforce"]),
                summary=["Policy removed: its restrictions no longer apply."],
            )
        )
    for pid in sorted(new.keys() - old.keys()):
        broad = _blocks_everything(new[pid])
        changes.append(
            PolicyChange(
                target="policy",
                target_id=pid,
                change="added",
                high_impact=broad,
                summary=["Applies to every request."] if broad else [],
            )
        )
    for pid in sorted(old.keys() & new.keys()):
        if old[pid] == new[pid]:
            continue
        relaxed = _policy_relaxations(old[pid], new[pid])
        broad = _blocks_everything(new[pid]) and not _blocks_everything(old[pid])
        changes.append(
            PolicyChange(
                target="policy",
                target_id=pid,
                change="modified",
                high_impact=bool(relaxed) or broad,
                summary=relaxed + (["Now applies to every request."] if broad else [])
                or ["Restrictions tightened or descriptive fields changed."],
            )
        )
    return changes


def _blocks_everything(policy: dict[str, Any]) -> bool:
    return not policy["match"] and policy["enforce"].get("action") in {"deny", "approval_required"}


def _policy_relaxations(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    before, after = old["enforce"], new["enforce"]
    notes = []
    if _ACTION_RANK[after.get("action")] < _ACTION_RANK[before.get("action")]:
        notes.append(f"Action relaxed from {before.get('action')} to {after.get('action') or 'none'}.")
    notes += [message for key, message in _RESTRICTIONS.items() if key in before and key not in after]
    if before.get("audit_level") == "enhanced" and after.get("audit_level") != "enhanced":
        notes.append("Enhanced audit removed.")
    if "allowed_models" in before and (
        "allowed_models" not in after or set(after["allowed_models"]) - set(before["allowed_models"])
    ):
        notes.append("Model allowlist widened.")
    if before and _narrower_match(old["match"], new["match"]):
        notes.append("Matches fewer requests.")
    return notes


def _narrower_match(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when `after` may match fewer requests than `before`."""
    for key, value in after.items():
        if key not in before:
            return True
        if isinstance(value, list) and set(before[key]) - set(value):
            return True
        if not isinstance(value, list) and value != before[key]:
            return True
    return False


def _grant_changes(current: dict[str, Any], candidate: dict[str, Any]) -> list[PolicyChange]:
    changes = []
    for app_id in sorted(current.keys() | candidate.keys()):
        old, new = current.get(app_id), candidate.get(app_id)
        if old == new:
            continue
        if old is None or new is None:
            changes.append(
                PolicyChange(
                    target="application",
                    target_id=app_id,
                    change="added" if old is None else "removed",
                    high_impact=old is None and bool(new and new["enabled"]),
                    summary=["New application with grants."] if old is None else [],
                )
            )
            continue
        widened = [
            f"Grants more {label}: {', '.join(sorted(set(new.get(key, [])) - set(old.get(key, []))))}."
            for key, label in (
                ("trusted_network_ids", "networks"),
                ("allowed_tasks", "tasks"),
                ("allowed_models", "models"),
                ("allowed_tools", "tools"),
            )
            if set(new.get(key, [])) - set(old.get(key, []))
        ]
        if new["enabled"] and not old["enabled"]:
            widened.append("Application re-enabled.")
        if new.get("internet_callers") and not old.get("internet_callers"):
            widened.append("Accepts key calls from the internet (any source).")
        if (
            Classification(new["classification_floor"]).rank
            < Classification(old["classification_floor"]).rank
        ):
            widened.append("Classification floor lowered.")
        changes.append(
            PolicyChange(
                target="application",
                target_id=app_id,
                change="modified",
                high_impact=bool(widened),
                summary=widened or ["Grants narrowed."],
            )
        )
    return changes


def _model_relaxations(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    notes = []
    if new["sovereign"] and not old["sovereign"]:
        notes.append("Model becomes eligible for confidential data.")
    if old["external"] and not new["external"]:
        notes.append("Model no longer treated as external.")
    if new["enabled"] and not old["enabled"]:
        notes.append("Model re-enabled.")
    return notes


def _tool_relaxations(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    notes = []
    if old["access"] == "write" and new["access"] != "write":
        notes.append("Tool no longer treated as having side effects.")
    if old["egress"] == "external" and new["egress"] != "external":
        notes.append("Tool no longer treated as external.")
    if new["enabled"] and not old["enabled"]:
        notes.append("Tool re-enabled.")
    return notes


def _network_relaxations(old: str, new: str) -> list[str]:
    return [f"CIDR changed from {old} to {new}."] if old != new else []


def _registry_changes(
    target: str, current: dict[str, Any], candidate: dict[str, Any], relaxations: Any
) -> list[PolicyChange]:
    changes = []
    for item_id in sorted(current.keys() | candidate.keys()):
        old, new = current.get(item_id), candidate.get(item_id)
        if old == new:
            continue
        if old is None or new is None:
            # A new network can widen an application's grants only through the grant itself.
            changes.append(
                PolicyChange(
                    target=target,
                    target_id=item_id,
                    change="added" if old is None else "removed",
                    high_impact=False,
                )
            )
            continue
        notes = relaxations(old, new)
        changes.append(
            PolicyChange(
                target=target,
                target_id=item_id,
                change="modified",
                high_impact=bool(notes),
                summary=notes or ["Narrowed or descriptive change."],
            )
        )
    return changes
