import { Badge, Button, ErrorState, SelectField, TextField } from '@aiva/ui';
import { CircleCheck, CircleMinus, CircleX, GitCompareArrows, Play, PlayCircle } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import {
  unwrap,
  type PolicyAction,
  type PolicyTestCase,
  type PolicyTestResponse,
  type PolicyTestResult,
  type PolicyVersionDetail,
  type Scenario,
} from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { IdList, Mono } from '../components/inline';
import { Section } from '../components/Section';
import { t, tPlural, tRich } from '../i18n';
import { classificationLabel } from '../labels';
import { DEFAULT_SIMULATED_IP } from '../run/draft';
import { PolicyActionBadge, PolicyDecisionView } from './PolicyDecisionView';
import { caseForScenario } from './simulation';

const ACTIONS: readonly PolicyAction[] = ['allow', 'deny', 'approval_required'];

interface SimulationResult {
  subject: PolicyTestResponse;
  /** The same cases against the published version, when the subject is not the published version. */
  baseline: PolicyTestResponse | null;
}

/**
 * Simulates a policy version against catalog scenarios (POST /v1/admin/policies/test)
 * and, for drafts and archived versions, compares each decision with the
 * published version. Nothing is executed and no model is called.
 */
export function PolicySimulator({
  detail,
  scenarios,
}: {
  detail: PolicyVersionDetail;
  scenarios: readonly Scenario[];
}) {
  const [scenarioId, setScenarioId] = useState(scenarios[0]?.id ?? '');
  const [extraTags, setExtraTags] = useState('');
  const [sourceIp, setSourceIp] = useState(DEFAULT_SIMULATED_IP);
  const [expectedAction, setExpectedAction] = useState<PolicyAction | ''>('');
  const compareWith = detail.status === 'published' ? null : (detail.published_version ?? null);
  const simulation = useApiAction(async (client, cases: PolicyTestCase[]): Promise<SimulationResult> => {
    const test = (version: number) =>
      unwrap(client.POST('/v1/admin/policies/test', { body: { version, cases } }));
    const [subject, baseline] = await Promise.all([
      test(detail.version),
      compareWith === null ? Promise.resolve(null) : test(compareWith),
    ]);
    return { subject, baseline };
  });

  const options = { extraTags, sourceIp, expectedAction };
  function simulateOne(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const scenario = scenarios.find((item) => item.id === scenarioId);
    if (scenario) void simulation.run([caseForScenario(scenario, options)]);
  }
  function simulateAll() {
    void simulation.run(scenarios.map((scenario) => caseForScenario(scenario, { ...options, expectedAction: '' })));
  }

  const pending = simulation.state.status === 'pending';
  return (
    <div className="space-y-6">
      <form onSubmit={simulateOne} className="grid gap-4 md:grid-cols-2">
        <SelectField
          label={t('simulate.scenario')}
          value={scenarioId}
          required
          onChange={(event) => setScenarioId(event.target.value)}
        >
          {scenarios.map((scenario) => (
            <option key={scenario.id} value={scenario.id}>
              {scenario.number}. {scenario.name}
            </option>
          ))}
        </SelectField>
        <SelectField
          label={t('simulate.expected')}
          hint={t('simulate.expected.hint')}
          value={expectedAction}
          onChange={(event) => setExpectedAction(event.target.value as PolicyAction | '')}
        >
          <option value="">{t('simulate.expected.none')}</option>
          {ACTIONS.map((action) => (
            <option key={action} value={action}>
              {t(`policyAction.${action}`)}
            </option>
          ))}
        </SelectField>
        <TextField
          label={t('simulate.tags')}
          hint={t('simulate.tags.hint')}
          value={extraTags}
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          onChange={(event) => setExtraTags(event.target.value)}
        />
        <TextField
          label={t('simulate.sourceIp')}
          hint={t('simulate.sourceIp.hint')}
          value={sourceIp}
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          onChange={(event) => setSourceIp(event.target.value)}
        />
        <div className="flex flex-wrap gap-3 md:col-span-2">
          <Button type="submit" disabled={pending || !scenarioId} aria-busy={pending}>
            <Play className="size-4" />
            {t('simulate.one')}
          </Button>
          <Button variant="secondary" onClick={simulateAll} disabled={pending || scenarios.length === 0}>
            <PlayCircle className="size-4" />
            {tPlural('simulate.all', scenarios.length)}
          </Button>
        </div>
      </form>
      {pending && (
        <p role="status" className="text-ink-muted">
          {t('simulate.running')}
        </p>
      )}
      {simulation.state.status === 'error' && (
        <ErrorState title={t('simulate.error')} description={describeError(simulation.state.error)} />
      )}
      {simulation.state.status === 'success' && <SimulationResults result={simulation.state.data} />}
    </div>
  );
}

