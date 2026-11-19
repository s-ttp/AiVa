import { Badge, Ltr, cx, type BadgeTone } from '@aiva/ui';
import {
  Circle,
  CircleCheck,
  CircleDashed,
  CircleHelp,
  CircleStop,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import type { ExecutionPath, TraceSpan } from '../api/client';
import { Section } from '../components/Section';
import { formatDurationMs, formatList, formatMicrosUsd } from '../format';
import { t } from '../i18n';
import { pathLabel } from '../labels';
import { rootSpan, stageNodes, type StageName, type StageNode, type StageStatus } from './trace';

/** The branches of LLD §6 after the first RLCD assessment, in decision order. */
const PATHS: readonly ExecutionPath[] = ['cache-return', 'sovereign-direct', 'local-fast', 'cim-assisted'];

const STATUS_BADGES: Record<StageStatus, { icon: LucideIcon; tone: BadgeTone }> = {
  executed: { icon: CircleCheck, tone: 'success' },
  skipped: { icon: CircleDashed, tone: 'neutral' },
  halted: { icon: CircleStop, tone: 'info' },
  error: { icon: TriangleAlert, tone: 'danger' },
  missing: { icon: CircleHelp, tone: 'warning' },
};

/** Attributes worth showing inline for each stage; every attribute is listed under the stage too. */
const HIGHLIGHTS: Record<StageName, readonly string[]> = {
  ingress: ['aiva.application_id', 'aiva.identity_mode', 'aiva.source_mode'],
  policy: ['aiva.dlp.finding_types', 'aiva.dlp.injection_detected'],
  cache: ['aiva.cache.type', 'aiva.cache.hit'],
  rlcd1: ['aiva.rlcd.task_category', 'aiva.rlcd.complexity', 'aiva.rlcd.local_fast_eligible'],
  retrieval: ['aiva.retrieval.supplied', 'aiva.retrieval.retrieved'],
  cim: ['aiva.cim.revision', 'aiva.cim.provenance', 'aiva.cim.egress'],
  context_guard: ['aiva.guard.passed', 'aiva.guard.failure_count'],
  rlcd2: ['aiva.rlcd.model_id', 'aiva.rlcd.eligible'],
  quota: ['aiva.quota.reserved_micros', 'aiva.quota.provenance'],
  preflight: ['aiva.preflight.egress', 'aiva.preflight.input_tokens'],
  inference: ['aiva.inference.model_id', 'aiva.inference.provenance', 'aiva.baseline.status'],
  assurance: ['aiva.assurance.passed', 'aiva.assurance.output_dlp'],
  reconciliation: ['aiva.reconciliation.api_cost_micros', 'aiva.reconciliation.released_micros'],
};

function formatAttribute(key: string, value: unknown): string {
  if (Array.isArray(value)) return value.length ? formatList(value.map(String)) : t('graph.attr.none');
  if (key.endsWith('_micros') && typeof value === 'number' && Number.isSafeInteger(value)) {
    return formatMicrosUsd(value);
  }
  return String(value);
}

interface ExecutionGraphProps {
  spans: readonly TraceSpan[];
  executionPath: ExecutionPath;
}

/** The execution graph of one run, built only from its recorded trace (LLD §14, ADR-0004). */
export function ExecutionGraph({ spans, executionPath }: ExecutionGraphProps) {
  if (spans.length === 0) {
    return <p className="text-secondary text-ink-muted">{t('graph.noTrace')}</p>;
  }
  const nodes = stageNodes(spans);
  const root = rootSpan(spans);
  const longest = Math.max(...nodes.map((node) => node.span?.duration_ms ?? 0), 0.001);
  return (
    <div className="space-y-6">
      <Section
        title={t('graph.path.title')}
        description={executionPath === 'none' ? t('graph.path.none') : t('graph.path.description')}
      >
        <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {PATHS.map((path) => {
            const taken = path === executionPath;
            const Icon = taken ? CircleCheck : Circle;
            return (
              <li
                key={path}
                className={cx(
                  'flex items-center gap-2 rounded-card border px-3 py-2',
                  taken ? 'border-brand-teal bg-brand-teal/10 font-semibold text-ink' : 'border-line text-ink-muted',
                )}
              >
                <Icon className={cx('size-4 shrink-0', taken && 'text-brand-navy')} />
                <span>{pathLabel(path)}</span>
                <span className="ms-auto text-secondary">{taken ? t('graph.path.taken') : t('graph.path.notTaken')}</span>
              </li>
            );
          })}
        </ul>
      </Section>
      <Section
        title={t('graph.stages.title')}
        description={
          root
            ? t('graph.stages.descriptionWithTotal', { total: formatDurationMs(root.duration_ms) })
            : t('graph.stages.description')
        }
      >
        <ol className="space-y-2">
          {nodes.map((node, index) => (
            <StageRow key={node.name} node={node} index={index} longest={longest} />
          ))}
        </ol>
      </Section>
    </div>
  );
}

function StageRow({ node, index, longest }: { node: StageNode; index: number; longest: number }) {
  const { icon, tone } = STATUS_BADGES[node.status];
  const attributes = Object.entries(node.span?.attributes ?? {}).filter(
    ([key]) => key !== 'aiva.stage' && key !== 'aiva.stage.status',
  );
  const highlights = HIGHLIGHTS[node.name].flatMap((key) =>
    node.span && key in node.span.attributes ? [[key, node.span.attributes[key]] as const] : [],
  );
  const dimmed = node.status === 'skipped' || node.status === 'missing';
  return (
    <li className="grid grid-cols-[2rem_minmax(0,1fr)] gap-3">
      <span
        aria-hidden="true"
        className={cx(
          'mt-3 flex size-8 items-center justify-center rounded-full border text-secondary font-semibold tabular-nums',
          dimmed ? 'border-dashed border-ink-muted text-ink-muted' : 'border-brand-navy bg-brand-navy text-on-brand',
        )}
      >
        {index + 1}
      </span>
      <div className={cx('rounded-card border p-3', dimmed ? 'border-dashed border-line bg-canvas' : 'border-line bg-card')}>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <p className="font-semibold text-ink">{t(`stage.${node.name}`)}</p>
          <Badge icon={icon} tone={tone}>
            {t(`graph.status.${node.status}`)}
          </Badge>
          {node.span && (
            <span className="ms-auto text-secondary tabular-nums text-ink-muted">
              <Ltr>{formatDurationMs(node.span.duration_ms)}</Ltr>
            </span>
          )}
        </div>
        {node.span && (
          <div aria-hidden="true" className="mt-2 h-1 rounded-full bg-line">
            <div
              className="h-1 rounded-full bg-brand-teal"
              style={{ inlineSize: `${Math.max(1, (node.span.duration_ms / longest) * 100)}%` }}
            />
          </div>
        )}
        {node.reason && <p className="mt-2 text-secondary text-ink">{node.reason}</p>}
        {node.status === 'missing' && <p className="mt-2 text-secondary text-ink-muted">{t('graph.missing')}</p>}
        {highlights.length > 0 && (
          <ul className="mt-2 flex flex-wrap gap-2" aria-label={t('graph.highlights', { stage: t(`stage.${node.name}`) })}>
            {highlights.map(([key, value]) => (
              <li key={key} className="max-w-full rounded-md bg-canvas px-2 py-0.5 text-secondary wrap-anywhere">
                <Ltr className="font-mono">
                  {key.replace(/^aiva\./, '')}: {formatAttribute(key, value)}
                </Ltr>
              </li>
            ))}
          </ul>
        )}
        {attributes.length > 0 && (
          <details className="mt-2 text-secondary">
            <summary className="cursor-pointer font-medium text-brand-navy underline underline-offset-2">
              {t('graph.attributes', { count: attributes.length })}
            </summary>
            <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,16rem)_1fr]">
              {attributes.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-mono text-ink-muted wrap-anywhere">
                    <Ltr>{key}</Ltr>
                  </dt>
                  <dd className="min-w-0 font-mono wrap-anywhere">
                    <Ltr>{formatAttribute(key, value)}</Ltr>
                  </dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </div>
    </li>
  );
}
