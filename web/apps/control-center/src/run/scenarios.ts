import type { Scenario } from '../api/client';

/**
 * The Test Console's scenarios: the catalog without the live demo's own (`live_demo_use_case`). Those run live
 * only, so a replay of them would miss; they are served through the API alone.
 */
export function consoleScenarios(scenarios: readonly Scenario[]): Scenario[] {
  return scenarios.filter((scenario) => !scenario.live_demo_use_case);
}
