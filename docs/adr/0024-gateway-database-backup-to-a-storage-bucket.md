# ADR-0024: Space 2's database survives restarts through a private Storage Bucket

**Status.** Accepted. The owner approved it on 2026-10-22: keep Space 2's database across restarts, with a
backup to a private HF Storage Bucket that is restored on boot.

**Owners.** Deploy & Hardening: `deploy/spaces/gateway/` (`db_backup.py`, `gateway.sh`, `supervisord.conf`),
`deploy/scripts/push_spaces.py`, the runbook.

**Context.** Space 2 (`gct-rlcd-gateway`, cpu-upgrade) runs Postgres inside the container on an ephemeral
disk, and sleeps after 2 h idle. Every wake, restart or rebuild therefore starts an empty cluster:
- boot migrates, seeds, ingests the knowledge base and generates 60 synthetic runs;
- runs, ledger, audit log, approvals and Insights history from before are gone;
- the "What AiVa did this week" page only ever shows data since the last start.

Two ways to keep the data:
- HF persistent storage, a paid add-on mounted at `/data`;
- a Storage Bucket, either mounted as a volume or used through the Hub API.

Running Postgres on a bucket mount is unsafe: object storage has no fsync or rename guarantees for a live
cluster. The database is a few MB.

**Decision.**
1. **Dump and restore, not a live mount.** `db_backup.py` writes custom-format `pg_dump`s to the private bucket
   `s-ttp/aiva-gateway-backups` through `huggingface_hub`'s bucket API. Variable `AIVA_BACKUP_BUCKET`; secret
   `AIVA_BACKUP_HF_TOKEN`, a fine-grained token with read and write access to that bucket only.
2. **Restore on boot, before `migrate`.**
   - Restore runs only into an empty database, in one transaction.
   - The dump must match `latest.json`'s sha256, and its Alembic revision must be known to the image. Failing
     either, the restore tries older dumps, then gives up and boots fresh.
   - `migrate` then runs forward, and `seed` stays idempotent.
   - A restored database is not fresh, so the synthetic boot traffic is skipped.
3. **Back up while running.**
   - A supervisord program dumps every 15 minutes, only when the data changed, and once more on SIGTERM. Its
     priority makes supervisord stop it before Postgres.
   - "Changed" ignores the tables the gateway rewrites on a timer: Insights aggregates and the
     reconciliation log.
   - Each dump goes up under a new name, and only then is `latest.json` rewritten. A half-written upload never
     replaces the only good dump.
   - The 3 newest dumps are kept.
4. **Never block the gateway.** No bucket configured, an unreachable bucket, or no usable dump: the gateway
   logs it and boots on a fresh database, as before. The backup program can never stop the container.

**Consequences.**
- Runs, ledger, audit log, approvals and Insights history survive sleeps and restarts.
- At most one backup interval is lost when a container dies without its final dump (killed, or a platform
  stop shorter than the dump and upload).
- `usage_ledger` and `audit_log` stay append-only. A restore never writes over existing rows, and their
  guard triggers return with the schema.
- The dumps hold the same data as the database: synthetic and pseudonymous, no prompt text or secrets
  (LLD §17). The bucket is private anyway.
- Cost: a few MB in the bucket's free allowance (about $0). HF's paid persistent storage stays off.
- Redis still starts empty; quota counters are rebuilt from the ledger by reconciliation (ADR-0008).
- Deploying an image older than the backup's schema refuses that dump and starts fresh, rather than failing
  `migrate`.
- To start fresh on purpose, delete the bucket's `gateway/` folder (runbook §4).

**Amendment (2026-10-22, after the first live restart).**
- **What the restart showed.** HF starts the new container before it stops the old one. The new container
  therefore restored before the old one's final dump, which itself succeeded. A 15-minute interval left a
  short-lived container with no dump at all.
- **Checks.** The backup now checks right after boot and then every 60 s (`AIVA_BACKUP_INTERVAL_SECONDS`). It
  uploads only when the data changed, plus the final dump on stop.
  - The check is local (`pg_dump` and a fingerprint).
  - Only a change calls the Hub: about 10 calls per upload. A continuously busy demo stays near 2% of a PRO
    account's 5-minute API window, and an idle gateway makes none.
- **Guard.** Only a container whose boot restore restored a dump, confirmed the bucket empty, or found an
  existing database backs up (`backup-ok` in its run directory). A container that started fresh because the
  bucket was unreachable, or because every dump was refused, never uploads. Its empty database cannot replace
  the bucket's history and then age it out of the rolling copies.
- **Bounded loss.** A sleep loses nothing. A crash loses at most a minute. A restart or redeploy loses what the
  old container wrote between the new container's restore and the switch-over: about 1.5 to 2 minutes.
- A handover, where the new container waits for the old one's final dump, was considered and not built. The
  owner does not need restarts during live use to be lossless.
