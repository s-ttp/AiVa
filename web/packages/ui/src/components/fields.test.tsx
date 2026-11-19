import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CheckboxField, SelectField, TextAreaField, TextField } from './fields';

describe('form fields', () => {
  it('give every control a visible label and wire the hint as its description', () => {
    render(
      <>
        <TextField label="Simulated source IP" hint="Recorded as simulated." defaultValue="" />
        <TextAreaField label="Synthetic prompt" defaultValue="" />
        <SelectField label="Quality tier" hint="Standard by default." defaultValue="standard">
          <option value="standard">Standard</option>
        </SelectField>
        <CheckboxField label="Compare with the baseline" hint="Metered separately." />
      </>,
    );

    expect(screen.getByRole('textbox', { name: 'Simulated source IP' })).toHaveAccessibleDescription('Recorded as simulated.');
    expect(screen.getByRole('textbox', { name: 'Synthetic prompt' })).not.toHaveAttribute('aria-describedby');
    expect(screen.getByRole('combobox', { name: 'Quality tier' })).toHaveAccessibleDescription('Standard by default.');
    expect(screen.getByRole('checkbox', { name: 'Compare with the baseline' })).toHaveAccessibleDescription(
      'Metered separately.',
    );
  });
});
