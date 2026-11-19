import { Card, EmptyState, ErrorState, LoadingState } from '@aiva/ui';
import { SearchX } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import {
  ApiError,
  isNotImplemented,
  toApiError,
  unwrap,
  type ArtifactCreateRequest,
  type ArtifactCreateResponse,
  type ArtifactOut,
  type GatewayClient,
} from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { Mono, Note } from '../components/inline';
import { NotImplementedState } from '../components/NotImplementedState';
import { QueryView } from '../components/QueryView';
import { ArtifactForm } from '../console/ArtifactForm';
import { DocumentCard } from '../console/DocumentCard';
import { DOCUMENT_SCENARIO_ID, toArtifactRequest, type ArtifactDraft } from '../console/requests';
import { formatUtc } from '../format';
import { t } from '../i18n';
import { classificationLabel } from '../labels';
import { Page } from '../layout/Page';
import { ExecutionGraph } from '../run/ExecutionGraph';
import { RunDetails } from '../run/RunDetails';
import { RunSummary } from '../run/RunSummary';

function isCreateResponse(value: unknown): value is ArtifactCreateResponse {
  return typeof value === 'object' && value !== null && 'run' in value && 'artifact' in value;
}

/** Drafts a document; a run the gateway blocked still returns its run record, shown like any other. */
async function createArtifact(client: GatewayClient, body: ArtifactCreateRequest): Promise<ArtifactCreateResponse> {
  const headers: Record<string, string> = {};
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) headers['Idempotency-Key'] = crypto.randomUUID();
  let result;
  try {
    result = await client.POST('/v1/artifacts', { body, headers });
  } catch {
    throw new ApiError({ kind: 'network', message: t('run.networkError') });
  }
  if (result.data) return result.data;
  if (isCreateResponse(result.error)) return result.error;
  throw toApiError(result.response.status, result.error, undefined, result.response.headers);
}

/**
 * AiVa Test Console › Document copilot (LLD §15 #10): drafts an editable DOCX
 * or PPTX through one governed run, then shows the run with the existing run
 * panels and the document with its output checks (LLD §16).
 */
export function ArtifactsPage() {
  const [params] = useSearchParams();
  const artifactId = params.get('artifact')?.trim() || null;
  const scenarios = useApiQuery('catalog/scenarios', (client, signal) =>
    unwrap(client.GET('/v1/catalog/scenarios', { signal })),
  );
  const mine = useApiQuery('artifacts', (client, signal) => unwrap(client.GET('/v1/artifacts', { signal })));
  const [instructions, setInstructions] = useState('');
  const create = useApiAction((client, body: ArtifactCreateRequest) => createArtifact(client, body));
  const resultHeading = useRef<HTMLElement>(null);

  // Move focus to the outcome so keyboard and screen-reader users land on it.
  useEffect(() => {
    if (create.state.status === 'success' || create.state.status === 'error') resultHeading.current?.focus();
  }, [create.state.status]);

  async function draft(value: ArtifactDraft) {
    setInstructions(value.instructions);
    const response = await create.run(toArtifactRequest(value));
    if (response?.artifact) mine.reload();
  }

  return (
    <Page title={t('view.artifacts.title')} eyebrow={t('area.console')} description={t('view.artifacts.description')}>
      <Card title={t('artifacts.form.cardTitle')} description={t('artifacts.form.cardDescription')}>
        <QueryView
          state={scenarios.state}
          onRetry={scenarios.reload}
          loadingLabel={t('scenarios.loading')}
          errorTitle={t('scenarios.error')}
        >
          {(catalog) => (
            <ArtifactForm
              scenario={catalog.find((scenario) => scenario.id === DOCUMENT_SCENARIO_ID)}
              applications={[...new Set(catalog.map((scenario) => scenario.application_id))].sort()}
              pending={create.state.status === 'pending'}
              onSubmit={(value) => void draft(value)}
            />
          )}
        </QueryView>
      </Card>

      {create.state.status === 'pending' && <LoadingState label={t('artifacts.form.drafting')} />}
      {create.state.status === 'error' && (
        <div ref={(node) => void (resultHeading.current = node)} tabIndex={-1} className="outline-none">
          {isNotImplemented(create.state.error) ? (
            <NotImplementedState error={create.state.error} feature={t('artifacts.feature.create')} />
          ) : (
            <ErrorState title={t('artifacts.create.error')} description={describeError(create.state.error)} />
          )}
        </div>
      )}
      {create.state.status === 'success' && (
        <>
          <Card title={t('artifacts.run.title')}>
            <h3 ref={(node) => void (resultHeading.current = node)} tabIndex={-1} className="sr-only">
              {t('run.result.heading', { status: t(`runStatus.${create.state.data.run.status}`) })}
            </h3>
            <RunSummary run={create.state.data.run} />
          </Card>
          {create.state.data.artifact ? (
            <DocumentCard artifact={create.state.data.artifact} />
          ) : (
            <Card>
              <Note>{t('artifacts.noDocument')}</Note>
            </Card>
          )}
          <Card title={t('run.graph.title')} description={t('run.graph.description')}>
            <ExecutionGraph spans={create.state.data.run.trace ?? []} executionPath={create.state.data.run.execution_path} />
          </Card>
          <Card title={t('run.details.title')}>
            <RunDetails run={create.state.data.run} prompt={instructions} baselineRequested={false} />
          </Card>
        </>
      )}

      <Card title={t('artifacts.mine.title')} description={t('artifacts.mine.description')}>
        <QueryView
          state={mine.state}
          onRetry={mine.reload}
          loadingLabel={t('artifacts.mine.loading')}
          errorTitle={t('artifacts.mine.error')}
          feature={t('artifacts.feature.list')}
          empty={{ title: t('artifacts.mine.empty.title'), description: t('artifacts.mine.empty.body') }}
          isEmpty={(list) => list.items.length === 0}
        >
          {(list) => <ArtifactTable artifacts={list.items} selected={artifactId} />}
        </QueryView>
      </Card>
      {artifactId && <ArtifactDetail key={artifactId} artifactId={artifactId} />}
    </Page>
  );
}

