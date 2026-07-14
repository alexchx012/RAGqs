import React, { useCallback, useEffect } from 'react';
import { ChatProvider } from '../features/chat/ChatContext';
import { ChatHistoryProvider } from '../features/history/ChatHistoryContext';
import { KnowledgeProvider, useKnowledge } from '../features/knowledge/KnowledgeContext';
import ChatPanel from '../features/chat/ChatPanel';
import ChatHistorySidebar from '../features/history/ChatHistorySidebar';
import FileUpload from '../features/upload/FileUpload';

function ChatPageContent() {
  const { selectedSpaceId, refreshSpaces, spaceIdOf, setSelectedSpaceId } = useKnowledge();

  const handleRefresh = useCallback(async () => {
    try {
      const spaces = await refreshSpaces();
      if (spaces.length > 0 && !spaces.some((s) => spaceIdOf(s) === selectedSpaceId)) {
        setSelectedSpaceId(spaceIdOf(spaces[0]));
      }
    } catch {
      /* silent */
    }
  }, [refreshSpaces, selectedSpaceId, spaceIdOf, setSelectedSpaceId]);

  useEffect(() => {
    refreshSpaces().catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="app-layout" data-testid="chat-page">
      <ChatHistorySidebar />
      <main className="main-content">
        <ChatPanel
          spaceId={selectedSpaceId}
          uploadSlot={<FileUpload spaceId={selectedSpaceId} onRefresh={handleRefresh} />}
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
