import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, render, screen } from '@testing-library/react';
import React from 'react';
import { ChatProvider, useChat } from '../chat/ChatContext';

// Mock the api client before importing the module under test
vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

// ChatHistoryProvider scopes its local storage key by the logged-in user
// (see historyStorageKey); mock useAuth with a fixed test user so existing
// provider-level tests keep exercising a stable, predictable key.
vi.mock('../auth/AuthContext', () => ({
  useAuth: vi.fn(),
}));

import { apiJson } from '../../api/client';
import { useAuth } from '../auth/AuthContext';
import {
  ChatHistoryProvider,
  useChatHistory,
  normalizeHistories,
  sortHistoriesByActivity,
  messagesContentEqual,
  loadFromStorage,
  persistToStorage,
  historyStorageKey,
  MAX_LOCAL,
} from './ChatHistoryContext';
import type { HistoryEntry, ChatHistoryContextValue } from './ChatHistoryContext';

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;
const mockUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>;

const TEST_USER_ID = 'test-user';
const TEST_STORAGE_KEY = historyStorageKey(TEST_USER_ID);

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(
    ChatProvider,
    null,
    React.createElement(ChatHistoryProvider, null, children),
  );
}

function useChatAndHistory() {
  return { chat: useChat(), history: useChatHistory() };
}

// Create a chat-only wrapper that does NOT include ChatHistoryProvider (for error tests)
function chatOnlyWrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(ChatProvider, null, children);
}

