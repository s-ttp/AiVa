import { createTranslator } from './i18n';

/** Copy owned by the shared components (status copy per LLD §22.8). */
const en = {
  'brand.name': 'AiVa',
  'shell.skipToContent': 'Skip to main content',
  'shell.menu': 'Menu',
  'provenance.label': 'Data provenance',
  'provenance.srPrefix': 'Data provenance:',
  'provenance.measured': 'Measured',
  'provenance.estimated': 'Estimated',
  'provenance.simulated': 'Simulated',
  'provenance.replayed': 'Replayed',
  'provenance.synthetic': 'Synthetic',
  'provenance.source': 'Source',
  'provenance.calculation': 'Calculation',
  'provenance.window': 'Time window',
  'provenance.pricingVersion': 'Pricing version',
  'provenance.cacheAccounting': 'Cache accounting',
  'provenance.limitations': 'Limitations',
  'status.allowed': 'Allowed by policy',
  'status.blocked': 'Blocked by AI policy',
  'status.sovereignDirect': 'Sovereign direct route required',
  'status.approvalRequired': 'Approval required',
  'status.cimActive': 'Context package generated; sufficiency verified',
  'status.cimFailed': 'Context sufficiency check failed; safe fallback applied',
  'banner.synthetic.title': 'Synthetic demo data · Illustrative UI',
  'banner.synthetic.body': 'Nothing shown here is a production measurement.',
  'banner.measured.title': 'Measured execution',
  'banner.measured.body':
    'Values come from recorded gateway executions; estimates are labelled as estimates.',
  'kpi.noData': 'No data yet',
  'state.loading': 'Loading…',
  'state.retry': 'Try again',
  'state.gatewayStarting.title': 'The demo is waking up',
  'state.gatewayStarting.body':
    'The AiVa gateway sleeps when no one is using it and takes about 3 minutes to start. This page tries again every 30 seconds.',
  'state.gatewayStarting.retry': 'Try again now',
  'state.gatewayPaused.title': 'The demo is paused',
  'state.gatewayPaused.body':
    'Its backend is paused on Hugging Face and does not wake on its own. Ask a maintainer to restart it, then try again.',
} as const;

export const t = createTranslator({ en });
