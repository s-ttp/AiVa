import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PROVENANCES, ProvenanceBadge, StatusBadge, type Status } from './badges';

describe('ProvenanceBadge', () => {
  it.each([
    ['measured', 'Measured'],
    ['estimated', 'Estimated'],
    ['simulated', 'Simulated'],
    ['replayed', 'Replayed'],
    ['synthetic', 'Synthetic'],
  ] as const)('labels %s values with text and an icon', (provenance, label) => {
    const { container } = render(<ProvenanceBadge provenance={provenance} />);

    expect(screen.getByText(label)).toBeVisible();
    expect(container.firstElementChild).toHaveTextContent(`Data provenance: ${label}`);
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });

  it('covers every provenance value', () => {
    expect(PROVENANCES).toEqual(['measured', 'estimated', 'simulated', 'replayed', 'synthetic']);
  });
});

describe('StatusBadge', () => {
  it.each<[Status, string]>([
    ['allowed', 'Allowed by policy'],
    ['blocked', 'Blocked by AI policy'],
    ['sovereignDirect', 'Sovereign direct route required'],
    ['approvalRequired', 'Approval required'],
    ['cimActive', 'Context package generated; sufficiency verified'],
    ['cimFailed', 'Context sufficiency check failed; safe fallback applied'],
  ])('uses the LLD §22.8 copy for %s', (status, copy) => {
    const { container } = render(<StatusBadge status={status} />);

    expect(screen.getByText(copy)).toBeVisible();
    expect(container.querySelector('svg')).not.toBeNull();
  });
});
