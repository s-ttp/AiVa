import { KpiTile, ProvenanceBadge, Tabs } from '@aiva/ui';
import type { ReactNode } from 'react';
import type { ChatResponse } from '../api/client';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { BulletList, Mono, Note } from '../components/inline';
import { Section } from '../components/Section';
import { formatCount, formatMicrosUsd, formatPercent, weakestProvenance } from '../format';
import { t } from '../i18n';
import { cacheTypeLabel, classificationLabel, isInternetCaller } from '../labels';
import { listAttribute, stageAttribute } from './trace';

/** Run-workspace views of LLD §22.5, in order. */
const RUN_VIEWS = [
  'response-comparison',
  'prompt-context',
  'routing-decision',
  'cache-tokens',
  'savings-analysis',
] as const;

interface RunDetailsProps {
  run: ChatResponse;
  /** The prompt as the tester submitted it; kept in the browser only, never sent back from the gateway. */
  prompt: string;
  baselineRequested: boolean;
}

export function RunDetails({ run, prompt, baselineRequested }: RunDetailsProps) {
  const views: Record<(typeof RUN_VIEWS)[number], ReactNode> = {
    'response-comparison': <ResponseView run={run} baselineRequested={baselineRequested} />,
    'prompt-context': <PromptView run={run} prompt={prompt} />,
    'routing-decision': <RoutingView run={run} />,
    'cache-tokens': <CacheView run={run} />,
    'savings-analysis': <SavingsView run={run} />,
  };
  return (
    <Tabs
      label={t('run.tabsLabel')}
      items={RUN_VIEWS.map((id) => ({ id, label: t(`run.tab.${id}.label`), content: views[id] }))}
    />
  );
}

// --- Response Comparison --------------------------------------------------------------------------

function ResponseView({ run, baselineRequested }: { run: ChatResponse; baselineRequested: boolean }) {
  const baselineStatus = stageAttribute(run.trace ?? [], 'inference', 'aiva.baseline.status');
  return (
    <div className="space-y-8">
      <Section
        title={t('response.actual.title')}
        aside={run.response ? <ProvenanceBadge provenance={run.response.provenance} /> : undefined}
      >
        {run.response ? (
          <>
            <p className="text-secondary text-ink-muted">
              {t('response.actual.meta', {
                model: run.selected_model ?? '—',
                finish: run.response.finish_reason ?? '—',
              })}
            </p>
            <div className="whitespace-pre-wrap rounded-card border border-line bg-canvas p-4 [overflow-wrap:anywhere]">{run.response.content}</div>
          </>
        ) : (
          <Note>{run.outcome === 'quota_denied' ? t('response.none.quota') : t(`response.none.${run.status}`)}</Note>
        )}
      </Section>
      <Section title={t('response.baseline.title')}>
        {!baselineRequested ? (
          <Note>{t('response.baseline.notRequested')}</Note>
        ) : baselineStatus === 'ok' ? (
          <FactList
            facts={[
              { label: t('response.baseline.status'), value: t('response.baseline.ran') },
              {
                label: t('response.baseline.cost'),
                value: (
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <Mono>{formatMicrosUsd(run.cost.baseline_micros)}</Mono>
                    <ProvenanceBadge provenance={run.cost.provenance} />
                  </span>
                ),
              },
              { label: t('response.baseline.output'), value: t('response.baseline.outputUnavailable') },
            ]}
          />
        ) : (
          <Note>
            {typeof baselineStatus === 'string'
              ? t('response.baseline.notRun', { status: baselineStatus })
              : t('response.baseline.notReached')}
          </Note>
        )}
      </Section>
      <Section title={t('response.quality.title')}>
        <FactList
          facts={[
            {
              label: t('response.quality.result'),
              value: run.assurance.passed ? t('run.assurance.passed') : t('run.assurance.notPassed'),
            },
            { label: t('response.quality.dlp'), value: t(`outputDlp.${run.assurance.output_dlp}`) },
            {
              label: t('response.quality.checks'),
              value: <BulletList items={(run.assurance.checks ?? []).map((check) => <Mono key={check}>{check}</Mono>)} empty={t('common.none')} />,
            },
          ]}
        />
      </Section>
    </div>
  );
}

