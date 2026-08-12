/*
 * 抽屉左栏项右侧摘要测试（spec §1；验收 A10/A20/A23/A40 部分）。
 * 各摘要组件挂载自加载 + AdminProvider.summariesVersion 变化重取（invalidateSummaries）：
 * >0 渲染徽标/状态点、=0 不渲染、加载失败静默（渲染 null）。
 * 角色差异：ops 审批中心徽标 = quota_pending + submission_pending 合计；
 * admin 侧仅挂载投稿摘要（submission_pending），数字不同来自同一 summary 数据源。
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fakeAdminApi } from '../test/auth-fixtures';
import { AdminProvider, useAdmin } from './AdminProvider';
import type { AdminApi } from './api';
import {
  ApprovalsSummaryBadge,
  EvaluationWindowDot,
  OperationsStaleBadge,
  QuotaRequestsSummaryBadge,
  SubmissionsSummaryBadge,
} from './summaries';

function renderSummary(node: ReactElement, api: AdminApi) {
  return render(<AdminProvider api={api}>{node}</AdminProvider>);
}

/** 让挂载自加载的请求落定（微任务冲刷），再断言静默（渲染 null）。 */
async function flushReads(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

function apiWithSummary(quotaPending: number, submissionPending: number): AdminApi {
  return fakeAdminApi({
    getApprovalSummary: vi.fn(async () => ({
      quota_pending: quotaPending,
      submission_pending: submissionPending,
    })),
  });
}

function openCalibrationApi(): AdminApi {
  return fakeAdminApi({
    getCalibrationWindow: vi.fn(async () => ({
      window_id: 'cw_1',
      status: 'open' as const,
      opened_at: '2026-08-03T02:00:00Z',
      closed_at: null,
      pairs_collected: 12,
      close_deadline_at: null,
      window_kind: 'manual' as const,
      policy_version: 'eval_2026_v1',
      sample_rate: 0.1,
      opened_by: 'u_ops',
      closed_by: null,
    })),
  });
}

describe('审批摘要徽标（GET /approvals/summary）', () => {
  it('ops 审批中心徽标 = 配额 + 投稿合计；>0 显示 CountBadge', async () => {
    const { container } = renderSummary(<ApprovalsSummaryBadge />, apiWithSummary(2, 3));
    expect(await screen.findByText('5')).toBeInTheDocument();
    expect(container.querySelector('.bg-mist-gray')).not.toBeNull();
  });

  it('配额 / 投稿下钻徽标分别取 quota_pending / submission_pending（admin 侧仅投稿摘要数字）', async () => {
    renderSummary(
      <>
        <QuotaRequestsSummaryBadge />
        <SubmissionsSummaryBadge />
      </>,
      apiWithSummary(2, 3),
    );
    expect(await screen.findByText('2')).toBeInTheDocument();
    expect(await screen.findByText('3')).toBeInTheDocument();
    expect(screen.queryByText('5')).not.toBeInTheDocument();
  });

  it('合计为 0 时不渲染任何内容', async () => {
    const { container } = renderSummary(<ApprovalsSummaryBadge />, apiWithSummary(0, 0));
    await flushReads();
    expect(container.childElementCount).toBe(0);
  });

  it('加载失败静默：渲染 null', async () => {
    const { container } = renderSummary(
      <ApprovalsSummaryBadge />,
      fakeAdminApi({ getApprovalSummary: vi.fn(() => Promise.reject(new Error('boom'))) }),
    );
    await flushReads();
    expect(container.childElementCount).toBe(0);
  });

  it('invalidateSummaries 触发重取并刷新计数', async () => {
    let summary = { quota_pending: 1, submission_pending: 0 };
    const getApprovalSummary = vi.fn(async () => summary);
    function Harness() {
      const { invalidateSummaries } = useAdmin();
      return (
        <>
          <button type="button" onClick={invalidateSummaries}>
            invalidate
          </button>
          <ApprovalsSummaryBadge />
        </>
      );
    }
    render(
      <AdminProvider api={fakeAdminApi({ getApprovalSummary })}>
        <Harness />
      </AdminProvider>,
    );
    expect(await screen.findByText('1')).toBeInTheDocument();

    summary = { quota_pending: 4, submission_pending: 0 };
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'invalidate' }));
    expect(await screen.findByText('4')).toBeInTheDocument();
    expect(getApprovalSummary).toHaveBeenCalledTimes(2);
  });
});

describe('评测窗口状态点（GET /calibration/window）', () => {
  it('open → 成功绿脉冲 StatusDot', async () => {
    const { container } = renderSummary(<EvaluationWindowDot />, openCalibrationApi());
    await waitFor(() => expect(container.querySelector('.bg-success')).not.toBeNull());
    expect(container.querySelector('.ui-status-pulse')).not.toBeNull();
  });

  it('closed → 不渲染；加载失败静默', async () => {
    const closed = renderSummary(<EvaluationWindowDot />, fakeAdminApi());
    await flushReads();
    expect(closed.container.childElementCount).toBe(0);
    const failed = renderSummary(
      <EvaluationWindowDot />,
      fakeAdminApi({ getCalibrationWindow: vi.fn(() => Promise.reject(new Error('boom'))) }),
    );
    await flushReads();
    expect(failed.container.childElementCount).toBe(0);
  });
});

describe('系统运维超时徽标（GET /ops/jobs?view=stale 的 stale_count）', () => {
  it('stale_count >0 → 警告琥珀 pill 计数', async () => {
    const { container } = renderSummary(
      <OperationsStaleBadge />,
      fakeAdminApi({ listOpsJobs: vi.fn(async () => ({ items: [], stale_count: 4 })) }),
    );
    expect(await screen.findByText('4')).toBeInTheDocument();
    const pill = container.querySelector('.bg-warning\\/15');
    expect(pill).not.toBeNull();
    expect((pill as HTMLElement).className).toContain('text-warning');
  });

  it('stale_count =0 不渲染；加载失败静默', async () => {
    const zero = renderSummary(<OperationsStaleBadge />, fakeAdminApi());
    await flushReads();
    expect(zero.container.childElementCount).toBe(0);
    const failed = renderSummary(
      <OperationsStaleBadge />,
      fakeAdminApi({ listOpsJobs: vi.fn(() => Promise.reject(new Error('boom'))) }),
    );
    await flushReads();
    expect(failed.container.childElementCount).toBe(0);
  });
});
