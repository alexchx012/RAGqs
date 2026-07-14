import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { KnowledgeProvider } from './KnowledgeContext';
import AuditList from './AuditList';

const mockFetch = vi.fn();
global.fetch = mockFetch;

function renderList() {
  return render(<KnowledgeProvider><AuditList /></KnowledgeProvider>);
}

describe('AuditList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({
        code: 200,
        data: { audits: [{ id: 'a1', question: '测试问题', sources: [{ content: 's', score: 0.9 }], createdAt: '2026-07-14' }] },
      }),
    });
  });

  it('shows audit items after loading', async () => {
    renderList();
    await waitFor(() => { expect(screen.getByText('测试问题')).toBeInTheDocument(); });
  });

  it('shows empty state when no audits', async () => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ code: 200, data: { audits: [] } }),
    });
    renderList();
    await waitFor(() => { expect(screen.getByText('暂无审计记录')).toBeInTheDocument(); });
  });

  it('shows error state with retry on failure', async () => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ code: 500, message: '服务不可用' }),
    });
    renderList();
    await waitFor(() => { expect(screen.getByText(/加载失败/)).toBeInTheDocument(); });
  });
});
