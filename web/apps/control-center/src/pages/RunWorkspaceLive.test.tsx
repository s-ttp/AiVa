import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import chatSovereign from '../test/fixtures/chatSovereign.json';
import chatExecutionModeUnavailable from '../test/fixtures/demo/chatExecutionModeUnavailable.json';
import chatLiveVisitor from '../test/fixtures/demo/chatLiveVisitor.json';
import chatLiveVisitorLimit from '../test/fixtures/demo/chatLiveVisitorLimit.json';
import chatReplay from '../test/fixtures/demo/chatReplay.json';
import chatSimulatedMode from '../test/fixtures/demo/chatSimulatedMode.json';
import liveBudgetVisitor from '../test/fixtures/demo/liveBudgetVisitor.json';
import liveBudgetVisitorLimit from '../test/fixtures/demo/liveBudgetVisitorLimit.json';
import profiles from '../test/fixtures/profiles.json';
import scenarios from '../test/fixtures/scenarios.json';
import { errorEnvelope, json, signedInAs, type MockGateway } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

/**
 * The public demo's Replay/Live choice (ADR-0019). Runs, refusals and live
 * budgets are recorded from a local gateway (simulated inference; a live-mode
 * gateway without provider keys for the visitor's refusal). A live run cannot
 * be recorded without spend, so its label is checked on a recorded run with
 * `execution_mode: 'live'`.
 */
const runButton = () => screen.findByRole('button', { name: 'Run through AiVa' });
const modeGroup = async () => within(await screen.findByRole('group', { name: 'Execution mode' }));

async function chatBodies(gateway: MockGateway): Promise<Record<string, unknown>[]> {
  return Promise.all(
    gateway.requests.filter((r) => r.url.endsWith('/v1/ai/chat')).map((r) => r.clone().json() as Promise<Record<string, unknown>>),
  );
}

describe('execution mode choice', () => {
  it('starts a visitor on the recorded replay and shows the public live budget', async () => {
    const gateway = signedInAs('u-public-visitor', {
      'GET /v1/catalog/scenarios': scenarios,
      'GET /v1/demo/live-budget': liveBudgetVisitor,
      'POST /v1/ai/chat': chatReplay,
    });
    renderRoute('/console/run?scenario=customer-support');

    const mode = await modeGroup();
    expect(mode.getByRole('radio', { name: /^Recorded replay/ })).toBeChecked();
    expect(mode.getByRole('radio', { name: /^Live/ })).not.toBeChecked();
    expect(mode.queryByRole('radio', { name: /^Gateway default/ })).not.toBeInTheDocument();
    expect(await screen.findByText('Public live budget left today: 100%')).toBeVisible();
    expect(screen.getByText('You have 5 live runs left this hour.')).toBeVisible();
    expect(mode.getByRole('radio', { name: /^Live/ })).toHaveAccessibleDescription(/Live runs in the public demo/);

    await userEvent.click(await runButton());

    expect(await screen.findByText('Recorded replay', { selector: 'span *, span' })).toBeVisible();
    expect((await chatBodies(gateway))[0]).toMatchObject({ execution_mode: 'replay' });
  });

  it('lets a visitor ask for a live run', async () => {
    const gateway = signedInAs('u-public-visitor', {
      'GET /v1/catalog/scenarios': scenarios,
      'GET /v1/demo/live-budget': liveBudgetVisitor,
      'POST /v1/ai/chat': chatLiveVisitor,
    });
    renderRoute('/console/run?scenario=customer-support');

    await userEvent.click((await modeGroup()).getByRole('radio', { name: /^Live/ }));
    await userEvent.click(await runButton());

    await screen.findByRole('heading', { level: 2, name: 'Run result' });
    expect((await chatBodies(gateway))[0]).toMatchObject({ execution_mode: 'live' });
    // The budget is read again after the run.
    await waitFor(() =>
      expect(gateway.requests.filter((r) => r.url.endsWith('/v1/demo/live-budget')).length).toBeGreaterThanOrEqual(2),
    );
  });

  it('leaves the mode to the gateway for other live-capable roles unless they choose one', async () => {
    const gateway = signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': scenarios,
      'POST /v1/ai/chat': chatSovereign,
    });
    renderRoute('/console/run?scenario=fraud-management');

    const mode = await modeGroup();
    expect(mode.getByRole('radio', { name: /^Gateway default/ })).toBeChecked();
    await userEvent.click(await runButton());
    await screen.findByRole('heading', { level: 2, name: 'Run result' });
    await userEvent.click(mode.getByRole('radio', { name: /^Live/ }));
    await userEvent.click(await runButton());

    await waitFor(async () => expect(await chatBodies(gateway)).toHaveLength(2));
    const [first, second] = await chatBodies(gateway);
    expect(first).not.toHaveProperty('execution_mode');
    expect(second).toMatchObject({ execution_mode: 'live' });
    expect(gateway.requests.some((r) => r.url.endsWith('/v1/demo/live-budget'))).toBe(false);
  });

  it('offers no choice to a role without chat:live', async () => {
    const withoutLive = {
      ...profiles['u-tester'],
      permissions: profiles['u-tester'].permissions.filter((p) => p !== 'chat:live'),
    };
    signedInAs('u-tester', { 'GET /v1/auth/me': withoutLive, 'GET /v1/catalog/scenarios': scenarios });
    renderRoute('/console/run?scenario=fraud-management');

    await runButton();
    expect(screen.queryByRole('group', { name: 'Execution mode' })).not.toBeInTheDocument();
  });

  it('shows why live runs would be refused now and when they free up', async () => {
    signedInAs('u-public-visitor', {
      'GET /v1/catalog/scenarios': scenarios,
      'GET /v1/demo/live-budget': liveBudgetVisitorLimit,
    });
    renderRoute('/console/run?scenario=customer-support');

    expect(await screen.findByText('You have 0 live runs left this hour.')).toBeVisible();
    expect(screen.getByText(/^Your next live run frees up at 15 Oct 2026, 20:32:50 UTC\.$/)).toBeVisible();
    expect(screen.getByText('You have used every live run for this hour.')).toBeVisible();
  });
});

