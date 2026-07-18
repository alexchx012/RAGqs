import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import AuditList from './AuditList';

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

describe('AuditList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupKnowledge();
    mockApiJson.mockResolvedValue({
      code: 200,
      data: { audits: [{ id: 'a1', question: '测试问题', sources: [{ content: 's', score: 0.9 }], createdAt: '2026-07-14' }] },
    });
  });

  it('shows loading state initially when spaces are not ready', () => {
    setupKnowledge({ spacesReady: false });
    render(<AuditList />);
    expect(screen.getByText('加载中...')).toBeInTheDocument();
    expect(mockApiJson).not.toHaveBeenCalled();
  });

  it('shows audit items after loading', async () => {
    render(<AuditList />);
    await waitFor(() => { expect(screen.getByText('测试问题')).toBeInTheDocument(); });
    expect(mockApiJson).toHaveBeenCalledWith('/chat/audits?space_id=default');
  });

  it('shows empty state when no audits', async () => {
    mockApiJson.mockResolvedValue({ code: 200, data: { audits: [] } });
    render(<AuditList />);
    await waitFor(() => { expect(screen.getByText('暂无审计记录')).toBeInTheDocument(); });
  });

  it('shows safe empty state and skips API when selected space is empty', async () => {
    setupKnowledge({ selectedSpaceId: '', spacesReady: true });
    render(<AuditList />);
    await waitFor(() => {
      expect(screen.getByText('暂无可用知识空间')).toBeInTheDocument();
    });
    expect(mockApiJson).not.toHaveBeenCalled();
  });

  it('shows error state with retry on failure', async () => {
    mockApiJson.mockRejectedValue(new Error('服务不可用'));
    render(<AuditList />);
    await waitFor(() => { expect(screen.getByText(/加载失败/)).toBeInTheDocument(); });
    expect(screen.getByText('↻ 重试')).toBeInTheDocument();
  });
});
