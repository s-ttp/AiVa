import type { ApplicationOut } from '../api/client';
import { useSession } from '../auth/session';
import { Mono } from '../components/inline';
import { Section } from '../components/Section';
import { t } from '../i18n';

/**
 * How a client calls AiVa with one of an application's keys (ADR-0026): any OpenAI SDK, pointed at the
 * gateway's OpenAI-compatible base URL. The example names where the key goes and never holds one.
 */
export function UseTheApi({ application }: { application: ApplicationOut }) {
  const { gatewayUrl } = useSession();
  if (!gatewayUrl) return null;
  const base = `${gatewayUrl}/v1`;
  // Code, not copy: it stays in English and left to right whatever the page's language.
  const example = [
    'from openai import OpenAI',
    '',
    `client = OpenAI(base_url="${base}", api_key="<a key for ${application.id}>")`,
    'completion = client.chat.completions.create(',
    '    model="aiva-auto",',
    '    messages=[{"role": "user", "content": "…"}],',
    ')',
    'print(completion.choices[0].message.content)',
  ].join('\n');
  return (
    <Section title={t('keys.api.title')} description={t('keys.api.intro')}>
      <p className="flex flex-wrap items-baseline gap-x-2 text-secondary">
        <span className="font-semibold">{t('keys.api.baseUrl')}</span>
        <Mono>{base}</Mono>
      </p>
      <pre
        role="region"
        aria-label={t('keys.api.example')}
        tabIndex={0}
        dir="ltr"
        className="overflow-auto rounded-card border border-line bg-canvas p-3 text-start font-mono text-secondary"
      >
        <code>{example}</code>
      </pre>
      <p className="text-secondary text-ink-muted">{t('keys.api.model')}</p>
    </Section>
  );
}
