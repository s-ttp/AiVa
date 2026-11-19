import { Badge, Card, Ltr } from '@aiva/ui';
import { Archive, BadgeCheck, FilePen } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import {
  unwrap,
  type PolicyDocument,
  type PolicyPublishResponse,
  type PolicyVersionDetail,
  type PolicyVersionSummary,
} from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { useSession } from '../auth/session';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { BulletList, Mono, Note } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { formatUtc } from '../format';
import { ChangesTable } from '../govern/ChangesTable';
import { DraftEditor } from '../govern/DraftEditor';
import { enforcement, matchConditions } from '../govern/policyText';
import { PolicySimulator } from '../govern/PolicySimulator';
import { PublishPanel, PublishSummary } from '../govern/PublishPanel';
import { t } from '../i18n';
import { Page } from '../layout/Page';

const STATUS_BADGES = {
  published: { icon: BadgeCheck, tone: 'success' },
  draft: { icon: FilePen, tone: 'neutral' },
  archived: { icon: Archive, tone: 'neutral' },
} as const;

function VersionStatusBadge({ status }: { status: PolicyVersionSummary['status'] }) {
  const badge = STATUS_BADGES[status];
  return (
    <Badge icon={badge.icon} tone={badge.tone}>
      {t(`policies.status.${status}`)}
    </Badge>
  );
}

function parseVersion(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  return Number(value);
}

/**
 * AiVa Govern › Policy Rules (LLD §22.6, ADR-0012): AI-PEL policy versions and
 * their lifecycle. Draft, simulate, review the changes against the published
 * version, publish with an approval note, and roll back to an earlier bundle.
 */
export function PolicyVersionsPage() {
  const [params, setParams] = useSearchParams();
  const selected = parseVersion(params.get('version'));
  const versions = useApiQuery('admin/policies', (client, signal) =>
    unwrap(client.GET('/v1/admin/policies', { signal })),
  );
  const [outcome, setOutcome] = useState<PolicyPublishResponse | null>(null);
  const [revision, setRevision] = useState(0);

  function onPublished(result: PolicyPublishResponse) {
    setOutcome(result);
    setRevision((n) => n + 1);
    versions.reload();
  }

  function onDraftCreated(draft: PolicyVersionDetail) {
    setOutcome(null);
    versions.reload();
    setParams({ version: String(draft.version) });
  }

  return (
    <Page
      title={t('view.policy-rules.title')}
      eyebrow={t('area.govern')}
      description={t('view.policy-rules.description')}
    >
      <Card title={t('policies.versions.title')} description={t('policies.versions.description')}>
        <QueryView
          state={versions.state}
          onRetry={versions.reload}
          loadingLabel={t('policies.loading')}
          errorTitle={t('policies.error')}
          empty={{ title: t('policies.empty.title'), description: t('policies.empty.body') }}
          isEmpty={(list) => list.length === 0}
        >
          {(list) => <VersionTable versions={list} selected={selected} />}
        </QueryView>
      </Card>
      {outcome && outcome.version === selected && <PublishOutcome result={outcome} />}
      {selected === null ? (
        <Card>
          <p className="text-body text-ink-muted">{t('policies.detail.idle')}</p>
        </Card>
      ) : (
        <VersionWorkspace
          key={`${selected}#${revision}`}
          version={selected}
          onPublished={onPublished}
          onDraftCreated={onDraftCreated}
        />
      )}
    </Page>
  );
}

function VersionTable({ versions, selected }: { versions: readonly PolicyVersionSummary[]; selected: number | null }) {
  return (
    <DataTable
      caption={t('policies.versions.caption')}
      minWidth="50rem"
      columns={[
        { label: t('policies.col.version'), numeric: true },
        { label: t('policies.col.status') },
        { label: t('policies.col.hash') },
        { label: t('policies.col.created') },
        { label: t('policies.col.published') },
        { label: t('policies.col.notes') },
        { label: t('policies.col.open') },
      ]}
      rows={versions.map((version) => ({
        key: String(version.version),
        cells: [
          version.version,
          <VersionStatusBadge key="s" status={version.status} />,
          <span key="h" title={version.bundle_hash}>
            <Ltr className="font-mono text-secondary">
              {t('policies.hashPrefix', { prefix: version.bundle_hash.slice(0, 12) })}
            </Ltr>
          </span>,
          formatUtc(version.created_at),
          version.published_at ? (
            formatUtc(version.published_at)
          ) : (
            t('policies.notPublished')
          ),
          version.notes ?? '—',
          <Link
            key="o"
            to={{ search: `?version=${version.version}` }}
            aria-current={selected === version.version ? 'true' : undefined}
            className="whitespace-nowrap font-semibold text-brand-navy underline underline-offset-2"
          >
            {t('policies.open', { version: version.version })}
          </Link>,
        ],
      }))}
    />
  );
}

function PublishOutcome({ result }: { result: PolicyPublishResponse }) {
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Move focus to the outcome so keyboard and screen-reader users land on it.
  useEffect(() => headingRef.current?.focus(), [result]);
  return (
    <section aria-labelledby={headingId} className="rounded-card border border-success/40 bg-card p-6">
      <h2 id={headingId} ref={headingRef} tabIndex={-1} className="text-heading font-semibold text-brand-navy outline-none">
        {t(result.kind === 'rollback' ? 'publish.done.rollback.title' : 'publish.done.publish.title', {
          version: result.version,
        })}
      </h2>
      <div className="mt-4">
        <PublishSummary result={result} />
      </div>
    </section>
  );
}

