import React, { useCallback, useEffect } from 'react';
import { ChatProvider } from './features/chat/ChatContext';
import { ChatHistoryProvider } from './features/history/ChatHistoryContext';
import { KnowledgeProvider, useKnowledge } from './features/knowledge/KnowledgeContext';
import ChatPanel from './features/chat/ChatPanel';
import ChatHistorySidebar from './features/history/ChatHistorySidebar';
import FileUpload from './features/upload/FileUpload';
import KnowledgeSpaceSelector from './features/knowledge/KnowledgeSpaceSelector';
import DocumentList from './features/knowledge/DocumentList';
import IndexJobList from './features/knowledge/IndexJobList';
import AuditList from './features/knowledge/AuditList';

function AppContent() {
  const { selectedSpaceId, refreshSpaces, spaceIdOf, setSelectedSpaceId } = useKnowledge();

  const handleRefresh = useCallback(async () => {
    try {
      const spaces = await refreshSpaces();
      if (spaces.length > 0 && !spaces.some(s => spaceIdOf(s) === selectedSpaceId)) {
        setSelectedSpaceId(spaceIdOf(spaces[0]));
      }
    } catch { /* silently handled */ }
  }, [refreshSpaces, selectedSpaceId, spaceIdOf, setSelectedSpaceId]);

  useEffect(() => { refreshSpaces().catch(() => {}); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="app-layout">
      <ChatHistorySidebar />
      <main className="main-content">
        <ChatPanel
          spaceId={selectedSpaceId}
          uploadSlot={<FileUpload spaceId={selectedSpaceId} onRefresh={handleRefresh} />}
        />
      </main>
      <aside className="management-panel">
        <KnowledgeSpaceSelector onSpaceChange={handleRefresh} />
        <DocumentList />
        <IndexJobList />
        <AuditList />
      </aside>
    </div>
  );
}

export default function App() {
  return (
    <KnowledgeProvider>
      <ChatProvider>
        <ChatHistoryProvider>
          <AppContent />
        </ChatHistoryProvider>
      </ChatProvider>
    </KnowledgeProvider>
  );
}
