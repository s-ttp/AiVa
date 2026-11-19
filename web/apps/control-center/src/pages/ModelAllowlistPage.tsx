import { Badge, Card } from '@aiva/ui';
import { Globe, LockKeyhole } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { unwrap, type ModelAllowlist, type ModelSwitchResponse } from '../api/client';
import { useApiAction } from '../api/useApiAction';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { BulletList, IdList, Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { EnabledBadge } from '../govern/EnabledBadge';
import { EnforcedBy } from '../govern/EnforcedBy';
import { GrantedApplications, SwitchFailed, SwitchResult, type SwitchRequest } from '../govern/ModelSwitches';
import { matchConditions } from '../govern/policyText';
import { t, tRich } from '../i18n';
import { pathLabel } from '../labels';
import { Page } from '../layout/Page';

/**
 * AiVa Govern › Model Allowlist (GET /v1/admin/models): which applications are
 * granted each model, the policies that narrow the set when they match, and
 * the built-in invariants no policy version can remove. An administrator who
 * may write policies switches a model off or back on for one application
 * (ADR-0027): that drafts a policy version to test and publish in Policy Rules,
 * never a change that is live at once.
 */
export function ModelAllowlistPage() {
  const { state, reload } = useApiQuery('admin/models', (client, signal) =>
    unwrap(client.GET('/v1/admin/models', { signal })),
  );
  return (
    <Page
      title={t('view.model-allowlist.title')}
      eyebrow={t('area.govern')}
      description={t('view.model-allowlist.description')}
    >
      <QueryView state={state} onRetry={reload} loadingLabel={t('models.loading')} errorTitle={t('models.error')}>
        {(list) => <AllowlistViews list={list} />}
      </QueryView>
    </Page>
  );
}

function AllowlistViews({ list }: { list: ModelAllowlist }) {
  const [asked, setAsked] = useState<SwitchRequest | null>(null);
  const [drafted, setDrafted] = useState<{ result: ModelSwitchResponse; modelName: string } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const action = useApiAction((client, request: SwitchRequest) =>
    unwrap(
      client.POST('/v1/admin/policies/model-switch', {
        body: { application_id: request.applicationId, model_id: request.model.id, enabled: request.enabled },
      }),
    ),
  );
  // A new draft takes focus to what it would change.
  useEffect(() => {
    if (drafted) heading.current?.focus();
  }, [drafted]);

  async function draftSwitch(request: SwitchRequest) {
    setAsked(request);
    setDrafted(null);
    const result = await action.run(request);
    setAsked(null);
    if (result) setDrafted({ result, modelName: request.model.display_name });
  }

  return (
    <>
      <Card title={t('models.invariants.title')} description={t('models.invariants.description')}>
        <BulletList items={list.invariants} empty={t('common.none')} />
      </Card>
      <Card title={t('models.title')} description={t('models.description')}>
        <div className="space-y-4">
          <EnforcedBy version={list.policy_version} bundleHash={list.bundle_hash} />
          {list.viewer_can_switch && <p className="text-secondary text-ink-muted">{t('models.switch.hint')}</p>}
          <DataTable
            caption={t('models.caption')}
            minWidth="56rem"
            columns={[
              { label: t('models.col.model') },
              { label: t('models.col.role') },
              { label: t('models.col.provider') },
              { label: t('models.col.residency') },
              { label: t('models.col.applications') },
            ]}
            rows={list.models.map((model) => ({
              key: model.id,
              cells: [
                <span key="m" className="flex flex-col gap-0.5">
                  <span>{model.display_name}</span>
                  <span className="font-normal text-secondary text-ink-muted">
                    <Mono>{model.id}</Mono>
                  </span>
                </span>,
                t(`models.role.${model.role}`),
                model.catalog_configured && model.provider && model.provider_model_id ? (
                  <span key="p" className="flex flex-col gap-0.5">
                    <span>{model.provider}</span>
                    <span className="text-secondary text-ink-muted">
                      <Mono>{model.provider_model_id}</Mono>
                    </span>
                    {model.revision && (
                      <span className="text-secondary text-ink-muted">
                        {tRich('models.revision', { revision: <Mono>{model.revision}</Mono> })}
                      </span>
                    )}
                  </span>
                ) : (
                  t('models.notInCatalog')
                ),
                <span key="r" className="flex flex-col items-start gap-1.5">
                  {model.sovereign ? (
                    <Badge icon={LockKeyhole} tone="info">
                      {t('models.sovereign')}
                    </Badge>
                  ) : (
                    <Badge icon={Globe} tone="neutral">
                      {t(model.external ? 'models.external' : 'models.internal')}
                    </Badge>
                  )}
                  <span className="text-secondary text-ink-muted">
                    {model.region ? t('models.region', { region: model.region }) : t('models.region.none')}
                  </span>
                  <EnabledBadge enabled={model.enabled} />
                </span>,
                <GrantedApplications
                  key="a"
                  model={model}
                  canSwitch={list.viewer_can_switch}
                  pending={action.state.status === 'pending' ? asked : null}
                  onSwitch={(request) => void draftSwitch(request)}
                />,
              ],
            }))}
            empty={t('models.empty')}
          />
          {action.state.status === 'error' && <SwitchFailed error={action.state.error} />}
        </div>
      </Card>
      {drafted && (
        <div className="rounded-card border border-line bg-card p-6">
          <SwitchResult result={drafted.result} modelName={drafted.modelName} headingRef={heading} />
        </div>
      )}
      <Card title={t('models.policies.title')} description={t('models.policies.description')}>
        <Section title={t('models.policies.section')}>
          <DataTable
            caption={t('models.policies.caption')}
            minWidth="56rem"
            columns={[
              { label: t('models.policies.col.policy') },
              { label: t('models.policies.col.when') },
              { label: t('models.policies.col.models') },
              { label: t('models.policies.col.constraints') },
            ]}
            rows={list.policy_allowlists.map((policy) => ({
              key: policy.policy_id,
              cells: [
                <span key="p" className="flex flex-col gap-0.5">
                  <Mono>{policy.policy_id}</Mono>
                  <span className="font-normal text-secondary text-ink-muted">{policy.title}</span>
                </span>,
                <BulletList key="w" items={matchConditions(policy.match)} empty="—" />,
                policy.allowed_models ? (
                  <IdList key="m" ids={policy.allowed_models} empty={t('policyEnforce.models.none')} />
                ) : (
                  t('models.policies.unchanged')
                ),
                <BulletList
                  key="c"
                  items={[
                    policy.execution_path ? t('policyEnforce.path', { path: pathLabel(policy.execution_path) }) : null,
                    policy.external_providers === 'deny' ? t('policyEnforce.externalDeny') : null,
                    policy.cim === 'bypass' ? t('policyEnforce.cimBypass') : null,
                  ].filter((item): item is string => item !== null)}
                  empty="—"
                />,
              ],
            }))}
            empty={t('models.policies.empty')}
          />
        </Section>
      </Card>
    </>
  );
}
