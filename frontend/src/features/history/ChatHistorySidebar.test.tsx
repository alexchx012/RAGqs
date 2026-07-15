import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import ChatHistorySidebar from './ChatHistorySidebar';
import { useChat } from '../chat/ChatContext';
import { useChatHistory } from './ChatHistoryContext';

// Mock hooks
vi.mock('../chat/ChatContext', () => ({
  useChat: vi.fn(),
}));

vi.mock('./ChatHistoryContext', () => ({
  useChatHistory: vi.fn(),
}));

const mockUseChat = useChat as unknown as ReturnType<typeof vi.fn>;
const mockUseChatHistory = useChatHistory as unknown as ReturnType<typeof vi.fn>;

function createDefaultMocks() {
  mockUseChat.mockReturnValue({
    sessionId: 'session-1',
    currentChatHistory: [{ type: 'user', content: 'Hello' }],
    clearChat: vi.fn(),
    regenerateSessionId: vi.fn(),
    setSessionId: vi.fn(),
    abortActiveStream: vi.fn(),
    addMessage: vi.fn(),
  });

  mockUseChatHistory.mockReturnValue({
    chatHistories: [
      { id: 'session-1', title: 'Current Chat', messages: [{ type: 'user', content: 'Hello' }], source: 'local' },
      { id: 'session-2', title: 'Old Chat', messages: [{ type: 'user', content: 'Hi' }], source: 'local' },
    ],
    saveCurrentChat: vi.fn(),
    deleteHistory: vi.fn(),
    searchHistories: vi.fn().mockResolvedValue(undefined),
    refreshFromBackend: vi.fn().mockResolvedValue([]),
  });
}