// --- Prompt & Context Inspector -------------------------------------------------------------------

function PromptView({ run, prompt }: { run: ChatResponse; prompt: string }) {
  const usage = run.usage;
  return (
    <div className="space-y-8">
      <Section title={t('prompt.original.title')} description={t('prompt.original.description')}>
        <div className="whitespace-pre-wrap rounded-card border border-line bg-canvas p-4 [overflow-wrap:anywhere]">{prompt}</div>
      </Section>
      <Section title={t('prompt.tokens.title')} aside={<ProvenanceBadge provenance={usage.provenance} />}>
        <FactList
          facts={[
            { label: t('usage.original_context_tokens'), value: formatCount(usage.original_context_tokens) },
            { label: t('usage.constructed_context_tokens'), value: formatCount(usage.constructed_context_tokens) },
            {
              label: t('usage.upstream_submitted_input_tokens'),
              value: run.cache.upstream_call ? formatCount(usage.upstream_submitted_input_tokens) : t('kpi.noUpstreamCall'),
            },
          ]}
        />
      </Section>
      <Section title={t('prompt.payload.title')}>
        <Note>{run.cache.upstream_call ? t('prompt.payload.notExposed') : t('prompt.payload.noSubmission')}</Note>
      </Section>
      <Section title={t('prompt.passages.title')}>
        <Note>{run.cim_invoked ? t('prompt.passages.notExposed') : run.cim.message}</Note>
      </Section>
    </div>
  );
}

// --- Routing Decision -----------------------------------------------------------------------------

