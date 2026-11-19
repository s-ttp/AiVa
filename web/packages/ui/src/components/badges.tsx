import {
  Calculator,
  CircleCheck,
  ClipboardCheck,
  FlaskConical,
  Gauge,
  LockKeyhole,
  RotateCcw,
  Shapes,
  ShieldCheck,
  ShieldX,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import type { ReactNode } from 'react';
import { cx } from '../cx';
import { t } from '../strings';

export type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: 'border-line bg-canvas',
  success: 'border-success/40 bg-success/10 [&>svg]:text-success',
  warning: 'border-warning/40 bg-warning/10 [&>svg]:text-warning',
  danger: 'border-danger/40 bg-danger/10 [&>svg]:text-danger',
  info: 'border-info/40 bg-info/10 [&>svg]:text-info',
};

interface BadgeProps {
  /** Required: badges always pair text with an icon, never colour alone. */
  icon: LucideIcon;
  tone?: BadgeTone;
  children: ReactNode;
}

export function Badge({ icon: Icon, tone = 'neutral', children }: BadgeProps) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-secondary font-medium text-ink',
        TONE_CLASSES[tone],
      )}
    >
      <Icon className="size-3.5 shrink-0" />
      {children}
    </span>
  );
}

export const PROVENANCES = ['measured', 'estimated', 'simulated', 'replayed', 'synthetic'] as const;
export type Provenance = (typeof PROVENANCES)[number];

const PROVENANCE_ICONS: Record<Provenance, LucideIcon> = {
  measured: Gauge,
  estimated: Calculator,
  simulated: FlaskConical,
  replayed: RotateCcw,
  synthetic: Shapes,
};

/** Where a value comes from; neutral styling, the text label carries the meaning. */
export function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  return (
    <Badge icon={PROVENANCE_ICONS[provenance]}>
      <span className="sr-only">{t('provenance.srPrefix')} </span>
      <span>{t(`provenance.${provenance}`)}</span>
    </Badge>
  );
}

/**
 * A small provenance mark beside a value, for views with one provenance legend
 * instead of a badge on every value: the icon shows it, the label is announced.
 */
export function ProvenanceMark({ provenance }: { provenance: Provenance }) {
  const Icon = PROVENANCE_ICONS[provenance];
  return (
    <span className="inline-flex items-center align-middle text-ink-muted" title={t(`provenance.${provenance}`)}>
      <Icon aria-hidden="true" className="size-3.5 shrink-0" />
      <span className="sr-only">
        {t('provenance.srPrefix')} {t(`provenance.${provenance}`)}
      </span>
    </span>
  );
}

/** The icon a provenance is marked with, for a legend that explains the marks. */
export function provenanceIcon(provenance: Provenance): LucideIcon {
  return PROVENANCE_ICONS[provenance];
}

const STATUSES = {
  allowed: { icon: ShieldCheck, tone: 'success' },
  blocked: { icon: ShieldX, tone: 'danger' },
  sovereignDirect: { icon: LockKeyhole, tone: 'info' },
  approvalRequired: { icon: ClipboardCheck, tone: 'warning' },
  cimActive: { icon: CircleCheck, tone: 'success' },
  cimFailed: { icon: TriangleAlert, tone: 'warning' },
} as const satisfies Record<string, { icon: LucideIcon; tone: BadgeTone }>;

export type Status = keyof typeof STATUSES;

/** Policy and context states with the LLD §22.8 copy. */
export function StatusBadge({ status }: { status: Status }) {
  const { icon, tone } = STATUSES[status];
  return (
    <Badge icon={icon} tone={tone}>
      {t(`status.${status}`)}
    </Badge>
  );
}
