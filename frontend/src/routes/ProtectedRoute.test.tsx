import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import ProtectedRoute from './ProtectedRoute';
import { useAuth } from '../features/auth/AuthContext';

vi.mock('../features/auth/AuthContext', () => ({
  useAuth: vi.fn(),
}));

const mockUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>;

describe('ProtectedRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('shows back link to /chat when requireAdmin fails', () => {
    mockUseAuth.mockReturnValue({
      status: 'authenticated',
      userId: 'u1',
      roles: ['user'],
      spaces: [],
      errorMessage: null,
      login: vi.fn(),
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(
      <MemoryRouter>
        <ProtectedRoute requireAdmin>
          <div data-testid="admin-content">admin</div>
        </ProtectedRoute>
      </MemoryRouter>,
    );

    expect(screen.getByTestId('auth-forbidden')).toBeDefined();
    expect(screen.queryByTestId('admin-content')).toBeNull();
    const back = screen.getByRole('link', { name: /返回聊天|回到聊天|返回/ });
    expect(back.getAttribute('href')).toBe('/chat');
  });
});