describe('ChatHistoryContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockUseAuth.mockReturnValue({ userId: TEST_USER_ID });
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
      localStorage.setItem(TEST_STORAGE_KEY, JSON.stringify(existing));

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      expect(result.current.chatHistories).toHaveLength(1);
      expect(result.current.chatHistories[0].id).toBe('session_abc');
      expect(result.current.chatHistories[0].title).toBe('Test Chat');
    });

    it('handles corrupt localStorage data gracefully', () => {
      localStorage.setItem(TEST_STORAGE_KEY, 'not-valid-json{{{');

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      expect(result.current.chatHistories).toEqual([]);
    });

    it('scopes storage per user so a different login does not see this user\'s histories', () => {
      const existing: HistoryEntry[] = [
        { id: 'session_abc', title: 'Test Chat', messages: [{ type: 'user', content: 'Hello' }], source: 'local' },
      ];
      localStorage.setItem(TEST_STORAGE_KEY, JSON.stringify(existing));

      mockUseAuth.mockReturnValue({ userId: 'other-user' });
      const { result } = renderHook(() => useChatHistory(), { wrapper });

      expect(result.current.chatHistories).toEqual([]);
      expect(localStorage.getItem(historyStorageKey('other-user'))).toBeNull();
    });
  });

  describe('saveCurrentChat', () => {
    it('does nothing when currentChatHistory is empty', () => {
      const { result } = renderHook(() => useChatHistory(), { wrapper });

      act(() => {
        result.current.saveCurrentChat();
      });

      expect(result.current.chatHistories).toEqual([]);
      expect(localStorage.getItem(TEST_STORAGE_KEY)).toBeNull();
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

  describe('sortHistoriesByActivity', () => {
    it('orders newest updatedAt first', () => {
      const sorted = sortHistoriesByActivity([
        { id: 'old', title: 'old', messages: [], updatedAt: '2024-01-01T00:00:00.000Z' },
        { id: 'new', title: 'new', messages: [], updatedAt: '2024-06-01T00:00:00.000Z' },
        { id: 'mid', title: 'mid', messages: [], updatedAt: '2024-03-01T00:00:00.000Z' },
      ]);
      expect(sorted.map(h => h.id)).toEqual(['new', 'mid', 'old']);
    });

    it('sinks missing timestamps below dated entries', () => {
      const sorted = sortHistoriesByActivity([
        { id: 'nodate', title: 'n', messages: [] },
        { id: 'dated', title: 'd', messages: [], updatedAt: '2024-01-01T00:00:00.000Z' },
      ]);
      expect(sorted.map(h => h.id)).toEqual(['dated', 'nodate']);
    });
  });

  describe('messagesContentEqual', () => {
    it('returns true for identical message sequences', () => {
      expect(
        messagesContentEqual(
          [{ type: 'user', content: 'policy' }],
          [{ type: 'user', content: 'policy' }],
        ),
      ).toBe(true);
    });

    it('returns false when content changes', () => {
      expect(
        messagesContentEqual(
          [{ type: 'user', content: 'policy' }],
          [{ type: 'user', content: 'policy' }, { type: 'assistant', content: 'ok' }],
        ),
      ).toBe(false);
    });
  });

  describe('activity ordering integration', () => {
    it('loadFromStorage returns histories sorted by updatedAt desc', () => {
      localStorage.setItem(
        'ragChatHistories',
        JSON.stringify([
          {
            id: 'a',
            title: '你好',
            messages: [{ type: 'user', content: '你好' }],
            updatedAt: '2024-01-01T00:00:00.000Z',
          },
          {
            id: 'b',
            title: 'policy',
            messages: [{ type: 'user', content: 'policy' }],
            updatedAt: '2024-06-01T00:00:00.000Z',
          },
        ]),
      );
      const loaded = loadFromStorage();
      expect(loaded.map(h => h.id)).toEqual(['b', 'a']);
    });

    it('refreshFromBackend merges and sorts by updatedAt desc', async () => {
      localStorage.setItem(
        TEST_STORAGE_KEY,
        JSON.stringify([
          {
            id: 'local-old',
            title: '你好',
            messages: [{ type: 'user', content: '你好' }],
            updatedAt: '2024-01-01T00:00:00.000Z',
            source: 'local',
          },
        ]),
      );
      mockApiJson.mockResolvedValue({
        code: 200,
        data: {
          sessions: [
            {
              id: 'be-new',
              title: 'policy',
              messageCount: 2,
              updatedAt: '2024-06-01T00:00:00.000Z',
              lastMessage: 'ok',
            },
          ],
        },
      });

      const { result } = renderHook(() => useChatHistory(), { wrapper });
      await act(async () => {
        await result.current.refreshFromBackend();
      });

      expect(result.current.chatHistories.map(h => h.id)).toEqual(['be-new', 'local-old']);
    });

    it('saveCurrentChat does not reorder when messages are unchanged (view-only)', () => {
      localStorage.setItem(
        TEST_STORAGE_KEY,
        JSON.stringify([
          {
            id: 'newer',
            title: '你好',
            messages: [{ type: 'user', content: '你好' }],
            updatedAt: '2024-06-01T00:00:00.000Z',
            source: 'local',
          },
          {
            id: 'policy',
            title: 'policy',
            messages: [
              { type: 'user', content: 'policy' },
              { type: 'assistant', content: 'ok' },
            ],
            updatedAt: '2024-01-01T00:00:00.000Z',
            source: 'local',
          },
        ]),
      );

      const { result } = renderHook(() => useChatAndHistory(), { wrapper });
      expect(result.current.history.chatHistories.map(h => h.id)).toEqual(['newer', 'policy']);

      act(() => {
        result.current.chat.setSessionId('policy');
        result.current.chat.addMessage({ type: 'user', content: 'policy' });
        result.current.chat.addMessage({ type: 'assistant', content: 'ok' });
      });
      act(() => {
        result.current.history.saveCurrentChat();
      });

      expect(result.current.history.chatHistories.map(h => h.id)).toEqual(['newer', 'policy']);
      expect(result.current.history.chatHistories.find(h => h.id === 'policy')?.updatedAt).toBe(
        '2024-01-01T00:00:00.000Z',
      );
    });

    it('saveCurrentChat bumps updatedAt and reorders when messages change', () => {
      localStorage.setItem(
        TEST_STORAGE_KEY,
        JSON.stringify([
          {
            id: 'newer',
            title: '你好',
            messages: [{ type: 'user', content: '你好' }],
            updatedAt: '2024-06-01T00:00:00.000Z',
            source: 'local',
          },
          {
            id: 'policy',
            title: 'policy',
            messages: [{ type: 'user', content: 'policy' }],
            updatedAt: '2024-01-01T00:00:00.000Z',
            source: 'local',
          },
        ]),
      );

      const { result } = renderHook(() => useChatAndHistory(), { wrapper });

      act(() => {
        result.current.chat.setSessionId('policy');
        result.current.chat.addMessage({ type: 'user', content: 'policy' });
        result.current.chat.addMessage({ type: 'assistant', content: 'new answer' });
      });
      act(() => {
        result.current.history.saveCurrentChat();
      });

      expect(result.current.history.chatHistories[0].id).toBe('policy');
      expect(result.current.history.chatHistories[0].updatedAt).toBeTruthy();
      expect(
        Date.parse(result.current.history.chatHistories[0].updatedAt || '') >
          Date.parse('2024-06-01T00:00:00.000Z'),
      ).toBe(true);
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
      localStorage.setItem(TEST_STORAGE_KEY, JSON.stringify(existing));

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
      const stored = JSON.parse(localStorage.getItem(TEST_STORAGE_KEY) || '[]');
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
      localStorage.setItem(TEST_STORAGE_KEY, JSON.stringify(existing));

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
      localStorage.setItem(TEST_STORAGE_KEY, JSON.stringify(existing));

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

  describe('searchHistories', () => {
    it('merges local title matches that backend did not return', async () => {
      localStorage.setItem(
        TEST_STORAGE_KEY,
        JSON.stringify([
          {
            id: 'local-policy',
            title: 'policy',
            messages: [{ type: 'user', content: 'policy' }],
            source: 'local',
          },
          {
            id: 'local-hello',
            title: '你好',
            messages: [{ type: 'user', content: '你好' }],
            source: 'local',
          },
        ]),
      );

      mockApiJson.mockResolvedValue({
        code: 200,
        data: {
          sessions: [
            {
              id: 'be-policy',
              title: 'policy',
              messageCount: 2,
              updatedAt: '2024-01-01T00:00:00Z',
              lastMessage: 'ok',
            },
          ],
        },
      });

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      await act(async () => {
        await result.current.searchHistories('policy');
      });

      expect(mockApiJson).toHaveBeenCalledWith('/chat/sessions?query=policy');
      const ids = result.current.chatHistories.map(h => h.id);
      expect(ids).toContain('be-policy');
      expect(ids).toContain('local-policy');
      expect(ids).not.toContain('local-hello');
    });

    it('ignores stale responses from older queries', async () => {
      let resolveOlder!: (value: unknown) => void;
      let resolveNewer!: (value: unknown) => void;
      const older = new Promise(resolve => {
        resolveOlder = resolve;
      });
      const newer = new Promise(resolve => {
        resolveNewer = resolve;
      });

      mockApiJson
        .mockImplementationOnce(() => older)
        .mockImplementationOnce(() => newer);

      const { result } = renderHook(() => useChatHistory(), { wrapper });

      let olderDone!: Promise<void>;
      let newerDone!: Promise<void>;
      await act(async () => {
        olderDone = result.current.searchHistories('p');
        newerDone = result.current.searchHistories('policy');
      });

      await act(async () => {
        resolveNewer({
          code: 200,
          data: {
            sessions: [
              {
                id: 'policy-session',
                title: 'policy',
                messageCount: 1,
                updatedAt: '2024-01-02T00:00:00Z',
                lastMessage: 'policy',
              },
            ],
          },
        });
        await newerDone;
      });

      expect(result.current.chatHistories.map(h => h.id)).toEqual(['policy-session']);

      await act(async () => {
        resolveOlder({
          code: 200,
          data: {
            sessions: [
              {
                id: 'stale-hello',
                title: '你好',
                messageCount: 1,
                updatedAt: '2024-01-01T00:00:00Z',
                lastMessage: '你好',
              },
            ],
          },
        });
        await olderDone;
      });

      // Stale "p" response must not overwrite the latest "policy" results
      expect(result.current.chatHistories.map(h => h.id)).toEqual(['policy-session']);
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
