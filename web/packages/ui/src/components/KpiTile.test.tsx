import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { KpiTile } from './KpiTile';

const detail = {
  source: 'Gateway usage ledger',
  calculation: 'Sum of provider-reported input and output tokens',
  window: 'Last 7 days',
};

describe('KpiTile', () => {
  it('shows an explicit empty value instead of a number when there is no data', () => {
    render(<KpiTile label="Total tokens" value={null} provenance="measured" detail={detail} />);

    const tile = screen.getByRole('group', { name: 'Total tokens' });
    expect(tile).toHaveTextContent('No data yet');
    expect(tile).toHaveTextContent('Measured');
  });

  it('discloses data provenance on demand', async () => {
    render(<KpiTile label="Estimated local GPU cost" value="$12.40" provenance="estimated" detail={detail} />);

    expect(screen.getByText('$12.40')).toHaveAttribute('dir', 'ltr');
    expect(screen.getByText('Gateway usage ledger')).not.toBeVisible();

    await userEvent.click(screen.getByText('Data provenance'));

    expect(screen.getByText('Gateway usage ledger')).toBeVisible();
    expect(screen.getByText('Last 7 days')).toBeVisible();
    expect(screen.queryByText('Pricing version')).not.toBeInTheDocument();
  });
});
