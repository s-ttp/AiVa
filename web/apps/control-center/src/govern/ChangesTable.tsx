import { Badge } from '@aiva/ui';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import type { PolicyChange } from '../api/client';
import { DataTable } from '../components/DataTable';
import { BulletList, Mono } from '../components/inline';
import { t } from '../i18n';

/** Differences between two policy bundles, with high-impact changes marked. */
export function ChangesTable({ changes, caption, empty }: { changes: readonly PolicyChange[]; caption: string; empty: string }) {
  return (
    <DataTable
      caption={caption}
      minWidth="48rem"
      columns={[
        { label: t('changes.col.target') },
        { label: t('changes.col.type') },
        { label: t('changes.col.change') },
        { label: t('changes.col.impact') },
        { label: t('changes.col.summary') },
      ]}
      rows={changes.map((change) => ({
        key: `${change.target}:${change.target_id}`,
        cells: [
          <Mono key="id">{change.target_id}</Mono>,
          t(`changes.target.${change.target}`),
          t(`changes.change.${change.change}`),
          change.high_impact ? (
            <Badge key="i" icon={ShieldAlert} tone="warning">
              {t('changes.highImpact')}
            </Badge>
          ) : (
            <Badge key="i" icon={ShieldCheck} tone="neutral">
              {t('changes.standard')}
            </Badge>
          ),
          <BulletList key="s" items={change.summary ?? []} empty="—" />,
        ],
      }))}
      empty={empty}
    />
  );
}
