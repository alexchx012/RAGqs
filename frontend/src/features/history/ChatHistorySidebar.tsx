import React, { useState, useEffect, useCallback } from 'react';
import { useChat } from '../chat/ChatContext';
import { useChatHistory } from './ChatHistoryContext';

export default function ChatHistorySidebar() {
  const { sessionId, currentChatHistory, clearChat, regenerateSessionId } = useChat();
  const { chatHistories, saveCurrentChat, deleteHistory, searchHistories, refreshFromBackend } = useChatHistory();
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => { refreshFromBackend(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleNewChat = useCallback(() => {
    if (currentChatHistory.length > 0) saveCurrentChat();
    clearChat();
    regenerateSessionId();
  }, [currentChatHistory, saveCurrentChat, clearChat, regenerateSessionId]);

  const handleSearch = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value;
    setSearchQuery(q);
    searchHistories(q);
  }, [searchHistories]);

  const handleDelete = useCallback((e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    deleteHistory(id);
  }, [deleteHistory]);

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
              <div key={h.id} className={`history-item ${h.id === sessionId ? 'active' : ''}`} title={h.title}>
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
