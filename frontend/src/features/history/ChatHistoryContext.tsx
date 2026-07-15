import React, { createContext, useContext, useState, useCallback, useRef } from 'react';
import { useChat } from '../chat/ChatContext';
import { apiJson } from '../../api/client';
import type { ChatMessage, SessionsData, ApiResponse } from '../../api/types';

export interface HistoryEntry {
  id: string;
  title: string;
  messages: ChatMessage[];
  messageCount?: number;
  updatedAt?: string;
  lastMessage?: string;
  source?: 'local' | 'backend';
}

export const MAX_LOCAL = 30;

export function normalizeHistories(histories: unknown[]): HistoryEntry[] {
  if (!Array.isArray(histories)) return [];
  const seen = new Set<string>();
  const result: HistoryEntry[] = [];
  for (const h of histories) {
    if (!h || typeof h !== 'object') continue;
    const e = h as Record<string, unknown>;
    const id = String(e.id || '');
    if (!id || seen.has(id)) continue;
    seen.add(id);
    result.push({
      id,
      title: String(e.title || '新对话'),
      messages: Array.isArray(e.messages) ? (e.messages as ChatMessage[]) : [],
      messageCount: Number(e.messageCount ?? e.message_count ?? 0),
      updatedAt: String(e.updatedAt ?? e.updated_at ?? ''),
      lastMessage: String(e.lastMessage ?? e.last_message ?? ''),
      source: (e.source as 'local' | 'backend') || 'local',
    });
  }
  return result;
}

/** Newest activity first. Invalid/missing timestamps sink to the bottom. */
export function sortHistoriesByActivity(histories: HistoryEntry[]): HistoryEntry[] {
  return [...histories].sort((a, b) => {
    const ta = Date.parse(a.updatedAt || '') || 0;
    const tb = Date.parse(b.updatedAt || '') || 0;
    return tb - ta;
  });
}

export function messagesContentEqual(a: ChatMessage[], b: ChatMessage[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i].type !== b[i].type || a[i].content !== b[i].content) return false;
  }
  return true;
}

export function loadFromStorage(): HistoryEntry[] {
  try {
    const raw = JSON.parse(localStorage.getItem('ragChatHistories') || '[]');
    return sortHistoriesByActivity(normalizeHistories(raw));
  } catch { return []; }
}

export function persistToStorage(histories: HistoryEntry[]): void {
  const persisted = histories.filter(h => Array.isArray(h.messages) && h.messages.length > 0);
  try { localStorage.setItem('ragChatHistories', JSON.stringify(persisted)); }
  catch { /* quota exceeded */ }
}

export interface ChatHistoryContextValue {
  chatHistories: HistoryEntry[];
  saveCurrentChat: () => void;
  loadHistory: (entry: HistoryEntry) => Promise<HistoryEntry>;
  deleteHistory: (id: string) => Promise<void>;
  searchHistories: (query: string) => Promise<void>;
  refreshFromBackend: () => Promise<HistoryEntry[]>;
}

const ChatHistoryContext = createContext<ChatHistoryContextValue | null>(null);

function titleMatchesQuery(title: string, query: string): boolean {
  return title.toLowerCase().includes(query.toLowerCase());
}

