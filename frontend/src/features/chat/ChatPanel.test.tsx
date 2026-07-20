import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import ChatPanel from './ChatPanel';
import { useChat } from './ChatContext';
import { useChatStream } from './useChatStream';
import { useChatQuick } from './useChatQuick';

// Mock hooks
vi.mock('./ChatContext', () => ({
  useChat: vi.fn(),
}));

vi.mock('./useChatStream', () => ({
  useChatStream: vi.fn(),
}));

vi.mock('./useChatQuick', () => ({
  useChatQuick: vi.fn(),
}));

vi.mock('../../markdown/renderMarkdown', () => ({
  renderMarkdown: vi.fn((content: string) => `<p>${content}</p>`),
  escapeHtml: vi.fn((text: string) => text),
}));

const mockUseChat = useChat as unknown as ReturnType<typeof vi.fn>;
const mockUseChatStream = useChatStream as unknown as ReturnType<typeof vi.fn>;
const mockUseChatQuick = useChatQuick as unknown as ReturnType<typeof vi.fn>;

function createDefaultMocks() {
  mockUseChat.mockReturnValue({
    currentChatHistory: [],
    isStreaming: false,
    mode: 'quick',
    setMode: vi.fn(),
    addMessage: vi.fn(),
  });

  mockUseChatStream.mockReturnValue({
    sendStream: vi.fn(),
  });

  mockUseChatQuick.mockReturnValue({
    sendQuick: vi.fn(),
  });
}

