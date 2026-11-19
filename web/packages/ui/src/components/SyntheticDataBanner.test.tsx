import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SyntheticDataBanner } from './SyntheticDataBanner';

describe('SyntheticDataBanner', () => {
  it('labels synthetic illustration by default', () => {
    render(<SyntheticDataBanner />);
    expect(screen.getByRole('note', { name: 'Synthetic demo data · Illustrative UI' })).toBeVisible();
  });

  it('labels measured execution', () => {
    render(<SyntheticDataBanner mode="measured" />);
    expect(screen.getByRole('note', { name: 'Measured execution' })).toBeVisible();
  });
});
