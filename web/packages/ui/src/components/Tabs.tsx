import { useId, useState, type KeyboardEvent, type ReactNode } from 'react';
import { cx } from '../cx';

export interface TabItem {
  id: string;
  label: string;
  content: ReactNode;
}

interface TabsProps {
  /** Accessible name of the tab list. */
  label: string;
  items: readonly TabItem[];
}

/**
 * WAI-ARIA tabs with automatic activation: arrow keys (mirrored in RTL), Home
 * and End move between tabs; only the selected tab is in the Tab sequence.
 */
export function Tabs({ label, items }: TabsProps) {
  const baseId = useId();
  const [selectedId, setSelectedId] = useState(items[0]?.id);
  const tabId = (id: string) => `${baseId}-tab-${id}`;
  const panelId = (id: string) => `${baseId}-panel-${id}`;

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const rtl = getComputedStyle(event.currentTarget).direction === 'rtl';
    const forward = rtl ? 'ArrowLeft' : 'ArrowRight';
    const backward = rtl ? 'ArrowRight' : 'ArrowLeft';
    const targets: Record<string, number> = {
      [forward]: (index + 1) % items.length,
      [backward]: (index - 1 + items.length) % items.length,
      Home: 0,
      End: items.length - 1,
    };
    const next = items[targets[event.key] ?? -1];
    if (!next) return;
    event.preventDefault();
    setSelectedId(next.id);
    document.getElementById(tabId(next.id))?.focus();
  }

  return (
    <div>
      <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b border-line">
        {items.map((item, index) => {
          const selected = item.id === selectedId;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={tabId(item.id)}
              aria-selected={selected}
              aria-controls={panelId(item.id)}
              tabIndex={selected ? 0 : -1}
              onClick={() => setSelectedId(item.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
              className={cx(
                '-mb-px whitespace-nowrap border-b-2 px-4 py-2 text-body',
                selected
                  ? 'border-brand-navy font-semibold text-brand-navy'
                  : 'border-transparent text-ink-muted hover:text-ink',
              )}
            >
              {item.label}
            </button>
          );
        })}
      </div>
      {items.map((item) => (
        <div
          key={item.id}
          role="tabpanel"
          id={panelId(item.id)}
          aria-labelledby={tabId(item.id)}
          hidden={item.id !== selectedId}
          tabIndex={0}
          className="pt-6"
        >
          {item.content}
        </div>
      ))}
    </div>
  );
}
