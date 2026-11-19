import type { ReactNode } from 'react';

interface LtrProps {
  children: ReactNode;
  className?: string;
}

/** Keeps IDs, IPs, JSON, currency and trace IDs left-to-right inside RTL text. */
export function Ltr({ children, className }: LtrProps) {
  return (
    <bdi dir="ltr" className={className}>
      {children}
    </bdi>
  );
}