export function ChatHistoryProvider({ children }: { children: React.ReactNode }) {
  const { sessionId, currentChatHistory } = useChat();
  const [chatHistories, setChatHistories] = useState<HistoryEntry[]>(() => loadFromStorage());
  const refreshRequestIdRef = useRef(0);

  const saveCurrentChat = useCallback(() => {
    if (currentChatHistory.length === 0) return;
    const first = currentChatHistory.find(m => m.type === 'user');
    const title = first ? first.content.substring(0, 30) : '新对话';
    const messages = [...currentChatHistory];
    setChatHistories(prev => {
      const existing = prev.find(h => h.id === sessionId);
      // Viewing an existing chat without new messages must not bump order.
      if (existing && messagesContentEqual(existing.messages || [], messages)) {
        return prev;
      }
      const entry: HistoryEntry = {
        id: sessionId,
        title,
        messages,
        source: 'local',
        updatedAt: new Date().toISOString(),
        lastMessage: messages[messages.length - 1]?.content?.substring(0, 120) || existing?.lastMessage || '',
        messageCount: messages.length,
      };
      const filtered = prev.filter(h => h.id !== sessionId);
      const updated = sortHistoriesByActivity([entry, ...filtered]).slice(0, MAX_LOCAL);
      persistToStorage(updated);
      return updated;
    });
  }, [currentChatHistory, sessionId]);

  const loadHistory = useCallback(async (entry: HistoryEntry): Promise<HistoryEntry> => {
    if (Array.isArray(entry.messages) && entry.messages.length > 0) return entry;
    if (entry.source !== 'backend') return { ...entry, messages: entry.messages || [] };
    try {
      const res = await fetch(`/api/chat/session/${encodeURIComponent(entry.id)}`);
      if (!res.ok) return { ...entry, messages: [] };
      const data: ApiResponse = await res.json();
      const rawMessages = Array.isArray(data.history) ? data.history : [];
      const messages: ChatMessage[] = [];
      for (const msg of rawMessages) {
        if (!msg || typeof msg !== 'object') continue;
        const m = msg as unknown as Record<string, unknown>;
        const type = String(m.type || m.role || '');
        const content = String(m.content || '');
        if (!type || typeof content !== 'string') continue;
        messages.push({ type: type === 'user' ? 'user' : 'assistant', content });
      }
      const loadedEntry = { ...entry, messages };
      setChatHistories(prev => {
        const updated = prev.map(h => h.id === loadedEntry.id ? loadedEntry : h);
        persistToStorage(updated);
        return updated;
      });
      return loadedEntry;
    } catch { return { ...entry, messages: [] }; }
  }, []);

  const deleteHistory = useCallback(async (id: string): Promise<void> => {
    await apiJson('/chat/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId: id }),
    });
    setChatHistories(prev => {
      const updated = prev.filter(h => h.id !== id);
      persistToStorage(updated);
      return updated;
    });
  }, []);

  const _refreshImpl = useCallback(async (query?: string): Promise<HistoryEntry[]> => {
    const requestId = ++refreshRequestIdRef.current;
    const search = query ? `?query=${encodeURIComponent(query)}` : '';
    try {
      const data = await apiJson<SessionsData>(`/chat/sessions${search}`);
      if (requestId !== refreshRequestIdRef.current) {
        return [];
      }
      const sessions = Array.isArray(data.data?.sessions) ? data.data.sessions : [];
      const bh: HistoryEntry[] = [];
      for (const s of sessions) {
        const id = s?.id || s?.session_id;
        if (!id) continue;
        bh.push({
          id: String(id), title: String(s.title || '新对话'), messages: [],
          messageCount: Number(s.messageCount ?? s.message_count ?? 0),
          updatedAt: String(s.updatedAt ?? s.updated_at ?? ''),
          lastMessage: String(s.lastMessage ?? s.last_message ?? ''),
          source: 'backend',
        });
      }
      if (query) {
        const backendIds = new Set(bh.map(h => h.id));
        const localHits = loadFromStorage().filter(
          h => !backendIds.has(h.id) && titleMatchesQuery(h.title, query),
        );
        const merged = sortHistoriesByActivity(normalizeHistories([...bh, ...localHits]));
        if (requestId !== refreshRequestIdRef.current) {
          return [];
        }
        setChatHistories(merged);
        return merged;
      }
      const localHistories = loadFromStorage();
      const merged = sortHistoriesByActivity(normalizeHistories([...bh, ...localHistories]));
      if (requestId !== refreshRequestIdRef.current) {
        return [];
      }
      setChatHistories(merged);
      return merged;
    } catch {
      return requestId === refreshRequestIdRef.current ? chatHistories : [];
    }
  }, [chatHistories]);

  const searchHistories = useCallback(async (query: string) => {
    const trimmed = query.trim();
    await _refreshImpl(trimmed || undefined);
  }, [_refreshImpl]);

  const refreshFromBackend = useCallback(async () => _refreshImpl(), [_refreshImpl]);

  return (
    <ChatHistoryContext.Provider value={{ chatHistories, saveCurrentChat, loadHistory, deleteHistory, searchHistories, refreshFromBackend }}>
      {children}
    </ChatHistoryContext.Provider>
  );
}

export function useChatHistory(): ChatHistoryContextValue {
  const ctx = useContext(ChatHistoryContext);
  if (!ctx) throw new Error('useChatHistory must be used within ChatHistoryProvider');
  return ctx;
}