function SimulationResults({ result }: { result: SimulationResult }) {
  const { subject, baseline } = result;
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Move focus to the results so keyboard and screen-reader users land on them.
  useEffect(() => headingRef.current?.focus(), [result]);
  const expectations = subject.results.filter((item) => item.expected_action != null).length;
  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <h3 id={headingId} ref={headingRef} tabIndex={-1} className="text-body font-semibold text-brand-navy outline-none">
        {t('simulate.results.title', { version: subject.version })}
      </h3>
      <p className="text-secondary">
        {tPlural('simulate.results.summary', subject.results.length, { version: subject.version })}{' '}
        {expectations > 0
          ? t('simulate.results.passFail', { passed: subject.passed ?? 0, failed: subject.failed ?? 0 })
          : t('simulate.results.noExpectations')}
      </p>
      <p className="text-secondary text-ink-muted wrap-anywhere">
        {tRich('simulate.results.bundle', { hash: <Mono>{subject.bundle_hash}</Mono> })}
      </p>
      <DataTable
        caption={t('simulate.results.caption', { version: subject.version })}
        minWidth="52rem"
        columns={[
          { label: t('simulate.col.case') },
          { label: t('simulate.col.decision', { version: subject.version }) },
          ...(baseline ? [{ label: t('simulate.col.published', { version: baseline.version }) }] : []),
          ...(expectations > 0 ? [{ label: t('simulate.col.expected') }, { label: t('simulate.col.result') }] : []),
        ]}
        rows={subject.results.map((item, index) => {
          const before = baseline?.results[index];
          return {
            key: `${item.name}-${index}`,
            cells: [
              item.name,
              <DecisionCell key="d" item={item} />,
              ...(baseline
                ? [
                    before ? (
                      <div key="b" className="flex flex-col items-start gap-1.5">
                        <PolicyActionBadge action={before.decision.action} />
                        {before.decision.action !== item.decision.action && (
                          <Badge icon={GitCompareArrows} tone="warning">
                            {t('simulate.changed')}
                          </Badge>
                        )}
                      </div>
                    ) : (
                      '—'
                    ),
                  ]
                : []),
              ...(expectations > 0
                ? [
                    item.expected_action ? t(`policyAction.${item.expected_action}`) : t('simulate.expected.none'),
                    <ResultBadge key="r" passed={item.passed ?? null} />,
                  ]
                : []),
            ],
          };
        })}
      />
      <div className="space-y-2">
        {subject.results.map((item, index) => (
          <details key={`${item.name}-${index}`} className="rounded-card border border-line p-4">
            <summary className="cursor-pointer font-semibold">{t('simulate.details', { name: item.name })}</summary>
            <div className="mt-4 space-y-6">
              <Section title={t('simulate.input.title')} description={t('simulate.input.description')}>
                <FactList
                  facts={[
                    { label: t('simulate.input.application'), value: <Mono>{item.input.identity.application_id}</Mono> },
                    {
                      label: t('simulate.input.classification'),
                      value: classificationLabel(item.input.classification.effective),
                    },
                    {
                      label: t('simulate.input.dlp'),
                      value: <IdList ids={item.input.dlp.finding_types ?? []} empty={t('common.none')} />,
                    },
                    {
                      label: t('simulate.input.injection'),
                      value: item.input.dlp.injection_detected ? t('common.yes') : t('common.no'),
                    },
                    { label: t('simulate.input.tags'), value: <IdList ids={item.input.request.tags ?? []} empty={t('common.none')} /> },
                    { label: t('simulate.input.source'), value: item.input.source.ip ? <Mono>{item.input.source.ip}</Mono> : '—' },
                  ]}
                />
              </Section>
              <Section title={t('simulate.decision.title', { version: item.decision.policy_version || '—' })}>
                <PolicyDecisionView decision={item.decision} caption={item.name} />
              </Section>
            </div>
          </details>
        ))}
      </div>
    </section>
  );
}

function DecisionCell({ item }: { item: PolicyTestResult }) {
  return (
    <div className="flex flex-col items-start gap-1.5">
      <PolicyActionBadge action={item.decision.action} />
      <span className="text-secondary text-ink-muted">
        <IdList ids={item.decision.matched_rule_ids ?? []} empty={t('simulate.noRules')} />
      </span>
    </div>
  );
}

function ResultBadge({ passed }: { passed: boolean | null }) {
  if (passed === null) {
    return (
      <Badge icon={CircleMinus} tone="neutral">
        {t('simulate.result.none')}
      </Badge>
    );
  }
  return passed ? (
    <Badge icon={CircleCheck} tone="success">
      {t('simulate.result.passed')}
    </Badge>
  ) : (
    <Badge icon={CircleX} tone="danger">
      {t('simulate.result.failed')}
    </Badge>
  );
}
