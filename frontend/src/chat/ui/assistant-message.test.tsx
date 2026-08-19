import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { EscStackProvider } from '../../lib/esc-stack-provider';
import { copy } from '../../copy';
import { formatRelativeTime } from '../../notifications/relative-time';
import type { AssistantMessageView } from '../store';
import { AssistantMessage } from './assistant-message';

/*
 * assistant 回答渲染测试（共用基座 §3.4；spec §4–§6）：
 * 系统提示条已知/未知 notice 映射、深度研究步骤折叠、停止态文案、失败错误行 + 重试、
 * 常设反馈（已投固化 / A/B voted:false 隐藏）、A/B 对比视图与投票、hover 淡入相对时间。
 */

function makeMessage(overrides: Partial<AssistantMessageView> = {}): AssistantMessageView {
  return {
    id: 'm_1',
    role: 'assistant',
    content: 'Mock answer.',
    created_at: '2026-08-16T00:00:00Z',
    answer_mode: 'grounded',
    effort_level: 'quick',
    generation_id: 'g_1',
    root_generation_id: 'g_1',
    retry_of_generation_id: null,
    attempt_number: 1,
    status: 'completed',
    stop_reason: null,
    notices: [],
    citations: [],
    feedback: null,
    ab: null,
    generation: { phase: null, content: 'Mock answer.', abContents: null, complete: true, stage: null, steps: [], notices: [], requestError: null },
    ...overrides,
  };
}

function renderMessage(message: AssistantMessageView, overrides: Partial<Parameters<typeof AssistantMessage>[0]> = {}) {
  const props = {
    message,
    onRetry: vi.fn(),
    onFeedback: vi.fn(),
    onAbVote: vi.fn(),
    ...overrides,
  };
  render(
    <EscStackProvider>
      <AssistantMessage {...props} />
    </EscStackProvider>,
  );
  return props;
}

