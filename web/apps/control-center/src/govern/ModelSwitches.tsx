import { Badge, Button, ErrorState } from '@aiva/ui';
import { CircleCheck, CircleOff, OctagonX, TriangleAlert } from 'lucide-react';
import { useId, type RefObject } from 'react';
import { Link } from 'react-router';
import type { ApiError, ModelOut, ModelSwitchResponse } from '../api/client';
import { describeError } from '../api/describeError';
import { DataTable } from '../components/DataTable';
import { IdList, Mono } from '../components/inline';
import { t } from '../i18n';
import { classificationLabel } from '../labels';

/** A switch asked for: one model, one application, on or off (ADR-0027). */
export interface SwitchRequest {
  model: ModelOut;
  applicationId: string;
  enabled: boolean;
}

/**
 * The applications granted a model, each on or switched off in the published version. Whoever may draft
 * switches (`viewer_can_switch`) gets a button per application; it drafts a policy version, never publishes.
 */
export function GrantedApplications({
  model,
  canSwitch,
  pending,
  onSwitch,
}: {
  model: ModelOut;
  canSwitch: boolean;
  /** The switch being drafted, if any: every button waits for it. */
  pending: SwitchRequest | null;
  onSwitch: (request: SwitchRequest) => void;
}) {
  if (model.application_ids.length === 0) return <span className="text-ink-muted">{t('models.applications.none')}</span>;
  const off = new Set(model.switched_off_application_ids ?? []);
  return (
    <ul className="space-y-2">
      {model.application_ids.map((applicationId) => {
        const on = !off.has(applicationId);
        const busy = pending?.model.id === model.id && pending.applicationId === applicationId;
        return (
          <li key={applicationId} className="flex flex-wrap items-center gap-2">
            <Mono>{applicationId}</Mono>
            {on ? (
              <Badge icon={CircleCheck} tone="success">
                {t('models.switch.on')}
              </Badge>
            ) : (
              <Badge icon={CircleOff} tone="neutral">
                {t('models.switch.off')}
              </Badge>
            )}
            {canSwitch && (
              <Button
                variant="secondary"
                className="min-h-11"
                disabled={pending !== null}
                aria-busy={busy}
                aria-label={t(on ? 'models.switch.turnOffFor' : 'models.switch.turnOnFor', {
                  model: model.display_name,
                  application: applicationId,
                })}
                onClick={() => onSwitch({ model, applicationId, enabled: !on })}
              >
                {busy ? t('models.switch.pending') : t(on ? 'models.switch.turnOff' : 'models.switch.turnOn')}
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * What a drafted switch would change: the draft to test and publish in Policy Rules, the gateway's own warnings
 * (shown as written: a sovereign switch-off leaves confidential requests blocked, never rerouted), and the
 * models each classification of the application's requests may use before and after. A classification with a model
 * left only has one available: the check sends no task and runs nothing.
 */
export function SwitchResult({
  result,
  modelName,
  headingRef,
}: {
  result: ModelSwitchResponse;
  modelName: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
}) {
  const headingId = useId();
  const warningsId = useId();
  const version = result.draft.version;
  const warnings = result.warnings ?? [];
  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <h2 id={headingId} ref={headingRef} tabIndex={-1} className="text-heading font-semibold text-brand-navy">
        {t(result.enabled ? 'models.switch.result.on' : 'models.switch.result.off', {
          version,
          model: modelName,
          application: result.application_id,
        })}
      </h2>
      <p className="text-secondary">{t('models.switch.notLive', { base: result.base_version })}</p>
      {result.high_impact && <p className="text-secondary">{t('models.switch.highImpact')}</p>}
      {warnings.length > 0 && (
        <section
          aria-labelledby={warningsId}
          className={
            result.sovereign_blocked
              ? 'space-y-2 rounded-card border border-danger/40 bg-danger/10 p-4'
              : 'space-y-2 rounded-card border border-warning/40 bg-warning/10 p-4'
          }
        >
          <h3 id={warningsId} className="flex items-center gap-2 text-body font-semibold">
            {result.sovereign_blocked ? (
              <OctagonX aria-hidden="true" className="size-4 shrink-0 text-danger" />
            ) : (
              <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-warning" />
            )}
            {t('models.switch.warnings')}
          </h3>
          <ul className="list-disc space-y-1 ps-5 text-secondary">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </section>
      )}
      <DataTable
        caption={t('models.switch.checks')}
        minWidth="44rem"
        columns={[
          { label: t('models.switch.col.classification') },
          { label: t('models.switch.col.path') },
          { label: t('models.switch.col.before') },
          { label: t('models.switch.col.after') },
          { label: t('models.switch.col.result') },
        ]}
        rows={result.checks.map((check) => ({
          key: check.classification,
          cells: [
            classificationLabel(check.classification),
            t(check.sovereign_path ? 'models.switch.path.sovereign' : 'models.switch.path.routed'),
            <IdList key="b" ids={check.allowed_models_before} empty={t('models.switch.noModels')} />,
            <IdList key="a" ids={check.allowed_models_after} empty={t('models.switch.noModels')} />,
            check.blocked
              ? t(check.blocked_before ? 'models.switch.blockedBefore' : 'models.switch.blocked')
              : t('models.switch.served'),
          ],
        }))}
      />
      <p>
        <Link
          to={`/admin/govern/policy-rules?version=${version}`}
          className="font-semibold text-brand-navy underline underline-offset-2"
        >
          {t('models.switch.open', { version })}
        </Link>
      </p>
    </section>
  );
}

export function SwitchFailed({ error }: { error: ApiError }) {
  return <ErrorState title={t('models.switch.failed')} description={describeError(error)} />;
}
