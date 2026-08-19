import { render, waitFor } from '@testing-library/react';
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
    onRetry: vi.fn(),
    onFeedback: vi.fn(),
    onAbVote: vi.fn(),
  };
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
});
