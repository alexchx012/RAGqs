import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { KnowledgeProvider } from './KnowledgeContext';
import DocumentList from './DocumentList';

const mockFetch = vi.fn();
global.fetch = mockFetch;

function renderList() {
  return render(<KnowledgeProvider><DocumentList /></KnowledgeProvider>);
}

describe('DocumentList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({
        code: 200,
        data: { documents: [{ document_id: 'doc1', file_name: 'test.txt', status: 'completed', indexed_chunks: 10, total_chunks: 10 }] },
      }),
    });
  });

  it('shows loading state initially', () => {
    renderList();
    expect(screen.getByText('加载中...')).toBeInTheDocument();
  });

  it('shows document after loading', async () => {
    renderList();
    await waitFor(() => { expect(screen.getByText(/test\.txt/)).toBeInTheDocument(); });
  });

  it('shows empty state when no documents', async () => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ code: 200, data: { documents: [] } }),
    });
    renderList();
    await waitFor(() => { expect(screen.getByText('暂无文档')).toBeInTheDocument(); });
  });

  it('shows error state with retry button on failure', async () => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ code: 500, message: '服务器错误' }),
    });
    renderList();
    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument();
      expect(screen.getByText('↻ 重试')).toBeInTheDocument();
    });
  });

  it('has refresh button', () => {
    renderList();
    expect(screen.getByTitle('刷新文档')).toBeInTheDocument();
  });
});
