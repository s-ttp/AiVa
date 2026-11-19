import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import chatSovereign from '../test/fixtures/chatSovereign.json';
import artifact from '../test/fixtures/console/artifact.json';
import artifactPreview from '../test/fixtures/console/artifactPreview.json';
import scenarios from '../test/fixtures/scenarios.json';
import { errorEnvelope, signedInAs, type Route } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const notImplemented = () => errorEnvelope(501, 'NOT_IMPLEMENTED', 'Artifacts are not implemented yet.');
const routes = (extra: Record<string, Route | unknown> = {}) => ({
  'GET /v1/catalog/scenarios': scenarios,
  'GET /v1/artifacts': { items: [], page: { next_cursor: null, limit: 50 } },
  ...extra,
});
const scenario10 = required(scenarios.find((scenario) => scenario.id === 'document-copilot'));
const sample = required(scenario10.sample_artifact);
/** The catalog without its structured sample, as before scenario samples existed. */
const scenariosWithoutSample = scenarios.map((scenario) => ({ ...scenario, sample_artifact: null }));

/** Fills the required fields a catalog without a sample leaves blank: a title and one evidence source. */
async function fillForm() {
  await userEvent.type(await screen.findByRole('textbox', { name: 'Document title' }), 'Q3 capex board summary');
  await userEvent.type(screen.getByRole('textbox', { name: 'Source 1 ID' }), 'capex-plan-q3');
  await userEvent.type(screen.getByRole('textbox', { name: 'Source 1 text' }), 'Synthetic Q3 plan: AED 1,284.5 m.');
}

