import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import React from 'react';
import App from './App';

// Mock the API client to prevent actual fetch calls
vi.mock('./api/client', () => ({
  apiJson: vi.fn().mockResolvedValue({ code: 200, data: { spaces: [], sessions: [], documents: [], jobs: [], audits: [] } }),
  ApiError: class extends Error { constructor(msg: string) { super(msg); } },
}));

describe('App', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders the app layout with sidebar, main content, and management panel', () => {
    render(<App />);

    // Sidebar: ChatHistorySidebar renders "RAG 知识库问答" title
    expect(screen.getByText('RAG 知识库问答')).toBeDefined();

    // Sidebar: "新建对话" button
    expect(screen.getByText('新建对话')).toBeDefined();

    // Main content: ChatPanel renders welcome greeting
    expect(screen.getByText('你好！我是知识库问答助手')).toBeDefined();

    // Management panel: KnowledgeSpaceSelector section header
    expect(screen.getByText('知识空间')).toBeDefined();

    // Management panel: DocumentList section header
    expect(screen.getByText('文档')).toBeDefined();

    // Management panel: IndexJobList section header
    expect(screen.getByText('索引任务')).toBeDefined();

    // Management panel: AuditList section header
    expect(screen.getByText('检索审计')).toBeDefined();
  });

  it('renders the app-layout root div', () => {
    render(<App />);
    const layout = document.querySelector('.app-layout');
    expect(layout).not.toBeNull();
  });

  it('renders the chat input field', () => {
    render(<App />);
    const input = screen.getByPlaceholderText('输入你的问题...');
    expect(input).toBeDefined();
  });
});
