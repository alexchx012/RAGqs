import React, { useState, useEffect, useCallback } from 'react';
import { useChat } from '../chat/ChatContext';
import { useChatHistory } from './ChatHistoryContext';

export default function ChatHistorySidebar() {
  const { sessionId, currentChatHistory, addMessage, clearChat, regenerateSessionId, setSessionId, abortActiveStream } = useChat();
  const { chatHistories, saveCurrentChat, loadHistory, deleteHistory, searchHistories, refreshFromBackend } = useChatHistory();
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => { refreshFromBackend(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleNewChat = useCallback(() => {
    abortActiveStream();
    if (currentChatHistory.length > 0) saveCurrentChat();
    clearChat();
    regenerateSessionId();
  }, [abortActiveStream, currentChatHistory, saveCurrentChat, clearChat, regenerateSessionId]);

  const handleSearch = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value;
    setSearchQuery(q);
    searchHistories(q);
  }, [searchHistories]);

  const handleLoadHistory = useCallback(async (h: typeof chatHistories[number]) => {
    abortActiveStream();
    const loaded = await loadHistory(h);
    clearChat();
    // Adopt the history entry id so "新建对话" does not save a duplicate under a new id.
    setSessionId(h.id);
    for (const msg of loaded.messages) {
      addMessage(msg);
    }
  }, [abortActiveStream, loadHistory, clearChat, setSessionId, addMessage]);

  const handleDelete = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    try {
      await deleteHistory(id);
      if (id === sessionId) {
        abortActiveStream();
        clearChat();
        regenerateSessionId();
      }
    } catch (err) {
      console.error('删除会话失败', err);
    }
  }, [deleteHistory, sessionId, abortActiveStream, clearChat, regenerateSessionId]);

  return (
    <aside className="sidebar">
      <div className="sidebar-header"><h2 className="sidebar-title">RAG 知识库问答</h2></div>
      <div className="sidebar-content">
        <button className="new-chat-btn" onClick={handleNewChat}>
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M12 5V19M5 12H19" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <span>新建对话</span>
        </button>
        <div className="chat-history-section">
          <div className="history-header"><span>近期对话</span></div>
          <input type="search" className="history-search-input" placeholder="搜索历史" value={searchQuery} onChange={handleSearch} />
          <div className="chat-history-list">
            {chatHistories.map(h => (
              <div key={h.id} className={`history-item ${h.id === sessionId ? 'active' : ''}`} title={h.title} onClick={() => handleLoadHistory(h)}>
                <div className="history-item-content"><span className="history-item-title">{h.title}</span></div>
                <button className="history-item-delete" onClick={(e) => handleDelete(e, h.id)} title="删除">
                  <svg viewBox="0 0 24 24" fill="none">
                    <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </aside>
  );
}
