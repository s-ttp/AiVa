# Scenario recordings (the public replay set)

These are the recorded provider and endpoint exchanges that public replay runs answer from (ADR-0019,
ADR-0009). The gateway image copies this directory to `/app/deploy/recordings` and reads it through
`AIVA_RECORDINGS_DIR`. While the directory has no recordings, every replay is a miss and is answered by
the simulated adapter, labelled `simulated`.

- **Format.** One file per exchange, named `<label>-<fingerprint>.json`, as the gateway writes them in
  `record` mode. The fingerprint covers the method, the URL and the upstream request body that the
  gateway builds.
- **Bodies only.** A file holds the request (method, URL, JSON body) and the response (status, content
  type, body). Headers, API keys and tokens are never stored, and credentials in a query string are
  removed. Never edit a file by hand: a changed body no longer matches its name, and replay misses it.
- **Made by** the recording run in `deploy/DEPLOY_PLAN.md` step 5, on Space 2 itself, so the fingerprints
  match the deployed configuration. All content is synthetic.
- **Re-record** whenever any of these change: prompts, the knowledge base, model pins, CIM packaging, the
  RLCD model or the reranker. Any of these changes the upstream bodies, so the old recordings stop
  matching. Each re-recording is quoted to the owner first.
- **Checked** by `deploy/spaces/gateway/check_recordings.py`, which the image smoke test runs. It checks
  that every file parses, is named after its own fingerprint, and keeps no credential in its URL.
