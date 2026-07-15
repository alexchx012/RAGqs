import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import React from 'react';
import KnowledgePage from './KnowledgePage';
import { apiJson } from '../api/client';

vi.mock('../api/client', () => ({
  apiJson: vi.fn(),
}));

describe('KnowledgePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === '/knowledge-spaces') {
        return {
          code: 200,
          data: { spaces: [{ space_id: 'personal', name: '个人知识库' }] },
        };
      }
      if (path.includes('/documents')) {
        return { code: 200, data: { documents: [] } };
      }
      if (path === '/index-jobs') {
        return { code: 200, data: { jobs: [] } };
      }
      return { code: 200, data: {} };
    });
  });

  afterEach(() => {
    cleanup();
  });

  it('renders knowledge page panels without audit list or create form', async () => {
    render(<KnowledgePage />);

    await waitFor(() => {
      expect(screen.getByTestId('knowledge-page')).toBeDefined();
    });

    expect(screen.getByText('知识空间')).toBeDefined();
    expect(screen.queryByText('检索审计')).toBeNull();
    expect(document.querySelector('form.space-form')).toBeNull();
    expect(screen.queryByPlaceholderText('space id')).toBeNull();
  });

  it('corrects stale unauthorized space and loads documents for an accessible space', async () => {
    localStorage.setItem('ragSelectedSpaceId', 'finance');
    const calls: string[] = [];
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      calls.push(path);
      if (path === '/knowledge-spaces') {
        return {
          code: 200,
          data: { spaces: [{ space_id: 'personal', name: '个人知识库' }] },
        };
      }
      if (path.includes('/documents')) {
        return { code: 200, data: { documents: [] } };
      }
      if (path === '/index-jobs') {
        return { code: 200, data: { jobs: [] } };
      }
      return { code: 200, data: {} };
    });

    render(<KnowledgePage />);

    await waitFor(() => {
      expect(calls.some((p) => p === '/knowledge-spaces/personal/documents')).toBe(true);
    });
    expect(calls.some((p) => p.includes('/knowledge-spaces/finance/'))).toBe(false);
    expect(localStorage.getItem('ragSelectedSpaceId')).toBe('personal');
    expect(screen.queryByText(/user is not allowed to access knowledge space/)).toBeNull();
  });

  it('shows safe empty document state when user has no accessible spaces', async () => {
    localStorage.setItem('ragSelectedSpaceId', 'finance');
    (apiJson as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === '/knowledge-spaces') {
        return { code: 200, data: { spaces: [] } };
      }
      if (path.includes('/documents')) {
        throw new Error('user is not allowed to access knowledge space: finance');
      }
      if (path === '/index-jobs') {
        return { code: 200, data: { jobs: [] } };
      }
      return { code: 200, data: {} };
    });

    render(<KnowledgePage />);

    await waitFor(() => {
      expect(screen.getAllByText('暂无可用知识空间').length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/user is not allowed to access knowledge space/)).toBeNull();
  });
});
