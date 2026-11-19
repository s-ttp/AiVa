import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Tabs } from './Tabs';

const items = [
  { id: 'one', label: 'Response Comparison', content: 'First panel' },
  { id: 'two', label: 'Routing Decision', content: 'Second panel' },
  { id: 'three', label: 'Savings Analysis', content: 'Third panel' },
];

describe('Tabs', () => {
  it('supports arrow, Home and End keys with a roving tab stop', async () => {
    const user = userEvent.setup();
    render(<Tabs label="Run workspace" items={items} />);

    expect(screen.getByRole('tablist', { name: 'Run workspace' })).toBeInTheDocument();
    await user.tab();
    expect(screen.getByRole('tab', { name: 'Response Comparison' })).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveTextContent('First panel');

    await user.keyboard('{ArrowRight}');
    const routing = screen.getByRole('tab', { name: 'Routing Decision' });
    expect(routing).toHaveFocus();
    expect(routing).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Second panel');

    await user.keyboard('{End}');
    expect(screen.getByRole('tab', { name: 'Savings Analysis' })).toHaveFocus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Response Comparison' })).toHaveFocus();
    await user.keyboard('{Home}{ArrowLeft}');
    expect(screen.getByRole('tab', { name: 'Savings Analysis' })).toHaveAttribute('tabindex', '0');
  });
});
