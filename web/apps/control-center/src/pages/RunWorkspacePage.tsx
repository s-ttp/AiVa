import { Card, EmptyState, ErrorState, LoadingState } from '@aiva/ui';
import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { ApiError, unwrap, type ChatResponse } from '../api/client';
import { describeError } from '../api/describeError';
import { useApiQuery } from '../api/useApiQuery';
import { isPublicVisitor, useSession } from '../auth/session';
import { QueryView } from '../components/QueryView';
import { t } from '../i18n';
import { Page } from '../layout/Page';
import { ApprovalPanel } from '../run/ApprovalPanel';
import { ExecutionGraph } from '../run/ExecutionGraph';
import { RunDetails } from '../run/RunDetails';
import { toChatRequest, type RunDraft } from '../run/draft';
import { executeRun } from '../run/execute';
import { liveRefusal, refusesEveryLiveRun, type ModeChoice } from '../run/executionMode';
import { LiveRefusedPanel } from '../run/LiveRefusedPanel';
import { RunForm } from '../run/RunForm';
import { RunSummary } from '../run/RunSummary';
import { consoleScenarios } from '../run/scenarios';

type RunState =
  | { status: 'idle' }
  | { status: 'running' }
  | { status: 'error'; error: ApiError; draft: RunDraft }
  | { status: 'done'; run: ChatResponse; draft: RunDraft };

export function RunWorkspacePage() {
  const { client, state: session, can } = useSession();
  const [params] = useSearchParams();
  const scenarios = useApiQuery('catalog/scenarios', (gateway, signal) =>
    unwrap(gateway.GET('/v1/catalog/scenarios', { signal })).then(consoleScenarios),
  );
  const [state, setState] = useState<RunState>({ status: 'idle' });
  const [runCount, setRunCount] = useState(0);
  // This role or gateway cannot run live: the choice is hidden and runs leave the mode to the gateway.
  const [liveRefused, setLiveRefused] = useState(false);
  const resultHeading = useRef<HTMLElement>(null);
  const visitor = session.status === 'signedIn' && isPublicVisitor(session.profile);
  const modeChoice: ModeChoice = !can('chat:live') || liveRefused ? 'none' : visitor ? 'visitor' : 'member';

  // Move focus to the outcome so keyboard and screen-reader users land on it.
  useEffect(() => {
    if (state.status === 'done' || state.status === 'error') resultHeading.current?.focus();
  }, [state]);

  /** Runs a draft; `approvalId` resumes a run that paused for approval with the identical request. */
  async function run(draft: RunDraft, approvalId?: string) {
    if (!client) return;
    setState({ status: 'running' });
    // Without a choice no live run is asked for; an explicit replay (the offer after a refusal) is kept.
    const sent = modeChoice === 'none' && draft.executionMode === 'live' ? { ...draft, executionMode: null } : draft;
    try {
      const body = { ...toChatRequest(sent), approval_id: approvalId ?? null };
      const response = await executeRun(client, body);
      if (refusesEveryLiveRun(liveRefusal(response.error))) setLiveRefused(true);
      setState({ status: 'done', run: response, draft: sent });
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError({ kind: 'network', message: t('run.networkError') });
      if (refusesEveryLiveRun(liveRefusal(apiError))) setLiveRefused(true);
      setState({ status: 'error', error: apiError, draft: sent });
    } finally {
      setRunCount((count) => count + 1);
    }
  }

  const refusal =
    state.status === 'done' ? liveRefusal(state.run.error) : state.status === 'error' ? liveRefusal(state.error) : null;

  return (
    <Page title={t('view.run.title')} eyebrow={t('area.console')} description={t('view.run.description')}>
      <Card title={t('run.form.title')} description={t('run.form.description')}>
        <QueryView
          state={scenarios.state}
          onRetry={scenarios.reload}
          loadingLabel={t('scenarios.loading')}
          errorTitle={t('scenarios.error')}
        >
          {(catalog) => (
            <RunForm
              scenarios={catalog}
              initialScenarioId={params.get('scenario')}
              pending={state.status === 'running'}
              modeChoice={modeChoice}
              runCount={runCount}
              onSubmit={run}
            />
          )}
        </QueryView>
      </Card>

      <div aria-live="polite" className="sr-only">
        {state.status === 'running' && t('run.announce.running')}
        {state.status === 'done' && t('run.announce.done', { status: t(`runStatus.${state.run.status}`) })}
        {state.status === 'error' && t('run.announce.error')}
      </div>

      {state.status === 'idle' && (
        <EmptyState title={t('run.empty.title')} description={t('run.empty.body')} />
      )}
      {state.status === 'running' && <LoadingState label={t('run.running')} />}
      {state.status === 'error' && (
        <div ref={(node) => void (resultHeading.current = node)} tabIndex={-1} className="outline-none">
          <ErrorState title={t('run.error.title')} description={describeError(state.error)} />
        </div>
      )}
      {state.status === 'error' && refusal && (
        <Card>
          <LiveRefusedPanel
            reason={refusal.reason}
            resetsAt={refusal.resetsAt}
            onReplay={() => void run({ ...state.draft, executionMode: 'replay' })}
          />
        </Card>
      )}
      {state.status === 'done' && (
        <>
          <Card title={t('run.result.title')}>
            <h3 ref={(node) => void (resultHeading.current = node)} tabIndex={-1} className="sr-only">
              {t('run.result.heading', { status: t(`runStatus.${state.run.status}`) })}
            </h3>
            <RunSummary run={state.run} />
          </Card>
          {refusal && (
            <Card>
              <LiveRefusedPanel
                reason={refusal.reason}
                resetsAt={refusal.resetsAt}
                onReplay={() => void run({ ...state.draft, executionMode: 'replay' })}
              />
            </Card>
          )}
          {state.run.status === 'approval_required' && state.run.approval?.approval_id && (
            <Card>
              <ApprovalPanel run={state.run} onResubmit={(approvalId) => void run(state.draft, approvalId)} />
            </Card>
          )}
          <Card title={t('run.graph.title')} description={t('run.graph.description')}>
            <ExecutionGraph spans={state.run.trace ?? []} executionPath={state.run.execution_path} />
          </Card>
          <Card title={t('run.details.title')}>
            <RunDetails
              run={state.run}
              prompt={state.draft.prompt}
              baselineRequested={state.draft.baselineComparison}
            />
          </Card>
        </>
      )}
    </Page>
  );
}