function RoutingView({ run }: { run: ChatResponse }) {
  const { policy, identity, routing, cim } = run;
  const eligible = listAttribute(
    run.trace?.find((span) => span.name === 'rlcd2')?.attributes ?? {},
    'aiva.rlcd.eligible',
  );
  return (
    <div className="space-y-8">
      <Section title={t('routing.policy.title')}>
        <FactList
          facts={[
            { label: t('routing.policy.action'), value: t(`policyAction.${policy.action}`) },
            { label: t('routing.policy.version'), value: policy.policy_version || '—' },
            { label: t('routing.policy.bundle'), value: policy.bundle_hash ? <Mono>{policy.bundle_hash}</Mono> : '—' },
            {
              label: t('routing.policy.rules'),
              value: <BulletList items={policy.matched_rule_ids.map((id) => <Mono key={id}>{id}</Mono>)} empty={t('common.none')} />,
            },
            {
              label: t('routing.policy.declared'),
              value: policy.declared_classification
                ? classificationLabel(policy.declared_classification)
                : t('runForm.classification.none'),
            },
            { label: t('routing.policy.effective'), value: classificationLabel(policy.effective_classification) },
            {
              label: t('routing.policy.allowed'),
              value: <BulletList items={policy.allowed_models.map((id) => <Mono key={id}>{id}</Mono>)} empty={t('common.none')} />,
            },
          ]}
        />
        <DataTable
          caption={t('routing.denied.caption')}
          showCaption
          columns={[{ label: t('routing.denied.model') }, { label: t('routing.denied.reason') }]}
          rows={(policy.denied_candidates ?? []).map((candidate) => ({
            key: candidate.model_id,
            cells: [<Mono key="m">{candidate.model_id}</Mono>, candidate.reason],
          }))}
          empty={t('routing.denied.empty')}
        />
        <DataTable
          caption={t('routing.reasons.caption')}
          showCaption
          columns={[
            { label: t('routing.reasons.rule') },
            { label: t('routing.reasons.message') },
            { label: t('routing.reasons.remediation') },
          ]}
          rows={(policy.reasons ?? []).map((reason, index) => ({
            key: `${reason.rule_id}-${index}`,
            cells: [<Mono key="r">{reason.rule_id}</Mono>, reason.message, reason.remediation ?? '—'],
          }))}
          empty={t('routing.reasons.empty')}
        />
      </Section>
      <Section title={t('routing.identity.title')} description={t('routing.identity.description')}>
        <FactList
          facts={[
            { label: t('routing.identity.application'), value: <Mono>{identity.application_id}</Mono> },
            { label: t('routing.identity.mode'), value: t(`identityMode.${identity.identity_mode}`) },
            { label: t('routing.identity.source'), value: t(`sourceMode.${identity.source_mode}`) },
            {
              label: t('routing.identity.network'),
              value: isInternetCaller(identity.matched_network_id) ? (
                t('run.network.internet')
              ) : identity.matched_network_id ? (
                <Mono>{identity.matched_network_id}</Mono>
              ) : (
                t('run.fact.noNetwork')
              ),
            },
          ]}
        />
      </Section>
      <Section title={t('routing.rlcd.title')} description={t('routing.rlcd.description')}>
        {routing ? (
          <FactList
            facts={[
              { label: t('routing.rlcd.version'), value: <Mono>{routing.rlcd_version}</Mono> },
              { label: t('routing.rlcd.task'), value: routing.task_category ?? '—' },
              { label: t('routing.rlcd.complexity'), value: routing.complexity ?? '—' },
              {
                label: t('routing.rlcd.confidence'),
                value: routing.confidence == null ? t('routing.rlcd.noConfidence') : formatPercent(routing.confidence),
              },
              { label: t('routing.rlcd.strategy'), value: routing.context_strategy ?? '—' },
              {
                label: t('routing.rlcd.eligible'),
                value: <BulletList items={eligible.map((id) => <Mono key={id}>{id}</Mono>)} empty={t('routing.rlcd.noEligible')} />,
              },
              { label: t('routing.rlcd.selected'), value: run.selected_model ? <Mono>{run.selected_model}</Mono> : t('run.fact.noModel') },
              { label: t('routing.rlcd.reasons'), value: <BulletList items={routing.reasons ?? []} empty={t('common.none')} /> },
            ]}
          />
        ) : (
          <Note>{t('routing.rlcd.notReached')}</Note>
        )}
      </Section>
      <Section title={t('routing.cim.title')}>
        <FactList
          facts={[
            { label: t('routing.cim.state'), value: cim.message },
            { label: t('routing.cim.invoked'), value: run.cim_invoked ? t('common.yes') : t('common.no') },
            { label: t('routing.cim.revision'), value: cim.cim_revision ? <Mono>{cim.cim_revision}</Mono> : '—' },
            { label: t('routing.cim.sufficiency'), value: t(`sufficiency.${cim.sufficiency}`) },
          ]}
        />
      </Section>
    </div>
  );
}

// --- Cache & Tokens -------------------------------------------------------------------------------

const USAGE_FIELDS = [
  'original_context_tokens',
  'constructed_context_tokens',
  'upstream_submitted_input_tokens',
  'upstream_output_tokens',
  'cached_input_tokens',
  'reasoning_tokens',
  'cim_input_tokens',
  'cim_output_tokens',
  'retry_tokens',
] as const;

function CacheView({ run }: { run: ChatResponse }) {
  const { cache, usage } = run;
  return (
    <div className="space-y-8">
      <Section title={t('cache.title')} description={t('cache.description')}>
        <FactList
          facts={[
            { label: t('cache.checked'), value: cache.checked ? t('common.yes') : t('cache.notChecked') },
            { label: t('cache.type'), value: cache.cache_type ? cacheTypeLabel(cache.cache_type) : '—' },
            { label: t('cache.result'), value: cache.checked ? (cache.hit ? t('cache.hit') : t('cache.miss')) : '—' },
            {
              label: t('cache.upstream'),
              value: cache.upstream_call ? t('cache.upstreamMade') : t('kpi.noUpstreamCall'),
            },
          ]}
        />
      </Section>
      <Section title={t('tokens.title')} aside={<ProvenanceBadge provenance={usage.provenance} />}>
        {!cache.upstream_call && <Note>{t('tokens.noUpstream')}</Note>}
        <DataTable
          caption={t('tokens.caption')}
          columns={[{ label: t('tokens.col.field') }, { label: t('tokens.col.value'), numeric: true }]}
          rows={USAGE_FIELDS.map((field) => ({
            key: field,
            cells: [t(`usage.${field}`), formatCount(usage[field] ?? 0)],
          }))}
        />
      </Section>
    </div>
  );
}

