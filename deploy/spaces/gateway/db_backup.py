"""Keep Space 2's database across restarts: dumps in a private HF Storage Bucket, restored on boot.

The Space disk is ephemeral, so every restart, rebuild or wake from sleep starts an empty Postgres cluster.
This program copies the database to a bucket and back (deploy/RUNBOOK.md, "The demo resets on restart"):

    python db_backup.py restore   # gateway.sh, before `migrate`: newest good dump into an empty database
    python db_backup.py backup    # one dump, uploaded only when the data changed
    python db_backup.py loop      # supervisord: a check right after boot, then every
                                  # AIVA_BACKUP_INTERVAL_SECONDS (60), and a final dump on SIGTERM

**Never blocks the gateway.** Without `AIVA_BACKUP_BUCKET` it does nothing. A bucket that cannot be reached,
a missing or damaged dump, or a dump from a schema this image does not know are logged, and the gateway
starts on a fresh database exactly as before. Every command exits 0.

**Restore.**
- Only into an empty database, so restoring never rewrites existing rows. That includes `usage_ledger` and
  `audit_log`, which stay append-only: their triggers come back with the schema.
- One transaction (`pg_restore --single-transaction`): a failed restore leaves the database empty.
- After restoring it checks that this image knows the dump's Alembic revision, then `migrate` runs forward.
- A restored database is not fresh. It clears `$AIVA_RUN_DIR/fresh-database`, so gateway.sh skips the
  synthetic boot traffic and seeded runs are not duplicated.

**Backup.**
- Checked right after boot and then every minute; uploaded only when the data changed. The check is local
  (pg_dump and a fingerprint); only a change calls the Hub.
- Only a container whose boot restore restored a dump, found the bucket empty or found an existing database
  backs up (`$AIVA_RUN_DIR/backup-ok`). A container that started fresh because the bucket was unreachable or
  every dump was refused never uploads, so it cannot replace the bucket's history with an empty database.
- A custom-format `pg_dump`, uploaded under a new, unique name. Only then is `latest.json` rewritten to point
  at it, so a half-written upload never replaces the only good dump.
- The newest `AIVA_BACKUP_KEEP` dumps are kept (default 3); older ones are deleted.
- On a restart or redeploy HF starts the new container before it stops the old one, so the new one restores
  before the old one's final dump. Whatever the old container wrote in that overlap (about 1.5 to 2 minutes)
  is lost when the new container uploads. A sleep has no overlap: the final dump runs, and the wake restores
  it.
- "Changed" means the dump's content, minus pg_dump's per-run `\\restrict` key and minus bookkeeping tables
  that the gateway rewrites on a timer (`IGNORED_FOR_CHANGES`). Those tables are still in every dump.

**Bucket layout** (`AIVA_BACKUP_PREFIX`, default `gateway`):
    <prefix>/latest.json                         the dump to restore and its sha256
    <prefix>/dumps/aiva-<UTC time to the µs>-<sha8>.dump   the rolling copies, oldest first by name

Environment:
    AIVA_BACKUP_BUCKET            `<owner>/<bucket>` (a private HF Storage Bucket), or `file:<dir>` (tests)
    AIVA_BACKUP_HF_TOKEN          Space secret: read and write access to that bucket only; never printed
    AIVA_BACKUP_PREFIX            folder in the bucket (default `gateway`)
    AIVA_BACKUP_INTERVAL_SECONDS  seconds between checks in `loop` (default 60)
    AIVA_BACKUP_KEEP              dumps to keep (default 3)
    AIVA_RUN_DIR                  the container's run directory (boot.done, fresh-database)
    PGHOST, PGPORT, PGUSER, PGPASSFILE / PGPASSWORD   libpq settings for the local Postgres (entrypoint.sh)
    AIVA_BACKUP_DATABASE          database name (default `aiva`)

The data is synthetic and pseudonymous, and runs keep no prompt text (LLD §17). The bucket is private anyway.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

MANIFEST = "latest.json"
DUMPS = "dumps"
DUMP_NAME = re.compile(r"^aiva-\d{8}T\d{12}Z-[0-9a-f]{8}\.dump$")
# Rewritten on a timer while the gateway runs, whether or not anything happened: Insights aggregates (rebuilt
# from runs and the ledger every minute) and the reconciliation log (every 5 minutes).
IGNORED_FOR_CHANGES = ("public.report_aggregate_", "public.report_refresh", "public.reconciliation_run")
COMMAND_TIMEOUT_SECONDS = 600
# Written by `restore` when this container may back up: it restored a dump, found the bucket empty, or found
# an existing database. Without it the loop uploads nothing.
BACKUP_OK = "backup-ok"


def log(message: str) -> None:
    print(f"db-backup: {message}", flush=True)


class StoreUnavailable(Exception):
    """The bucket could not be read or written. Holds a short reason, never a token or a URL with one."""


class Store(Protocol):
    def list_dumps(self) -> list[str]: ...
    def get(self, name: str, dest: Path) -> None: ...
    def put(self, src: Path, name: str) -> None: ...
    def get_manifest(self) -> dict[str, Any] | None: ...
    def put_manifest(self, manifest: dict[str, Any]) -> None: ...
    def delete(self, names: list[str]) -> None: ...


class DirStore:
    """A local directory with the bucket's layout: for tests and local runs."""

    def __init__(self, root: Path, prefix: str) -> None:
        self.base = root / prefix

    def _path(self, name: str) -> Path:
        return self.base / DUMPS / name

    def list_dumps(self) -> list[str]:
        folder = self.base / DUMPS
        if not self.base.parent.is_dir():
            raise StoreUnavailable(f"no such directory: {self.base.parent}")
        return sorted(p.name for p in folder.glob("*.dump")) if folder.is_dir() else []

    def get(self, name: str, dest: Path) -> None:
        shutil.copyfile(self._path(name), dest)

    def put(self, src: Path, name: str) -> None:
        self._path(name).parent.mkdir(parents=True, exist_ok=True)
        partial = self._path(name).with_suffix(".partial")
        shutil.copyfile(src, partial)
        partial.replace(self._path(name))

    def get_manifest(self) -> dict[str, Any] | None:
        path = self.base / MANIFEST
        if not self.base.parent.is_dir():
            raise StoreUnavailable(f"no such directory: {self.base.parent}")
        return json.loads(path.read_text()) if path.is_file() else None

    def put_manifest(self, manifest: dict[str, Any]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        partial = self.base / f"{MANIFEST}.partial"
        partial.write_text(json.dumps(manifest, indent=2))
        partial.replace(self.base / MANIFEST)

    def delete(self, names: list[str]) -> None:
        for name in names:
            self._path(name).unlink(missing_ok=True)


class BucketStore:
    """A private HF Storage Bucket (huggingface_hub bucket API). Errors become StoreUnavailable."""

    def __init__(self, bucket: str, prefix: str, token: str | None) -> None:
        from huggingface_hub import HfApi

        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.api = HfApi(token=token or False)

    def _key(self, *parts: str) -> str:
        return "/".join((self.prefix, *parts))

    def _call(self, what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # network, auth, missing bucket: the reason only, never the request
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise StoreUnavailable(
                f"{what}: {type(exc).__name__}{f' (HTTP {status})' if status else ''}"
            ) from None

    def list_dumps(self) -> list[str]:
        prefix = self._key(DUMPS) + "/"
        entries = self._call(
            "list", lambda: list(self.api.list_bucket_tree(self.bucket, prefix, recursive=True))
        )
        names = (str(getattr(e, "path", "")).rsplit("/", 1)[-1] for e in entries)
        return sorted(n for n in names if DUMP_NAME.match(n))

    def get(self, name: str, dest: Path) -> None:
        files = [(self._key(DUMPS, name), str(dest))]
        self._call(
            "download", self.api.download_bucket_files, self.bucket, files, raise_on_missing_files=True
        )

    def put(self, src: Path, name: str) -> None:
        self._call(
            "upload", self.api.batch_bucket_files, self.bucket, add=[(str(src), self._key(DUMPS, name))]
        )

    def get_manifest(self) -> dict[str, Any] | None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / MANIFEST
            files = [(self._key(MANIFEST), str(dest))]
            self._call("download manifest", self.api.download_bucket_files, self.bucket, files)
            return json.loads(dest.read_text()) if dest.is_file() else None

    def put_manifest(self, manifest: dict[str, Any]) -> None:
        body = json.dumps(manifest, indent=2).encode()
        self._call(
            "upload manifest", self.api.batch_bucket_files, self.bucket, add=[(body, self._key(MANIFEST))]
        )

    def delete(self, names: list[str]) -> None:
        if names:
            keys = [self._key(DUMPS, n) for n in names]
            self._call("delete", self.api.batch_bucket_files, self.bucket, delete=keys)


def store_from_env() -> Store | None:
    bucket = os.environ.get("AIVA_BACKUP_BUCKET", "").strip()
    prefix = os.environ.get("AIVA_BACKUP_PREFIX", "gateway").strip("/") or "gateway"
    if not bucket:
        return None
    if bucket.startswith("file:"):
        return DirStore(Path(bucket.removeprefix("file:")), prefix)
    return BucketStore(bucket, prefix, os.environ.get("AIVA_BACKUP_HF_TOKEN"))


# --- Postgres --------------------------------------------------------------------------------------------


def database() -> str:
    return os.environ.get("AIVA_BACKUP_DATABASE", "aiva")


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    # libpq reads host, port, user and password from the environment (PGHOST, PGPASSFILE, ...).
    return subprocess.run(args, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False, **kwargs)  # noqa: S603


def psql(sql: str) -> str:
    result = run(["psql", "-X", "-d", database(), "-tAc", sql])
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.decode(errors='replace').strip()[:200]}")
    return result.stdout.decode().strip()


def database_is_empty() -> bool:
    return psql("select count(*) from pg_tables where schemaname = 'public'") == "0"


def reset_database() -> None:
    """Drop and recreate the (just restored, unusable) database; gateway.sh created it the same way."""
    for args in (["dropdb", "--if-exists", database()], ["createdb", database()]):
        result = run(args)
        if result.returncode != 0:
            raise RuntimeError(f"{args[0]} failed: {result.stderr.decode(errors='replace').strip()[:200]}")


def dump_to(path: Path) -> None:
    result = run(
        ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "-d", database(), "-f", str(path)]
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()[:200]}")


