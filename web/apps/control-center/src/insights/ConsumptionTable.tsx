import { Ltr } from '@aiva/ui';
import type { ReactNode } from 'react';
import { DataTable } from '../components/DataTable';
import { t } from '../i18n';
import { SplitCell } from './figures';
import { dimensionLabel } from './labels';
import { formatCount, formatMicrosUsd, formatRatio } from './numbers';
import type { ConsumptionRow } from './types';

/**
 * Consumption rows with input, output, CIM and retry tokens kept apart and
 * spend split by provenance, plus the modelled avoided cost (LLD §21.4, §22.7).
 */
export function ConsumptionTable({
  rows,
  groupBy,
  caption,
  groupHeader,
  link,
}: {
  rows: readonly ConsumptionRow[];
  groupBy: string;
  caption: string;
  groupHeader: string;
  /** The drilldown link for a row, if any. */
  link?: (row: ConsumptionRow, label: string) => ReactNode;
}) {
  return (
    <DataTable
      caption={caption}
      minWidth="72rem"
      empty={t('insights.consumption.empty')}
      columns={[
        { label: groupHeader },
        { label: t('insights.col.requests'), numeric: true },
        { label: t('insights.col.inputTokens'), numeric: true },
        { label: t('insights.col.outputTokens'), numeric: true },
        { label: t('insights.col.cimTokens'), numeric: true },
        { label: t('insights.col.retryTokens'), numeric: true },
        { label: t('insights.col.apiSpend'), numeric: true },
        { label: t('insights.col.gpuEstimate'), numeric: true },
        { label: t('insights.col.avoided'), numeric: true },
        { label: t('insights.col.share'), numeric: true },
      ]}
      rows={rows.map((row) => {
        const usage = row.usage;
        const tokens = usage.tokens;
        const label = dimensionLabel(groupBy, row.group, row.label);
        return {
          key: row.group || '(none)',
          cells: [
            <span key="g" className="flex flex-col">
              {link ? link(row, label) : <bdi>{label}</bdi>}
              {label !== row.group && row.group && <Ltr className="font-mono text-secondary text-ink-muted">{row.group}</Ltr>}
            </span>,
            formatCount(usage.request_count),
            <SplitCell key="i" split={tokens.input_tokens} />,
            <SplitCell key="o" split={tokens.output_tokens} />,
            formatCount(tokens.cim_input_tokens + tokens.cim_output_tokens),
            formatCount(tokens.retry_input_tokens + tokens.retry_output_tokens),
            <SplitCell key="s" split={usage.spend.api} money />,
            <Ltr key="gpu" className="tabular-nums">{formatMicrosUsd(usage.spend.gpu_estimated.value)}</Ltr>,
            <Ltr key="a" className="tabular-nums">{formatMicrosUsd(usage.avoided.micros)}</Ltr>,
            formatRatio(row.spend_share) ?? '—',
          ],
        };
      })}
    />
  );
}
