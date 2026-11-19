import { cx } from '../cx';
import { useId, type ReactNode } from 'react';

interface DataTableProps {
  caption: string;
  /** Show the caption visually; otherwise it is for assistive technology only. */
  showCaption?: boolean;
  columns: ReadonlyArray<{ label: string; numeric?: boolean }>;
  rows: ReadonlyArray<{ key: string; cells: readonly ReactNode[] }>;
  /** Shown as a single row when there are no rows; never left blank. */
  empty?: string;
  minWidth?: string;
}

/**
 * Plain text and numbers are isolated with <bdi>, so dates, amounts and IDs
 * keep their own direction in right-to-left layouts.
 */
function isolate(cell: ReactNode): ReactNode {
  return typeof cell === 'string' || typeof cell === 'number' ? <bdi>{cell}</bdi> : cell;
}

/**
 * A captioned, keyboard-scrollable table; the first cell of each row is its header.
 * The scroller is `relative` so absolutely positioned content (e.g. a badge's
 * screen-reader label) is clipped with the table instead of widening the page.
 */
export function DataTable({ caption, showCaption, columns, rows, empty, minWidth = '32rem' }: DataTableProps) {
  const captionId = useId();
  return (
    <div role="region" aria-labelledby={captionId} tabIndex={0} className="relative overflow-x-auto">
      <table className="w-full text-start" style={{ minInlineSize: minWidth }}>
        <caption id={captionId} className={showCaption ? 'mb-2 text-start text-secondary font-semibold' : 'sr-only'}>
          {caption}
        </caption>
        <thead>
          <tr className="text-secondary text-ink-muted">
            {columns.map((column) => (
              <th
                key={column.label}
                scope="col"
                className={cx('px-3 pb-3 font-semibold', column.numeric ? 'text-end' : 'text-start')}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr className="border-t border-line">
              <td colSpan={columns.length} className="px-3 py-3 text-ink-muted">
                {empty ?? '—'}
              </td>
            </tr>
          )}
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-line align-top">
              {row.cells.map((rawCell, index) => {
                const cell = isolate(rawCell);
                const numeric = columns[index]?.numeric;
                const className = cx('px-3 py-3', numeric ? 'text-end tabular-nums' : 'text-start');
                return index === 0 ? (
                  <th key={index} scope="row" className={cx(className, 'font-semibold')}>
                    {cell}
                  </th>
                ) : (
                  <td key={index} className={className}>
                    {cell}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
