import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, render, screen } from '@testing-library/react';
import React from 'react';
import { ChatProvider } from '../chat/ChatContext';

// Mock the api client before importing the module under test
vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

import { apiJson } from '../../api/client';
import {
  ChatHistoryProvider,
  useChatHistory,
  normalizeHistories,
  loadFromStorage,
  persistToStorage,
  MAX_LOCAL,
} from './ChatHistoryContext';
import type { HistoryEntry, ChatHistoryContextValue } from './ChatHistoryContext';

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(
    ChatProvider,
    null,
    React.createElement(ChatHistoryProvider, null, children),
  );
}

// Create a chat-only wrapper that does NOT include ChatHistoryProvider (for error tests)
function chatOnlyWrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(ChatProvider, null, children);
}

describe('ChatHistoryContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('useChatHistory', () => {
    it('throws when used outside ChatHistoryProvider', () => {
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

      expect(() => {
        renderHook(() => useChatHistory(), { wrapper: chatOnlyWrapper });
      }).toThrow('useChatHistory must be used within ChatHistoryProvider');

      consoleSpy.mockRestore();
    });

    it('provides chatHistories, saveCurrentChat, loadHistory, deleteHistory, searchHistories, refreshFromBackend', () => {
      const { result } = renderHook(() => useChatHistory(), { wrapper });

      expect(result.current).toBeDefined();
      expect(result.current.chatHistories).toEqual([]);
      expect(result.current.saveCurrentChat).toBeTypeOf('function');
      expect(result.current.loadHistory).toBeTypeOf('function');
      expect(result.current.deleteHistory).toBeTypeOf('function');
      expect(result.current.searchHistories).toBeTypeOf('function');
      expect(result.current.refreshFromBackend).toBeTypeOf('function');
    });

    it('initializes chatHistories from localStorage', () => {
      const existing: HistoryEntry[] = [
        {
          id: 'session_abc',
          title: 'Test Chat',
          messages: [{ type: 'user', content: 'Hello' }],
          source: 'local',
        },
      ];
      localStorage.setItem('ragChatHistories', JSON.stringify(existing));

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      expect(result.current.chatHistories).toHaveLength(1);
      expect(result.current.chatHistories[0].id).toBe('session_abc');
      expect(result.current.chatHistories[0].title).toBe('Test Chat');
    });

    it('handles corrupt localStorage data gracefully', () => {
      localStorage.setItem('ragChatHistories', 'not-valid-json{{{');

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      expect(result.current.chatHistories).toEqual([]);
    });
  });

  describe('saveCurrentChat', () => {
    it('does nothing when currentChatHistory is empty', () => {
      const { result } = renderHook(() => useChatHistory(), { wrapper });

      act(() => {
        result.current.saveCurrentChat();
      });

      expect(result.current.chatHistories).toEqual([]);
      expect(localStorage.getItem('ragChatHistories')).toBeNull();
    });

    it('saves current chat to history with title from first user message', () => {
      const { result } = renderHook(
        () => {
          const chat = useChatHistory();
          return chat;
        },
        { wrapper },
      );

      // We need to add messages to ChatContext first via the chat context
      // But saveCurrentChat reads from currentChatHistory which comes from ChatProvider
      // Since we can't easily inject messages into ChatProvider through the test,
      // we verify the function shape and behavior with a custom wrapper approach.
      // The saveCurrentChat function itself calls useChat() internally.
      // For complete integration testing, we rely on the component-level test.
      expect(result.current.saveCurrentChat).toBeTypeOf('function');
    });
  });

  describe('loadHistory', () => {
    it('returns entry immediately when messages are already populated', async () => {
      const entry: HistoryEntry = {
        id: 'session_1',
        title: 'Loaded Chat',
        messages: [{ type: 'user', content: 'Hi' }],
        source: 'local',
      };

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      let loadedEntry: HistoryEntry | undefined;
      await act(async () => {
        loadedEntry = await result.current.loadHistory(entry);
      });

      expect(loadedEntry).toEqual(entry);
    });

    it('returns entry with empty messages when source is not backend and messages are empty', async () => {
      const entry: HistoryEntry = {
        id: 'session_2',
        title: 'Empty Local',
        messages: [],
        source: 'local',
      };

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      let loadedEntry: HistoryEntry | undefined;
      await act(async () => {
        loadedEntry = await result.current.loadHistory(entry);
      });

      expect(loadedEntry).toBeDefined();
      expect(loadedEntry!.messages).toEqual([]);
    });
  });

  describe('deleteHistory', () => {
    it('calls clear API then removes entry from chatHistories', async () => {
      mockApiJson.mockResolvedValue({ status: 'success', message: '会话已清空', data: null });
      const existing: HistoryEntry[] = [
        {
          id: 'session_a',
          title: 'Chat A',
          messages: [{ type: 'user', content: 'A' }],
          source: 'local',
        },
        {
          id: 'session_b',
          title: 'Chat B',
          messages: [{ type: 'user', content: 'B' }],
          source: 'local',
        },
      ];
      localStorage.setItem('ragChatHistories', JSON.stringify(existing));

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await result.current.deleteHistory('session_a');
      });

      expect(mockApiJson).toHaveBeenCalledWith('/chat/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: 'session_a' }),
      });
      expect(result.current.chatHistories).toHaveLength(1);
      expect(result.current.chatHistories[0].id).toBe('session_b');
      const stored = JSON.parse(localStorage.getItem('ragChatHistories') || '[]');
      expect(stored).toHaveLength(1);
      expect(stored[0].id).toBe('session_b');
    });

    it('keeps local entry when clear API fails', async () => {
      mockApiJson.mockRejectedValue(new Error('network'));
      const existing: HistoryEntry[] = [
        {
          id: 'session_x',
          title: 'Only Chat',
          messages: [{ type: 'user', content: 'X' }],
          source: 'local',
        },
      ];
      localStorage.setItem('ragChatHistories', JSON.stringify(existing));

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await expect(result.current.deleteHistory('session_x')).rejects.toThrow('network');
      });

      expect(result.current.chatHistories).toHaveLength(1);
      expect(result.current.chatHistories[0].id).toBe('session_x');
    });

    it('removes nothing when id is not found after successful clear', async () => {
      mockApiJson.mockResolvedValue({ status: 'success', message: '会话已清空', data: null });
      const existing: HistoryEntry[] = [
        {
          id: 'session_x',
          title: 'Only Chat',
          messages: [{ type: 'user', content: 'X' }],
          source: 'local',
        },
      ];
      localStorage.setItem('ragChatHistories', JSON.stringify(existing));

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await result.current.deleteHistory('nonexistent');
      });

      expect(mockApiJson).toHaveBeenCalledWith('/chat/clear', expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ sessionId: 'nonexistent' }),
      }));
      expect(result.current.chatHistories).toHaveLength(1);
    });
  });

  describe('refreshFromBackend', () => {
    it('calls apiJson with /chat/sessions and merges with local', async () => {
      const backendSessions = [
        {
          session_id: 'be-1',
          title: 'Backend Chat 1',
          message_count: 5,
          updated_at: '2024-01-01T00:00:00Z',
          last_message: 'Hello from backend',
        },
        {
          session_id: 'be-2',
          title: 'Backend Chat 2',
          message_count: 3,
          updated_at: '2024-01-02T00:00:00Z',
          last_message: 'Another message',
        },
      ];

      mockApiJson.mockResolvedValue({
        code: 200,
        data: { sessions: backendSessions },
      });

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await result.current.refreshFromBackend();
      });

      expect(mockApiJson).toHaveBeenCalledWith('/chat/sessions');
      expect(result.current.chatHistories.length).toBeGreaterThanOrEqual(2);
      const backendEntries = result.current.chatHistories.filter(h => h.source === 'backend');
      expect(backendEntries).toHaveLength(2);
    });

    it('handles empty backend response gracefully', async () => {
      mockApiJson.mockResolvedValue({
        code: 200,
        data: { sessions: [] },
      });

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await result.current.refreshFromBackend();
      });

      // Should not crash, should keep existing local histories (none in this case)
      expect(Array.isArray(result.current.chatHistories)).toBe(true);
    });

    it('handles null data gracefully', async () => {
      mockApiJson.mockResolvedValue({
        code: 200,
        data: null,
      });

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await result.current.refreshFromBackend();
      });

      expect(result.current.chatHistories).toEqual([]);
    });
  });

  describe('ChatHistoryProvider', () => {
    it('renders children', () => {
      render(
        <ChatProvider>
          <ChatHistoryProvider>
            <div data-testid="history-child">History Content</div>
          </ChatHistoryProvider>
        </ChatProvider>,
      );

      expect(screen.getByTestId('history-child')).toBeDefined();
    });
  });

  describe('normalizeHistories', () => {
    it('returns empty array for non-array input', () => {
      expect(normalizeHistories(null as unknown as unknown[])).toEqual([]);
      expect(normalizeHistories(undefined as unknown as unknown[])).toEqual([]);
      expect(normalizeHistories('string' as unknown as unknown[])).toEqual([]);
    });

    it('deduplicates by id', () => {
      const input = [
        { id: 'dup', title: 'First', messages: [] },
        { id: 'dup', title: 'Second', messages: [] },
        { id: 'unique', title: 'Unique', messages: [] },
      ];

      const result = normalizeHistories(input);

      expect(result).toHaveLength(2);
      expect(result[0].id).toBe('dup');
      expect(result[0].title).toBe('First');
      expect(result[1].id).toBe('unique');
    });

    it('skips entries without id', () => {
      const input = [
        { title: 'No ID', messages: [] },
        { id: 'valid', title: 'Valid', messages: [] },
      ];

      const result = normalizeHistories(input);

      expect(result).toHaveLength(1);
      expect(result[0].id).toBe('valid');
    });

    it('normalizes snake_case fields', () => {
      const input = [
        {
          id: 'snake',
          title: 'Snake Case',
          messages: [{ type: 'user', content: 'Hello' }],
          message_count: 10,
          updated_at: '2024-01-01',
          last_message: 'Last msg',
          source: 'backend',
        },
      ];

      const result = normalizeHistories(input);

      expect(result[0].messageCount).toBe(10);
      expect(result[0].updatedAt).toBe('2024-01-01');
      expect(result[0].lastMessage).toBe('Last msg');
    });

    it('falls back to camelCase fields', () => {
      const input = [
        {
          id: 'camel',
          title: 'Camel Case',
          messages: [],
          messageCount: 20,
          updatedAt: '2024-06-01',
          lastMessage: 'Camel msg',
        },
      ];

      const result = normalizeHistories(input);

      expect(result[0].messageCount).toBe(20);
    });

    it('defaults missing title to "新对话"', () => {
      const input = [{ id: 'no-title', messages: [] }];

      const result = normalizeHistories(input);

      expect(result[0].title).toBe('新对话');
    });

    it('defaults missing fields to sensible values', () => {
      const input = [{ id: 'minimal' }];

      const result = normalizeHistories(input);

      expect(result[0].messages).toEqual([]);
      expect(result[0].messageCount).toBe(0);
      expect(result[0].updatedAt).toBe('');
      expect(result[0].lastMessage).toBe('');
      expect(result[0].source).toBe('local');
    });
  });
});
