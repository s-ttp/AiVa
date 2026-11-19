import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

/**
 * End-to-end tests of the Control Center against a local AiVa gateway.
 *
 * Prerequisites (see e2e/README.md): Postgres, Redis and OPA running, the
 * database migrated and seeded (`bash scripts/dev-services.sh && make migrate seed`).
 * Playwright starts the gateway with dev and passcode sign-in and CORS for the
 * UI unless one is already listening, and serves the Control Center with Vite.
 */
const repoRoot = fileURLToPath(new URL('../../../', import.meta.url));
const uiPort = Number(process.env.AIVA_E2E_UI_PORT ?? 4173);
const uiUrl = `http://localhost:${uiPort}`;
export const gatewayUrl = process.env.AIVA_E2E_GATEWAY_URL ?? 'http://localhost:8000';
const gatewayPort = new URL(gatewayUrl).port || '8000';
/** A synthetic passcode for the gateway Playwright starts; not a secret (ADR-0019 passcode sign-in). */
export const demoPasscode = process.env.AIVA_E2E_DEMO_PASSCODE ?? 'synthetic-e2e-passcode-0001';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  timeout: 60_000,
  use: {
    baseURL: uiUrl,
    trace: 'retain-on-failure',
    viewport: { width: 1280, height: 900 },
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
      : {},
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 900 } } }],
  webServer: [
    {
      command: `uv run uvicorn --factory aiva_gateway.main:create_app --no-proxy-headers --port ${gatewayPort}`,
      cwd: repoRoot,
      url: `${gatewayUrl}/health/ready`,
      reuseExistingServer: true,
      timeout: 60_000,
      env: {
        AIVA_DEV_SIGN_IN: 'true',
        AIVA_DEMO_PASSCODE: demoPasscode,
        AIVA_CORS_ORIGINS: JSON.stringify([uiUrl]),
        // Room for every visitor journey in one run; honoured only on a local or test gateway (#147).
        AIVA_PUBLIC_SESSIONS_PER_SOURCE: '50',
      },
    },
    {
      command: `pnpm exec vite --port ${uiPort} --strictPort`,
      url: uiUrl,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
