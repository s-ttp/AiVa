import { Ltr } from '@aiva/ui';
import { Fragment, type ReactNode } from 'react';

/** Identifiers, hashes and CIDRs: monospaced and always left-to-right. */
export function Mono({ children }: { children: ReactNode }) {
  return <Ltr className="font-mono">{children}</Ltr>;
}

/** A dashed note for explanations that stand in for a value. */
export function Note({ children }: { children: ReactNode }) {
  return <p className="rounded-card border border-dashed border-line bg-canvas px-4 py-3 text-secondary">{children}</p>;
}

/** A bulleted list, or the `empty` text when there is nothing to list. */
export function BulletList({ items, empty }: { items: readonly ReactNode[]; empty: string }) {
  if (items.length === 0) return <span className="text-ink-muted">{empty}</span>;
  return (
    <ul className="list-disc space-y-1 ps-5">
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );
}

/** Identifiers as a bulleted list of monospaced values. */
export function IdList({ ids, empty }: { ids: readonly string[]; empty: string }) {
  return <BulletList items={ids.map((id) => <Mono key={id}>{id}</Mono>)} empty={empty} />;
}

/** Identifiers as a compact, comma-separated run that wraps between items: for table cells. */
export function InlineIds({ ids, empty }: { ids: readonly string[]; empty: string }) {
  if (ids.length === 0) return <span className="text-ink-muted">{empty}</span>;
  return (
    <span className="font-mono text-secondary">
      {ids.map((id, index) => (
        <Fragment key={id}>
          {index > 0 && ', '}
          <Ltr className="whitespace-nowrap">{id}</Ltr>
        </Fragment>
      ))}
    </span>
  );
}
