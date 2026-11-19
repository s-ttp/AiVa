"""Writes `src/test/fixtures/run/replayParity.json` for `src/run/draft.test.ts`.

The fixture holds the scenario catalog as `GET /v1/catalog/scenarios` serves it, the chat body the
deployed replay check sends for each scenario (`tests/e2e/scenario_requests.py::chat_body`), and the replay
body story mode sends: the scenario's Baseline-comparison variant where the check has one, else its default
request, with `execution_mode: replay`. The tests check that the run form and story mode build the same
bodies from the catalog, so each scenario's recorded replay still matches (ADR-0019).
Run it after changing `config/scenarios.yaml` or either builder:

    uv run python web/apps/control-center/scripts/replay_parity_fixture.py
"""

import importlib.util
import json
from pathlib import Path

from aiva_gateway.catalog.loader import load_catalog

ROOT = Path(__file__).resolve().parents[4]
MIRROR = ROOT / "tests" / "e2e" / "scenario_requests.py"
OUT = ROOT / "web" / "apps" / "control-center" / "src" / "test" / "fixtures" / "run" / "replayParity.json"


def main() -> None:
    spec = importlib.util.spec_from_file_location("scenario_requests", MIRROR)
    assert spec is not None and spec.loader is not None
    requests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(requests)

    catalog = load_catalog(ROOT / "config")
    scenarios = [s.model_dump(mode="json") for s in sorted(catalog.scenarios, key=lambda s: s.number)]
    bodies = {s["id"]: requests.chat_body(s) for s in requests.scenarios()}
    # The Baseline-comparison variant follows its scenario's default request, so the last chat wins.
    chats = [r for r in requests.scenario_set(baseline=True) if r.kind == "chat"]
    story = {r.scenario: requests.with_execution_mode(r, "replay") for r in chats}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"scenarios": scenarios, "chat_bodies": bodies, "story_bodies": story}
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = f"{len(scenarios)} scenarios, {len(bodies)} chat and {len(story)} story bodies"
    print(f"wrote {OUT.relative_to(ROOT)}: {counts}")


if __name__ == "__main__":
    main()
