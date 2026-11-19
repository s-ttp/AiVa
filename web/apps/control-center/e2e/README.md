# Control Center end-to-end tests

Playwright tests of the AiVa Test Console, Admin Dashboard and AiVa Govern views against a **local gateway**
(simulated inference, synthetic data). Every new view is also scanned with axe for WCAG 2.2 AA (`e2e/support.ts`),
and the run workspace is checked for reflow at 320 CSS pixels.

```bash
bash scripts/dev-services.sh          # Postgres, Redis, OPA
make migrate seed                     # schema, demo users and the policy bundle
pnpm -C web/apps/control-center exec playwright install chromium   # once
pnpm -C web/apps/control-center e2e
```

Playwright starts the gateway (`AIVA_DEV_SIGN_IN=true`, a synthetic `AIVA_DEMO_PASSCODE`, CORS for the UI
origin) unless one already listens on `AIVA_E2E_GATEWAY_URL` (default `http://localhost:8000`), and serves the
Control Center with Vite on `AIVA_E2E_UI_PORT` (default 4173). A gateway you start yourself needs the same
settings, with the passcode in `AIVA_E2E_DEMO_PASSCODE` too:

```bash
AIVA_DEV_SIGN_IN=true AIVA_DEMO_PASSCODE=synthetic-e2e-passcode-0001 AIVA_PUBLIC_SESSIONS_PER_SOURCE=50 \
  AIVA_CORS_ORIGINS='["http://localhost:4173"]' make dev
```

`sign-in.spec.ts` covers the public demo's sign-ins (ADR-0019): a visitor session, the passcode sign-in after a
wrong passcode, and the "demo is waking up" state, for which it replays the platform proxy's `GATEWAY_STARTING`
answer. The gateway Playwright starts issues 50 visitor sessions per hour per source
(`AIVA_PUBLIC_SESSIONS_PER_SOURCE`, a setting a local or test gateway honours and any other refuses), enough for
every visitor journey in a run; a gateway you start yourself issues 5 unless you set it too. The passcode locks
after 5 failures. Clear both with `redis-cli --scan --pattern 'aiva:public-session:*' | xargs -r redis-cli del`
(and `'aiva:demo-sign-in:*'`) if you rerun the suite often.

To use an already installed Chromium instead of `playwright install` (e.g. cloud sessions), set
`PLAYWRIGHT_CHROMIUM_PATH=/opt/pw-browsers/chromium`.

`govern.spec.ts` publishes a policy version with an approval rule for a tag unique to the run, takes a tagged
request through the Approval Queue (approve and resume, deny) and rolls back to the version that was published
before it started; an `afterAll` hook restores that version if a test fails midway. Drafts it creates stay in
the version history, unenforced.

The tests compare what the UI shows with the gateway's own response (for example, a provenance badge must
match the provenance the gateway reported), so they stay valid when inference switches from simulated to live.

`insights.spec.ts` first runs `uv run aiva-admin traffic` (synthetic runs through the pipeline, simulated inference)
against the same database as the gateway, then checks every AiVa Insights view with axe, the enterprise → trace
drilldown, a department manager's scope (UI and direct API) and a CSV export download.