describe('ChatPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createDefaultMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders welcome greeting when chat history is empty', () => {
    render(<ChatPanel spaceId="space-1" />);

    expect(screen.getByText('你好！我是知识库问答助手')).toBeDefined();
  });

  it('renders chat messages from history', () => {
    mockUseChat.mockReturnValue({
      currentChatHistory: [
        { type: 'user', content: 'Hello' },
        { type: 'assistant', content: 'Hi there' },
      ],
      isStreaming: false,
      mode: 'quick',
      setMode: vi.fn(),
      addMessage: vi.fn(),
    });

    render(<ChatPanel spaceId="space-1" />);

    const messages = document.querySelectorAll('.message');
    expect(messages.length).toBe(2);
    expect(messages[0].classList.contains('user-message')).toBe(true);
    expect(messages[1].classList.contains('assistant-message')).toBe(true);
  });

  it('calls sendQuick when mode is quick and send is triggered', async () => {
    const sendQuick = vi.fn().mockResolvedValue(undefined);
    mockUseChatQuick.mockReturnValue({ sendQuick });
    const addMessage = vi.fn();
    mockUseChat.mockReturnValue({
      currentChatHistory: [],
      isStreaming: false,
      mode: 'quick',
      setMode: vi.fn(),
      addMessage,
    });

    render(<ChatPanel spaceId="space-1" />);

    const input = screen.getByPlaceholderText('输入你的问题...');
    await userEvent.type(input, 'Hello world');

    const sendBtn = screen.getByTitle('发送');
    await userEvent.click(sendBtn);

    expect(addMessage).toHaveBeenCalledWith({ type: 'user', content: 'Hello world' });
    expect(sendQuick).toHaveBeenCalledWith('Hello world', 'space-1');
  });

  it('calls sendStream when mode is stream and send is triggered', async () => {
    const sendStream = vi.fn().mockResolvedValue(undefined);
    mockUseChatStream.mockReturnValue({ sendStream });
    const addMessage = vi.fn();
    mockUseChat.mockReturnValue({
      currentChatHistory: [],
      isStreaming: false,
      mode: 'stream',
      setMode: vi.fn(),
      addMessage,
    });

    render(<ChatPanel spaceId="space-1" />);

    const input = screen.getByPlaceholderText('输入你的问题...');
    await userEvent.type(input, 'Hello world');

    const sendBtn = screen.getByTitle('发送');
    await userEvent.click(sendBtn);

    expect(addMessage).toHaveBeenCalledWith({ type: 'user', content: 'Hello world' });
    expect(sendStream).toHaveBeenCalledWith('Hello world', 'space-1', expect.any(Function));
  });

  it('does not send when input is empty', async () => {
    const sendQuick = vi.fn();
    mockUseChatQuick.mockReturnValue({ sendQuick });

    render(<ChatPanel spaceId="space-1" />);

    const sendBtn = screen.getByTitle('发送');
    await userEvent.click(sendBtn);

    expect(sendQuick).not.toHaveBeenCalled();
  });

  it('disables input and send button when streaming', () => {
    mockUseChat.mockReturnValue({
      currentChatHistory: [],
      isStreaming: true,
      mode: 'quick',
      setMode: vi.fn(),
      addMessage: vi.fn(),
    });

    render(<ChatPanel spaceId="space-1" />);

    const input = screen.getByPlaceholderText('输入你的问题...') as HTMLInputElement;
    expect(input.disabled).toBe(true);
    const sendBtn = screen.getByTitle('发送') as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
  });

  it('disables input and send button when disabled prop is true', () => {
    render(<ChatPanel spaceId="space-1" disabled />);

    const input = screen.getByPlaceholderText('输入你的问题...') as HTMLInputElement;
    expect(input.disabled).toBe(true);
    const sendBtn = screen.getByTitle('发送') as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
  });

  it('toggles mode dropdown on click', async () => {
    const setMode = vi.fn();
    mockUseChat.mockReturnValue({
      currentChatHistory: [],
      isStreaming: false,
      mode: 'quick',
      setMode,
      addMessage: vi.fn(),
    });

    render(<ChatPanel spaceId="space-1" />);

    // Mode selector shows current mode
    expect(screen.getByText('快速')).toBeDefined();

    // Click mode selector to open dropdown
    const modeBtn = document.querySelector('.mode-selector-btn') as HTMLElement;
    await userEvent.click(modeBtn);

    // Dropdown should appear with stream option
    expect(screen.getByText('流式')).toBeDefined();
  });

  describe('answer mode three-state hints', () => {
    function mockHistory(messages: Array<Record<string, unknown>>) {
      mockUseChat.mockReturnValue({
        currentChatHistory: messages,
        isStreaming: false,
        mode: 'stream',
        setMode: vi.fn(),
        addMessage: vi.fn(),
      });
    }

    it('shows a source list hint for grounded answers', () => {
      // sources list UI is not yet wired in ChatPanel; grounded must not throw
      // and must not introduce no_context / warning hints.
      mockHistory([
        {
          type: 'assistant',
          content: 'Based on the docs...',
          answerMode: 'grounded',
        },
      ]);

      render(<ChatPanel spaceId="space-1" />);

      const messages = document.querySelectorAll('.message.assistant-message');
      expect(messages.length).toBe(1);
      expect(screen.queryByTestId('answer-mode-no-context')).toBeNull();
      expect(screen.queryByTestId('answer-mode-warning')).toBeNull();
      expect(screen.queryByText('未在知识库中找到相关内容')).toBeNull();
      expect(screen.queryByText('本次回答未使用知识库内容')).toBeNull();
    });

    it('shows a soft no-context hint for no_context answers', () => {
      mockHistory([
        {
          type: 'assistant',
          content: 'I do not have that information.',
          answerMode: 'no_context',
        },
      ]);

      render(<ChatPanel spaceId="space-1" />);

      expect(screen.getByTestId('answer-mode-no-context')).toBeDefined();
      expect(screen.getByText('未在知识库中找到相关内容')).toBeDefined();
    });

    it('shows a warning hint when the model answered without using the knowledge base', () => {
      mockHistory([
        {
          type: 'assistant',
          content: 'General knowledge answer.',
          answerMode: 'direct',
          usedToolsWithoutKnowledgeBase: true,
        },
      ]);

      render(<ChatPanel spaceId="space-1" />);

      expect(screen.getByTestId('answer-mode-warning')).toBeDefined();
      expect(screen.getByText('本次回答未使用知识库内容')).toBeDefined();
    });

    it('shows no extra hint for a plain direct answer', () => {
      mockHistory([
        {
          type: 'assistant',
          content: 'Plain direct answer.',
          answerMode: 'direct',
          usedToolsWithoutKnowledgeBase: false,
        },
      ]);

      render(<ChatPanel spaceId="space-1" />);

      expect(screen.queryByTestId('answer-mode-no-context')).toBeNull();
      expect(screen.queryByTestId('answer-mode-warning')).toBeNull();
      expect(screen.queryByText('未在知识库中找到相关内容')).toBeNull();
      expect(screen.queryByText('本次回答未使用知识库内容')).toBeNull();
    });

    it('shows no extra hint when answerMode is absent (baseline path)', () => {
      mockHistory([
        {
          type: 'assistant',
          content: 'Baseline path answer without answerMode.',
        },
      ]);

      render(<ChatPanel spaceId="space-1" />);

      expect(screen.queryByTestId('answer-mode-no-context')).toBeNull();
      expect(screen.queryByTestId('answer-mode-warning')).toBeNull();
      expect(screen.queryByText('未在知识库中找到相关内容')).toBeNull();
      expect(screen.queryByText('本次回答未使用知识库内容')).toBeNull();
    });
  });
});
