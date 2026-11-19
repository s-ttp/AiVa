import { Card, Ltr, ProvenanceBadge } from '@aiva/ui';
import { readRuntimeConfig } from '../config';
import { t } from '../i18n';
import { Page } from '../layout/Page';

export function SettingsPage() {
  const { gatewayUrl } = readRuntimeConfig();
  return (
    <Page title={t('view.settings.title')} eyebrow={t('area.admin')} description={t('view.settings.description')}>
      <Card title={t('settings.runtime.title')}>
        <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-[minmax(0,14rem)_1fr]">
          <dt className="font-semibold">{t('settings.gateway.label')}</dt>
          <dd>
            {gatewayUrl ? (
              <Ltr className="font-mono">{gatewayUrl}</Ltr>
            ) : (
              t('settings.gateway.notConfigured')
            )}
            <p className="mt-1 text-secondary text-ink-muted">{t('settings.gateway.source')}</p>
          </dd>
          <dt className="font-semibold">{t('settings.language.label')}</dt>
          <dd>{t('settings.language.value')}</dd>
          <dt className="font-semibold">{t('settings.data.label')}</dt>
          <dd>
            <ProvenanceBadge provenance="synthetic" />
          </dd>
        </dl>
      </Card>
    </Page>
  );
}
