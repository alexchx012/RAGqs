import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from './features/auth/AuthContext';
import LoginPage from './features/auth/LoginPage';
import ProtectedRoute from './routes/ProtectedRoute';
import RootRedirect from './routes/RootRedirect';
import AppNav from './routes/AppNav';
import ChatPage from './pages/ChatPage';
import KnowledgePage from './pages/KnowledgePage';
import AdminProjectsPage from './pages/AdminProjectsPage';
import { apiJson, registerUnauthorizedHandler, ApiError } from './api/client';

vi.mock('./api/client', async () => {
  const actual = await vi.importActual<typeof import('./api/client')>('./api/client');
  return {
    ...actual,
    apiJson: vi.fn(),
  };
});

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="app-shell">
      <AppNav />
      {children}
    </div>
  );
}

function TestApp({ initialEntries }: { initialEntries: string[] }) {
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/chat"
            element={
              <ProtectedRoute>
                <Shell>
                  <ChatPage />
                </Shell>
              </ProtectedRoute>
            }
          />
          <Route
            path="/knowledge"
            element={
              <ProtectedRoute>
                <Shell>
                  <KnowledgePage />
                </Shell>
              </ProtectedRoute>
            }
          />
          <Route
            path="/admin/projects"
            element={
              <ProtectedRoute requireAdmin>
                <Shell>
                  <AdminProjectsPage />
                </Shell>
              </ProtectedRoute>
            }
          />
          <Route path="/" element={<RootRedirect />} />
          <Route path="*" element={<RootRedirect />} />
        </Routes>
      </AuthProvider>
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

  it('shows admin nav link for authenticated admin', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      code: 200,
      data: { user_id: 'admin1', roles: ['admin'], spaces: ['*'] },
    });

    render(<TestApp initialEntries={['/chat']} />);

    await waitFor(() => {
      expect(screen.getByTestId('chat-page')).toBeDefined();
    });
    expect(screen.getByText('项目管理')).toBeDefined();
  });
});
