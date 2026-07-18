import React, { useCallback, useEffect, useState } from 'react';
import { ChatProvider } from '../features/chat/ChatContext';
import { ChatHistoryProvider } from '../features/history/ChatHistoryContext';
import { KnowledgeProvider, useKnowledge } from '../features/knowledge/KnowledgeContext';
import ChatPanel from '../features/chat/ChatPanel';
import ChatHistorySidebar from '../features/history/ChatHistorySidebar';
import FileUpload from '../features/upload/FileUpload';

function ChatPageContent() {
  const { selectedSpaceId, spacesReady, refreshSpaces, spaceIdOf, setSelectedSpaceId } = useKnowledge();
  const [spacesError, setSpacesError] = useState('');

  const handleRefresh = useCallback(async () => {
    setSpacesError('');
    try {
      const spaces = await refreshSpaces();
      if (spaces.length > 0 && !spaces.some((s) => spaceIdOf(s) === selectedSpaceId)) {
        setSelectedSpaceId(spaceIdOf(spaces[0]));
      }
    } catch (err: unknown) {
      setSpacesError(err instanceof Error ? err.message : '知识空间加载失败');
    }
  }, [refreshSpaces, selectedSpaceId, spaceIdOf, setSelectedSpaceId]);

  useEffect(() => {
    handleRefresh();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Gate sending/uploading on a confirmed, server-authorized space so a stale
  // localStorage space id can never be used to fire a request that the backend
  // would reject with a 403 the user has no visibility into.
  const canSend = spacesReady && !!selectedSpaceId;

  return (
    <div className="app-layout" data-testid="chat-page">
      <ChatHistorySidebar />
      <main className="main-content">
        {spacesError && (
          <div className="knowledge-space-banner" style={{ color: '#b3261e', padding: '8px 16px' }}>
            知识空间加载失败: {spacesError}{' '}
            <button
              type="button"
              onClick={handleRefresh}
              style={{ background: 'none', border: 'none', color: '#1a73e8', cursor: 'pointer', textDecoration: 'underline' }}
            >
              ↻ 重试
            </button>
          </div>
        )}
        {!spacesError && spacesReady && !selectedSpaceId && (
          <div className="knowledge-space-banner" style={{ padding: '8px 16px' }}>
            暂无可用知识空间，无法发送消息或上传文件
          </div>
        )}
        <ChatPanel
          spaceId={selectedSpaceId}
          disabled={!canSend}
          uploadSlot={<FileUpload spaceId={selectedSpaceId} disabled={!canSend} onRefresh={handleRefresh} />}
        />
      </main>
    </div>
  );
}

export default function ChatPage() {
  return (
    <KnowledgeProvider>
      <ChatProvider>
        <ChatHistoryProvider>
          <ChatPageContent />
        </ChatHistoryProvider>
      </ChatProvider>
    </KnowledgeProvider>
  );
}
