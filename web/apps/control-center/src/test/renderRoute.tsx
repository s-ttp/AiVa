import { render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { expect } from 'vitest';
import { routes } from '../routes';

export function renderRoute(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return { router, ...render(<RouterProvider router={router} />) };
}

/** Waits until a restored session has been verified (or rejected) by GET /v1/auth/me. */
export async function sessionSettled(): Promise<void> {
  await waitFor(() => expect(screen.queryByText('Checking your session…')).not.toBeInTheDocument());
}
