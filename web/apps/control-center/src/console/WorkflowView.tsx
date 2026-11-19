import { Badge, Button, buttonClasses, Card, ErrorState, ProvenanceBadge, StatusBadge, type BadgeTone } from '@aiva/ui';
import {
  Bot,
  CircleCheck,
  CircleSlash,
  CircleX,
  Hand,
  RefreshCw,
  ShieldAlert,
  SkipForward,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { useId } from 'react';
import { Link } from 'react-router';
import {
  isNotImplemented,
  unwrap,
  type AgentStepOut,
  type AgentWorkflowOut,
  type ApprovalRequest,
  type WorkflowBudgetOut,
} from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction, type ActionState } from '../api/useApiAction';
import { useSession } from '../auth/session';
import { FactList, type Fact } from '../components/FactList';
import { BulletList, IdList, Mono, Note } from '../components/inline';
import { NotImplementedState } from '../components/NotImplementedState';
import { Section } from '../components/Section';
import { formatMicrosUsd, formatUtc, isProvenance } from '../format';
import { t } from '../i18n';
import { classificationLabel, designationLabel, pathLabel } from '../labels';

const STEP_BADGES: Record<AgentStepOut['status'], { icon: LucideIcon; tone: BadgeTone }> = {
  completed: { icon: CircleCheck, tone: 'success' },
  denied: { icon: CircleX, tone: 'danger' },
  approval_required: { icon: ShieldAlert, tone: 'warning' },
  human_review: { icon: Hand, tone: 'info' },
  skipped: { icon: SkipForward, tone: 'neutral' },
  failed: { icon: CircleSlash, tone: 'danger' },
};

const WORKFLOW_BADGES: Record<AgentWorkflowOut['status'], { icon: LucideIcon; tone: BadgeTone }> = {
  completed: { icon: CircleCheck, tone: 'success' },
  completed_with_denials: { icon: CircleCheck, tone: 'warning' },
  approval_required: { icon: ShieldAlert, tone: 'warning' },
  blocked: { icon: CircleX, tone: 'danger' },
  budget_exhausted: { icon: CircleSlash, tone: 'danger' },
  failed: { icon: CircleSlash, tone: 'danger' },
};

function Money({ micros, provenance }: { micros: number | null | undefined; provenance?: string }) {
  if (micros == null) return <span className="text-ink-muted">—</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Mono>{formatMicrosUsd(micros)}</Mono>
      {isProvenance(provenance) && <ProvenanceBadge provenance={provenance} />}
    </span>
  );
}

function TextOutput({ text, retained }: { text: string | null | undefined; retained: boolean }) {
  if (text) return <p className="whitespace-pre-wrap rounded-card border border-line bg-canvas p-3 text-secondary">{text}</p>;
  return <span className="text-ink-muted">{retained ? t('agents.output.none') : t('agents.output.notRetained')}</span>;
}

function RunLink({ runId }: { runId: string }) {
  return (
    <Link to={`/admin/audit?run=${encodeURIComponent(runId)}`} className="font-semibold text-brand-navy underline underline-offset-2">
      <Mono>{runId}</Mono>
    </Link>
  );
}

