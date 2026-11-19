"""Check the replay set the gateway image ships (ADR-0019): python check_recordings.py [directory]

Loads every recording in the directory (default: the configured `AIVA_RECORDINGS_DIR`) the way replay does,
and fails if one does not parse, is filed under a name its request does not fingerprint to (so replay would
never find it), or keeps a credential in its URL. An empty or missing directory passes: until recordings
land, every replay is a miss answered `simulated`. Prints the count per label, never a body.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

from aiva_gateway.config import Settings
from aiva_gateway.core.canonical import canonical_json, sha256_hex
from aiva_gateway.inference.recording import Recording, _clean_url

_NAME = re.compile(r"^(?P<label>[a-z0-9][a-z0-9.-]*)-(?P<fingerprint>[0-9a-f]{24})\.json$")


def check(directory: Path) -> Counter[str]:
    labels: Counter[str] = Counter()
    if not directory.is_dir():
        return labels
    for path in sorted(directory.glob("*.json")):
        name = _NAME.match(path.name)
        if name is None:
            raise SystemExit(f"{path.name}: not named <label>-<fingerprint>.json")
        try:
            recording = Recording.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError) as error:
            raise SystemExit(f"{path.name}: not a recording ({type(error).__name__})") from None
        if _clean_url(recording.url) != recording.url:
            raise SystemExit(
                f"{path.name}: the URL keeps a credential, a private endpoint host or an unsorted query"
            )
        request = {"method": recording.method, "url": recording.url, "body": recording.request_body}
        if sha256_hex(canonical_json(request))[:24] != name["fingerprint"]:
            raise SystemExit(f"{path.name}: the request does not fingerprint to its name; replay would miss")
        labels[name["label"]] += 1
    return labels


def main() -> None:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Settings().recordings_dir
    labels = check(directory)
    summary = ", ".join(f"{label} {count}" for label, count in sorted(labels.items())) or "none yet"
    print(f"recordings in {directory}: {sum(labels.values())} ({summary})")


if __name__ == "__main__":
    main()
