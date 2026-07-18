import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import React from 'react';
import DocumentList from './DocumentList';

vi.mock('./KnowledgeContext', () => ({
  useKnowledge: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

import { useKnowledge } from './KnowledgeContext';
import { apiJson } from '../../api/client';

const mockUseKnowledge = useKnowledge as unknown as ReturnType<typeof vi.fn>;
const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

function setupKnowledge(overrides: Record<string, unknown> = {}) {
  mockUseKnowledge.mockReturnValue({
    selectedSpaceId: 'default',
    spacesReady: true,
    setSelectedSpaceId: vi.fn(),
    knowledgeSpaces: [{ space_id: 'default', name: 'Default' }],
    refreshSpaces: vi.fn(),
    spaceIdOf: (s: { space_id?: string }) => s.space_id || 'default',
    ...overrides,
  });
}

describe('DocumentList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupKnowledge();
    mockApiJson.mockResolvedValue({
      code: 200,
      data: {
        documents: [
          {
            document_id: 'doc1',
            file_name: 'test.txt',
            status: 'completed',
            indexed_chunks: 10,
            total_chunks: 10,
          },
        ],
      },
    });
  });

  it('shows loading state initially when spaces are not ready', () => {
    setupKnowledge({ spacesReady: false });
    render(<DocumentList />);
    expect(screen.getByText('加载中...')).toBeInTheDocument();
    expect(mockApiJson).not.toHaveBeenCalled();
  });

  it('shows document after loading', async () => {
    render(<DocumentList />);
    await waitFor(() => {
      expect(screen.getByText(/test\.txt/)).toBeInTheDocument();
    });
    expect(mockApiJson).toHaveBeenCalledWith('/knowledge-spaces/default/documents');
  });

  it('shows empty state when no documents', async () => {
    mockApiJson.mockResolvedValue({ code: 200, data: { documents: [] } });
    render(<DocumentList />);
    await waitFor(() => {
      expect(screen.getByText('暂无文档')).toBeInTheDocument();
    });
  });

  it('shows safe empty state and skips API when selected space is empty', async () => {
    setupKnowledge({ selectedSpaceId: '', spacesReady: true });
    render(<DocumentList />);
    await waitFor(() => {
      expect(screen.getByText('暂无可用知识空间')).toBeInTheDocument();
    });
    expect(mockApiJson).not.toHaveBeenCalled();
    expect(screen.queryByText(/user is not allowed to access knowledge space/)).toBeNull();
  });

  it('shows error state with retry button on failure', async () => {
    mockApiJson.mockRejectedValue(new Error('服务器错误'));
    render(<DocumentList />);
    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument();
      expect(screen.getByText('↻ 重试')).toBeInTheDocument();
    });
  });

  it('has refresh button', () => {
    render(<DocumentList />);
    expect(screen.getByTitle('刷新文档')).toBeInTheDocument();
  });

  it('does not let a stale in-flight request overwrite the empty state after the space is cleared', async () => {
    let resolveFetch: (value: unknown) => void;
    mockApiJson.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));

    const { rerender } = render(<DocumentList />);
    await waitFor(() => { expect(mockApiJson).toHaveBeenCalledTimes(1); });

    // Space becomes unavailable (e.g. refreshSpaces corrected selection) while the
    // first request for the old space is still in flight.
    setupKnowledge({ selectedSpaceId: '', spacesReady: true });
    rerender(<DocumentList />);
    await waitFor(() => { expect(screen.getByText('暂无可用知识空间')).toBeInTheDocument(); });

    // The stale request for the old space finally resolves — it must not clobber
    // the safe empty state that's already showing.
    await act(async () => {
      resolveFetch!({
        code: 200,
        data: { documents: [{ document_id: 'doc1', file_name: 'stale.txt', status: 'completed' }] },
      });
    });

    expect(screen.getByText('暂无可用知识空间')).toBeInTheDocument();
    expect(screen.queryByText(/stale\.txt/)).toBeNull();
  });
});