// --- Savings Analysis -----------------------------------------------------------------------------

function SavingsView({ run }: { run: ChatResponse }) {
  const { cost, budget } = run;
  const upstream = cost.upstream_micros ?? 0;
  const cimCost = cost.cim_micros ?? 0;
  const gpu = cost.gpu_estimated_micros ?? 0;
  const baseline = cost.baseline_micros ?? 0;
  // Null when no call ran on a priced GPU endpoint: the row reads "Not applicable", never $0.
  const gpuProvenance = cost.gpu_estimated_provenance ?? null;
  const amount = (micros: number) => <Mono key="a">{formatMicrosUsd(micros)}</Mono>;
  const badge = <ProvenanceBadge key="p" provenance={cost.provenance} />;
  const rows: Array<[string, ReactNode, ReactNode]> = [
    [t('cost.upstream'), amount(upstream), badge],
    [t('cost.cim'), amount(cimCost), badge],
    gpuProvenance
      ? [t('cost.gpu'), amount(gpu), <ProvenanceBadge key="p" provenance={gpuProvenance} />]
      : [t('cost.gpu'), t('kpi.notApplicable'), '—'],
    [t('cost.baseline'), amount(baseline), badge],
    [t('cost.totalApi'), amount(cost.total_api_micros ?? 0), badge],
  ];
  return (
    <div className="space-y-8">
      <Section title={t('cost.title')} description={t('cost.description', { version: cost.rate_card_version })}>
        <DataTable
          caption={t('cost.caption')}
          columns={[
            { label: t('cost.col.component') },
            { label: t('cost.col.amount'), numeric: true },
            { label: t('cost.col.provenance') },
          ]}
          rows={rows.map(([label, value, provenance]) => ({ key: label, cells: [label, value, provenance] }))}
        />
      </Section>
      <Section title={t('budget.title')} aside={<ProvenanceBadge provenance={budget.provenance} />}>
        {budget.reservation_id ? (
          <FactList
            facts={[
              { label: t('budget.reservation'), value: <Mono>{budget.reservation_id}</Mono> },
              { label: t('budget.reserved'), value: <Mono>{formatMicrosUsd(budget.reserved_micros ?? 0)}</Mono> },
              { label: t('budget.committed'), value: <Mono>{formatMicrosUsd(budget.committed_micros ?? 0)}</Mono> },
              { label: t('budget.released'), value: <Mono>{formatMicrosUsd(budget.released_micros ?? 0)}</Mono> },
            ]}
          />
        ) : (
          <Note>{t('budget.none')}</Note>
        )}
      </Section>
      <Section title={t('savings.title')}>
        {baseline > 0 ? (
          <div className="max-w-md">
            <KpiTile
              label={t('savings.label')}
              value={formatMicrosUsd(baseline - (upstream + cimCost + gpu))}
              provenance={weakestProvenance('estimated', cost.provenance, ...(gpuProvenance ? [gpuProvenance] : []))}
              detail={{
                source: t('savings.source'),
                calculation: t('savings.calculation'),
                window: t('kpi.window.run'),
                pricingVersion: cost.rate_card_version,
                limitations: t('savings.limitations'),
              }}
            />
          </div>
        ) : (
          <Note>{run.cache.hit ? t('savings.cacheHit') : t('savings.noBaseline')}</Note>
        )}
      </Section>
    </div>
  );
}