function Step({ step, fromPost }: { step: AgentStepOut; fromPost: boolean }) {
  const { can } = useSession();
  const headingId = useId();
  const { icon, tone } = STEP_BADGES[step.status];
  const facts: Fact[] = [];
  if (step.model) {
    const model = step.model;
    facts.push(
      { label: t('agents.step.run'), value: <RunLink runId={model.run_id} /> },
      { label: t('agents.step.model'), value: model.selected_model ? <Mono>{model.selected_model}</Mono> : t('run.fact.noModel') },
      { label: t('agents.step.path'), value: pathLabel(model.execution_path) },
      { label: t('agents.step.cim'), value: t(`cimState.${model.cim_state}`) },
      {
        label: t('agents.step.classification'),
        value: model.effective_classification ? classificationLabel(model.effective_classification) : '—',
      },
      { label: t('agents.step.cost'), value: <Money micros={model.cost_micros} provenance={model.provenance} /> },
      { label: t('agents.step.response'), value: <TextOutput text={model.response} retained={fromPost} /> },
    );
  }
  if (step.tool) {
    const tool = step.tool;
    facts.push(
      { label: t('agents.step.tool'), value: <Mono>{tool.tool_id}</Mono> },
      {
        label: t('agents.step.access'),
        value: tool.access && tool.egress
          ? t('agents.step.accessValue', {
              access: t(`tools.access.${tool.access}`),
              egress: t(`tools.egress.${tool.egress}`),
            })
          : t('agents.step.unregistered'),
      },
      {
        label: t('agents.step.arguments'),
        value: (
          <BulletList
            items={Object.entries(tool.arguments ?? {}).map(([name, value]) => (
              <Mono key={name}>{t('agents.step.argument', { name, value })}</Mono>
            ))}
            empty={t('common.none')}
          />
        ),
      },
      { label: t('agents.step.policy'), value: t(`policyAction.${tool.policy_action}`) },
      { label: t('agents.step.rules'), value: <IdList ids={tool.rule_ids ?? []} empty={t('common.none')} /> },
      { label: t('agents.step.executed'), value: t(tool.executed ? 'common.yes' : 'common.no') },
    );
    if (tool.approval_id) {
      facts.push({
        label: t('agents.step.approval'),
        value: can('approvals:read') ? (
          <Link
            to={`/admin/govern/approval-queue?approval=${encodeURIComponent(tool.approval_id)}`}
            className="font-semibold text-brand-navy underline underline-offset-2"
          >
            <Mono>{tool.approval_id}</Mono>
          </Link>
        ) : (
          <span className="flex flex-col">
            <Mono>{tool.approval_id}</Mono>
            <span className="text-secondary text-ink-muted">{t('agents.step.approvalQueue')}</span>
          </span>
        ),
      });
    }
    if (step.authorized_by_run_id) {
      facts.push({ label: t('agents.step.authorizedBy'), value: <RunLink runId={step.authorized_by_run_id} /> });
    }
    if (tool.evidence_id) facts.push({ label: t('agents.step.evidence'), value: <Mono>{tool.evidence_id}</Mono> });
    facts.push({
      label: t('agents.step.output'),
      value: tool.executed ? (
        <TextOutput text={tool.output} retained={fromPost} />
      ) : (
        <span className="text-ink-muted">{t('agents.step.notExecuted')}</span>
      ),
    });
    if (tool.output_sha256) facts.push({ label: t('agents.step.outputHash'), value: <Mono>{tool.output_sha256}</Mono> });
  }
  facts.push({
    label: t('agents.step.time'),
    value: t('agents.step.timeValue', { start: formatUtc(step.started_at), end: formatUtc(step.completed_at) }),
  });
  return (
    <li aria-labelledby={headingId} className="space-y-3 rounded-card border border-line p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h4 id={headingId} className="font-semibold text-brand-navy">
          {t('agents.step.heading', { number: step.index + 1, name: step.name })}
        </h4>
        <Badge icon={step.kind === 'model' ? Bot : Wrench}>{t(`agents.step.kind.${step.kind}`)}</Badge>
        <Badge icon={icon} tone={tone}>
          {t(`agents.step.status.${step.status}`)}
        </Badge>
      </div>
      {step.message && step.message !== t(`agents.step.status.${step.status}`) && (
        <p className="text-secondary text-ink-muted">{step.message}</p>
      )}
      <FactList facts={facts} />
    </li>
  );
}

/** What the gateway does at the cap (`BudgetStatus.on_exhaustion`); an unknown action shows its code. */
const EXHAUSTION_ACTIONS = ['block', 'approval_required', 'lower_cost_route'] as const;

function isExhaustionAction(value: string): value is (typeof EXHAUSTION_ACTIONS)[number] {
  return (EXHAUSTION_ACTIONS as readonly string[]).includes(value);
}

function Budget({ budget }: { budget: WorkflowBudgetOut }) {
  return (
    <Section title={t('agents.budget.title')} description={t('agents.budget.description')}>
      {budget.cap_micros == null ? (
        <Note>{t('agents.budget.none')}</Note>
      ) : (
        <FactList
          facts={[
            { label: t('agents.budget.scope'), value: <Mono>{budget.scope_id}</Mono> },
            { label: t('agents.budget.cap'), value: <Money micros={budget.cap_micros} /> },
            { label: t('agents.budget.committed'), value: <Money micros={budget.committed_micros} provenance={budget.provenance} /> },
            { label: t('agents.budget.remaining'), value: <Money micros={budget.remaining_micros} provenance={budget.provenance} /> },
            {
              label: t('agents.budget.exhaustion'),
              value: !budget.on_exhaustion ? (
                '—'
              ) : isExhaustionAction(budget.on_exhaustion) ? (
                t(`agents.budget.exhaustion.${budget.on_exhaustion}`)
              ) : (
                <Mono>{budget.on_exhaustion}</Mono>
              ),
            },
          ]}
        />
      )}
    </Section>
  );
}