/** The document card, once shown. */
async function documentCard() {
  const element = required((await screen.findByRole('heading', { level: 2, name: 'Document' })).closest('section'));
  return Object.assign(within(element), { element });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('document copilot', () => {
  it('prefills the synthetic sample from scenario 10 and sends a traced Test Console request', async () => {
    const gateway = signedInAs('u-tester', routes({ 'POST /v1/artifacts': notImplemented() }));
    renderRoute('/console/artifacts');

    expect(await screen.findByRole('textbox', { name: 'Document title' })).toHaveValue(sample.title);
    expect(screen.getByRole('textbox', { name: 'Instructions' })).toHaveValue(sample.instructions);
    expect(screen.getByRole('combobox', { name: 'Application profile' })).toHaveValue('gct-doc-copilot');
    expect(screen.getByRole('combobox', { name: 'Declared classification' })).toHaveValue('confidential');
    expect(screen.getAllByText('Synthetic sample from the scenario catalog')).toHaveLength(2);
    expect(screen.getByRole('textbox', { name: 'Source 4 ID' })).toHaveValue('board-request#1');
    expect(screen.queryByRole('textbox', { name: 'Source 5 ID' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Figure 8 value' })).toHaveValue('20.6');
    await userEvent.click(screen.getByRole('button', { name: 'Draft document' }));

    await screen.findByText('Document drafting is not available yet');
    const post = required(gateway.requests.find((r) => r.method === 'POST'));
    expect(post.headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
    expect(await post.json()).toEqual({
      document_type: 'pptx',
      title: sample.title,
      instructions: sample.instructions,
      evidence: sample.evidence,
      figures: sample.figures.map((figure) => ({ ...figure, unit: figure.unit ?? null })),
      data_classification: 'confidential',
      scenario_id: 'document-copilot',
      max_output_tokens: 2048,
      trace: true,
      test_profile: { application_id: 'gct-doc-copilot', simulated_source_ip: '192.0.2.10' },
    });
  });

  it('takes the evidence and figures from the tester when the catalog has no sample', async () => {
    const gateway = signedInAs(
      'u-tester',
      routes({ 'GET /v1/catalog/scenarios': scenariosWithoutSample, 'POST /v1/artifacts': notImplemented() }),
    );
    renderRoute('/console/artifacts');

    expect(await screen.findByRole('textbox', { name: 'Instructions' })).toHaveValue(scenario10.sample_prompt);
    expect(screen.queryByText('Synthetic sample from the scenario catalog')).not.toBeInTheDocument();
    await fillForm();
    await userEvent.click(screen.getByRole('button', { name: 'Add a critical figure' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Figure 1 ID' }), 'capex-total');
    await userEvent.type(screen.getByRole('textbox', { name: 'Figure 1 label' }), 'Total capex');
    await userEvent.type(screen.getByRole('textbox', { name: 'Figure 1 value' }), '1,284.5');
    await userEvent.type(screen.getByRole('textbox', { name: 'Figure 1 unit' }), 'AED m');
    await userEvent.type(screen.getByRole('textbox', { name: 'Figure 1 source ID' }), 'capex-plan-q3');
    await userEvent.click(screen.getByRole('button', { name: 'Draft document' }));

    await screen.findByText('Document drafting is not available yet');
    const post = required(gateway.requests.find((r) => r.method === 'POST'));
    expect(await post.json()).toMatchObject({
      title: 'Q3 capex board summary',
      instructions: scenario10.sample_prompt,
      evidence: [{ id: 'capex-plan-q3', text: 'Synthetic Q3 plan: AED 1,284.5 m.' }],
      figures: [{ id: 'capex-total', label: 'Total capex', value: '1,284.5', unit: 'AED m', source_id: 'capex-plan-q3' }],
      scenario_id: 'document-copilot',
    });
  });

  it('shows the not-implemented state for every 501, never sample data', async () => {
    signedInAs('u-tester', routes({ 'POST /v1/artifacts': notImplemented(), 'GET /v1/artifacts': notImplemented() }));
    renderRoute('/console/artifacts');

    expect(await screen.findByText('The artifact list is not available yet')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Draft document' }));
    const state = (await screen.findByText('Document drafting is not available yet')).closest('[role="status"]');
    expect(state).toHaveTextContent('Artifacts are not implemented yet.');
    expect(state).toHaveTextContent('NOT_IMPLEMENTED');
    expect(screen.queryByRole('heading', { name: 'Document' })).not.toBeInTheDocument();
  });

  it('shows the drafting run with the run panels and the document with its checks', async () => {
    signedInAs('u-tester', routes({ 'POST /v1/artifacts': { run: chatSovereign, artifact } }));
    renderRoute('/console/artifacts');
    await userEvent.click(await screen.findByRole('button', { name: 'Draft document' }));

    const card = await documentCard();
    expect(screen.getByText('Sovereign direct route required')).toBeVisible();
    expect(card.getByText('Synthetic demo data · Simulated draft')).toBeVisible();
    expect(card.getByText(artifact.file_name)).toBeVisible();
    const checks = card.getByRole('table', { name: 'Document output checks' });
    expect(within(checks).getByRole('row', { name: /Critical figures preserved/ })).toHaveTextContent('Passed');
    expect(within(checks).getByRole('row', { name: /^Output DLP/ })).toHaveTextContent('Not run');
    const figures = card.getByRole('table', { name: 'Critical figure preservation' });
    expect(within(figures).getByRole('row', { name: /capex-total/ })).toHaveTextContent('capex-totalPreserved2slide 2slide 3');
    expect(card.getByRole('table', { name: 'Sources cited in the document' })).toHaveTextContent('capex-plan-q3Cited');
    expect(card.getByText('Owner only: other users and applications cannot open it')).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Pipeline stages' })).toBeVisible();
  });

  it('renders the preview as plain text, never HTML', async () => {
    signedInAs(
      'u-tester',
      routes({ 'GET /v1/artifacts/*': artifact, 'GET /v1/artifacts/*/preview': artifactPreview }),
    );
    renderRoute(`/console/artifacts?artifact=${artifact.artifact_id}`);

    const card = await documentCard();
    await userEvent.click(card.getByRole('button', { name: 'Show preview' }));
    expect(await card.findByRole('heading', { name: 'Slide 1: Q3 network capex' })).toBeVisible();
    expect(card.getByText(/AED 1,284\.5 m <b>across<\/b> three programmes/)).toBeVisible();
    expect(card.element.querySelector('b')).toBeNull();
    expect(card.getByText('Riyadh and Jeddah clusters')).toBeVisible();
    expect(card.getByRole('table', { name: 'Table on Slide 2: Programmes' })).toHaveTextContent('5G densification702.0');
    expect(card.getByText('Disabled')).toBeVisible();
  });

  it('reads any 404 as not found or not yours', async () => {
    signedInAs('u-tester', routes({ 'GET /v1/artifacts/*': errorEnvelope(404, 'NOT_FOUND', 'Artifact not found.') }));
    renderRoute('/console/artifacts?artifact=art_someone_else');

    expect(await screen.findByText('Not found or not yours')).toBeVisible();
    expect(screen.getByText('Artifacts are visible only to the user or application that created them.')).toBeVisible();
  });

  it('downloads the document with the session token', async () => {
    const gateway = signedInAs(
      'u-tester',
      routes({
        'GET /v1/artifacts/*': artifact,
        'GET /v1/artifacts/*/content': () =>
          new Response(new Uint8Array([0x50, 0x4b]), { status: 200, headers: { 'Content-Type': artifact.media_type } }),
      }),
    );
    const createObjectURL = vi.fn(() => 'blob:artifact');
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    renderRoute(`/console/artifacts?artifact=${artifact.artifact_id}`);

    const card = await documentCard();
    await userEvent.click(card.getByRole('button', { name: 'Download PowerPoint presentation (PPTX)' }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    const anchor = required(click.mock.contexts[0]) as HTMLAnchorElement;
    expect(anchor.download).toBe(artifact.file_name);
    expect(anchor.href).toBe('blob:artifact');
    // Revoked later, not at once: revoking straight away can cancel the download.
    expect(revokeObjectURL).not.toHaveBeenCalled();
    const download = required(gateway.requests.find((r) => r.url.endsWith('/content')));
    expect(download.headers.get('X-AiVa-Authorization')).toBe('Bearer test-token-u-tester');
  });
});
