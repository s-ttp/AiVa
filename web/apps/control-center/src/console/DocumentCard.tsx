import { Badge, Button, Card, ErrorState, ProvenanceBadge, type BadgeTone } from '@aiva/ui';
import { CircleCheck, CircleMinus, CircleX, Download, Eye, FileCheck2, FileWarning, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router';
import { isNotImplemented, unwrap, type ArtifactCheck, type ArtifactOut } from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { BulletList, IdList, Mono } from '../components/inline';
import { NotImplementedState } from '../components/NotImplementedState';
import { Section } from '../components/Section';
import { formatBytes, formatCount, formatUtc, isProvenance } from '../format';
import { t } from '../i18n';
import { classificationLabel, pathLabel } from '../labels';
import { DocumentPreview } from './DocumentPreview';
import { downloadArtifact } from './download';

const CHECK_BADGES: Record<ArtifactCheck['status'], { icon: LucideIcon; tone: BadgeTone }> = {
  passed: { icon: CircleCheck, tone: 'success' },
  failed: { icon: CircleX, tone: 'danger' },
  'not-run': { icon: CircleMinus, tone: 'neutral' },
};

function CheckStatus({ status }: { status: ArtifactCheck['status'] }) {
  const { icon, tone } = CHECK_BADGES[status];
  return (
    <Badge icon={icon} tone={tone}>
      {t(`artifacts.check.status.${status}`)}
    </Badge>
  );
}

function YesNo({ value, yes, no }: { value: boolean; yes: string; no: string }) {
  return value ? (
    <Badge icon={CircleCheck} tone="success">
      {yes}
    </Badge>
  ) : (
    <Badge icon={CircleX} tone="danger">
      {no}
    </Badge>
  );
}

/**
 * A drafted document: what it is, how it was produced (provenance, route,
 * classification), the document-output checks of LLD §16, figure and
 * citation results, its outline, a plain-text preview and a download.
 */
export function DocumentCard({ artifact }: { artifact: ArtifactOut }) {
  const preview = useApiAction((client) =>
    unwrap(
      client.GET('/v1/artifacts/{artifact_id}/preview', { params: { path: { artifact_id: artifact.artifact_id } } }),
    ),
  );
  const download = useApiAction((client) => downloadArtifact(client, artifact.artifact_id, artifact.file_name));
  const ready = artifact.status === 'ready';
  const provenance = isProvenance(artifact.provenance) ? artifact.provenance : null;
  return (
    <Card title={t('artifacts.document.title')} description={<Mono>{artifact.file_name}</Mono>}>
      <div className="space-y-8">
        <div className="flex flex-wrap items-center gap-2">
          <Badge icon={ready ? FileCheck2 : FileWarning} tone={ready ? 'success' : 'danger'}>
            {t(`artifacts.status.${artifact.status}`)}
          </Badge>
          {provenance && <ProvenanceBadge provenance={provenance} />}
          <span className="text-secondary text-ink-muted">
            {t('artifacts.provenanceLine', {
              designation: t(`artifacts.designation.${artifact.data_designation}`),
              provenance: provenance ? t(`artifacts.provenance.${provenance}`) : artifact.provenance,
            })}
          </span>
        </div>
        <FactList
          facts={[
            { label: t('artifacts.fact.type'), value: t(`artifacts.type.${artifact.document_type}`) },
            { label: t('artifacts.fact.classification'), value: classificationLabel(artifact.classification) },
            { label: t('artifacts.fact.path'), value: pathLabel(artifact.execution_path) },
            {
              label: t('artifacts.fact.model'),
              value: artifact.selected_model ? <Mono>{artifact.selected_model}</Mono> : t('run.fact.noModel'),
            },
            { label: t('artifacts.fact.size'), value: formatBytes(artifact.size_bytes) },
            { label: t('artifacts.fact.sha256'), value: <Mono>{artifact.sha256}</Mono> },
            { label: t('artifacts.fact.generator'), value: <Mono>{artifact.generator_version}</Mono> },
            { label: t('artifacts.fact.created'), value: formatUtc(artifact.created_at) },
            {
              label: t('artifacts.fact.run'),
              value: (
                <Link
                  to={`/admin/audit?run=${encodeURIComponent(artifact.run_id)}`}
                  className="font-semibold text-brand-navy underline underline-offset-2"
                >
                  <Mono>{artifact.run_id}</Mono>
                </Link>
              ),
            },
            { label: t('artifacts.fact.access'), value: t('artifacts.access.owner-only') },
          ]}
        />
        <div className="space-y-3">
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => void download.run()} disabled={download.state.status === 'pending'}>
              <Download className="size-4" />
              {t('artifacts.download', { type: t(`artifacts.type.${artifact.document_type}`) })}
            </Button>
            <Button
              variant="secondary"
              onClick={() => void preview.run()}
              disabled={preview.state.status === 'pending'}
              aria-busy={preview.state.status === 'pending'}
            >
              <Eye className="size-4" />
              {t('artifacts.preview.show')}
            </Button>
          </div>
          {download.state.status === 'error' &&
            (isNotImplemented(download.state.error) ? (
              <NotImplementedState error={download.state.error} feature={t('artifacts.feature.download')} />
            ) : (
              <ErrorState title={t('artifacts.download.error')} description={describeError(download.state.error)} />
            ))}
        </div>
        <Section title={t('artifacts.checks.title')} description={t('artifacts.checks.description')}>
          <DataTable
            caption={t('artifacts.checks.caption')}
            columns={[
              { label: t('artifacts.checks.col.check') },
              { label: t('artifacts.checks.col.status') },
              { label: t('artifacts.checks.col.detail') },
              { label: t('artifacts.checks.col.failures') },
            ]}
            rows={artifact.checks.map((check) => ({
              key: check.name,
              cells: [
                t(`artifacts.check.${check.name}`),
                <CheckStatus key="s" status={check.status} />,
                check.detail,
                <IdList key="f" ids={check.failures ?? []} empty={t('common.none')} />,
              ],
            }))}
            empty={t('artifacts.checks.empty')}
          />
        </Section>
        <Section title={t('artifacts.figures.title')} description={t('artifacts.figures.description')}>
          <DataTable
            caption={t('artifacts.figures.caption')}
            columns={[
              { label: t('artifacts.figures.col.figure') },
              { label: t('artifacts.figures.col.preserved') },
              { label: t('artifacts.figures.col.occurrences'), numeric: true },
              { label: t('artifacts.figures.col.locations') },
            ]}
            rows={(artifact.figures ?? []).map((figure) => ({
              key: figure.figure_id,
              cells: [
                <Mono key="f">{figure.figure_id}</Mono>,
                <YesNo key="p" value={figure.preserved} yes={t('artifacts.figures.preserved')} no={t('artifacts.figures.missing')} />,
                formatCount(figure.occurrences),
                <BulletList key="l" items={figure.locations ?? []} empty={t('common.none')} />,
              ],
            }))}
            empty={t('artifacts.figures.empty')}
          />
        </Section>
        <Section title={t('artifacts.citations.title')}>
          <DataTable
            caption={t('artifacts.citations.caption')}
            columns={[{ label: t('artifacts.citations.col.source') }, { label: t('artifacts.citations.col.cited') }]}
            rows={(artifact.citations ?? []).map((citation) => ({
              key: citation.source_id,
              cells: [
                <Mono key="s">{citation.source_id}</Mono>,
                <YesNo key="c" value={citation.cited} yes={t('artifacts.citations.cited')} no={t('artifacts.citations.missing')} />,
              ],
            }))}
            empty={t('artifacts.citations.empty')}
          />
        </Section>
        <Section title={t('artifacts.outline.title')}>
          <FactList
            facts={[
              {
                label: t(artifact.document_type === 'pptx' ? 'artifacts.outline.slides' : 'artifacts.outline.sections'),
                value: formatCount(artifact.outline.sections),
              },
              { label: t('artifacts.outline.paragraphs'), value: formatCount(artifact.outline.paragraphs) },
              { label: t('artifacts.outline.tables'), value: formatCount(artifact.outline.tables) },
              {
                label: t('artifacts.outline.editable'),
                value: t(artifact.outline.editable ? 'artifacts.outline.editable.yes' : 'artifacts.outline.editable.no'),
              },
            ]}
          />
        </Section>
        {preview.state.status !== 'idle' && (
          <Section title={t('artifacts.preview.title')} description={t('artifacts.preview.description')}>
            {preview.state.status === 'pending' && <p role="status">{t('artifacts.preview.loading')}</p>}
            {preview.state.status === 'error' &&
              (isNotImplemented(preview.state.error) ? (
                <NotImplementedState error={preview.state.error} feature={t('artifacts.feature.preview')} />
              ) : (
                <ErrorState title={t('artifacts.preview.error')} description={describeError(preview.state.error)} />
              ))}
            {preview.state.status === 'success' && <DocumentPreview preview={preview.state.data} />}
          </Section>
        )}
      </div>
    </Card>
  );
}