describe('ChatHistorySidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createDefaultMocks();
  });

  afterEach(() => {
    cleanup();
  });

  describe('rendering', () => {
    it('renders sidebar with app title', () => {
      render(<ChatHistorySidebar />);

      expect(screen.getByText('RAG 知识库问答')).toBeDefined();
    });

    it('renders new chat button', () => {
      render(<ChatHistorySidebar />);

      expect(screen.getByText('新建对话')).toBeDefined();
    });

    it('renders search input with placeholder', () => {
      render(<ChatHistorySidebar />);

      const searchInput = screen.getByPlaceholderText('搜索历史');
      expect(searchInput).toBeDefined();
    });

    it('renders section header "近期对话"', () => {
      render(<ChatHistorySidebar />);

      expect(screen.getByText('近期对话')).toBeDefined();
    });

    it('renders history items from chatHistories', () => {
      render(<ChatHistorySidebar />);

      expect(screen.getByText('Current Chat')).toBeDefined();
      expect(screen.getByText('Old Chat')).toBeDefined();
    });

    it('marks active session with active class', () => {
      render(<ChatHistorySidebar />);

      const activeItem = screen.getByText('Current Chat').closest('.history-item');
      expect(activeItem).not.toBeNull();
      expect(activeItem!.classList.contains('active')).toBe(true);

      const inactiveItem = screen.getByText('Old Chat').closest('.history-item');
      expect(inactiveItem!.classList.contains('active')).toBe(false);
    });

    it('renders delete button on each history item', () => {
      render(<ChatHistorySidebar />);

      const deleteButtons = screen.getAllByTitle('删除');
      expect(deleteButtons.length).toBe(2);
    });

    it('renders empty history list when no histories', () => {
      mockUseChatHistory.mockReturnValue({
        chatHistories: [],
        saveCurrentChat: vi.fn(),
        deleteHistory: vi.fn(),
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);

      expect(screen.queryByText('Current Chat')).toBeNull();
      expect(screen.queryByText('Old Chat')).toBeNull();
    });
  });

  describe('mount behavior', () => {
    it('calls refreshFromBackend on mount', async () => {
      const refreshFromBackend = vi.fn().mockResolvedValue([]);
      mockUseChatHistory.mockReturnValue({
        chatHistories: [],
        saveCurrentChat: vi.fn(),
        deleteHistory: vi.fn(),
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend,
      });

      render(<ChatHistorySidebar />);

      await waitFor(() => {
        expect(refreshFromBackend).toHaveBeenCalledTimes(1);
      });
    });
  });

  describe('new chat button', () => {
    it('saves current chat, clears, and regenerates session when history is not empty', async () => {
      const clearChat = vi.fn();
      const regenerateSessionId = vi.fn();
      const saveCurrentChat = vi.fn();

      mockUseChat.mockReturnValue({
        sessionId: 'session-1',
        currentChatHistory: [{ type: 'user', content: 'Hello' }],
        clearChat,
        regenerateSessionId,
        abortActiveStream: vi.fn(),
      });
      mockUseChatHistory.mockReturnValue({
        chatHistories: [],
        saveCurrentChat,
        deleteHistory: vi.fn(),
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);

      const newChatBtn = screen.getByText('新建对话');
      await userEvent.click(newChatBtn);

      expect(saveCurrentChat).toHaveBeenCalledTimes(1);
      expect(clearChat).toHaveBeenCalledTimes(1);
      expect(regenerateSessionId).toHaveBeenCalledTimes(1);
    });

    it('skips save when currentChatHistory is empty', async () => {
      const clearChat = vi.fn();
      const regenerateSessionId = vi.fn();
      const saveCurrentChat = vi.fn();

      mockUseChat.mockReturnValue({
        sessionId: 'session-1',
        currentChatHistory: [],
        clearChat,
        regenerateSessionId,
        abortActiveStream: vi.fn(),
      });
      mockUseChatHistory.mockReturnValue({
        chatHistories: [],
        saveCurrentChat,
        deleteHistory: vi.fn(),
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);

      const newChatBtn = screen.getByText('新建对话');
      await userEvent.click(newChatBtn);

      expect(saveCurrentChat).not.toHaveBeenCalled();
      expect(clearChat).toHaveBeenCalledTimes(1);
      expect(regenerateSessionId).toHaveBeenCalledTimes(1);
    });
  });

  describe('load history', () => {
    it('adopts history entry id instead of regenerating session id', async () => {
      const clearChat = vi.fn();
      const regenerateSessionId = vi.fn();
      const setSessionId = vi.fn();
      const addMessage = vi.fn();
      const loadHistory = vi.fn().mockResolvedValue({
        id: 'policy-session',
        title: 'policy',
        messages: [
          { type: 'user', content: 'policy' },
          { type: 'assistant', content: 'ok' },
        ],
        source: 'local',
      });

      mockUseChat.mockReturnValue({
        sessionId: 'session-fresh',
        currentChatHistory: [],
        clearChat,
        regenerateSessionId,
        setSessionId,
        abortActiveStream: vi.fn(),
        addMessage,
      });
      mockUseChatHistory.mockReturnValue({
        chatHistories: [
          {
            id: 'policy-session',
            title: 'policy',
            messages: [
              { type: 'user', content: 'policy' },
              { type: 'assistant', content: 'ok' },
            ],
            source: 'local',
          },
        ],
        saveCurrentChat: vi.fn(),
        loadHistory,
        deleteHistory: vi.fn(),
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);
      await userEvent.click(screen.getByText('policy'));

      await waitFor(() => {
        expect(loadHistory).toHaveBeenCalledTimes(1);
      });
      expect(setSessionId).toHaveBeenCalledWith('policy-session');
      expect(regenerateSessionId).not.toHaveBeenCalled();
      expect(clearChat).toHaveBeenCalled();
      expect(addMessage).toHaveBeenCalledTimes(2);
    });
  });

  describe('search', () => {
    it('calls searchHistories when user types in search input', async () => {
      const searchHistories = vi.fn().mockResolvedValue(undefined);
      mockUseChatHistory.mockReturnValue({
        chatHistories: [],
        saveCurrentChat: vi.fn(),
        deleteHistory: vi.fn(),
        searchHistories,
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);

      const searchInput = screen.getByPlaceholderText('搜索历史');
      await userEvent.type(searchInput, 'test query');

      await waitFor(() => {
        expect(searchHistories).toHaveBeenCalledWith('test query');
      });
    });

    it('passes empty string to searchHistories when input is cleared', async () => {
      const searchHistories = vi.fn().mockResolvedValue(undefined);
      mockUseChatHistory.mockReturnValue({
        chatHistories: [{ id: 's1', title: 'Test', messages: [], source: 'local' }],
        saveCurrentChat: vi.fn(),
        deleteHistory: vi.fn(),
        searchHistories,
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);

      const searchInput = screen.getByPlaceholderText('搜索历史');
      await userEvent.type(searchInput, 'abc');
      await userEvent.clear(searchInput);

      expect(searchHistories).toHaveBeenCalledWith('');
    });
  });

  describe('delete', () => {
    it('calls deleteHistory when delete button is clicked', async () => {
      const deleteHistory = vi.fn().mockResolvedValue(undefined);
      mockUseChatHistory.mockReturnValue({
        chatHistories: [
          { id: 'session-1', title: 'Chat 1', messages: [], source: 'local' },
        ],
        saveCurrentChat: vi.fn(),
        deleteHistory,
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);

      const deleteBtn = screen.getByTitle('删除');
      await userEvent.click(deleteBtn);

      expect(deleteHistory).toHaveBeenCalledTimes(1);
      expect(deleteHistory).toHaveBeenCalledWith('session-1');
    });

    it('resets current chat when deleting the active session', async () => {
      const deleteHistory = vi.fn().mockResolvedValue(undefined);
      const clearChat = vi.fn();
      const regenerateSessionId = vi.fn();
      const abortActiveStream = vi.fn();
      mockUseChat.mockReturnValue({
        sessionId: 'session-1',
        currentChatHistory: [{ type: 'user', content: 'Hello' }],
        clearChat,
        regenerateSessionId,
        abortActiveStream,
      });
      mockUseChatHistory.mockReturnValue({
        chatHistories: [
          { id: 'session-1', title: 'Chat 1', messages: [], source: 'local' },
        ],
        saveCurrentChat: vi.fn(),
        deleteHistory,
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);
      await userEvent.click(screen.getByTitle('删除'));

      await waitFor(() => {
        expect(deleteHistory).toHaveBeenCalledWith('session-1');
        expect(abortActiveStream).toHaveBeenCalled();
        expect(clearChat).toHaveBeenCalled();
        expect(regenerateSessionId).toHaveBeenCalled();
      });
    });

    it('does not reset current chat when delete API fails', async () => {
      const deleteHistory = vi.fn().mockRejectedValue(new Error('fail'));
      const clearChat = vi.fn();
      const regenerateSessionId = vi.fn();
      const abortActiveStream = vi.fn();
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      mockUseChat.mockReturnValue({
        sessionId: 'session-1',
        currentChatHistory: [{ type: 'user', content: 'Hello' }],
        clearChat,
        regenerateSessionId,
        abortActiveStream,
      });
      mockUseChatHistory.mockReturnValue({
        chatHistories: [
          { id: 'session-1', title: 'Chat 1', messages: [], source: 'local' },
        ],
        saveCurrentChat: vi.fn(),
        deleteHistory,
        searchHistories: vi.fn().mockResolvedValue(undefined),
        refreshFromBackend: vi.fn().mockResolvedValue([]),
      });

      render(<ChatHistorySidebar />);
      await userEvent.click(screen.getByTitle('删除'));

      await waitFor(() => {
        expect(deleteHistory).toHaveBeenCalledWith('session-1');
      });
      expect(clearChat).not.toHaveBeenCalled();
      expect(regenerateSessionId).not.toHaveBeenCalled();
      expect(abortActiveStream).not.toHaveBeenCalled();
      consoleSpy.mockRestore();
    });
  });
});