def content_fingerprint(dump: Path) -> str:
    """sha256 of the dump as SQL, without pg_dump's per-run restrict key and the timer-rewritten tables."""
    result = run(["pg_restore", "-f", "-", str(dump)])
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore -f - failed: {result.stderr.decode(errors='replace').strip()[:200]}")
    digest = hashlib.sha256()
    skipping = False
    for line in result.stdout.decode(errors="replace").splitlines(keepends=True):
        if skipping:
            skipping = line != "\\.\n"
            continue
        if line.startswith(("\\restrict ", "\\unrestrict ")):
            continue
        if line.startswith("COPY ") and line[5:].startswith(IGNORED_FOR_CHANGES):
            skipping = True
            continue
        digest.update(line.encode())
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def known_revision(revision: str) -> bool:
    """Whether this image's migrations contain `revision` (else `migrate` could not run forward from it)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from aiva_gateway.config import REPO_ROOT

    ini = os.environ.get("AIVA_ALEMBIC_INI") or str(REPO_ROOT / "gateway" / "alembic.ini")
    try:
        return ScriptDirectory.from_config(Config(ini)).get_revision(revision) is not None
    except Exception:
        return False


def restored_revision() -> str | None:
    try:
        return psql("select version_num from alembic_version limit 1") or None
    except RuntimeError:
        return None


# --- Commands --------------------------------------------------------------------------------------------


def candidates(store: Store) -> Iterator[tuple[str, str | None]]:
    """Dumps to try, best first: the manifest's (with its checksum), then the others, newest first."""
    manifest = store.get_manifest()
    seen: set[str] = set()
    if manifest and DUMP_NAME.match(str(manifest.get("dump", ""))):
        seen.add(manifest["dump"])
        yield manifest["dump"], manifest.get("sha256")
    for name in reversed(store.list_dumps()):
        if name not in seen:
            yield name, None


