import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider, useAuth } from './AuthContext';
import { apiJson, registerUnauthorizedHandler } from '../../api/client';

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client');
  return {
    ...actual,
    apiJson: vi.fn(),
  };
});

function Probe() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="status">{auth.status}</span>
      <span data-testid="roles">{auth.roles.join(',')}</span>
    </div>
  );
}

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    registerUnauthorizedHandler(null);
  });
  afterEach(() => {
    cleanup();
    registerUnauthorizedHandler(null);
  });

  it('sets authenticated when /auth/me returns 200', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      code: 200,
      data: { user_id: 'u1', roles: ['admin'], spaces: ['*'] },
    });
    render(
      <MemoryRouter>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'));
    expect(screen.getByTestId('roles').textContent).toBe('admin');
  });

  it('sets unauthenticated when /auth/me returns 401 ApiError', async () => {
    const { ApiError } = await import('../../api/client');
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError('unauthorized', 401),
    );
    render(
      <MemoryRouter>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('unauthenticated'));
  });
});
