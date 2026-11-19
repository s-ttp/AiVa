import { describe, expect, it } from 'vitest';
import type { Scenario } from '../api/client';
import parity from '../test/fixtures/run/replayParity.json';
import scenarios from '../test/fixtures/scenarios.json';
import { DEFAULT_SIMULATED_IP, draftFor, toChatRequest } from './draft';

const fraud = scenarios.find((scenario) => scenario.id === 'fraud-management') as Scenario;

describe('toChatRequest', () => {
  it('always traces and runs as a simulated Test Console identity', () => {
    expect(toChatRequest(draftFor(fraud))).toEqual({
      messages: [{ role: 'user', content: fraud.sample_prompt }],
      routing: { mode: 'hybrid', tags: ['fraud-investigation'] },
      data_classification: 'confidential',
      quality_tier: 'standard',
      max_output_tokens: 1024,
      scenario_id: 'fraud-management',
      baseline_comparison: false,
      trace: true,
      test_profile: { application_id: 'gct-fraud-investigator', simulated_source_ip: DEFAULT_SIMULATED_IP },
    });
  });

  it('sends an undeclared classification, no scenario and the observed source as null', () => {
    const draft = { ...draftFor(undefined), applicationId: 'gct-support-chatbot', simulatedSourceIp: '  ', tags: ' a, ,b ' };

    expect(toChatRequest(draft)).toMatchObject({
      data_classification: null,
      scenario_id: null,
      routing: { tags: ['a', 'b'] },
      test_profile: { application_id: 'gct-support-chatbot', simulated_source_ip: null },
    });
  });

  it('adds a scenario’s sample evidence after the test profile, item by item, and nothing otherwise', () => {
    const evidence = [
      { id: 'alarm-ALM-0001', text: 'Synthetic alarm ALM-0001: site DXB-017, LINK_DOWN, major.' },
      { id: 'alarm-ALM-0002', text: 'Synthetic alarm ALM-0002: site DXB-017, HIGH_BER, minor.' },
    ];
    const body = toChatRequest(draftFor({ ...fraud, sample_evidence: evidence }));

    expect(body.evidence).toEqual(evidence);
    expect(Object.keys(body).slice(-2)).toEqual(['test_profile', 'evidence']);
    expect(toChatRequest(draftFor(fraud))).not.toHaveProperty('evidence');
    expect(toChatRequest(draftFor(undefined))).not.toHaveProperty('evidence');
  });

  it('keeps tester settings when switching scenario', () => {
    const custom = { ...draftFor(fraud), simulatedSourceIp: '198.51.100.7', baselineComparison: true };
    const next = draftFor(scenarios[0] as Scenario, custom);

    expect(next).toMatchObject({ simulatedSourceIp: '198.51.100.7', baselineComparison: true, scenarioId: 'customer-support' });
  });

  it('takes the scenario’s quality tier and output budget, also when switching from an edited draft', () => {
    const coding = { ...(scenarios[1] as Scenario), quality_tier: 'critical' as const, max_output_tokens: 6144 };
    const edited = { ...draftFor(fraud), qualityTier: 'high' as const, maxOutputTokens: 300 };

    expect(toChatRequest(draftFor(coding))).toMatchObject({ quality_tier: 'critical', max_output_tokens: 6144 });
    expect(draftFor(coding, edited)).toMatchObject({ qualityTier: 'critical', maxOutputTokens: 6144 });
    expect(draftFor(fraud, draftFor(coding))).toMatchObject({ qualityTier: 'standard', maxOutputTokens: 1024 });
    expect(draftFor(undefined, edited)).toMatchObject({ qualityTier: 'high', maxOutputTokens: 300 });
  });
});

/**
 * The public replay matches a run by the upstream request the gateway builds, so the run form must send
 * exactly the body the replay was recorded with (`tests/e2e/scenario_requests.py::chat_body`, ADR-0019).
 * Regenerate the fixture with `scripts/replay_parity_fixture.py` after changing either builder or the catalog.
 */
describe('replay parity', () => {
  const catalog = parity.scenarios as unknown as readonly Scenario[];

  it.each(Object.entries(parity.chat_bodies))('%s: the default draft sends the recorded body', (id, recorded) => {
    const scenario = catalog.find((candidate) => candidate.id === id);

    expect(scenario).toBeDefined();
    expect(JSON.stringify(toChatRequest(draftFor(scenario)))).toBe(JSON.stringify(recorded));
  });

});
