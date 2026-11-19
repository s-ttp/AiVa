import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import models from '../test/fixtures/govern/adminModels.json';
import switchedOff from '../test/fixtures/govern/modelSwitchOff.json';
import sovereignOff from '../test/fixtures/govern/modelSwitchSovereign.json';
import { errorEnvelope, json, signedInAs, type DemoUserId, type Route } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';

const SWITCH = 'POST /v1/admin/policies/model-switch';

/** The allowlist as the gateway shows it to a caller who may (or may not) draft switches. */
function allowlist(user: DemoUserId, canSwitch: boolean, routes: Record<string, Route | unknown> = {}, overrides = {}) {
  const gateway = signedInAs(user, {
    'GET /v1/admin/models': { ...models, viewer_can_switch: canSwitch, ...overrides },
    ...routes,
  });
  renderRoute('/admin/govern/model-allowlist');
  return gateway;
}

const table = async () => within(await screen.findByRole('table', { name: 'Models and the applications granted them' }));
const sentSwitch = async (gateway: { requests: Request[] }) =>
  gateway.requests.find((request) => request.method === 'POST' && request.url.endsWith('/v1/admin/policies/model-switch'))?.json();

describe('model switches', () => {
  it('drafts switching the sovereign model off, and shows the gateway’s own warning before anything is published', async () => {
    const gateway = allowlist('u-platform-admin', true, { [SWITCH]: json(sovereignOff, 201) });
    expect(
      await screen.findByText(/Switching a model off or back on for an application drafts a new policy version\./),
    ).toBeVisible();

    await userEvent.click((await table()).getByRole('button', { name: 'Draft switching GCT Sovereign Qwen off for gct-fraud-investigator' }));
    expect(await sentSwitch(gateway)).toEqual({ application_id: 'gct-fraud-investigator', model_id: 'gct-qwen-finetuned', enabled: false });

    const draft = within(
      await screen.findByRole('region', { name: 'Draft version 153: GCT Sovereign Qwen switched off for gct-fraud-investigator' }),
    );
    expect(draft.getByRole('heading', { level: 2 })).toHaveFocus();
    expect(draft.getByText('Not live yet: published version 148 still applies until this draft is tested and published.')).toBeVisible();
    // The warning is the gateway's text, word for word.
    const warnings = within(draft.getByRole('region', { name: 'Before you publish' }));
    expect(warnings.getByText(sovereignOff.warnings[0] ?? '')).toBeVisible();

    const checks = within(draft.getByRole('table', { name: 'What the draft changes, by classification' }));
    const confidential = checks.getByRole('row', { name: /^Confidential/ });
    expect(confidential).toHaveTextContent('Sovereign direct');
    expect(confidential).toHaveTextContent('gct-qwen-finetuned');
    expect(confidential).toHaveTextContent('No model');
    expect(confidential).toHaveTextContent('Blocked by AI policy');
    expect(draft.getByRole('link', { name: 'Test and publish version 153' })).toHaveAttribute(
      'href',
      '/admin/govern/policy-rules?version=153',
    );
  });

  it('drafts an ordinary switch-off without a warning, and shows which models each classification keeps', async () => {
    allowlist('u-platform-admin', true, { [SWITCH]: json(switchedOff, 201) });
    await userEvent.click((await table()).getByRole('button', { name: 'Draft switching Claude Opus 5.5 off for gct-coding-assistant' }));

    const draft = within(await screen.findByRole('region', { name: /^Draft version 154: Claude Opus 5\.5 switched off/ }));
    expect(draft.queryByRole('region', { name: 'Before you publish' })).not.toBeInTheDocument();
    const internal = within(draft.getByRole('table', { name: 'What the draft changes, by classification' })).getByRole('row', {
      name: /^Internal/,
    });
    expect(internal).toHaveTextContent('Routed by RLCD');
    expect(internal).toHaveTextContent('Model available');
  });

  it('shows an application switched off in the published version, with a draft to switch it back on', async () => {
    const off = {
      models: models.models.map((model) =>
        model.id === 'claude-flagship' ? { ...model, switched_off_application_ids: ['gct-coding-assistant'] } : model,
      ),
    };
    const gateway = allowlist('u-platform-admin', true, { [SWITCH]: json({ ...switchedOff, enabled: true, high_impact: true }, 201) }, off);
    const row = (await table()).getByRole('row', { name: /^Claude Opus 5\.5/ });
    const entry = within(row).getByRole('button', { name: 'Draft switching Claude Opus 5.5 back on for gct-coding-assistant' });
    expect(entry.closest('li')).toHaveTextContent('Switched off');

    await userEvent.click(entry);
    expect(await sentSwitch(gateway)).toMatchObject({ enabled: true });
    expect(
      await screen.findByText('This draft widens access, so a different administrator must publish it.'),
    ).toBeVisible();
  });

  it('shows the gateway’s refusal', async () => {
    allowlist('u-platform-admin', true, {
      [SWITCH]: errorEnvelope(409, 'MODEL_SWITCH_UNCHANGED', 'claude-flagship is already on for gct-coding-assistant in the published version.'),
    });
    await userEvent.click((await table()).getByRole('button', { name: 'Draft switching Claude Opus 5.5 off for gct-coding-assistant' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The draft could not be created');
    expect(alert).toHaveTextContent('claude-flagship is already on for gct-coding-assistant in the published version.');
  });

  it('offers no switches to a caller who may not draft them', async () => {
    allowlist('u-auditor', false);
    const models = await table();
    expect(models.getAllByText('On').length).toBeGreaterThan(0);
    expect(models.queryByRole('button', { name: /^Draft switching/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/drafts a new policy version/)).not.toBeInTheDocument();
  });
});
