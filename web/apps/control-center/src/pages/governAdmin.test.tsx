import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import applications from '../test/fixtures/govern/adminApplications.json';
import models from '../test/fixtures/govern/adminModels.json';
import tools from '../test/fixtures/govern/adminTools.json';
import networks from '../test/fixtures/govern/adminTrustedNetworks.json';
import { errorEnvelope, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const enforcedBy = () => screen.getByRole('link', { name: `policy version ${applications.policy_version}` });

describe('applications & identities', () => {
  it('lists each application’s grants and the bundle they come from', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/applications': applications });
    renderRoute('/admin/govern/applications');

    const table = await screen.findByRole('table', { name: 'Registered applications and their grants' });
    const row = within(table).getByRole('row', { name: /Coding Assistant/ });
    expect(row).toHaveTextContent('gct-coding-assistant');
    expect(row).toHaveTextContent('Enabled');
    expect(row).toHaveTextContent('Internal');
    expect(row).toHaveTextContent('claude-flagship');
    expect(row).toHaveTextContent('Engineering VPN 10.40.0.0/16');
    expect(row).toHaveTextContent('0 active');
    expect(within(row).getByRole('button', { name: 'Manage keys for Coding Assistant' })).toBeVisible();
    expect(enforcedBy()).toHaveAttribute('href', '/admin/govern/policy-rules?version=2');
    expect(screen.getByText(applications.bundle_hash)).toBeVisible();
  });

  it('shows an explicit empty state', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/applications': { ...applications, applications: [] } });
    renderRoute('/admin/govern/applications');
    expect(await screen.findByText('No applications')).toBeVisible();
  });

  it('reports the gateway’s refusal', async () => {
    signedInAs('u-auditor', {
      'GET /v1/admin/applications': errorEnvelope(403, 'PERMISSION_DENIED', 'You do not have permission for this action.'),
    });
    renderRoute('/admin/govern/applications');
    expect(await screen.findByRole('alert')).toHaveTextContent('Applications could not be loaded');
  });
});

describe('trusted networks', () => {
  it('explains how the client address is derived and lists the CIDR rules', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/trusted-networks': networks });
    renderRoute('/admin/govern/trusted-networks');

    const ingress = within(required((await screen.findByRole('heading', { name: 'Client address' })).closest('section')));
    expect(ingress.getByText('None configured')).toBeVisible();
    expect(ingress.getByText('The TCP peer address; X-Forwarded-For is ignored')).toBeVisible();
    expect(ingress.getByText(/Allowed for testers/)).toBeVisible();
    const table = screen.getByRole('table', { name: 'Trusted networks and the applications that accept them' });
    const row = within(table).getByRole('row', { name: /Engineering VPN/ });
    expect(row).toHaveTextContent('10.40.0.0/16');
    expect(row).toHaveTextContent('gct-knowledge-portal');
  });

  it('reports the gateway’s refusal', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/trusted-networks': errorEnvelope(503, 'UNAVAILABLE', 'Try again.') });
    renderRoute('/admin/govern/trusted-networks');
    expect(await screen.findByRole('alert')).toHaveTextContent('Trusted networks could not be loaded');
  });
});

describe('model allowlist', () => {
  it('shows invariants, model grants and the policies that narrow the model set', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/models': models });
    renderRoute('/admin/govern/model-allowlist');

    const invariants = within(required((await screen.findByRole('heading', { name: 'Built-in invariants' })).closest('section')));
    expect(invariants.getByText(/AIPEL-INV-001/)).toBeVisible();
    const table = screen.getByRole('table', { name: 'Models and the applications granted them' });
    const sovereign = within(table).getByRole('row', { name: /gct-qwen-finetuned/ });
    expect(sovereign).toHaveTextContent('Sovereign');
    const external = within(table).getByRole('row', { name: /deepseek-v4-1-flash/ });
    expect(external).toHaveTextContent('External provider');
    expect(external).toHaveTextContent('5254448103b6254fc646dacbca1cbbf20e9216c1');
    const policies = screen.getByRole('table', { name: 'Policies restricting models' });
    const row = within(policies).getByRole('row', { name: /GCT-CONF-001/ });
    expect(row).toHaveTextContent('Classification is Confidential, Restricted, Sovereign-only');
    expect(row).toHaveTextContent('Mandatory path: Sovereign direct (CIM bypassed)');
    expect(row).toHaveTextContent('External providers denied');
  });

  it('says when the catalog does not configure a model the bundle names', async () => {
    const first = required(models.models[0]);
    const missing = { ...first, catalog_configured: false, provider: null, provider_model_id: null, revision: null };
    signedInAs('u-auditor', { 'GET /v1/admin/models': { ...models, models: [missing] } });
    renderRoute('/admin/govern/model-allowlist');

    const table = await screen.findByRole('table', { name: 'Models and the applications granted them' });
    const row = within(table).getByRole('row', { name: new RegExp(first.id) });
    expect(row).toHaveTextContent('Not configured in the model catalog');
    expect(row).not.toHaveTextContent('Revision');
  });
});

describe('tool permissions', () => {
  it('lists tools with access, egress and the applications granted them', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/tools': tools });
    renderRoute('/admin/govern/tool-permissions');

    const table = await screen.findByRole('table', { name: 'Registered tools and the applications granted them' });
    const web = within(table).getByRole('row', { name: /Public web search/ });
    expect(web).toHaveTextContent('External');
    expect(web).toHaveTextContent('Not granted to any application');
    expect(within(table).getByRole('row', { name: /Incident lookup/ })).toHaveTextContent('gct-incident-agent');
  });

  it('shows an explicit empty state', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/tools': { ...tools, tools: [] } });
    renderRoute('/admin/govern/tool-permissions');
    expect(await screen.findByText('No tools')).toBeVisible();
  });
});
