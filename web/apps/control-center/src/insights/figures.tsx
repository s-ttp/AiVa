import { Badge, KpiTile, Ltr, ProvenanceBadge, type Provenance } from '@aiva/ui';
import { FlaskConical, Gauge } from 'lucide-react';
import type { ReactNode } from 'react';
import { formatList, formatUtc } from '../format';
import { t, tPlural } from '../i18n';
import { formatCompact, formatCount, formatMicrosUsd, provenanceOf, splitParts } from './numbers';
import type { Amount, ProvenanceSplit, ReportMetadata, UsageSummary } from './types';

/** A micro-USD amount with its provenance badge (ADR-0006). */
export function MoneyCell({ amount }: { amount: Amount }) {
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Ltr className="tabular-nums">{formatMicrosUsd(amount.value)}</Ltr>
      <ProvenanceBadge provenance={provenanceOf(amount)} />
    </span>
  );
}

/**
 * A total split by provenance: the total with its weakest label, then each
 * non-zero part, so measured, estimated and simulated amounts are never merged.
 */
export function SplitCell({ split, money }: { split: ProvenanceSplit; money?: boolean }) {
  const format = money ? formatMicrosUsd : formatCount;
  const parts = splitParts(split);
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Ltr className="tabular-nums">{format(split.total)}</Ltr>
      <ProvenanceBadge provenance={provenanceOf(split)} />
      {parts.length > 1 && (
        <span className="flex flex-wrap justify-end gap-x-2 text-secondary text-ink-muted">
          {parts.map((part) => (
            <bdi key={part.provenance}>
              {t('insights.part.value', { part: t(`insights.part.${part.provenance}`), value: format(part.value) })}
            </bdi>
          ))}
        </span>
      )}
    </span>
  );
}

/**
 * Data designation, window, scope, versions and freshness of a report (LLD §22.6, §22.9).
 * `computedAt` marks a report computed at request time, which has no aggregates.
 */
