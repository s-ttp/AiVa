import { Card, EmptyState, ErrorState, LoadingState } from '@aiva/ui';
import { SearchX } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import {
  ApiError,
  isNotImplemented,
  toApiError,
  unwrap,
  type AgentWorkflowOut,
  type AgentWorkflowRequest,
  type GatewayClient,
} from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { Mono } from '../components/inline';
import { NotImplementedState } from '../components/NotImplementedState';
import { QueryView } from '../components/QueryView';
import { AGENT_SCENARIO_ID, toWorkflowRequest, type WorkflowDraft } from '../console/requests';
import { WorkflowForm } from '../console/WorkflowForm';
import { WorkflowView } from '../console/WorkflowView';
import { formatUtc } from '../format';
import { t } from '../i18n';
import { Page } from '../layout/Page';
import { DEFAULT_SIMULATED_IP } from '../run/draft';

function isWorkflow(value: unknown): value is AgentWorkflowOut {
  return typeof value === 'object' && value !== null && 'workflow_id' in value && 'steps' in value;
}

/** A workflow the gateway blocked or paused still returns its record, shown like any other. */
async function post<T>(call: () => Promise<{ data?: T; error?: unknown; response: Response }>): Promise<AgentWorkflowOut> {
  let result;
  try {
    result = await call();
  } catch {
    throw new ApiError({ kind: 'network', message: t('run.networkError') });
  }
  if (isWorkflow(result.data)) return result.data;
  if (isWorkflow(result.error)) return result.error;
  throw toApiError(result.response.status, result.error, undefined, result.response.headers);
}

function idempotency(): Record<string, string> {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? { 'Idempotency-Key': crypto.randomUUID() } : {};
}

function startWorkflow(client: GatewayClient, body: AgentWorkflowRequest) {
  return post(() => client.POST('/v1/agents/workflows', { body, headers: idempotency() }));
}

/** Resumes a paused workflow with an approved request, as the Test Console identity that started it. */
function resumeWorkflow(client: GatewayClient, workflow: AgentWorkflowOut, approvalId: string, sourceIp: string) {
  return post(() =>
    client.POST('/v1/agents/workflows/{workflow_id}/resume', {
      params: { path: { workflow_id: workflow.workflow_id } },
      body: {
        approval_id: approvalId,
        test_profile: { application_id: workflow.application_id, simulated_source_ip: sourceIp },
      },
      headers: idempotency(),
    }),
  );
}

/**
 * AiVa Test Console › Agent workflows (LLD §15 #6): the service-incident agent
 * with per-tool policy, approvals, human-review items and one workflow budget.
 */