function VersionWorkspace({
  version,
  onPublished,
  onDraftCreated,
}: {
  version: number;
  onPublished: (result: PolicyPublishResponse) => void;
  onDraftCreated: (draft: PolicyVersionDetail) => void;
}) {
  const { can } = useSession();
  const detail = useApiQuery(`admin/policies/${version}`, (client, signal) =>
    unwrap(client.GET('/v1/admin/policies/{version}', { params: { path: { version } }, signal })),
  );
  const scenarios = useApiQuery(can('policies:write') ? 'catalog/scenarios' : null, (client, signal) =>
    unwrap(client.GET('/v1/catalog/scenarios', { signal })),
  );
  return (
    <QueryView
      state={detail.state}
      onRetry={detail.reload}
      loadingLabel={t('policies.detail.loading', { version })}
      errorTitle={t('policies.detail.error', { version })}
    >
      {(data) => (
        <>
          <Card title={t('policies.detail.title', { version })}>
            <VersionReview detail={data} />
          </Card>
          <Card title={t('simulate.title', { version })} description={t('simulate.description')}>
            {can('policies:write') ? (
              <QueryView
                state={scenarios.state}
                onRetry={scenarios.reload}
                loadingLabel={t('scenarios.loading')}
                errorTitle={t('scenarios.error')}
                empty={{ title: t('scenarios.empty.title'), description: t('scenarios.empty.body') }}
                isEmpty={(list) => list.length === 0}
              >
                {(list) => <PolicySimulator detail={data} scenarios={list} />}
              </QueryView>
            ) : (
              <Note>{t('simulate.readOnly')}</Note>
            )}
          </Card>
          <Card
            title={t(data.status === 'archived' ? 'publish.rollback.title' : 'publish.publish.title', { version })}
            description={t('publish.description')}
          >
            <PublishPanel detail={data} onPublished={onPublished} />
          </Card>
          <Card title={t('draft.title')} description={t('draft.description', { version })}>
            {can('policies:write') ? (
              <DraftEditor base={data} onCreated={onDraftCreated} />
            ) : (
              <Note>{t('draft.readOnly')}</Note>
            )}
          </Card>
        </>
      )}
    </QueryView>
  );
}

function VersionReview({ detail }: { detail: PolicyVersionDetail }) {
  const comparedWith = detail.published_version;
  return (
    <div className="space-y-8">
      <FactList
        facts={[
          { label: t('policies.fact.status'), value: <VersionStatusBadge status={detail.status} /> },
          { label: t('policies.fact.bundle'), value: <Mono>{detail.bundle_hash}</Mono> },
          {
            label: t('policies.fact.created'),
            value: t('policies.fact.by', {
              time: formatUtc(detail.created_at),
              actor: detail.created_by ?? '—',
            }),
          },
          {
            label: t('policies.fact.published'),
            value: detail.published_at
              ? t('policies.fact.by', { time: formatUtc(detail.published_at), actor: detail.published_by ?? '—' })
              : t('policies.notPublished'),
          },
          { label: t('policies.fact.notes'), value: detail.notes ?? '—' },
        ]}
      />
      <Section
        title={
          comparedWith == null
            ? t('policies.changes.titleNoPublished')
            : t('policies.changes.title', { version: comparedWith })
        }
        description={t('policies.changes.description')}
      >
        {detail.status === 'published' ? (
          <Note>{t('policies.changes.isPublished')}</Note>
        ) : (
          <ChangesTable
            changes={detail.changes_from_published ?? []}
            caption={t('policies.changes.caption', { version: detail.version })}
            empty={t('changes.none')}
          />
        )}
      </Section>
      <Section title={t('policies.documents.title')} description={t('policies.documents.description')}>
        <PolicyTable policies={detail.policies} version={detail.version} />
      </Section>
    </div>
  );
}

function PolicyTable({ policies, version }: { policies: readonly PolicyDocument[]; version: number }) {
  const ordered = [...policies].sort((a, b) => b.priority - a.priority);
  return (
    <DataTable
      caption={t('policies.documents.caption', { version })}
      minWidth="56rem"
      columns={[
        { label: t('policies.documents.col.policy') },
        { label: t('policies.documents.col.priority'), numeric: true },
        { label: t('policies.documents.col.when') },
        { label: t('policies.documents.col.enforces') },
      ]}
      rows={ordered.map((policy) => ({
        key: policy.policy_id,
        cells: [
          <span key="p" className="flex flex-col gap-0.5">
            <Mono>{policy.policy_id}</Mono>
            <span className="font-normal text-secondary text-ink-muted">{policy.title}</span>
          </span>,
          policy.priority,
          <BulletList key="w" items={matchConditions(policy.match)} empty="—" />,
          <BulletList key="e" items={enforcement(policy.enforce)} empty="—" />,
        ],
      }))}
      empty={t('policies.documents.empty')}
    />
  );
}