/** Checks a pending approval and, once approved, resumes the workflow with it. */
function Resume({
  approvalId,
  onResume,
  resuming,
}: {
  approvalId: string;
  onResume: (approvalId: string) => void;
  resuming: boolean;
}) {
  const check = useApiAction((client) =>
    unwrap(client.GET('/v1/approvals/{approval_id}', { params: { path: { approval_id: approvalId } } })),
  );
  const current: ApprovalRequest | null = check.state.status === 'success' ? check.state.data : null;
  return (
    <li className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <Mono>{approvalId}</Mono>
        {current && <span className="text-secondary">{t(`approvals.status.${current.status}`)}</span>}
        <Button variant="secondary" onClick={() => void check.run()} disabled={check.state.status === 'pending'}>
          <RefreshCw className="size-4" />
          {t('runApproval.check')}
        </Button>
        {current?.status === 'approved' && (
          <Button onClick={() => onResume(approvalId)} disabled={resuming} aria-busy={resuming}>
            {resuming ? t('agents.resume.running') : t('agents.resume.submit')}
          </Button>
        )}
      </div>
      {check.state.status === 'error' && (
        <ErrorState title={t('runApproval.checkFailed')} description={describeError(check.state.error)} />
      )}
    </li>
  );
}

interface WorkflowViewProps {
  workflow: AgentWorkflowOut;
  /** True for the POST (or resume) response, which carries tool output and model text. */
  fromPost: boolean;
  onResume: (approvalId: string) => void;
  resumeState: ActionState<AgentWorkflowOut>;
}

/**
 * An agent workflow: status, the shared workflow budget, each step with its
 * policy decision, approvals and human-review items. Tool output and model
 * text exist only in the POST response; a stored workflow keeps hashes, so
 * missing text reads "Output not retained", never an empty box.
 */
export function WorkflowView({ workflow, fromPost, onResume, resumeState }: WorkflowViewProps) {
  const { can } = useSession();
  const { icon, tone } = WORKFLOW_BADGES[workflow.status];
  const provenance = isProvenance(workflow.provenance) ? workflow.provenance : null;
  return (
    <Card title={t('agents.workflow.title')} description={<Mono>{workflow.workflow_id}</Mono>}>
      <div className="space-y-8">
        <div className="flex flex-wrap items-center gap-2">
          {workflow.status === 'approval_required' ? (
            <StatusBadge status="approvalRequired" />
          ) : (
            <Badge icon={icon} tone={tone}>
              {t(`agents.workflow.status.${workflow.status}`)}
            </Badge>
          )}
          {provenance && <ProvenanceBadge provenance={provenance} />}
          <span className="text-secondary text-ink-muted">{designationLabel(workflow.data_designation)}</span>
        </div>
        {workflow.error && (
          <ErrorState title={t('agents.workflow.error')} description={t('agents.workflow.errorValue', { message: workflow.error.message, code: workflow.error.code })} />
        )}
        <FactList
          facts={[
            { label: t('agents.workflow.incident'), value: <Mono>{workflow.incident_id}</Mono> },
            {
              label: t('agents.workflow.agent'),
              value: t('agents.workflow.agentValue', { agent: workflow.agent, version: workflow.definition_version }),
            },
            { label: t('agents.workflow.application'), value: <Mono>{workflow.application_id}</Mono> },
            { label: t('agents.workflow.created'), value: formatUtc(workflow.created_at) },
            {
              label: t('agents.workflow.completed'),
              value: workflow.completed_at ? formatUtc(workflow.completed_at) : t('agents.workflow.notCompleted'),
            },
          ]}
        />
        <Budget budget={workflow.budget} />
        <Section title={t('agents.steps.title')} description={t('agents.steps.description')}>
          {workflow.steps.length === 0 ? (
            <Note>{t('agents.steps.empty')}</Note>
          ) : (
            <ol className="space-y-3">
              {workflow.steps.map((step) => (
                <Step key={step.index} step={step} fromPost={fromPost} />
              ))}
            </ol>
          )}
        </Section>
        {(workflow.human_review_tool_ids ?? []).length > 0 && (
          <Section title={t('agents.humanReview.title')} description={t('agents.step.status.human_review')}>
            <IdList ids={workflow.human_review_tool_ids ?? []} empty={t('common.none')} />
          </Section>
        )}
        {(workflow.pending_approval_ids ?? []).length > 0 && (
          <Section title={t('agents.resume.title')} description={t('agents.resume.description')}>
            <ul className="space-y-3">
              {(workflow.pending_approval_ids ?? []).map((approvalId) => (
                <Resume
                  key={approvalId}
                  approvalId={approvalId}
                  onResume={onResume}
                  resuming={resumeState.status === 'pending'}
                />
              ))}
            </ul>
            {can('approvals:read') && (
              <Link to="/admin/govern/approval-queue" className={buttonClasses('secondary')}>
                {t('runApproval.openQueue')}
              </Link>
            )}
            {resumeState.status === 'error' &&
              (isNotImplemented(resumeState.error) ? (
                <NotImplementedState error={resumeState.error} feature={t('agents.feature.resume')} />
              ) : (
                <ErrorState title={t('agents.resume.error')} description={describeError(resumeState.error)} />
              ))}
          </Section>
        )}
        <Section title={t('agents.report.title')}>
          <TextOutput text={workflow.report} retained={fromPost && workflow.completed_at != null} />
        </Section>
      </div>
    </Card>
  );
}
