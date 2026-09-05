import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../../copy';
import type { ChatConversationStatus, ChatMessageView } from '../store';
import { MessageList } from './message-list';

function userMessage(id: string, content: string): ChatMessageView {
  return { id, role: 'user', content, created_at: '2026-08-16T00:00:00Z' };
}

function props(
  conversationId: string | null,
  messages: readonly ChatMessageView[],
  conversationStatus: ChatConversationStatus = 'ready',
) {
  return {
    conversationId,
    conversationStatus,
    messages,
    onOpenRetry: vi.fn(),
    onRetry: vi.fn(),
    onFeedback: vi.fn(),
    onAbVote: vi.fn(),
  };
}

/** jsdom 无布局：手工固定滚动几何（scrollHeight 800 / clientHeight 200）。 */
function setupScroller(scroller: HTMLElement): void {
  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 800 });
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 200 });
}

function scroller(container: HTMLElement): HTMLElement {
  const node = container.querySelector<HTMLElement>('.overflow-y-auto');
  if (node === null) throw new Error('message scroller is missing');
  return node;
}

function greeting(container: HTMLElement): HTMLElement {
  const node = container.querySelector<HTMLElement>('.chat-empty-greeting');
  if (node === null) throw new Error('greeting is missing');
  return node;
}

describe('MessageList', () => {
  it('切换会话时定位到新会话的最新消息', () => {
    const first = props('conversation-a', [userMessage('a-1', '旧会话')]);
    const { container, rerender } = render(<MessageList {...first} />);
    const scroller = container.querySelector<HTMLDivElement>('.overflow-y-auto');

    if (scroller === null) throw new Error('message scroller is missing');
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 800 });
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 200 });
    scroller.scrollTop = 40;

    rerender(<MessageList {...props('conversation-b', [userMessage('b-1', '新会话')])} />);

    expect(scroller.scrollTop).toBe(800);
  });

  // R10/A3：首次挂载到非空会话——问候语第一帧即隐藏且直出（无收拢动画）
  it('首次挂载非空会话：问候语初始即隐藏（data-instant 直出，不播收拢动画）', () => {
    const { container } = render(<MessageList {...props('conversation-a', [userMessage('a-1', '历史消息')])} />);
    const node = greeting(container);
    expect(node).toHaveAttribute('data-hidden', 'true');
    expect(node).toHaveAttribute('data-instant', 'true');
    expect(node).toHaveAttribute('aria-hidden', 'true');
  });

  // R10/A3：空会话切换到非空会话——加载期间问候语立即直出隐藏，消息到达后仍无收拢动画
  it('切换到非空会话（经加载）：问候语立即隐藏且无动画，消息到达后保持直出', () => {
    const { container, rerender } = render(<MessageList {...props(null, [], 'idle')} />);
    expect(greeting(container)).toHaveAttribute('data-hidden', 'false');

    // 打开既有会话：加载期间不再视为空态，问候语直出隐藏（不等消息、不播动画）
    rerender(<MessageList {...props('conversation-b', [], 'loading')} />);
    expect(greeting(container)).toHaveAttribute('data-hidden', 'true');
    expect(greeting(container)).toHaveAttribute('data-instant', 'true');

    rerender(<MessageList {...props('conversation-b', [userMessage('b-1', '历史消息')], 'ready')} />);
    expect(greeting(container)).toHaveAttribute('data-hidden', 'true');
    expect(greeting(container)).toHaveAttribute('data-instant', 'true');
  });

  // R10/A3：空会话发出首条消息——保留既有淡出收拢动画（非 instant）
  it('空会话发出首条消息：问候语经 rAF 淡出收拢（保留既有动画路径）', async () => {
    const { container, rerender } = render(<MessageList {...props('conversation-c', [], 'ready')} />);
    expect(greeting(container)).toHaveAttribute('data-hidden', 'false');

    rerender(<MessageList {...props('conversation-c', [userMessage('c-1', '首条提问')], 'ready')} />);

    await waitFor(() => expect(greeting(container)).toHaveAttribute('data-hidden', 'true'));
    expect(greeting(container)).toHaveAttribute('data-instant', 'false');
  });

  it('空会话空态展示问候语文案', () => {
    const { container } = render(<MessageList {...props(null, [], 'idle')} />);
    const node = greeting(container);
    expect(node).toHaveAttribute('data-hidden', 'false');
    expect(node.textContent).toContain(copy.chat.sidebar.emptyGreeting);
  });

  // A20：消息流滚动容器为 log 区域 + polite live region（流式正文读屏可达）
  it('A20：滚动容器 role="log" aria-live="polite"', () => {
    const { container } = render(<MessageList {...props('conversation-a', [userMessage('a-1', '历史消息')])} />);
    const log = container.querySelector('[role="log"]');
    expect(log).not.toBeNull();
    expect(log).toHaveAttribute('aria-live', 'polite');
  });

  // A15：吸底跟随——距底 <120px 时流式 tick/追加自动滚底。
  // jsdom 无布局：先手工固定滚动几何，再以一次 rerender（新 messages 引用）驱动跟随 effect。
  it('A15：吸附底部时消息追加自动跟底', () => {
    const { container, rerender } = render(
      <MessageList {...props('conversation-a', [userMessage('a-1', '第一条')])} />,
    );
    const node = scroller(container);
    setupScroller(node);

    // 消息追加（流式 tick 同路径）：仍吸附 → 视口拖到底部
    rerender(
      <MessageList
        {...props('conversation-a', [userMessage('a-1', '第一条'), userMessage('a-2', '第二条')])}
      />,
    );
    expect(node.scrollTop).toBe(800);
    expect(screen.queryByRole('button', { name: copy.chat.message.scrollToBottom })).not.toBeInTheDocument();
  });

  // A15：上翻释放——视口距底 >120px 后 tick 不再拖拽视口，仅浮出「回到底部」
  it('A15：上翻释放吸底，tick 不拖拽视口并浮出回底钮', () => {
    const { container, rerender } = render(
      <MessageList {...props('conversation-a', [userMessage('a-1', '第一条')])} />,
    );
    const node = scroller(container);
    setupScroller(node);
    // 先驱动一次跟随（几何就位后 rerender），确认初始吸底
    rerender(<MessageList {...props('conversation-a', [userMessage('a-1', '第一条·tick')])} />);
    expect(node.scrollTop).toBe(800);

    // 用户上翻（距底 500px > 120px）：释放吸底
    node.scrollTop = 100;
    fireEvent.scroll(node);
    expect(screen.getByRole('button', { name: copy.chat.message.scrollToBottom })).toBeInTheDocument();

    // 消息追加：不再自动跟底
    rerender(
      <MessageList
        {...props('conversation-a', [userMessage('a-1', '第一条·tick'), userMessage('a-2', '第二条')])}
      />,
    );
    expect(node.scrollTop).toBe(100);
  });

  // A22(b)：会话打开失败——错误态 + 重试，旧消息不残留
  it('A22：会话打开失败渲染错误态与重试，重试触发 onOpenRetry', async () => {
    const onOpenRetry = vi.fn();
    const { container } = render(
      <MessageList {...props('conversation-a', [], 'error')} onOpenRetry={onOpenRetry} />,
    );
    expect(screen.getByText(copy.chat.conversationLoadFailed)).toBeInTheDocument();
    expect(container.querySelector('[role="log"]')).toBeNull();
    fireEvent.click(screen.getByText(copy.states.retry));
    expect(onOpenRetry).toHaveBeenCalledTimes(1);
  });
});
