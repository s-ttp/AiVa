import { LoadingState } from '@aiva/ui';
import { lazy, Suspense, type ComponentType, type ReactElement } from 'react';
import type { RouteObject } from 'react-router';
import { ControlCenterShell } from './layout/ControlCenterShell';
import { RootLayout } from './layout/RootLayout';
import { ViewGate } from './layout/ViewGate';
import { t } from './i18n';
import { listViews, type ViewId } from './navigation';
import { LandingPage } from './pages/LandingPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { RouteErrorPage } from './pages/RouteErrorPage';
import { ViewPlaceholderPage } from './pages/ViewPlaceholderPage';

/**
 * A page loaded on first visit, so each view ships as its own chunk and the
 * sign-in page loads only the shell. Pages are named exports.
 */
function lazyPage<Name extends string>(
  load: () => Promise<Record<Name, ComponentType>>,
  name: Name,
): ReactElement {
  const Page = lazy(async (): Promise<{ default: ComponentType }> => ({ default: (await load())[name] }));
  return <Page />;
}

/** Views with dedicated pages; every other navigation entry is a placeholder. */
const PAGES: Partial<Record<ViewId, ReactElement>> = {
  library: lazyPage(() => import('./pages/ScenarioLibraryPage'), 'ScenarioLibraryPage'),
  run: lazyPage(() => import('./pages/RunWorkspacePage'), 'RunWorkspacePage'),
  artifacts: lazyPage(() => import('./pages/ArtifactsPage'), 'ArtifactsPage'),
  'agent-workflows': lazyPage(() => import('./pages/AgentWorkflowsPage'), 'AgentWorkflowsPage'),
  overview: lazyPage(() => import('./pages/OverviewPage'), 'OverviewPage'),
  'applications-identities': lazyPage(() => import('./pages/ApplicationsPage'), 'ApplicationsPage'),
  'trusted-networks': lazyPage(() => import('./pages/TrustedNetworksPage'), 'TrustedNetworksPage'),
  'policy-rules': lazyPage(() => import('./pages/PolicyVersionsPage'), 'PolicyVersionsPage'),
  'model-allowlist': lazyPage(() => import('./pages/ModelAllowlistPage'), 'ModelAllowlistPage'),
  'tool-permissions': lazyPage(() => import('./pages/ToolPermissionsPage'), 'ToolPermissionsPage'),
  'model-registry': lazyPage(() => import('./pages/ModelRegistryPage'), 'ModelRegistryPage'),
  'budgets-quotas': lazyPage(() => import('./pages/BudgetsPage'), 'BudgetsPage'),
  'approval-queue': lazyPage(() => import('./pages/ApprovalQueuePage'), 'ApprovalQueuePage'),
  'consumption-overview': lazyPage(() => import('./pages/insights/ConsumptionOverviewPage'), 'ConsumptionOverviewPage'),
  applications: lazyPage(() => import('./pages/insights/InsightsApplicationsPage'), 'InsightsApplicationsPage'),
  users: lazyPage(() => import('./pages/insights/InsightsUsersPage'), 'InsightsUsersPage'),
  'prompt-categories': lazyPage(() => import('./pages/insights/PromptCategoriesPage'), 'PromptCategoriesPage'),
  models: lazyPage(() => import('./pages/insights/ModelsPage'), 'ModelsPage'),
  savings: lazyPage(() => import('./pages/insights/SavingsPage'), 'SavingsPage'),
  quotas: lazyPage(() => import('./pages/insights/QuotasPage'), 'QuotasPage'),
  'export-center': lazyPage(() => import('./pages/insights/ExportCenterPage'), 'ExportCenterPage'),
  'quality-assurance': lazyPage(() => import('./pages/QualityPage'), 'QualityPage'),
  audit: lazyPage(() => import('./pages/TraceExplorerPage'), 'TraceExplorerPage'),
  settings: lazyPage(() => import('./pages/SettingsPage'), 'SettingsPage'),
};

export const routes: RouteObject[] = [
  {
    element: <RootLayout />,
    children: [
      {
        errorElement: <RouteErrorPage />,
        children: [
          // One front door; "/sign-in" is the same page at its own address.
          { index: true, element: <LandingPage /> },
          { path: 'sign-in', element: <LandingPage /> },
          {
            element: <ControlCenterShell />,
            children: [
              ...listViews().map((view) => ({
                path: view.path,
                element: (
                  <ViewGate view={view}>
                    <Suspense fallback={<LoadingState label={t('view.loading')} />}>
                      {PAGES[view.id] ?? <ViewPlaceholderPage view={view} />}
                    </Suspense>
                  </ViewGate>
                ),
              })),
              { path: '*', element: <NotFoundPage /> },
            ],
          },
        ],
      },
    ],
  },
];
