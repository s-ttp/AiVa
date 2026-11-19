import { Badge, Card } from '@aiva/ui';
import { Eye, Globe, PencilLine, Server } from 'lucide-react';
import { unwrap, type ToolList } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { InlineIds, Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { EnabledBadge } from '../govern/EnabledBadge';
import { EnforcedBy } from '../govern/EnforcedBy';
import { t } from '../i18n';
import { Page } from '../layout/Page';

/**
 * AiVa Govern › Tool Permissions (GET /v1/admin/tools): the tool registry and
 * the applications granted each tool. AI-PEL evaluates every tool call again
 * at the tool boundary.
 */
export function ToolPermissionsPage() {
  const { state, reload } = useApiQuery('admin/tools', (client, signal) =>
    unwrap(client.GET('/v1/admin/tools', { signal })),
  );
  return (
    <Page
      title={t('view.tool-permissions.title')}
      eyebrow={t('area.govern')}
      description={t('view.tool-permissions.description')}
    >
      <Card title={t('tools.title')} description={t('tools.description')}>
        <QueryView
          state={state}
          onRetry={reload}
          loadingLabel={t('tools.loading')}
          errorTitle={t('tools.error')}
          empty={{ title: t('tools.empty.title'), description: t('tools.empty.body') }}
          isEmpty={(list) => list.tools.length === 0}
        >
          {(list) => <ToolTable list={list} />}
        </QueryView>
      </Card>
    </Page>
  );
}

function ToolTable({ list }: { list: ToolList }) {
  return (
    <div className="space-y-4">
      <EnforcedBy version={list.policy_version} bundleHash={list.bundle_hash} />
      <DataTable
        caption={t('tools.caption')}
        minWidth="56rem"
        columns={[
          { label: t('tools.col.tool') },
          { label: t('tools.col.description') },
          { label: t('tools.col.access') },
          { label: t('tools.col.egress') },
          { label: t('tools.col.status') },
          { label: t('tools.col.applications') },
        ]}
        rows={list.tools.map((tool) => ({
          key: tool.id,
          cells: [
            <span key="t" className="flex flex-col gap-0.5">
              <span>{tool.display_name}</span>
              <span className="font-normal text-secondary text-ink-muted">
                <Mono>{tool.id}</Mono>
              </span>
            </span>,
            tool.description,
            <Badge key="a" icon={tool.access === 'write' ? PencilLine : Eye} tone={tool.access === 'write' ? 'warning' : 'neutral'}>
              {t(`tools.access.${tool.access}`)}
            </Badge>,
            <Badge key="e" icon={tool.egress === 'external' ? Globe : Server} tone={tool.egress === 'external' ? 'warning' : 'neutral'}>
              {t(`tools.egress.${tool.egress}`)}
            </Badge>,
            <EnabledBadge key="s" enabled={tool.enabled} />,
            <InlineIds key="apps" ids={tool.application_ids} empty={t('tools.applications.none')} />,
          ],
        }))}
      />
      <p className="text-secondary text-ink-muted">{t('tools.boundary')}</p>
    </div>
  );
}
