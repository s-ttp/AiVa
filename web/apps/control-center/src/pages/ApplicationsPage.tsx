import { Button, Card } from '@aiva/ui';
import { KeyRound } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { unwrap, type ApplicationList } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { useSession } from '../auth/session';
import { DataTable } from '../components/DataTable';
import { BulletList, InlineIds, Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { formatList } from '../format';
import { ApplicationKeys } from '../govern/ApplicationKeys';
import { EnabledBadge } from '../govern/EnabledBadge';
import { EnforcedBy } from '../govern/EnforcedBy';
import { t } from '../i18n';
import { classificationLabel } from '../labels';
import { Page } from '../layout/Page';

/**
 * AiVa Govern › Applications & Identities (GET /v1/admin/applications): the
 * application registry of the published bundle. Grants come from here;
 * policies can only narrow them. Whoever may read an application's keys
 * (ADR-0027) opens them from its row; an administrator issues and revokes them
 * there. Key secrets are shown once, when issued, and never returned.
 */
export function ApplicationsPage() {
  const { can } = useSession();
  const { state, reload } = useApiQuery('admin/applications', (client, signal) =>
    unwrap(client.GET('/v1/admin/applications', { signal })),
  );
  const keys = can('credentials:read');
  const [chosen, setChosen] = useState<string | null>(null);
  // Active keys per application as the open key list last read them, ahead of the registry's own count.
  const [counts, setCounts] = useState<Readonly<Record<string, number>>>({});
  const countKeys = useCallback((active: number) => {
    if (chosen !== null) setCounts((known) => ({ ...known, [chosen]: active }));
  }, [chosen]);
  const heading = useRef<HTMLHeadingElement>(null);
  const application = state?.status === 'success' ? state.data.applications.find((app) => app.id === chosen) : undefined;
  // Opening an application's keys takes focus to them, below the registry.
  useEffect(() => {
    if (chosen !== null) heading.current?.focus();
  }, [chosen]);
  return (
    <Page
      title={t('view.applications-identities.title')}
      eyebrow={t('area.govern')}
      description={t('view.applications-identities.description')}
    >
      <div className="space-y-6">
        <Card title={t('applications.title')} description={t('applications.description')}>
          <QueryView
            state={state}
            onRetry={reload}
            loadingLabel={t('applications.loading')}
            errorTitle={t('applications.error')}
            empty={{ title: t('applications.empty.title'), description: t('applications.empty.body') }}
            isEmpty={(list) => list.applications.length === 0}
          >
            {(list) => <ApplicationTable list={list} keys={keys} counts={counts} onKeys={setChosen} />}
          </QueryView>
        </Card>
        {keys && application && (
          <div className="rounded-card border border-line bg-card p-6">
            <ApplicationKeys key={application.id} application={application} headingRef={heading} onCount={countKeys} />
          </div>
        )}
      </div>
    </Page>
  );
}

function ApplicationTable({
  list,
  keys,
  counts,
  onKeys,
}: {
  list: ApplicationList;
  keys: boolean;
  counts: Readonly<Record<string, number>>;
  onKeys: (id: string) => void;
}) {
  // An explicit grant, never inferred from networks: trusted networks stay private ranges (ADR-0025, ADR-0027).
  const internet = list.applications.filter((app) => app.internet_callers).map((app) => app.name);
  return (
    <div className="space-y-4">
      <EnforcedBy version={list.policy_version} bundleHash={list.bundle_hash} />
      <DataTable
        caption={t('applications.caption')}
        minWidth="56rem"
        columns={[
          { label: t('applications.col.application') },
          { label: t('applications.col.tasks') },
          { label: t('applications.col.models') },
          { label: t('applications.col.tools') },
          { label: t('applications.col.networks') },
          ...(keys ? [{ label: t('applications.col.keys') }] : []),
        ]}
        rows={list.applications.map((app) => ({
          key: app.id,
          cells: [
            <span key="a" className="flex flex-col items-start gap-1">
              <span>{app.name}</span>
              <span className="font-normal text-secondary text-ink-muted">
                <Mono>{app.id}</Mono>
              </span>
              <EnabledBadge enabled={app.enabled} />
              <span className="font-normal text-secondary text-ink-muted">
                {t('applications.department', { department: app.department_id })}
              </span>
              <span className="font-normal text-secondary text-ink-muted">
                {t('applications.floor', { floor: classificationLabel(app.classification_floor) })}
              </span>
            </span>,
            <InlineIds key="t" ids={app.allowed_tasks} empty={t('common.none')} />,
            <InlineIds key="m" ids={app.allowed_models} empty={t('common.none')} />,
            <InlineIds key="tools" ids={app.allowed_tools} empty={t('common.none')} />,
            <span key="n" className="flex flex-col gap-1">
              <BulletList
                items={app.trusted_networks.map((network) => (
                  <span key={network.id}>
                    {network.name} <Mono>{network.cidr}</Mono>
                  </span>
                ))}
                empty={t('applications.networks.none')}
              />
              {app.internet_callers && <span className="text-secondary font-semibold">{t('applications.internet.cell')}</span>}
            </span>,
            ...(keys
              ? [
                  <span key="k" className="flex flex-col items-start gap-2">
                    <span>
                      {t('applications.keys.active', {
                        count: counts[app.id] ?? (app.credentials ?? []).filter((key) => key.active).length,
                      })}
                    </span>
                    <Button
                      variant="secondary"
                      className="min-h-11"
                      aria-label={t('applications.keys.manageFor', { application: app.name })}
                      onClick={() => onKeys(app.id)}
                    >
                      <KeyRound aria-hidden="true" className="size-4" />
                      {t('applications.keys.manage')}
                    </Button>
                  </span>,
                ]
              : []),
          ],
        }))}
      />
      <p className="text-secondary text-ink-muted">{t('applications.secrets')}</p>
      <p className="text-secondary text-ink-muted">
        {internet.length > 0
          ? t('applications.internet.some', { applications: formatList(internet, 'and') })
          : t('applications.internet.none')}
      </p>
    </div>
  );
}
