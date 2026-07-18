import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from '../features/auth/AuthContext';
import ProtectedRoute from '../routes/ProtectedRoute';
import ChatPage from './ChatPage';
import { apiJson, registerUnauthorizedHandler, ApiError } from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    apiJson: vi.fn(),
  };
});

function TestChatRoute() {
  return (
    <MemoryRouter initialEntries={['/chat']}>
      <AuthProvider>
        <Routes>
          <Route
            path="/chat"
            element={
              <ProtectedRoute>
                <ChatPage />
              </ProtectedRoute>
            }
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>
  );
}

describe('ChatPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    registerUnauthorizedHandler(null);
    localStorage.clear();
    // ChatPanel 滚动依赖
    Element.prototype.scrollIntoView = vi.fn();

    (apiJson as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === '/auth/me') {
        return {
          code: 200,
          data: { user_id: 'u1', roles: ['user'], spaces: ['personal'] },
        };
      }
      if (path === '/knowledge-spaces') {
        return {
          code: 200,
          data: { spaces: [{ space_id: 'personal', name: '个人知识库' }] },
        };
      }
      if (path.startsWith('/chat/sessions')) {
        return {
          code: 200,
          data: { sessions: [] },
        };
      }
      return { code: 200, data: {} };
    });
  });

  afterEach(() => {
    cleanup();
    registerUnauthorizedHandler(null);
  });

  it('renders chat workspace with welcome greeting and new-chat button when authenticated', async () => {
    render(<TestChatRoute />);

    await waitFor(() => {
      expect(screen.getByTestId('chat-page')).toBeDefined();
    });

    await waitFor(() => {
      expect(screen.getByText('你好！我是知识库问答助手')).toBeDefined();
    });
    expect(screen.getByText('新建对话')).toBeDefined();
  });

  it('does not render retrieval audit section on chat page', async () => {
    render(<TestChatRoute />);

    await waitFor(() => {
      expect(screen.getByTestId('chat-page')).toBeDefined();
    });

    await waitFor(() => {
      expect(screen.getByText('你好！我是知识库问答助手')).toBeDefined();
    });

    expect(screen.queryByText('检索审计')).toBeNull();
  });

  it('disables sending and shows a banner when no knowledge space is accessible', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === '/auth/me') {
        return { code: 200, data: { user_id: 'u1', roles: ['user'], spaces: [] } };
      }
      if (path === '/knowledge-spaces') {
        return { code: 200, data: { spaces: [] } };
      }
      if (path.startsWith('/chat/sessions')) {
        return { code: 200, data: { sessions: [] } };
      }
      return { code: 200, data: {} };
    });

    render(<TestChatRoute />);

    await waitFor(() => {
      expect(screen.getByText('暂无可用知识空间，无法发送消息或上传文件')).toBeDefined();
    });

    const input = screen.getByPlaceholderText('输入你的问题...') as HTMLInputElement;
    expect(input.disabled).toBe(true);
  });

  it('shows a retryable error banner when loading knowledge spaces fails', async () => {
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === '/auth/me') {
        return { code: 200, data: { user_id: 'u1', roles: ['user'], spaces: ['personal'] } };
      }
      if (path === '/knowledge-spaces') {
        throw new Error('网络错误');
      }
      if (path.startsWith('/chat/sessions')) {
        return { code: 200, data: { sessions: [] } };
      }
      return { code: 200, data: {} };
    });

    render(<TestChatRoute />);

    await waitFor(() => {
      expect(screen.getByText(/知识空间加载失败: 网络错误/)).toBeDefined();
    });

    const input = screen.getByPlaceholderText('输入你的问题...') as HTMLInputElement;
    expect(input.disabled).toBe(true);
  });
});
