import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react';
import React from 'react';
import IndexJobList from './IndexJobList';

vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

import { apiJson } from '../../api/client';

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

describe('IndexJobList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApiJson.mockResolvedValue({
      code: 200,
      data: { jobs: [{ job_id: 'j1', status: 'running', document_id: 'doc1' }] },
    });
  });

  afterEach(() => cleanup());

  it('shows jobs after loading', async () => {
    render(<IndexJobList />);
    await waitFor(() => { expect(screen.getByText(/j1/)).toBeInTheDocument(); });
  });

  it('shows empty state when no jobs', async () => {
    mockApiJson.mockResolvedValue({ code: 200, data: { jobs: [] } });
    render(<IndexJobList />);
    await waitFor(() => { expect(screen.getByText('暂无索引任务')).toBeInTheDocument(); });
  });

  it('shows error state with retry on failure', async () => {
    mockApiJson.mockRejectedValue(new Error('服务不可用'));
    render(<IndexJobList />);
    await waitFor(() => { expect(screen.getByText(/加载失败/)).toBeInTheDocument(); });
    expect(screen.getByText('↻ 重试')).toBeInTheDocument();
  });

  it('retries a job via POST /index-jobs/:id/retry', async () => {
    render(<IndexJobList />);
    await waitFor(() => { expect(screen.getByText(/j1/)).toBeInTheDocument(); });

    fireEvent.click(screen.getByText('重试'));

    await waitFor(() => {
      expect(mockApiJson).toHaveBeenCalledWith('/index-jobs/j1/retry', { method: 'POST' });
    });
  });

  it('ignores a stale refresh response when a newer refresh resolves first', async () => {
    render(<IndexJobList />);
    await waitFor(() => { expect(screen.getByText(/j1/)).toBeInTheDocument(); });

    let resolveFirst: (value: unknown) => void;
    let resolveSecond: (value: unknown) => void;
    const firstPromise = new Promise((resolve) => { resolveFirst = resolve; });
    const secondPromise = new Promise((resolve) => { resolveSecond = resolve; });
    mockApiJson
      .mockImplementationOnce(() => firstPromise)
      .mockImplementationOnce(() => secondPromise);

    const refreshBtn = screen.getByTitle('刷新任务');
    fireEvent.click(refreshBtn); // stale, slower
    fireEvent.click(refreshBtn); // fresh, resolves first

    await act(async () => {
      resolveSecond!({ code: 200, data: { jobs: [{ job_id: 'j2', status: 'completed' }] } });
      await secondPromise;
    });
    await waitFor(() => { expect(screen.getByText(/j2/)).toBeInTheDocument(); });

    // The stale first request resolves later — it must not overwrite j2 with j1.
    await act(async () => {
      resolveFirst!({ code: 200, data: { jobs: [{ job_id: 'j1', status: 'running' }] } });
      await firstPromise;
    });
    expect(screen.getByText(/j2/)).toBeInTheDocument();
    expect(screen.queryByText(/j1/)).toBeNull();
  });
});
