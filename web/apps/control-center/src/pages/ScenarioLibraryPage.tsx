import { Badge, Card, Ltr } from '@aiva/ui';
import { Clock, Play } from 'lucide-react';
import { useId } from 'react';
import { Link } from 'react-router';
import { unwrap, type Scenario } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { useSession } from '../auth/session';
import { QueryView } from '../components/QueryView';
import { t } from '../i18n';
import { classificationLabel } from '../labels';
import { Page } from '../layout/Page';
import { consoleScenarios } from '../run/scenarios';

const COLUMNS = ['number', 'name', 'id', 'proof', 'application', 'classification', 'status'] as const;

export function ScenarioLibraryPage() {
  const { state, reload } = useApiQuery('catalog/scenarios', (client, signal) =>
    unwrap(client.GET('/v1/catalog/scenarios', { signal })).then(consoleScenarios),
  );
  return (
    <Page title={t('view.library.title')} eyebrow={t('area.console')} description={t('view.library.description')}>
      <Card>
        <QueryView
          state={state}
          onRetry={reload}
          loadingLabel={t('scenarios.loading')}
          errorTitle={t('scenarios.error')}
          empty={{ title: t('scenarios.empty.title'), description: t('scenarios.empty.body') }}
          isEmpty={(scenarios) => scenarios.length === 0}
        >
          {(scenarios) => <ScenarioTable scenarios={scenarios} />}
        </QueryView>
      </Card>
    </Page>
  );
}

function ScenarioTable({ scenarios }: { scenarios: readonly Scenario[] }) {
  const captionId = useId();
  const { can } = useSession();
  const canRun = can('chat:test');
  return (
    // Focusable so keyboard users can scroll the table on narrow screens.
    <div role="region" aria-labelledby={captionId} tabIndex={0} className="overflow-x-auto">
      <table className="w-full min-w-[56rem] text-start">
        <caption id={captionId} className="sr-only">
          {t('scenarios.caption')}
        </caption>
        <thead>
          <tr className="text-secondary text-ink-muted">
            {COLUMNS.map((column) => (
              <th key={column} scope="col" className="px-3 pb-3 text-start font-semibold">
                {t(`scenarios.col.${column}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {scenarios.map((scenario) => (
            <tr key={scenario.id} className="border-t border-line align-top">
              <td className="px-3 py-3 tabular-nums">{scenario.number}</td>
              <th scope="row" className="px-3 py-3 text-start font-semibold">
                {scenario.name}
              </th>
              <td className="px-3 py-3">
                <Ltr className="whitespace-nowrap font-mono text-secondary">{scenario.id}</Ltr>
              </td>
              <td className="px-3 py-3 text-ink-muted">{scenario.primary_proof}</td>
              <td className="px-3 py-3">
                <Ltr className="whitespace-nowrap font-mono text-secondary">{scenario.application_id}</Ltr>
              </td>
              <td className="px-3 py-3 whitespace-nowrap">{classificationLabel(scenario.declared_classification)}</td>
              <td className="px-3 py-3">
                {scenario.deferred ? (
                  <>
                    <Badge icon={Clock}>{t('scenarios.status.deferred')}</Badge>
                    <span className="mt-1 block text-secondary text-ink-muted">{scenario.deferred}</span>
                  </>
                ) : canRun ? (
                  <Link
                    to={`/console/run?scenario=${encodeURIComponent(scenario.id)}`}
                    className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg font-semibold text-brand-navy underline underline-offset-2"
                    aria-label={t('scenarios.open.label', { name: scenario.name })}
                  >
                    <Play className="size-4" />
                    {t('scenarios.open')}
                  </Link>
                ) : (
                  <span className="text-secondary text-ink-muted">{t('scenarios.status.available')}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