export function ReportMeta({ metadata, computedAt }: { metadata: ReportMetadata; computedAt?: string }) {
  const scope =
    metadata.scope.level === 'tenant'
      ? t('insights.scope.tenant')
      : metadata.scope.level === 'department'
        ? t('insights.scope.department', { department: metadata.scope.department_id ?? '' })
        : t('insights.scope.applications', { applications: formatList(metadata.scope.application_ids ?? []) });
  const synthetic = metadata.data_designation === 'synthetic' || metadata.data_designation === 'mixed';
  const versions = (list: readonly string[] | undefined): ReactNode =>
    list?.length ? <Ltr>{formatList(list)}</Ltr> : t('insights.none');
  // Sentences are isolated in their own direction; identifiers and versions stay left-to-right.
  const facts: { label: string; value: ReactNode }[] = [
    { label: t('insights.meta.window'), value: t('insights.meta.windowValue', { from: metadata.window.from, to: metadata.window.to }) },
    { label: t('insights.meta.scope'), value: scope },
    computedAt
      ? { label: t('report.computedLive'), value: formatUtc(computedAt) }
      : {
          label: t('insights.meta.asOf'),
          value: metadata.aggregates_as_of ? formatUtc(metadata.aggregates_as_of) : t('insights.meta.neverRefreshed'),
        },
    { label: t('insights.meta.taxonomy'), value: versions(metadata.taxonomy_versions) },
    { label: t('insights.meta.classifier'), value: versions(metadata.classifier_versions) },
    { label: t('insights.meta.pricing'), value: <Ltr>{metadata.pricing_snapshot}</Ltr> },
  ];
  return (
    <div className="flex flex-col gap-2 text-secondary">
      <div className="flex flex-wrap items-center gap-2">
        <Badge icon={synthetic ? FlaskConical : Gauge} tone={synthetic ? 'info' : 'neutral'}>
          {t(`insights.designation.${metadata.data_designation}`)}
        </Badge>
        <span className="text-ink-muted">{t('insights.meta.attribution')}</span>
      </div>
      <dl className="flex flex-wrap gap-x-6 gap-y-1">
        {facts.map((fact) => (
          <div key={fact.label} className="flex gap-1.5">
            <dt className="font-semibold text-ink-muted">{fact.label}</dt>
            <dd className="text-ink">
              <bdi>{fact.value}</bdi>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function windowText(metadata: ReportMetadata): string {
  return t('insights.meta.windowValue', { from: metadata.window.from, to: metadata.window.to });
}

/** Headline usage as KPI tiles: requests, tokens, API spend, GPU estimate and avoided cost, kept apart. */
export function UsageTiles({ usage, metadata }: { usage: UsageSummary; metadata: ReportMetadata }) {
  const window = windowText(metadata);
  const pricing = metadata.pricing_snapshot;
  const tokens = usage.tokens;
  const totalTokens = tokens.input_tokens.total + tokens.output_tokens.total;
  const tokenProvenance: Provenance =
    provenanceOf(tokens.input_tokens) === 'measured' ? provenanceOf(tokens.output_tokens) : provenanceOf(tokens.input_tokens);
  const api = usage.spend.api;
  const apiMeasured = provenanceOf(api) === 'measured';
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      <KpiTile
        label={t('overview.kpi.requests.label')}
        value={formatCount(usage.request_count)}
        provenance={provenanceOf(usage)}
        detail={{
          source: t('overview.kpi.requests.source'),
          calculation: t('overview.kpi.requests.calculation'),
          window,
        }}
      />
      <KpiTile
        label={t('overview.kpi.tokens.label')}
        value={formatCompact(totalTokens)}
        provenance={tokenProvenance}
        detail={{
          source: t('overview.kpi.tokens.source'),
          calculation: t('overview.kpi.tokens.calculation', {
            cim: formatCompact(tokens.cim_input_tokens + tokens.cim_output_tokens),
            retries: formatCompact(tokens.retry_input_tokens + tokens.retry_output_tokens),
            baseline: formatCompact(tokens.baseline_input_tokens + tokens.baseline_output_tokens),
            quality: formatCompact((tokens.quality_comparison_input_tokens ?? 0) + (tokens.quality_comparison_output_tokens ?? 0)),
          }),
          window,
          cacheAccounting: t('overview.kpi.cacheAccounting'),
        }}
      />
      <KpiTile
        label={apiMeasured ? t('overview.kpi.apiCost.label') : t('overview.kpi.apiCost.labelUnmeasured')}
        value={formatMicrosUsd(api.total)}
        provenance={provenanceOf(api)}
        detail={{
          source: t('overview.kpi.apiCost.source'),
          calculation: t('overview.kpi.apiCost.calculation', {
            baseline: formatMicrosUsd(usage.spend.baseline_comparison_micros),
            retries: formatMicrosUsd(usage.spend.retry_micros),
            tools: formatMicrosUsd(usage.spend.tool_call_micros),
            quality: formatMicrosUsd(usage.spend.quality_comparison_micros ?? 0),
          }),
          window,
          pricingVersion: pricing,
          cacheAccounting: t('overview.kpi.cacheAccounting'),
          limitations: apiMeasured ? undefined : t('overview.kpi.apiCost.limitations'),
        }}
      />
      <KpiTile
        label={t('overview.kpi.gpuCost.label')}
        value={formatMicrosUsd(usage.spend.gpu_estimated.value)}
        provenance={provenanceOf(usage.spend.gpu_estimated)}
        detail={{
          source: t('overview.kpi.gpuCost.source'),
          calculation: t('overview.kpi.gpuCost.calculation'),
          window,
          pricingVersion: pricing,
          limitations: t('overview.kpi.gpuCost.limitations'),
        }}
      />
      <KpiTile
        label={t('overview.kpi.avoided.label')}
        value={formatMicrosUsd(usage.avoided.micros)}
        provenance={provenanceOf(usage.avoided)}
        detail={{
          source: t('overview.kpi.avoided.source'),
          calculation: tPlural('overview.kpi.avoided.calculation', usage.avoided.calls, {
            tokens: formatCompact(usage.avoided.tokens),
          }),
          window,
          pricingVersion: pricing,
          cacheAccounting: t('overview.kpi.avoided.cacheAccounting'),
        }}
      />
    </div>
  );
}
