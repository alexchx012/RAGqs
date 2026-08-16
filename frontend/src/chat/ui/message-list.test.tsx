import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessageView } from '../store';
import { MessageList } from './message-list';

function userMessage(id: string, content: string): ChatMessageView {
  return { id, role: 'user', content, created_at: '2026-08-16T00:00:00Z' };
}

function props(conversationId: string, messages: readonly ChatMessageView[]) {
  return {
    conversationId,
    messages,
    onRetry: vi.fn(),
    onFeedback: vi.fn(),
    onAbVote: vi.fn(),
  };
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
});
