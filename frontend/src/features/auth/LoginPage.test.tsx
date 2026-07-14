import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import LoginPage from './LoginPage';
import { useAuth } from './AuthContext';
import { ApiError } from '../../api/client';
import type { AuthContextValue } from './AuthContext';

vi.mock('./AuthContext', () => ({
  useAuth: vi.fn(),
}));

const mockUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>;

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

function ChatStub() {
  return <div data-testid="chat-page">聊天</div>;
}

function renderLogin(authPartial: Partial<AuthContextValue> = {}) {
  const login = authPartial.login ?? vi.fn().mockResolvedValue(undefined);
  mockUseAuth.mockReturnValue({
    status: 'unauthenticated',
    userId: null,
    roles: [],
    spaces: [],
    errorMessage: null,
    login,
    logout: vi.fn(),
    refresh: vi.fn(),
    ...authPartial,
  });

  return {
    login,
    ...render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/chat" element={<ChatStub />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>,
    ),
  };
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('shows error and stays on login when credentials invalid', async () => {
    const login = vi.fn().mockRejectedValue(new ApiError('用户名或密码错误', 401));
    renderLogin({ login });

    await userEvent.type(screen.getByLabelText('用户名'), 'baduser');
    await userEvent.type(screen.getByLabelText('密码'), 'badpass');
    await userEvent.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe('用户名或密码错误');
    });
    expect(screen.getByTestId('location').textContent).toBe('/login');
    expect(screen.getByTestId('login-page')).toBeDefined();
  });

  it('calls login and navigates to /chat on success', async () => {
    const login = vi.fn().mockResolvedValue(undefined);
    renderLogin({ login });

    await userEvent.type(screen.getByLabelText('用户名'), 'alice');
    await userEvent.type(screen.getByLabelText('密码'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => {
      expect(login).toHaveBeenCalledWith('alice', 'secret');
    });
    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/chat');
    });
  });

  it('redirects authenticated users away from /login', () => {
    renderLogin({
      status: 'authenticated',
      userId: 'u1',
      roles: ['user'],
      spaces: ['personal'],
    });

    expect(screen.getByTestId('location').textContent).toBe('/chat');
    expect(screen.queryByTestId('login-page')).toBeNull();
  });
});
