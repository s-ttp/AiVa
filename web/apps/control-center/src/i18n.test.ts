import { describe, expect, it } from 'vitest';
import { AREA_DICTIONARIES } from './i18n';

/** Key prefixes reserved for an area's own module; the core dictionary owns every other key. */
const AREA_PREFIXES: Record<Exclude<keyof typeof AREA_DICTIONARIES, 'core'>, readonly string[]> = {
  govern: [
    'admin.', 'applications.', 'approvals.', 'budgetAlerts.', 'budgets.', 'changes.', 'decision.', 'draft.',
    'enforcedBy.', 'keys.', 'models.', 'networks.', 'policies.', 'policyEnforce.', 'policyMatch.', 'publish.',
    'simulate.', 'tools.',
  ],
  insights: ['insights.', 'overview.'],
  console: ['artifacts.', 'agents.'],
  optimize: ['registry.'],
  quality: ['quality.'],
};

function ownerOf(key: string): keyof typeof AREA_DICTIONARIES {
  for (const [area, prefixes] of Object.entries(AREA_PREFIXES)) {
    if (prefixes.some((prefix) => key.startsWith(prefix))) return area as keyof typeof AREA_PREFIXES;
  }
  return 'core';
}

describe('Control Center copy', () => {
  it('defines every key in exactly one area module', () => {
    const areas = new Map<string, string[]>();
    for (const [area, dictionary] of Object.entries(AREA_DICTIONARIES)) {
      for (const key of Object.keys(dictionary)) areas.set(key, [...(areas.get(key) ?? []), area]);
    }
    expect([...areas].filter(([, owners]) => owners.length > 1)).toEqual([]);
  });

  it('keeps each key in the module of the area that owns its prefix', () => {
    const misplaced = Object.entries(AREA_DICTIONARIES).flatMap(([area, dictionary]) =>
      Object.keys(dictionary)
        .filter((key) => ownerOf(key) !== area)
        .map((key) => `${key} (in ${area}, owned by ${ownerOf(key)})`),
    );
    expect(misplaced).toEqual([]);
  });
});
