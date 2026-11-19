import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import applications from '../test/fixtures/govern/adminApplications.json';
import issued from '../test/fixtures/govern/adminCredentialIssued.json';
import credentials from '../test/fixtures/govern/adminCredentials.json';
import { errorEnvelope, json, signedInAs, type DemoUserId, type Route } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';

const LIST = 'GET /v1/admin/applications/*/credentials';
const ISSUE = 'POST /v1/admin/applications/*/credentials';
const REVOKE = 'POST /v1/admin/applications/*/credentials/*/revoke';
const [active, revoked] = credentials.credentials;
const exactly = (text = '') => new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));

/** The applications page with its key routes; the coding assistant's row reports one active key. */
function keysPage(user: DemoUserId, routes: Record<string, Route | unknown> = {}) {
  const registry = {
    ...applications,
    applications: applications.applications.map((app) =>
      app.id === 'gct-coding-assistant' ? { ...app, credentials: credentials.credentials } : app,
    ),
  };
  const gateway = signedInAs(user, { 'GET /v1/admin/applications': registry, [LIST]: credentials, ...routes });
  renderRoute('/admin/govern/applications');
  return gateway;
}

const sent = (gateway: { requests: Request[] }, method: string, suffix: string) =>
  gateway.requests.filter((request) => request.method === method && new URL(request.url).pathname.endsWith(suffix));

async function openKeys(name = 'Coding Assistant') {
  await userEvent.click(await screen.findByRole('button', { name: `Manage keys for ${name}` }));
  return screen.findByRole('region', { name: `Keys for ${name}` });
}

