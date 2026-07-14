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

  it('refresh probes /auth/me with skipUnauthorizedHandler', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      code: 200,
      data: { user_id: 'u1', roles: ['user'], spaces: [] },
    });
    render(
      <MemoryRouter>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'));
    expect(apiJson).toHaveBeenCalledWith(
      '/auth/me',
      undefined,
      expect.objectContaining({ skipUnauthorizedHandler: true }),
    );
  });

  it('login rejects when body.data is missing user_id and stays unauthenticated', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new (await import('../../api/client')).ApiError('unauthorized', 401),
    );

    function LoginProbe() {
      const auth = useAuth();
      return (
        <div>
          <span data-testid="status">{auth.status}</span>
          <span data-testid="user-id">{auth.userId ?? ''}</span>
          <button
            type="button"
            data-testid="do-login"
            onClick={() => {
              void auth.login('alice', 'pw').catch(() => undefined);
            }}
          >
            login
          </button>
        </div>
      );
    }

    render(
      <MemoryRouter>
        <AuthProvider>
          <LoginProbe />
        </AuthProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('unauthenticated'));

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ code: 200, data: {} }),
      }),
    );

    screen.getByTestId('do-login').click();

    await waitFor(() => {
      expect(screen.getByTestId('status').textContent).toBe('unauthenticated');
    });
    expect(screen.getByTestId('user-id').textContent).toBe('');

    vi.unstubAllGlobals();
  });
});
