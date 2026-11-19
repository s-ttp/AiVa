import { useId, type ReactNode } from 'react';
import type { ArtifactPreview, PreviewBlock } from '../api/client';
import { FactList } from '../components/FactList';
import { Mono } from '../components/inline';
import { formatDurationMs } from '../format';
import { t } from '../i18n';

/** Consecutive bullets form one list; other blocks stand alone. */
function groupBlocks(blocks: readonly PreviewBlock[]): Array<PreviewBlock | PreviewBlock[]> {
  const groups: Array<PreviewBlock | PreviewBlock[]> = [];
  for (const block of blocks) {
    const last = groups.at(-1);
    if (block.kind === 'bullet' && Array.isArray(last)) last.push(block);
    else groups.push(block.kind === 'bullet' ? [block] : block);
  }
  return groups;
}

function Block({ block, pageLabel }: { block: PreviewBlock; pageLabel: string }): ReactNode {
  switch (block.kind) {
    case 'heading':
      return <p className="font-semibold text-ink">{block.text}</p>;
    case 'paragraph':
      return <p>{block.text}</p>;
    case 'table':
      return (
        <div role="region" aria-label={t('artifacts.preview.table', { page: pageLabel })} tabIndex={0} className="overflow-x-auto">
          <table className="text-secondary">
            <caption className="sr-only">{t('artifacts.preview.table', { page: pageLabel })}</caption>
            <tbody>
              {(block.rows ?? []).map((row, rowIndex) => (
                <tr key={rowIndex} className="border-t border-line">
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className="px-3 py-1.5">
                      <bdi>{cell}</bdi>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return null;
  }
}

function Page({ page, documentPage }: { page: ArtifactPreview['pages'][number]; documentPage: string }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-3 rounded-card border border-line bg-canvas p-4">
      <h4 id={headingId} className="text-secondary font-semibold text-ink-muted">
        {documentPage}
      </h4>
      {groupBlocks(page.blocks).map((group, index) =>
        Array.isArray(group) ? (
          <ul key={index} className="list-disc space-y-1 ps-5">
            {group.map((bullet, bulletIndex) => (
              <li key={bulletIndex} style={{ marginInlineStart: `${Math.max(0, (bullet.level ?? 1) - 1) * 1.25}rem` }}>
                {bullet.text}
              </li>
            ))}
          </ul>
        ) : (
          <Block key={index} block={group} pageLabel={documentPage} />
        ),
      )}
    </section>
  );
}

/**
 * The structured preview the gateway renders from the stored file in a
 * network-less sandbox. Plain text only: blocks are rendered as React text,
 * never as HTML.
 */
export function DocumentPreview({ preview }: { preview: ArtifactPreview }) {
  return (
    <div className="space-y-4">
      {preview.pages.length === 0 ? (
        <p className="text-ink-muted">{t('artifacts.preview.empty')}</p>
      ) : (
        preview.pages.map((page) => {
          const label =
            page.kind === 'slide'
              ? t('artifacts.preview.slide', { number: page.index + 1 })
              : t('artifacts.preview.section', { number: page.index + 1 });
          return (
            <Page
              key={page.index}
              page={page}
              documentPage={page.title ? t('artifacts.preview.pageTitle', { page: label, title: page.title }) : label}
            />
          );
        })
      )}
      <FactList
        facts={[
          { label: t('artifacts.sandbox.renderer'), value: <Mono>{preview.sandbox.renderer}</Mono> },
          { label: t('artifacts.sandbox.network'), value: t('artifacts.sandbox.network.disabled') },
          {
            label: t('artifacts.sandbox.isolated'),
            value: t(preview.sandbox.isolated_interpreter ? 'common.yes' : 'common.no'),
          },
          { label: t('artifacts.sandbox.duration'), value: formatDurationMs(preview.sandbox.duration_ms) },
          { label: t('artifacts.sandbox.verified'), value: <Mono>{preview.sandbox.verified_sha256}</Mono> },
        ]}
      />
    </div>
  );
}
