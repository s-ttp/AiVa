import type { PolicyAction, PolicyTestCase, Scenario } from '../api/client';

export interface SimulationOptions {
  /** Comma-separated routing tags added to each scenario's own tags. */
  extraTags: string;
  sourceIp: string;
  expectedAction: PolicyAction | '';
}

export function parseTags(value: string): string[] {
  return value
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
}

/**
 * A policy test case for a catalog scenario, as the Test Console would send
 * it: a tester's simulated identity and source, the scenario's sample prompt,
 * declared classification and routing tags. The gateway runs DLP and
 * classification on it like a real request.
 */
export function caseForScenario(scenario: Scenario, options: SimulationOptions): PolicyTestCase {
  const tags = [...new Set([...scenario.routing_tags, ...parseTags(options.extraTags)])];
  return {
    name: `${scenario.number}. ${scenario.name}`,
    sample: {
      application_id: scenario.application_id,
      identity_mode: 'simulated',
      source_mode: 'simulated',
      source_ip: options.sourceIp.trim() || null,
      roles: ['tester'],
      messages: [{ role: 'user', content: scenario.sample_prompt }],
      data_classification: scenario.declared_classification,
      scenario_id: scenario.id,
      tags,
      routing_mode: 'hybrid',
      stage: 'request',
    },
    expected_action: options.expectedAction || null,
  };
}
