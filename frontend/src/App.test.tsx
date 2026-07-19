import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from './App';
import { apiJson, registerUnauthorizedHandler, ApiError } from './api/client';

vi.mock('./api/client', async () => {
  const actual = await vi.importActual<typeof import('./api/client')>('./api/client');
  return {
    ...actual,
    apiJson: vi.fn(),
  };
});

function TestApp({ initialEntries }: { initialEntries: string[] }) {
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <AppRoutes />
    </MemoryRouter>
  );
}

describe('App routes + auth guards', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    registerUnauthorizedHandler(null);
  });

  afterEach(() => {
    cleanup();
    registerUnauthorizedHandler(null);
  });

  it('redirects unauthenticated root to login page', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError('unauthorized', 401),
    );

    render(<TestApp initialEntries={['/']} />);

    await waitFor(() => {
      expect(screen.getByTestId('login-page')).toBeDefined();
    });
  });

  it('shows chat page for authenticated non-admin and hides admin nav', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      code: 200,
      data: { user_id: 'u1', roles: ['user'], spaces: ['personal'] },
    });

    render(<TestApp initialEntries={['/chat']} />);

    await waitFor(() => {
      expect(screen.getByTestId('chat-page')).toBeDefined();
    });
    expect(screen.getByTestId('app-nav')).toBeDefined();
    expect(screen.queryByText('项目管理')).toBeNull();
  });

  it('shows admin nav link for authenticated super_admin', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      code: 200,
      data: { user_id: 'admin1', roles: ['super_admin'], spaces: ['*'] },
    });

    render(<TestApp initialEntries={['/chat']} />);

    await waitFor(() => {
      expect(screen.getByTestId('chat-page')).toBeDefined();
    });
    expect(screen.getByText('项目管理')).toBeDefined();
  });

  it('blocks department admin from the super-admin page', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      code: 200,
      data: {
        user_id: 'lead1',
        roles: ['department_admin'],
        spaces: ['docs'],
      },
    });
    render(<TestApp initialEntries={['/admin/projects']} />);
    await waitFor(() => {
      expect(screen.getByTestId('auth-forbidden')).toBeDefined();
    });
    expect(screen.queryByTestId('admin-projects-page')).toBeNull();
    expect(screen.queryByText('项目管理')).toBeNull();
  });
});
