import { Card } from '@aiva/ui';
import { unwrap, type TrustedNetworkList } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { IdList, InlineIds, Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { EnforcedBy } from '../govern/EnforcedBy';
import { t } from '../i18n';
import { Page } from '../layout/Page';

/**
 * AiVa Govern › Trusted Networks (GET /v1/admin/trusted-networks, ADR-0007):
 * how the gateway derives the client address, and the source CIDRs each
 * application accepts. Client-supplied forwarding headers are never trusted
 * unless they come from a configured proxy.
 */
export function TrustedNetworksPage() {
  const { state, reload } = useApiQuery('admin/trusted-networks', (client, signal) =>
    unwrap(client.GET('/v1/admin/trusted-networks', { signal })),
  );
  return (
    <Page
      title={t('view.trusted-networks.title')}
      eyebrow={t('area.govern')}
      description={t('view.trusted-networks.description')}
    >
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('networks.loading')}
        errorTitle={t('networks.error')}
      >
        {(list) => <NetworkViews list={list} />}
      </QueryView>
    </Page>
  );
}

function NetworkViews({ list }: { list: TrustedNetworkList }) {
  const { ingress } = list;
  return (
    <>
      <Card title={t('networks.ingress.title')} description={t('networks.ingress.description')}>
        <FactList
          facts={[
            {
              label: t('networks.ingress.proxies'),
              value: <IdList ids={ingress.trusted_proxies} empty={t('networks.ingress.proxies.none')} />,
            },
            {
              label: t('networks.ingress.clientAddress'),
              value: t(ingress.forwarded_for_trusted ? 'networks.ingress.forwarded' : 'networks.ingress.peer'),
            },
            {
              label: t('networks.ingress.simulated'),
              value: t(ingress.simulated_ingress_allowed ? 'networks.ingress.simulated.yes' : 'networks.ingress.simulated.no'),
            },
          ]}
        />
      </Card>
      <Card title={t('networks.title')} description={t('networks.description')}>
        <Section title={t('networks.rules.title')}>
          <EnforcedBy version={list.policy_version} bundleHash={list.bundle_hash} />
          <DataTable
            caption={t('networks.caption')}
            minWidth="44rem"
            columns={[
              { label: t('networks.col.network') },
              { label: t('networks.col.cidr') },
              { label: t('networks.col.applications') },
            ]}
            rows={list.networks.map((network) => ({
              key: network.id,
              cells: [
                <span key="n" className="flex flex-col gap-0.5">
                  <span>{network.name}</span>
                  <span className="font-normal text-secondary text-ink-muted">
                    <Mono>{network.id}</Mono>
                  </span>
                </span>,
                <Mono key="c">{network.cidr}</Mono>,
                <InlineIds key="a" ids={network.application_ids} empty={t('networks.applications.none')} />,
              ],
            }))}
            empty={t('networks.empty')}
          />
        </Section>
      </Card>
    </>
  );
}
