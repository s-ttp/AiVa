import {
  Activity,
  AppWindow,
  BadgeCheck,
  Boxes,
  CirclePlay,
  ClipboardCheck,
  Cpu,
  DatabaseZap,
  FileDown,
  FileText,
  Gauge,
  Gavel,
  LayoutDashboard,
  LayoutGrid,
  Layers,
  Library,
  ListChecks,
  Network,
  PiggyBank,
  Scale,
  ScrollText,
  Settings,
  Split,
  Tags,
  Users,
  Wallet,
  Workflow,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import type { Permission } from './auth/permissions';
import type { MessageKey } from './i18n';

type Between<Key, Prefix extends string, Suffix extends string> =
  Key extends `${Prefix}${infer Id}${Suffix}` ? Id : never;

/** Views and areas are keyed by their copy, so every entry is guaranteed a label. */
export type ViewId = Between<MessageKey, 'view.', '.title'>;
export type AreaId = Between<MessageKey, 'area.', ''>;

export interface NavLeaf {
  id: ViewId;
  path: string;
  icon: LucideIcon;
  /** Gateway permission needed to use the view; absent = any signed-in user. */
  permission?: Permission;
  /** The view is about the user dimension, which public demo visitors do not see (ADR-0019). */
  userDimension?: true;
}

export interface NavGroup {
  id: AreaId;
  items: readonly NavEntry[];
}

export type NavEntry = NavLeaf | NavGroup;

/**
 * Information architecture of LLD §22.5, with the user-facing names of §22.2.
 * Permissions hide entries a role cannot use; the gateway still authorizes
 * every request, so hiding is a convenience, not access control.
 */
export const NAVIGATION: readonly NavGroup[] = [
  {
    id: 'console',
    items: [
      { id: 'library', path: '/console', icon: Library, permission: 'catalog:read' },
      { id: 'run', path: '/console/run', icon: CirclePlay, permission: 'chat:test' },
      { id: 'artifacts', path: '/console/artifacts', icon: FileText, permission: 'chat:test' },
      { id: 'agent-workflows', path: '/console/agents', icon: Workflow, permission: 'chat:test' },
    ],
  },
  {
    id: 'admin',
    items: [
      { id: 'overview', path: '/admin', icon: LayoutDashboard, permission: 'reports:read' },
      {
        id: 'govern',
        items: [
          { id: 'applications-identities', path: '/admin/govern/applications', icon: AppWindow, permission: 'policies:read' },
          { id: 'trusted-networks', path: '/admin/govern/trusted-networks', icon: Network, permission: 'policies:read' },
          { id: 'policy-rules', path: '/admin/govern/policy-rules', icon: Gavel, permission: 'policies:read' },
          { id: 'model-allowlist', path: '/admin/govern/model-allowlist', icon: ListChecks, permission: 'policies:read' },
          { id: 'tool-permissions', path: '/admin/govern/tool-permissions', icon: Wrench, permission: 'policies:read' },
          { id: 'budgets-quotas', path: '/admin/govern/budgets-quotas', icon: Wallet, permission: 'budgets:read' },
          { id: 'approval-queue', path: '/admin/govern/approval-queue', icon: ClipboardCheck, permission: 'approvals:read' },
        ],
      },
      {
        id: 'optimize',
        items: [
          { id: 'routing-policies', path: '/admin/optimize/routing-policies', icon: Split, permission: 'policies:read' },
          { id: 'cim-profiles', path: '/admin/optimize/cim-profiles', icon: Layers, permission: 'policies:read' },
          { id: 'cache-policies', path: '/admin/optimize/cache-policies', icon: DatabaseZap, permission: 'policies:read' },
          { id: 'model-registry', path: '/admin/optimize/model-registry', icon: Boxes, permission: 'policies:read' },
          { id: 'benchmarks', path: '/admin/optimize/benchmarks', icon: Scale, permission: 'evaluations:run' },
        ],
      },
      {
        id: 'insights',
        items: [
          { id: 'consumption-overview', path: '/admin/insights', icon: Activity, permission: 'reports:read' },
          { id: 'applications', path: '/admin/insights/applications', icon: LayoutGrid, permission: 'reports:read' },
          { id: 'users', path: '/admin/insights/users', icon: Users, permission: 'reports:read', userDimension: true },
          { id: 'prompt-categories', path: '/admin/insights/prompt-categories', icon: Tags, permission: 'reports:read' },
          { id: 'models', path: '/admin/insights/models', icon: Cpu, permission: 'reports:read' },
          { id: 'savings', path: '/admin/insights/savings', icon: PiggyBank, permission: 'reports:read' },
          { id: 'quotas', path: '/admin/insights/quotas', icon: Gauge, permission: 'budgets:read' },
          { id: 'export-center', path: '/admin/insights/export-center', icon: FileDown, permission: 'reports:export' },
        ],
      },
      { id: 'quality-assurance', path: '/admin/quality', icon: BadgeCheck, permission: 'reports:read' },
      { id: 'audit', path: '/admin/audit', icon: ScrollText, permission: 'runs:read' },
      { id: 'settings', path: '/admin/settings', icon: Settings },
    ],
  },
];

export interface RoutedView extends NavLeaf {
  /** Nearest enclosing area, shown as the page eyebrow. */
  area: AreaId;
}

/** Every navigable view with its enclosing area, in navigation order. */
export function listViews(groups: readonly NavGroup[] = NAVIGATION): RoutedView[] {
  return groups.flatMap((group) =>
    group.items.flatMap((entry) =>
      'path' in entry ? [{ ...entry, area: group.id }] : listViews([entry]),
    ),
  );
}

/** Whether a caller may use a view: its permission, and the user dimension where the view needs it. */
export function viewAllowed(
  view: NavLeaf,
  can: (permission: Permission) => boolean,
  usersVisible = true,
): boolean {
  return (!view.permission || can(view.permission)) && (usersVisible || !view.userDimension);
}

/** The navigation a caller may use: leaves they may not use and emptied groups are removed. */
export function permittedNavigation(
  can: (permission: Permission) => boolean,
  usersVisible = true,
  groups: readonly NavGroup[] = NAVIGATION,
): NavGroup[] {
  const keep = (entry: NavEntry): NavEntry[] => {
    if ('path' in entry) return viewAllowed(entry, can, usersVisible) ? [entry] : [];
    const items = entry.items.flatMap(keep);
    return items.length ? [{ ...entry, items }] : [];
  };
  return groups.flatMap((group) => keep(group) as NavGroup[]);
}

/** The navigation without the named sections, at any depth. */
export function withoutSections(groups: readonly NavGroup[], ids: readonly string[]): NavGroup[] {
  const keep = (entry: NavEntry): NavEntry[] => {
    if ('path' in entry) return [entry];
    if (ids.includes(entry.id)) return [];
    const items = entry.items.flatMap(keep);
    return items.length ? [{ ...entry, items }] : [];
  };
  return groups.flatMap((group) => keep(group) as NavGroup[]);
}

/** Where a caller lands after sign-in: the first view they may use. */
export function homePath(can: (permission: Permission) => boolean): string {
  return listViews(permittedNavigation(can))[0]?.path ?? '/admin/settings';
}

