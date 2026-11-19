import { Button } from '@aiva/ui';
import { History } from 'lucide-react';
import { Section } from '../components/Section';
import { formatUtc } from '../format';
import { t } from '../i18n';
import { liveRefusalText, type LiveRefusalReason } from './executionMode';

interface LiveRefusedPanelProps {
  /** Null for a reason this UI does not know; the gateway's message (shown above) explains it. */
  reason: LiveRefusalReason | null;
  resetsAt: string | null;
  onReplay: () => void;
}

/**
 * A live run was refused (`LIVE_DEMO_UNAVAILABLE`, `LIVE_NOT_PERMITTED` or
 * `EXECUTION_MODE_UNAVAILABLE`): nothing ran live. Offers the recorded replay of the same request, which the
 * person starts; a live request is never downgraded to a replay silently.
 */
export function LiveRefusedPanel({ reason, resetsAt, onReplay }: LiveRefusedPanelProps) {
  return (
    <Section title={t('liveRefused.title')} description={t('liveRefused.description')}>
      {reason && <p>{liveRefusalText(reason)}</p>}
      {resetsAt && <p>{t('liveDemo.resetsAt', { time: formatUtc(resetsAt) })}</p>}
      <div>
        <Button onClick={onReplay}>
          <History className="size-4" />
          {t('liveRefused.replay')}
        </Button>
      </div>
    </Section>
  );
}
