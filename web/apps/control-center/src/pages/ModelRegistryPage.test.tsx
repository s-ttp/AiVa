import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import models from '../test/fixtures/govern/adminModels.json';
import { errorEnvelope, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const caption = 'Registered models, their pinned versions and residency';

describe('model registry', () => {
  it('lists each model with its provider, pinned ID and revision, residency, status and grants', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/models': models });
    renderRoute('/admin/optimize/model-registry');

    const table = await screen.findByRole('table', { name: caption });
    expect(within(table).getAllByRole('row')).toHaveLength(models.models.length + 1);
    const sovereign = within(table).getByRole('row', { name: /gct-qwen-finetuned/ });
    expect(sovereign).toHaveTextContent('Hugging Face Inference Endpoint');
    expect(sovereign).toHaveTextContent('Qwen/Qwen3-32B');
    expect(sovereign).toHaveTextContent('Revision 9216db5781bf21249d130ec9da846c4624c16137');
    expect(sovereign).toHaveTextContent('Sovereign');
    expect(sovereign).toHaveTextContent('Region aws-us-east-1');
    expect(sovereign).toHaveTextContent('Adapter gct-qwen-lora');
    expect(sovereign).toHaveTextContent('32,768 tokens');
    expect(sovereign).toHaveTextContent('Enabled');
    expect(sovereign).toHaveTextContent('7 applications');
    const external = within(table).getByRole('row', { name: /deepseek-v4-1-flash/ });
    expect(external).toHaveTextContent('Hugging Face Inference Providers');
    expect(external).toHaveTextContent('External provider');
    expect(external).toHaveTextContent('No region pinned');
    expect(external).toHaveTextContent('Provider route deepinfra');
    expect(external).toHaveTextContent('1,048,576 tokens');
    const cim = within(table).getByRole('row', { name: /enterprise-cim/ });
    expect(cim).toHaveTextContent('Enterprise CIM');
    expect(cim).toHaveTextContent('Internal');
    expect(cim).toHaveTextContent('Not granted to any application');
    expect(screen.getByRole('link', { name: `policy version ${models.policy_version}` })).toBeVisible();
    const grants = required(screen.getByText(/Per-application grants and the policies that narrow them/).closest('p'));
    expect(within(grants).getByRole('link', { name: 'Model Allowlist' })).toHaveAttribute('href', '/admin/govern/model-allowlist');
  });

  it('cites each pinned price by its rate card, with no provenance badge', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/models': models });
    renderRoute('/admin/optimize/model-registry');

    const prices = await screen.findByRole('table', { name: 'Pinned prices per model, in US dollars' });
    const claude = within(prices).getByRole('row', { name: /claude-flagship/ });
    expect(claude).toHaveTextContent('Input $4.00');
    expect(claude).toHaveTextContent('Output $20.00');
    expect(claude).toHaveTextContent('Cached input $0.20');
    expect(claude).toHaveTextContent('No GPU price');
    expect(claude).toHaveTextContent('rc-claude-opus-5-5');
    expect(claude).toHaveTextContent('Version rc-2026-10-14');
    expect(claude).toHaveTextContent('Effective from 2026-10-14');
    expect(claude).toHaveTextContent('Anthropic API list price for claude-opus-5-5');
    const qwen = within(prices).getByRole('row', { name: /gct-qwen-finetuned/ });
    expect(qwen).toHaveTextContent('$0 (GPU-served; GPU cost reported separately)');
    expect(qwen).toHaveTextContent('$1.80 per GPU hour');
    expect(within(prices).getByRole('row', { name: /gemini-flash-lite/ })).toHaveTextContent('Cached input $0.03');
    expect(prices).not.toHaveTextContent(/free|Measured|Estimated/i);
  });

  it('marks a model the catalog does not configure, with no pin and no price', async () => {
    const first = required(models.models[0]);
    const missing = {
      ...first,
      id: 'retired-model',
      display_name: 'retired-model',
      catalog_configured: false,
      provider: null,
      provider_model_id: null,
      revision: null,
      region: null,
      context_window: null,
      provider_route: null,
      adapter: null,
      rate_card: null,
    };
    signedInAs('u-auditor', { 'GET /v1/admin/models': { ...models, models: [missing] } });
    renderRoute('/admin/optimize/model-registry');

    const row = within(await screen.findByRole('table', { name: caption })).getByRole('row', { name: /retired-model/ });
    expect(row).toHaveTextContent('Not configured in the model catalog');
    expect(row).not.toHaveTextContent('Revision');
    expect(row).not.toHaveTextContent('tokens');
    const prices = screen.getByRole('table', { name: 'Pinned prices per model, in US dollars' });
    expect(within(prices).getByRole('row', { name: /retired-model/ })).toHaveTextContent('No rate card configured');
  });

  it('shows a disabled model and an unknown provider as sent', async () => {
    const first = required(models.models[0]);
    signedInAs('u-auditor', {
      'GET /v1/admin/models': { ...models, models: [{ ...first, provider: 'future-provider', enabled: false }] },
    });
    renderRoute('/admin/optimize/model-registry');

    const row = within(await screen.findByRole('table', { name: caption })).getByRole('row', { name: /claude-flagship/ });
    expect(row).toHaveTextContent('future-provider');
    expect(row).toHaveTextContent('Disabled');
  });

  it('says the registry is not configured when no policy version is published', async () => {
    signedInAs('u-auditor', {
      'GET /v1/admin/models': errorEnvelope(503, 'POLICY_BUNDLE_MISSING', 'No policy version is published.'),
    });
    renderRoute('/admin/optimize/model-registry');

    expect(await screen.findByRole('heading', { name: 'The model registry is not configured' })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Open Policy Rules' })).toHaveAttribute('href', '/admin/govern/policy-rules');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows an explicit empty state', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/models': { ...models, models: [] } });
    renderRoute('/admin/optimize/model-registry');

    expect(await screen.findByRole('heading', { name: 'No models are registered' })).toBeVisible();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('reports any other failure with a retry', async () => {
    const gateway = signedInAs('u-auditor', {
      'GET /v1/admin/models': errorEnvelope(503, 'POLICY_ENGINE_UNAVAILABLE', 'The policy engine is unavailable.'),
    });
    renderRoute('/admin/optimize/model-registry');

    expect(await screen.findByText('The model registry could not be loaded')).toBeVisible();
    gateway.set('GET /v1/admin/models', models);
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('table', { name: caption })).toBeVisible();
  });
});
