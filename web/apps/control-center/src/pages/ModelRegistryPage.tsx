import { Badge, Card, EmptyState, Ltr } from '@aiva/ui';
import { CircleAlert, Globe, LockKeyhole, ServerCog } from 'lucide-react';
import { Link } from 'react-router';
import { unwrap, type ModelAllowlist } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { InlineIds, Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { EnabledBadge } from '../govern/EnabledBadge';
import { EnforcedBy } from '../govern/EnforcedBy';
import { formatMicrosUsd } from '../format';
import { t, tPlural, tRich } from '../i18n';
import { Page } from '../layout/Page';

type RegistryModel = ModelAllowlist['models'][number];
type RateCard = NonNullable<RegistryModel['rate_card']>;

const PROVIDERS = ['hf-endpoint', 'hf-router', 'google', 'anthropic', 'simulated'] as const;

/** The provider's name; an unknown provider is shown as sent, never hidden. */
function providerLabel(provider: string): string {
  return (PROVIDERS as readonly string[]).includes(provider)
    ? t(`registry.provider.${provider as (typeof PROVIDERS)[number]}`)
    : provider;
}

/**
 * AiVa Optimize › Model Registry (GET /v1/admin/models, LLD §19): every model
 * AiVa can call, with its pinned provider model ID and revision, residency,
 * context window, pinned prices and the applications granted it. The published
 * policy bundle is the source; with none published the registry is not
 * configured.
 */
export function ModelRegistryPage() {
  const { state, reload } = useApiQuery('admin/models', (client, signal) =>
    unwrap(client.GET('/v1/admin/models', { signal })),
  );
  const notConfigured = state?.status === 'error' && state.error.code === 'POLICY_BUNDLE_MISSING';
  return (
    <Page
      title={t('view.model-registry.title')}
      eyebrow={t('area.optimize')}
      description={t('view.model-registry.description')}
    >
      {notConfigured ? (
        <Card title={t('registry.title')} description={t('registry.description')}>
          <EmptyState
            headingLevel={3}
            icon={ServerCog}
            title={t('registry.notConfigured.title')}
            description={t('registry.notConfigured.body')}
          >
            <Link
              to="/admin/govern/policy-rules"
              className="font-semibold text-brand-navy underline underline-offset-2"
            >
              {t('registry.notConfigured.link')}
            </Link>
          </EmptyState>
        </Card>
      ) : (
        <QueryView
          state={state}
          onRetry={reload}
          loadingLabel={t('registry.loading')}
          errorTitle={t('registry.error')}
          empty={{ title: t('registry.empty.title'), description: t('registry.empty.body') }}
          isEmpty={(list) => list.models.length === 0}
        >
          {(list) => (
            <>
              <Card title={t('registry.title')} description={t('registry.description')}>
                <Registry list={list} />
              </Card>
              <Card title={t('registry.prices.title')} description={t('registry.prices.description')}>
                <Prices models={list.models} />
              </Card>
            </>
          )}
        </QueryView>
      )}
    </Page>
  );
}

function Registry({ list }: { list: ModelAllowlist }) {
  return (
    <div className="space-y-4">
      <EnforcedBy version={list.policy_version} bundleHash={list.bundle_hash} />
      <DataTable
        caption={t('registry.caption')}
        minWidth="72rem"
        columns={[
          { label: t('registry.col.model') },
          { label: t('registry.col.provider') },
          { label: t('registry.col.pin') },
          { label: t('registry.col.residency') },
          { label: t('registry.col.context'), numeric: true },
          { label: t('registry.col.status') },
          { label: t('registry.col.applications') },
        ]}
        rows={list.models.map((model) => ({
          key: model.id,
          cells: [
            <span key="m" className="flex flex-col gap-0.5">
              <span>{model.display_name}</span>
              <span className="font-normal text-secondary text-ink-muted">
                <Mono>{model.id}</Mono>
              </span>
              <span className="font-normal text-secondary text-ink-muted">{t(`registry.role.${model.role}`)}</span>
            </span>,
            model.catalog_configured && model.provider ? (
              providerLabel(model.provider)
            ) : (
              <Badge key="c" icon={CircleAlert} tone="warning">
                {t('registry.notInCatalog')}
              </Badge>
            ),
            <Pin key="p" model={model} />,
            <Residency key="r" model={model} />,
            model.context_window == null ? '—' : tPlural('registry.contextWindow', model.context_window),
            <EnabledBadge key="e" enabled={model.enabled} />,
            <Grants key="a" ids={model.application_ids} />,
          ],
        }))}
      />
      <p className="text-secondary text-ink-muted">
        {tRich('registry.grants', {
          link: (
            <Link
              to="/admin/govern/model-allowlist"
              className="font-semibold text-brand-navy underline underline-offset-2"
            >
              {t('registry.grants.link')}
            </Link>
          ),
        })}
      </p>
    </div>
  );
}

/** The pinned provider model ID and revision, with the provider route and adapter when pinned. */
function Pin({ model }: { model: RegistryModel }) {
  if (!model.catalog_configured || !model.provider_model_id) return <span>—</span>;
  return (
    <span className="flex flex-col gap-0.5">
      <Mono>{model.provider_model_id}</Mono>
      {model.revision && (
        <span className="text-secondary text-ink-muted wrap-anywhere">
          {tRich('registry.revision', { revision: <Mono>{model.revision}</Mono> })}
        </span>
      )}
      {model.provider_route && (
        <span className="text-secondary text-ink-muted">
          {tRich('registry.route', { route: <Mono>{model.provider_route}</Mono> })}
        </span>
      )}
      {model.adapter && (
        <span className="text-secondary text-ink-muted">
          {tRich('registry.adapter', { adapter: <Mono>{model.adapter}</Mono> })}
        </span>
      )}
    </span>
  );
}

/**
 * Pinned prices per model. Prices are configuration, not measured amounts, so
 * they carry no provenance badge; each is cited by its rate card instead.
 */
function Prices({ models }: { models: readonly RegistryModel[] }) {
  return (
    <DataTable
      caption={t('registry.prices.caption')}
      minWidth="64rem"
      columns={[
        { label: t('registry.prices.col.model') },
        { label: t('registry.prices.col.tokens') },
        { label: t('registry.prices.col.gpu') },
        { label: t('registry.prices.col.rateCard') },
        { label: t('registry.prices.col.source') },
      ]}
      rows={models.map((model) => {
        const card = model.rate_card;
        return {
          key: model.id,
          cells: card
            ? [
                <ModelName key="m" model={model} />,
                <TokenPrices key="t" card={card} />,
                card.gpu_micros_per_hour == null
                  ? t('registry.prices.noGpu')
                  : tRich('registry.prices.gpuPerHour', { price: <Ltr>{formatMicrosUsd(card.gpu_micros_per_hour)}</Ltr> }),
                <span key="r" className="flex flex-col gap-0.5">
                  <Mono>{card.id}</Mono>
                  <span className="text-secondary text-ink-muted">
                    {tRich('registry.prices.version', { version: <Mono>{card.version}</Mono> })}
                  </span>
                  <span className="text-secondary text-ink-muted">
                    {tRich('registry.prices.effective', { date: <Ltr>{card.effective_from}</Ltr> })}
                  </span>
                </span>,
                <span key="s" className="text-secondary wrap-anywhere">
                  {card.source}
                </span>,
              ]
            : [<ModelName key="m" model={model} />, t('registry.prices.none'), '—', '—', '—'],
        };
      })}
    />
  );
}

function ModelName({ model }: { model: RegistryModel }) {
  return (
    <span className="flex flex-col gap-0.5">
      <span>{model.display_name}</span>
      <span className="font-normal text-secondary text-ink-muted">
        <Mono>{model.id}</Mono>
      </span>
    </span>
  );
}

/** A $0 token price on a GPU-served model is the POC convention, never "free". */
function TokenPrices({ card }: { card: RateCard }) {
  const tokens = [card.input_micros_per_mtok, card.output_micros_per_mtok, card.cached_input_micros_per_mtok];
  if (card.gpu_micros_per_hour != null && tokens.every((micros) => micros === 0)) {
    return <span>{t('registry.prices.gpuServed')}</span>;
  }
  const price = (micros: number) => <Ltr>{formatMicrosUsd(micros)}</Ltr>;
  return (
    <span className="flex flex-col gap-0.5">
      <span>{tRich('registry.prices.input', { price: price(card.input_micros_per_mtok) })}</span>
      <span>{tRich('registry.prices.output', { price: price(card.output_micros_per_mtok) })}</span>
      <span>{tRich('registry.prices.cachedInput', { price: price(card.cached_input_micros_per_mtok) })}</span>
    </span>
  );
}

function Residency({ model }: { model: RegistryModel }) {
  return (
    <span className="flex flex-col items-start gap-1.5">
      {model.sovereign ? (
        <Badge icon={LockKeyhole} tone="info">
          {t('registry.sovereign')}
        </Badge>
      ) : (
        <Badge icon={Globe} tone="neutral">
          {t(model.external ? 'registry.external' : 'registry.internal')}
        </Badge>
      )}
      <span className="text-secondary text-ink-muted">
        {model.region ? (
          tRich('registry.region', { region: <Ltr>{model.region}</Ltr> })
        ) : (
          t('registry.region.none')
        )}
      </span>
    </span>
  );
}

function Grants({ ids }: { ids: readonly string[] }) {
  if (ids.length === 0) return <span className="text-ink-muted">{t('registry.applications.none')}</span>;
  return (
    <span className="flex flex-col gap-1">
      <span className="font-semibold">{tPlural('registry.applications.count', ids.length)}</span>
      <InlineIds ids={ids} empty={t('registry.applications.none')} />
    </span>
  );
}