function ArtifactTable({ artifacts, selected }: { artifacts: readonly ArtifactOut[]; selected: string | null }) {
  return (
    <DataTable
      caption={t('artifacts.mine.caption')}
      minWidth="48rem"
      columns={[
        { label: t('artifacts.mine.col.file') },
        { label: t('artifacts.mine.col.status') },
        { label: t('artifacts.mine.col.classification') },
        { label: t('artifacts.mine.col.created') },
        { label: t('artifacts.mine.col.open') },
      ]}
      rows={artifacts.map((artifact) => ({
        key: artifact.artifact_id,
        cells: [
          <Mono key="f">{artifact.file_name}</Mono>,
          t(`artifacts.status.${artifact.status}`),
          classificationLabel(artifact.classification),
          formatUtc(artifact.created_at),
          <Link
            key="o"
            to={{ search: `?artifact=${encodeURIComponent(artifact.artifact_id)}` }}
            aria-current={selected === artifact.artifact_id ? 'true' : undefined}
            className="whitespace-nowrap font-semibold text-brand-navy underline underline-offset-2"
          >
            {t('artifacts.mine.open', { file: artifact.file_name })}
          </Link>,
        ],
      }))}
    />
  );
}

/** An artifact opened by ID. Artifacts are owner-only: any 404 reads as "not found or not yours". */
function ArtifactDetail({ artifactId }: { artifactId: string }) {
  const { state, reload } = useApiQuery(`artifacts/${artifactId}`, (client, signal) =>
    unwrap(client.GET('/v1/artifacts/{artifact_id}', { params: { path: { artifact_id: artifactId } }, signal })),
  );
  if (state?.status === 'error' && state.error.status === 404) {
    return (
      <EmptyState
        icon={SearchX}
        title={t('artifacts.notFound.title')}
        description={t('artifacts.notFound.body')}
      />
    );
  }
  return (
    <QueryView
      state={state}
      onRetry={reload}
      loadingLabel={t('artifacts.detail.loading')}
      errorTitle={t('artifacts.detail.error')}
      feature={t('artifacts.feature.detail')}
    >
      {(artifact) => <DocumentCard artifact={artifact} />}
    </QueryView>
  );
}