def restore(store: Store, run_dir: Path) -> bool:
    """Restore the newest good dump into the empty database. True when one was restored."""
    if not database_is_empty():
        log("the database already has tables; nothing restored")
        (run_dir / BACKUP_OK).write_text("existing database\n")
        return False
    tried = 0
    with tempfile.TemporaryDirectory(dir=run_dir) as tmp:
        dest = Path(tmp) / "restore.dump"
        for name, expected in candidates(store):
            tried += 1
            store.get(name, dest)
            if expected and sha256_file(dest) != expected:
                log(f"{name}: checksum mismatch; trying an older dump")
                continue
            result = run(
                [
                    "pg_restore",
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "-d",
                    database(),
                    str(dest),
                ]
            )
            if result.returncode != 0:
                log(f"{name}: pg_restore failed; trying an older dump")
                continue
            revision = restored_revision()
            if revision is not None and not known_revision(revision):
                log(f"{name}: schema revision {revision} is unknown to this image; trying an older dump")
                reset_database()
                continue
            (run_dir / "fresh-database").unlink(missing_ok=True)
            (run_dir / "restored").write_text(name + "\n")
            (run_dir / BACKUP_OK).write_text(f"restored {name}\n")
            log(f"restored {name} (revision {revision}); boot traffic will be skipped")
            return True
    if tried:
        log(
            f"none of {tried} dump(s) could be restored; starting on a fresh database, and this container "
            "will not back up, so the bucket's dumps are kept"
        )
        return False
    (run_dir / BACKUP_OK).write_text("empty bucket\n")
    log("no dump in the bucket yet; starting on a fresh database")
    return False


