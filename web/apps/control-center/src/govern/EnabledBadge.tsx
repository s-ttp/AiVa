import { Badge } from '@aiva/ui';
import { CircleCheck, CircleOff } from 'lucide-react';
import { t } from '../i18n';

export function EnabledBadge({ enabled }: { enabled: boolean }) {
  return enabled ? (
    <Badge icon={CircleCheck} tone="success">
      {t('admin.enabled')}
    </Badge>
  ) : (
    <Badge icon={CircleOff} tone="neutral">
      {t('admin.disabled')}
    </Badge>
  );
}
