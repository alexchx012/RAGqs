import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
} from 'react';
import type { ChatMessage } from '../../api/types';

export type ChatMode = 'quick' | 'stream';

export interface ChatContextValue {
  sessionId: string;
  currentChatHistory: ChatMessage[];
  isStreaming: boolean;
  mode: ChatMode;
  setMode: (mode: ChatMode) => void;
  addMessage: (msg: ChatMessage) => void;
  setStreaming: (v: boolean) => void;
  clearChat: () => void;
  regenerateSessionId: () => void;
}

function generateSessionId(): string {
  return (
    'session_' +
    Math.random().toString(36).substr(2, 9) +
    '_' +
    Date.now()
  );
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [sessionId, setSessionId] = useState(() => generateSessionId());
  const [currentChatHistory, setCurrentChatHistory] = useState<ChatMessage[]>(
    [],
  );
  const [isStreaming, setIsStreaming] = useState(false);
  const [mode, setMode] = useState<ChatMode>('quick');
  const historyRef = useRef<ChatMessage[]>([]);

  const addMessage = useCallback((msg: ChatMessage) => {
    historyRef.current = [...historyRef.current, msg];
    setCurrentChatHistory(historyRef.current);
  }, []);

  const setStreaming = useCallback((v: boolean) => {
    setIsStreaming(v);
  }, []);

  const clearChat = useCallback(() => {
    historyRef.current = [];
    setCurrentChatHistory([]);
  }, []);

  const regenerateSessionId = useCallback(() => {
    setSessionId(generateSessionId());
  }, []);

  return (
    <ChatContext.Provider
      value={{
        sessionId,
        currentChatHistory,
        isStreaming,
        mode,
        setMode,
        addMessage,
        setStreaming,
        clearChat,
        regenerateSessionId,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error('useChat must be used within ChatProvider');
  return ctx;
}