def backup(store: Store, run_dir: Path, *, keep: int, last: dict[str, Any]) -> bool:
    """One dump, uploaded only if its content changed since `last` (the manifest). True when uploaded."""
    with tempfile.TemporaryDirectory(dir=run_dir) as tmp:
        dump = Path(tmp) / "backup.dump"
        dump_to(dump)
        fingerprint = content_fingerprint(dump)
        if fingerprint == last.get("content_sha256"):
            return False
        checksum = sha256_file(dump)
        name = f"aiva-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{checksum[:8]}.dump"
        store.put(dump, name)
        manifest = {
            "dump": name,
            "sha256": checksum,
            "bytes": dump.stat().st_size,
            "content_sha256": fingerprint,
            "alembic_revision": restored_revision(),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        store.put_manifest(manifest)
        last.clear()
        last.update(manifest)
    dumps = store.list_dumps()  # oldest first: names start with their UTC time
    kept = set(dumps[-keep:]) | {name}
    old = [n for n in dumps if n not in kept]
    store.delete(old)
    log(f"backed up {name} ({manifest['bytes']} bytes); {len(old)} older dump(s) removed")
    return True


def loop(
    store: Store, run_dir: Path, *, interval: float, keep: int, stop: threading.Event | None = None
) -> None:
    """A check right after boot, then one every `interval` seconds, and a final one when stopped."""
    if stop is None:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not (run_dir / "boot.done").is_file():
        if stop.wait(5):
            return  # stopped before boot finished: nothing new to keep
    if not (run_dir / BACKUP_OK).is_file():
        log(
            "backups are off for this container: its boot restore neither restored a dump nor found the "
            "bucket empty (unreachable, or every dump refused), so its database must not replace the "
            "bucket's dumps"
        )
        stop.wait()
        return
    last = safely("read manifest", store.get_manifest) or {}
    while True:
        # Read before the dump: a stop that lands while this backup runs (after its pg_dump) gets one more
        # check, so a write made in between is not lost. An unchanged database uploads nothing on it.
        stopping = stop.is_set()
        safely("backup", lambda: backup(store, run_dir, keep=keep, last=last))
        if stopping:
            log("stopped; final backup done")
            return
        stop.wait(interval)  # a stop during the wait still gets the final check above


def safely(what: str, fn: Any) -> Any:
    try:
        return fn()
    except StoreUnavailable as exc:
        log(f"bucket unavailable ({exc}); {what} skipped")
    except Exception as exc:
        log(f"{what} failed: {type(exc).__name__}: {str(exc)[:200]}")
    return None


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    command = args[0] if args else ""
    if command not in {"restore", "backup", "loop"}:
        print(__doc__, file=sys.stderr)
        return 2
    store = store_from_env()
    run_dir = Path(os.environ.get("AIVA_RUN_DIR", "/tmp/aiva-run"))  # noqa: S108 (entrypoint.sh default)
    keep = max(int(os.environ.get("AIVA_BACKUP_KEEP", "3")), 1)
    if store is None:
        log("AIVA_BACKUP_BUCKET is not set; backups are off")
        if command == "loop":
            idle()  # supervisord keeps a program running; nothing to do until it stops us
        return 0
    if command == "restore":
        safely("restore", lambda: restore(store, run_dir))
    elif command == "backup":
        safely("backup", lambda: backup(store, run_dir, keep=keep, last=store.get_manifest() or {}))
    else:
        loop(store, run_dir, interval=float(os.environ.get("AIVA_BACKUP_INTERVAL_SECONDS", "60")), keep=keep)
    return 0


def idle() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.wait(3600):
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