describe('a refused live run', () => {
  it('says why, when live runs resume, and offers the recorded replay of the same request', async () => {
    const gateway = signedInAs('u-public-visitor', {
      'GET /v1/catalog/scenarios': scenarios,
      'GET /v1/demo/live-budget': liveBudgetVisitor,
      'POST /v1/ai/chat': (request: Request) =>
        request
          .clone()
          .json()
          .then((body: { execution_mode?: string }) =>
            json(body.execution_mode === 'live' ? chatLiveVisitorLimit : chatReplay),
          ),
    });
    renderRoute('/console/run?scenario=customer-support');

    await userEvent.click((await modeGroup()).getByRole('radio', { name: /^Live/ }));
    await userEvent.click(await runButton());

    const panel = within((await screen.findByRole('heading', { name: 'Live run unavailable' })).closest('section') as HTMLElement);
    expect(panel.getByText('Nothing ran live. You can run the recorded replay of the same request instead.')).toBeVisible();
    expect(panel.getByText('You have used every live run for this hour.')).toBeVisible();
    expect(panel.getByText('Live runs can resume at 15 Oct 2026, 20:33:05 UTC.')).toBeVisible();
    expect(screen.getByText('This visitor has used every live run of the hour.')).toBeVisible();
    expect(screen.getAllByText('LIVE_DEMO_UNAVAILABLE').length).toBeGreaterThan(0);
    expect(screen.getByText('Live run unavailable', { selector: 'span *, span' })).toBeVisible();
    expect(screen.queryByText('Blocked by budget cap')).not.toBeInTheDocument();

    await userEvent.click(panel.getByRole('button', { name: 'Run the recorded replay' }));

    await waitFor(async () => expect(await chatBodies(gateway)).toHaveLength(2));
    const [live, replay] = await chatBodies(gateway);
    expect(live).toMatchObject({ execution_mode: 'live' });
    expect(replay).toMatchObject({ execution_mode: 'replay', messages: live?.messages, scenario_id: live?.scenario_id });
    expect(await screen.findByText('Recorded replay', { selector: 'span *, span' })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Live run unavailable' })).not.toBeInTheDocument();
  });

  it('hides the choice when the gateway says the role may not run live, and still offers the replay', async () => {
    const gateway = signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': scenarios,
      'POST /v1/ai/chat': (request: Request) =>
        request
          .clone()
          .json()
          .then((body: { execution_mode?: string }) =>
            body.execution_mode === 'live'
              ? errorEnvelope(403, 'LIVE_NOT_PERMITTED', 'Live runs are not available to your role; the recorded replay is.')
              : json(chatReplay),
          ),
    });
    renderRoute('/console/run?scenario=fraud-management');

    await userEvent.click((await modeGroup()).getByRole('radio', { name: /^Live/ }));
    await userEvent.click(await runButton());

    expect(await screen.findByText('Your role cannot start live runs.')).toBeVisible();
    expect(screen.queryByRole('group', { name: 'Execution mode' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Run the recorded replay' }));

    await waitFor(async () => expect(await chatBodies(gateway)).toHaveLength(2));
    expect((await chatBodies(gateway))[1]).toMatchObject({ execution_mode: 'replay' });
  });
});

describe('live runs the gateway cannot start', () => {
  it('says live runs are not available on this gateway, hides the choice and offers the replay', async () => {
    const gateway = signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': scenarios,
      'POST /v1/ai/chat': (request: Request) =>
        request
          .clone()
          .json()
          .then((body: { execution_mode?: string }) =>
            body.execution_mode === 'live' ? json(chatExecutionModeUnavailable, 409) : json(chatReplay),
          ),
    });
    renderRoute('/console/run?scenario=fraud-management');

    await userEvent.click((await modeGroup()).getByRole('radio', { name: /^Live/ }));
    await userEvent.click(await runButton());

    expect(await screen.findByText('Live runs aren’t available on this gateway.')).toBeVisible();
    expect(screen.getAllByText('EXECUTION_MODE_UNAVAILABLE').length).toBeGreaterThan(0);
    // The refused run says what it is: not an outage, not a policy block, nothing measured that did not happen.
    const result = within(required((await screen.findByRole('heading', { level: 2, name: 'Run result' })).closest('section')));
    expect(result.getByText('Live run unavailable')).toBeVisible();
    expect(result.queryByText('Run failed; the gateway failed closed')).not.toBeInTheDocument();
    expect(result.getByText('Not evaluated: the run stopped before AI policy')).toBeVisible();
    expect(result.queryByText(/Blocked by AI policy/)).not.toBeInTheDocument();
    expect(result.getByText(/trusted networks not evaluated/)).toBeVisible();
    expect(result.getByText('Not run')).toBeVisible();
    for (const name of ['Upstream input tokens', 'Upstream output tokens', 'Budget reserved']) {
      expect(result.getByRole('group', { name })).not.toHaveTextContent('Measured');
    }
    expect(screen.queryByRole('group', { name: 'Execution mode' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Run the recorded replay' }));

    await waitFor(async () => expect(await chatBodies(gateway)).toHaveLength(2));
    expect((await chatBodies(gateway))[1]).toMatchObject({ execution_mode: 'replay' });
  });

  it('says a sleeping live model is not woken for the public, with no reset time', async () => {
    const cold = {
      ...chatLiveVisitorLimit,
      error: { ...chatLiveVisitorLimit.error, retryable: false, details: { reason: 'endpoint_cold', resets_at: null } },
    };
    signedInAs('u-public-visitor', {
      'GET /v1/catalog/scenarios': scenarios,
      'GET /v1/demo/live-budget': liveBudgetVisitor,
      'POST /v1/ai/chat': cold,
    });
    renderRoute('/console/run?scenario=customer-support');

    await userEvent.click((await modeGroup()).getByRole('radio', { name: /^Live/ }));
    await userEvent.click(await runButton());

    const panel = within((await screen.findByRole('heading', { name: 'Live run unavailable' })).closest('section') as HTMLElement);
    expect(panel.getByText('The live model is asleep; public live runs don’t wake it.')).toBeVisible();
    expect(panel.queryByText(/Live runs can resume/)).not.toBeInTheDocument();
    expect(panel.getByRole('button', { name: 'Run the recorded replay' })).toBeVisible();
  });
});

describe('the mode that ran', () => {
  async function runWith(response: unknown) {
    signedInAs('u-tester', { 'GET /v1/catalog/scenarios': scenarios, 'POST /v1/ai/chat': response });
    renderRoute('/console/run?scenario=fraud-management');
    await userEvent.click(await runButton());
    return screen.findByRole('heading', { level: 2, name: 'Run result' });
  }

  it('labels a replay that found no recording, so the simulated adapter answered', async () => {
    await runWith(chatReplay);
    expect(await screen.findByText('Recorded replay', { selector: 'span *, span' })).toBeVisible();
    expect(
      screen.getByText('No recording matched this request, so the simulated adapter answered. Its values are labelled simulated.'),
    ).toBeVisible();
  });

  it('labels a run in the gateway’s simulated mode', async () => {
    await runWith(chatSimulatedMode);
    expect(await screen.findByText('Simulated run')).toBeVisible();
    expect(screen.queryByText(/No recording matched this request/)).not.toBeInTheDocument();
  });

  it('labels a live run', async () => {
    await runWith({ ...chatSovereign, execution_mode: 'live' });
    expect(await screen.findByText('Live run')).toBeVisible();
    expect(screen.queryByText(/No recording matched this request/)).not.toBeInTheDocument();
  });
});