describe('AssistantMessage', () => {
  it('已知 notice 映射 + 未知 kind 通用提示（不展示原始 kind）', () => {
    renderMessage(
      makeMessage({
        generation: {
          phase: null,
          content: 'x',
          abContents: null,
          complete: true,
          stage: null,
          steps: [],
          notices: [
            { kind: 'effort_upgraded', detail: {} },
            { kind: 'retrieval_degraded', detail: {} },
            { kind: 'future_unknown_kind', detail: { x: 1 } },
          ],
          requestError: null,
        },
      }),
    );
    expect(screen.getByText(copy.chat.notice.effortUpgraded)).toBeInTheDocument();
    expect(screen.getByText(copy.chat.notice.retrievalDegraded)).toBeInTheDocument();
    expect(screen.getByText(copy.chat.notice.generic)).toBeInTheDocument();
    // 未知 kind 原始机读值不展示
    expect(screen.queryByText('future_unknown_kind')).not.toBeInTheDocument();
  });

  it('深度研究步骤：生成中列出、完成后折叠为「已完成 N 步」可展开', async () => {
    const user = userEvent.setup();
    const steps = [
      { index: 0, label: 'retrieve_round_1', state: 'done' as const },
      { index: 1, label: 'retrieve_round_2', state: 'done' as const },
    ];
    renderMessage(
      makeMessage({
        generation: {
          phase: 'completed',
          content: 'x',
          abContents: null,
          complete: true,
          stage: null,
          steps,
          notices: [],
          requestError: null,
        },
      }),
    );
    expect(screen.getByText(copy.chat.stepsDone(2))).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.chat.stepsExpandAria }));
    expect(await screen.findByText(copy.chat.stepLabel('retrieve_round_1'))).toBeInTheDocument();
    expect(screen.getByText(copy.chat.stepLabel('retrieve_round_2'))).toBeInTheDocument();
  });

  it('停止态：stop_reason 固定文案', () => {
    renderMessage(
      makeMessage({
        status: 'stopped',
        stop_reason: 'client_disconnected',
        generation: { phase: 'stopped', content: '已展示正文', abContents: null, complete: true, stage: null, steps: [], notices: [], requestError: null },
      }),
    );
    expect(screen.getByText(copy.chat.stopReason.clientDisconnected)).toBeInTheDocument();
  });

  it('失败态：危险红错误行 + 重试文字链（仅 failed）', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderMessage(
      makeMessage({
        status: 'failed',
        generation: { phase: 'failed', content: '', abContents: null, complete: true, stage: null, steps: [], notices: [], requestError: null },
      }),
      { onRetry },
    );
    const errorLine = screen.getByText(copy.chat.message.errorLine);
    expect(errorLine).toHaveClass('text-danger');
    await user.click(screen.getByRole('button', { name: copy.chat.message.retry }));
    expect(onRetry).toHaveBeenCalledWith('m_1');
  });

  it('常设反馈：👍 直接计票调用 onFeedback；已投后固化无改票入口', async () => {
    const user = userEvent.setup();
    const onFeedback = vi.fn();
    const first = renderMessage(makeMessage(), { onFeedback });
    await user.click(screen.getByRole('button', { name: copy.chat.feedbackUpAria }));
    expect(onFeedback).toHaveBeenCalledWith('m_1', { vote: 'up' });
    first; // 保持引用
    screen.getByRole('button', { name: copy.chat.feedbackUpAria });
  });

  it('已投反馈固化：无改票入口（按钮 disabled）', () => {
    renderMessage(
      makeMessage({ feedback: { vote: 'up' }, generation: { phase: null, content: 'x', abContents: null, complete: true, stage: null, steps: [], notices: [], requestError: null } }),
    );
    expect(screen.getByRole('button', { name: copy.chat.feedbackUpAria })).toBeDisabled();
    expect(screen.getByRole('button', { name: copy.chat.feedbackDownAria })).toBeDisabled();
  });

  it('A/B voted:false（open）隐藏常设反馈，展示对比视图与投票', async () => {
    const user = userEvent.setup();
    const onAbVote = vi.fn();
    renderMessage(
      makeMessage({
        content: '',
        ab: {
          pair_id: 'pair_1',
          status: 'open',
          voted: false,
          choice: null,
          candidates: [
            { candidate: 0, content: '候选 A', citations: [], answer_mode: 'grounded' },
            { candidate: 1, content: '候选 B', citations: [], answer_mode: 'grounded' },
          ],
        },
        generation: {
          phase: 'completed',
          content: '',
          abContents: [
            { candidate: 0, content: '候选 A', citations: [] },
            { candidate: 1, content: '候选 B', citations: [] },
          ],
          complete: true,
          stage: null,
          steps: [],
          notices: [],
          requestError: null,
        },
      }),
      { onAbVote },
    );
    // 隐藏常设 👍👎
    expect(screen.queryByRole('button', { name: copy.chat.feedbackUpAria })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copy.chat.feedbackDownAria })).not.toBeInTheDocument();
    // 对比区 + 两列「选这条」（aria 名 = 选择回答 N）
    const compare = screen.getByRole('region', { name: copy.chat.abCompareAria });
    expect(within(compare).getByText('候选 A')).toBeInTheDocument();
    expect(within(compare).getByText('候选 B')).toBeInTheDocument();
    await user.click(within(compare).getByRole('button', { name: copy.chat.abVoteOptionAria('0') }));
    expect(onAbVote).toHaveBeenCalledWith('m_1', '0');
    // 「两个都不选，继续」
    await user.click(within(compare).getByRole('button', { name: copy.chat.abChoiceNeither }));
    expect(onAbVote).toHaveBeenCalledWith('m_1', 'neither');
  });

  it('A/B 投票提交中 disabled：按钮锁定，不会发第二次不同 choice（m2）', async () => {
    const user = userEvent.setup();
    const onAbVote = vi.fn();
    renderMessage(
      makeMessage({
        content: '',
        ab: {
          pair_id: 'pair_1',
          status: 'open',
          voted: false,
          choice: null,
          candidates: [
            { candidate: 0, content: '候选 A', citations: [], answer_mode: 'grounded' },
            { candidate: 1, content: '候选 B', citations: [], answer_mode: 'grounded' },
          ],
        },
        generation: {
          phase: 'completed',
          content: '',
          abContents: [
            { candidate: 0, content: '候选 A', citations: [] },
            { candidate: 1, content: '候选 B', citations: [] },
          ],
          complete: true,
          stage: null,
          steps: [],
          notices: [],
          requestError: null,
        },
      }),
      { onAbVote, pendingSubmits: [{ kind: 'ab-vote', messageId: 'm_1' }] },
    );
    const compare = screen.getByRole('region', { name: copy.chat.abCompareAria });
    const pick0 = within(compare).getByRole('button', { name: copy.chat.abVoteOptionAria('0') });
    const pick1 = within(compare).getByRole('button', { name: copy.chat.abVoteOptionAria('1') });
    const neither = within(compare).getByRole('button', { name: copy.chat.abChoiceNeither });
    expect(pick0).toBeDisabled();
    expect(pick1).toBeDisabled();
    expect(neither).toBeDisabled();
    await user.click(pick0);
    await user.click(pick1);
    await user.click(neither);
    expect(onAbVote).not.toHaveBeenCalled();
  });

  it('pre-start requestError：消息区展示请求错误文案（M2）', () => {
    renderMessage(
      makeMessage({
        content: '',
        status: 'failed',
        generation: {
          phase: 'connecting',
          content: '',
          abContents: null,
          complete: true,
          stage: null,
          steps: [],
          notices: [],
          requestError: { code: 'idempotency_key_conflict', messageKey: 'chat.requestError' },
        },
      }),
    );
    expect(screen.getByText(copy.chat.requestError)).toBeInTheDocument();
  });

  it('A/B 投票 0/1 后：恢复常设反馈，正文为所选候选', () => {
    renderMessage(
      makeMessage({
        content: '候选 A',
        ab: { pair_id: 'pair_1', status: 'voted', voted: true, choice: '0', candidates: null },
        generation: { phase: null, content: '候选 A', abContents: null, complete: true, stage: null, steps: [], notices: [], requestError: null },
      }),
    );
    expect(screen.getByText('候选 A')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.chat.feedbackUpAria })).toBeInTheDocument();
  });

  it('A/B neither：不保留候选正文、不渲染反馈', () => {
    renderMessage(
      makeMessage({
        content: '',
        ab: { pair_id: 'pair_1', status: 'voted', voted: true, choice: 'neither', candidates: null },
        generation: { phase: null, content: '', abContents: null, complete: true, stage: null, steps: [], notices: [], requestError: null },
      }),
    );
    expect(screen.queryByRole('button', { name: copy.chat.feedbackUpAria })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copy.chat.feedbackDownAria })).not.toBeInTheDocument();
  });

  it('引用角标：悬停卡「引自《文档名》」+ 定位行（M9：document_name 优先）', async () => {
    const user = userEvent.setup();
    renderMessage(
      makeMessage({
        citations: [
          {
            document_id: 'doc_1',
            document_version_id: 'v_1',
            document_name: '员工手册.pdf',
            locator: { page: 12, span: { start: 345, end: 412 } },
          },
        ],
      }),
    );
    const badge = screen.getByRole('button', { name: copy.chat.message.citeOpenAria });
    expect(badge.textContent).toBe('[1]');
    await user.hover(badge);
    expect(await screen.findByText(copy.chat.message.citeFrom('员工手册.pdf'))).toBeInTheDocument();
    expect(
      await screen.findByText(copy.chat.message.citePage(12)),
    ).toBeInTheDocument();
  });

  it('引用角标：document_name 缺失时回退通用「引自文档」（M9，不显示不透明 ID）', async () => {
    const user = userEvent.setup();
    renderMessage(
      makeMessage({
        citations: [{ document_id: 'doc_9', document_version_id: 'v_9', locator: { page: 1 } }],
      }),
    );
    const badge = screen.getByRole('button', { name: copy.chat.message.citeOpenAria });
    await user.hover(badge);
    expect(await screen.findByText(copy.chat.message.citeFromFallback)).toBeInTheDocument();
    // 不展示不透明 document_id
    expect(screen.queryByText(/doc_9/)).not.toBeInTheDocument();
  });

  // R9/A2：hover AI 回答时其下方淡入相对时间（与用户气泡同一规格）。
  // jsdom 不计算 CSS，断言淡入机制：时间行存在、默认隐藏、由 group-hover 驱动、规格类名与用户气泡一致。
  it('hover 相对时间：回答下方渲染相对时间行，默认 opacity-0、group-hover 淡入（同用户气泡规格）', () => {
    const message = makeMessage();
    renderMessage(message);
    const time = screen.getByText(formatRelativeTime(message.created_at));
    expect(time).toHaveClass(
      'mt-1',
      'text-[15px]',
      'text-slate-gray',
      'opacity-0',
      'transition-opacity',
      'duration-[var(--duration-fast)]',
      'group-hover:opacity-100',
    );
    // 位于整条回答末尾（反馈行之后），hover 区域为整条消息（group 容器）
    const root = time.parentElement;
    expect(root).not.toBeNull();
    expect(root).toHaveClass('chat-message-enter', 'group');
    expect(root?.lastElementChild).toBe(time);
  });
});