export function AgentWorkflowsPage() {
  const [params] = useSearchParams();
  const workflowId = params.get('workflow')?.trim() || null;
  const scenarios = useApiQuery('catalog/scenarios', (client, signal) =>
    unwrap(client.GET('/v1/catalog/scenarios', { signal })),
  );
  const mine = useApiQuery('agents/workflows', (client, signal) =>
    unwrap(client.GET('/v1/agents/workflows', { signal })),
  );
  const [draft, setDraft] = useState<WorkflowDraft | null>(null);
  const start = useApiAction((client, body: AgentWorkflowRequest) => startWorkflow(client, body));
  const resume = useApiAction((client, workflow: AgentWorkflowOut, approvalId: string) =>
    resumeWorkflow(client, workflow, approvalId, draft?.simulatedSourceIp.trim() || DEFAULT_SIMULATED_IP),
  );
  const resultHeading = useRef<HTMLElement>(null);
  // The latest record of the started workflow: the POST response, then each resume.
  const shown = resume.state.status === 'success' ? resume.state.data : start.state.status === 'success' ? start.state.data : null;

  // Move focus to the outcome so keyboard and screen-reader users land on it.
  useEffect(() => {
    if (start.state.status === 'success' || start.state.status === 'error') resultHeading.current?.focus();
  }, [start.state.status]);

  async function run(value: WorkflowDraft) {
    setDraft(value);
    resume.reset();
    const workflow = await start.run(toWorkflowRequest(value));
    if (workflow) mine.reload();
  }

  async function onResume(approvalId: string) {
    if (!shown) return;
    const workflow = await resume.run(shown, approvalId);
    if (workflow) mine.reload();
  }

  return (
    <Page
      title={t('view.agent-workflows.title')}
      eyebrow={t('area.console')}
      description={t('view.agent-workflows.description')}
    >
      <Card title={t('agents.form.cardTitle')} description={t('agents.form.cardDescription')}>
        <QueryView
          state={scenarios.state}
          onRetry={scenarios.reload}
          loadingLabel={t('scenarios.loading')}
          errorTitle={t('scenarios.error')}
        >
          {(catalog) => (
            <WorkflowForm
              scenario={catalog.find((scenario) => scenario.id === AGENT_SCENARIO_ID)}
              applications={[...new Set(catalog.map((scenario) => scenario.application_id))].sort()}
              pending={start.state.status === 'pending'}
              onSubmit={(value) => void run(value)}
            />
          )}
        </QueryView>
      </Card>

      {start.state.status === 'pending' && <LoadingState label={t('agents.form.running')} />}
      {start.state.status === 'error' && (
        <div ref={(node) => void (resultHeading.current = node)} tabIndex={-1} className="outline-none">
          {isNotImplemented(start.state.error) ? (
            <NotImplementedState error={start.state.error} feature={t('agents.feature.start')} />
          ) : (
            <ErrorState title={t('agents.start.error')} description={describeError(start.state.error)} />
          )}
        </div>
      )}
      {shown && (
        <div ref={(node) => void (resultHeading.current = node)} tabIndex={-1} className="outline-none">
          <WorkflowView workflow={shown} fromPost onResume={(id) => void onResume(id)} resumeState={resume.state} />
        </div>
      )}

      <Card title={t('agents.mine.title')} description={t('agents.mine.description')}>
        <QueryView
          state={mine.state}
          onRetry={mine.reload}
          loadingLabel={t('agents.mine.loading')}
          errorTitle={t('agents.mine.error')}
          feature={t('agents.feature.list')}
          empty={{ title: t('agents.mine.empty.title'), description: t('agents.mine.empty.body') }}
          isEmpty={(list) => list.items.length === 0}
        >
          {(list) => <WorkflowTable workflows={list.items} selected={workflowId} />}
        </QueryView>
      </Card>
      {workflowId && <StoredWorkflow key={workflowId} workflowId={workflowId} />}
    </Page>
  );
}

function WorkflowTable({ workflows, selected }: { workflows: readonly AgentWorkflowOut[]; selected: string | null }) {
  return (
    <DataTable
      caption={t('agents.mine.caption')}
      minWidth="44rem"
      columns={[
        { label: t('agents.mine.col.incident') },
        { label: t('agents.mine.col.status') },
        { label: t('agents.mine.col.steps'), numeric: true },
        { label: t('agents.mine.col.created') },
        { label: t('agents.mine.col.open') },
      ]}
      rows={workflows.map((workflow) => ({
        key: workflow.workflow_id,
        cells: [
          <Mono key="i">{workflow.incident_id}</Mono>,
          t(`agents.workflow.status.${workflow.status}`),
          workflow.steps.length,
          formatUtc(workflow.created_at),
          <Link
            key="o"
            to={{ search: `?workflow=${encodeURIComponent(workflow.workflow_id)}` }}
            aria-current={selected === workflow.workflow_id ? 'true' : undefined}
            className="whitespace-nowrap font-semibold text-brand-navy underline underline-offset-2"
          >
            {t('agents.mine.open', { id: workflow.workflow_id })}
          </Link>,
        ],
      }))}
    />
  );
}

/** A stored workflow: hashes only, never tool output or model text. */
function StoredWorkflow({ workflowId }: { workflowId: string }) {
  const { state, reload } = useApiQuery(`agents/workflows/${workflowId}`, (client, signal) =>
    unwrap(client.GET('/v1/agents/workflows/{workflow_id}', { params: { path: { workflow_id: workflowId } }, signal })),
  );
  const resume = useApiAction((client, workflow: AgentWorkflowOut, approvalId: string) =>
    resumeWorkflow(client, workflow, approvalId, DEFAULT_SIMULATED_IP),
  );
  if (state?.status === 'error' && state.error.status === 404) {
    return <EmptyState icon={SearchX} title={t('agents.notFound.title')} description={t('agents.notFound.body')} />;
  }
  return (
    <QueryView
      state={state}
      onRetry={reload}
      loadingLabel={t('agents.detail.loading')}
      errorTitle={t('agents.detail.error')}
      feature={t('agents.feature.detail')}
    >
      {(workflow) => (
        <WorkflowView
          workflow={resume.state.status === 'success' ? resume.state.data : workflow}
          fromPost={resume.state.status === 'success'}
          onResume={(id) => void resume.run(workflow, id)}
          resumeState={resume.state}
        />
      )}
    </QueryView>
  );
}