describe('application keys', () => {
  it('lists an application’s keys from its row, with who issued and revoked them, and takes focus there', async () => {
    keysPage('u-platform-admin');
    const row = within(await screen.findByRole('table', { name: 'Registered applications and their grants' })).getByRole('row', {
      name: /Coding Assistant/,
    });
    expect(row).toHaveTextContent('1 active');

    const keys = within(await openKeys());
    expect(keys.getByRole('heading', { name: 'Keys for Coding Assistant' })).toHaveFocus();
    // The coding assistant accepts key calls from the internet (`internet_callers`), which never wake a model.
    expect(keys.getByText(/Calls with these keys pass the network check from any source\./)).toBeVisible();
    expect(keys.getByText(/Key calls from the internet never wake a sleeping in-house model/)).toBeVisible();
    expect(await keys.findByText('1 of 5 active keys.')).toBeVisible();
    const table = within(keys.getByRole('table', { name: 'Issued keys and their status' }));
    const current = table.getByRole('row', { name: exactly(active?.label) });
    expect(current).toHaveTextContent('3f9a1c0d5e7b2a46');
    expect(current).toHaveTextContent('by usr_15427478b0b965e7');
    expect(current).toHaveTextContent('Active');
    const old = table.getByRole('row', { name: exactly(revoked?.label) });
    expect(old).toHaveTextContent(/Revoked.*by usr_aa30b1e4a5a5a11b/);
    expect(old).toHaveTextContent('Never');
    // A revoked key has nothing left to revoke.
    expect(within(old).queryByRole('button')).not.toBeInTheDocument();
  });

  it('issues a key, shows it once with a warning and a copy button, and forgets it when the dialog closes', async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    const gateway = keysPage('u-platform-admin', { [ISSUE]: json(issued, 201) });
    const keys = within(await openKeys());

    await userEvent.type(await keys.findByRole('textbox', { name: 'Label' }), 'Release bot (synthetic)');
    const days = keys.getByRole('spinbutton', { name: 'Expires in (days)' });
    expect(days).toHaveValue(30);
    await userEvent.clear(days);
    await userEvent.type(days, '14');
    await userEvent.click(keys.getByRole('button', { name: 'Issue key' }));

    const dialog = await screen.findByRole('dialog', { name: 'Key issued for Coding Assistant' });
    expect(dialog).toHaveAccessibleDescription(/This is the only time this key is shown/);
    expect(within(dialog).getByRole('textbox', { name: 'Key “Release bot (synthetic)”' })).toHaveValue(issued.credential);
    expect(await sent(gateway, 'POST', '/credentials')[0]?.json()).toEqual({ label: 'Release bot (synthetic)', expires_in_days: 14 });

    await userEvent.click(within(dialog).getByRole('button', { name: 'Copy key' }));
    expect(writeText).toHaveBeenCalledWith(issued.credential);
    expect(within(dialog).getByRole('status')).toHaveTextContent('Copied.');
    // The list is read again, so the new key's row comes from the gateway, without its secret.
    await waitFor(() => expect(sent(gateway, 'GET', '/credentials').length).toBeGreaterThanOrEqual(2));

    await userEvent.click(within(dialog).getByRole('button', { name: 'Done' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(issued.credential);
    expect(window.location.href).not.toContain(issued.credential);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(issued.credential);
  });

  it('shows how to call the API with a key, from the gateway this console uses, without ever holding a key', async () => {
    keysPage('u-auditor');
    const api = within(within(await openKeys()).getByRole('region', { name: 'Use the API' }));
    expect(api.getByText('http://gateway.test/v1')).toBeVisible();
    const example = api.getByRole('region', { name: 'Python example' });
    expect(example).toHaveAttribute('tabindex', '0');
    expect(example).toHaveAttribute('dir', 'ltr');
    expect(example).toHaveTextContent('client = OpenAI(base_url="http://gateway.test/v1", api_key="<a key for gct-coding-assistant>")');
    expect(example).toHaveTextContent('model="aiva-auto"');
    expect(example.textContent).not.toMatch(/aiva_[0-9a-f]{16}\./);
  });

  it('says when the key cannot be copied, so it can be selected instead', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: async () => Promise.reject(new Error('denied')) } });
    keysPage('u-platform-admin', { [ISSUE]: json(issued, 201) });
    const keys = within(await openKeys());
    await userEvent.type(await keys.findByRole('textbox', { name: 'Label' }), 'Release bot (synthetic)');
    await userEvent.click(keys.getByRole('button', { name: 'Issue key' }));

    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Copy key' }));
    expect(within(dialog).getByRole('status')).toHaveTextContent('Could not copy. Select the key and copy it yourself.');
  });

  it('revokes a key only after a confirm, which starts on its safe answer', async () => {
    const gateway = keysPage('u-platform-admin', {
      [REVOKE]: { ...active, active: false, revoked_by: 'usr_15427478b0b965e7', revoked_at: '2026-10-26T06:40:00Z' },
    });
    const keys = within(await openKeys());

    await userEvent.click(await keys.findByRole('button', { name: `Revoke ${active?.label}` }));
    const confirm = keys.getByRole('group', { name: `Revoke ${active?.label}` });
    expect(confirm).toHaveTextContent('Calls with this key are refused at once. This cannot be undone.');
    expect(within(confirm).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    await userEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(sent(gateway, 'POST', '/revoke')).toHaveLength(0);

    await userEvent.click(keys.getByRole('button', { name: `Revoke ${active?.label}` }));
    await userEvent.click(keys.getByRole('button', { name: 'Revoke key' }));
    expect(await keys.findByText(`Key “${active?.label}” revoked.`)).toBeVisible();
    expect(new URL(sent(gateway, 'POST', '/revoke')[0]?.url ?? '').pathname).toBe(
      `/v1/admin/applications/gct-coding-assistant/credentials/${active?.key_id}/revoke`,
    );
  });

  it('says why a key cannot be issued at the active limit, and what the gateway refused', async () => {
    const full = { ...credentials, viewer_can_issue: false, credentials: Array.from({ length: 5 }, (_, i) => ({ ...active, key_id: `00000000000000a${i}` })) };
    keysPage('u-platform-admin', { [LIST]: full });
    const keys = within(await openKeys());
    expect(await keys.findByText('This application holds 5 active keys, the most it may. Revoke one to issue another.')).toBeVisible();
    expect(keys.getByRole('button', { name: 'Issue key' })).toBeDisabled();
  });

  it('shows the gateway’s reason when issuing fails', async () => {
    keysPage('u-platform-admin', {
      [ISSUE]: errorEnvelope(409, 'CREDENTIAL_LIMIT', 'This application already has 5 active keys.'),
    });
    const keys = within(await openKeys());
    await userEvent.type(await keys.findByRole('textbox', { name: 'Label' }), 'One too many');
    await userEvent.click(keys.getByRole('button', { name: 'Issue key' }));
    expect(await keys.findByRole('alert')).toHaveTextContent('The key could not be issued');
    expect(keys.getByRole('alert')).toHaveTextContent('This application already has 5 active keys.');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('is read-only for an auditor: the keys, but no issuing or revoking', async () => {
    keysPage('u-auditor', { [LIST]: { ...credentials, viewer_can_issue: false, viewer_can_revoke: false } });
    const keys = within(await openKeys());
    expect(await keys.findByText('Read-only: you can see this application’s keys but not issue or revoke them.')).toBeVisible();
    expect(keys.queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument();
    expect(keys.queryByRole('button', { name: 'Issue key' })).not.toBeInTheDocument();
    expect(keys.queryByRole('columnheader', { name: 'Action' })).not.toBeInTheDocument();
  });

  it('shows no keys to a viewer who may not read them', async () => {
    keysPage('u-public-visitor');
    await screen.findByRole('table', { name: 'Registered applications and their grants' });
    expect(screen.queryByRole('columnheader', { name: 'Keys' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Manage keys/ })).not.toBeInTheDocument();
  });

  it('says which applications accept key calls from the internet, from their grant and never their networks', async () => {
    keysPage('u-auditor');
    const table = within(await screen.findByRole('table', { name: 'Registered applications and their grants' }));
    expect(
      screen.getByText(/^Accept key calls from the internet: Coding Assistant and Enterprise Knowledge Portal\. /),
    ).toHaveTextContent('Signed-in users and Test Console runs still need one of the application’s trusted networks.');
    expect(table.getByRole('row', { name: /Coding Assistant/ })).toHaveTextContent('Key calls: from any source');
    expect(table.getByRole('row', { name: /Fraud Investigation Assistant/ })).not.toHaveTextContent('Key calls');

    const keys = within(await openKeys('Fraud Investigation Assistant'));
    expect(keys.getByText(/Calls with these keys are accepted only from this application’s trusted networks\./)).toBeVisible();
    expect(keys.queryByText(/never wake a sleeping in-house model/)).not.toBeInTheDocument();
  });

  it('says when no application accepts key calls from the internet', async () => {
    signedInAs('u-auditor', {
      'GET /v1/admin/applications': {
        ...applications,
        applications: applications.applications.map((app) => ({ ...app, internet_callers: false })),
      },
    });
    renderRoute('/admin/govern/applications');
    expect(
      await screen.findByText('No application accepts key calls from the internet: a key works only from one of its application’s trusted networks.'),
    ).toBeVisible();
  });
});
