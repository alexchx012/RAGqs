import React, { useCallback, useEffect } from 'react';
import { KnowledgeProvider, useKnowledge } from '../features/knowledge/KnowledgeContext';
import KnowledgeSpaceSelector from '../features/knowledge/KnowledgeSpaceSelector';
import DocumentList from '../features/knowledge/DocumentList';
import IndexJobList from '../features/knowledge/IndexJobList';

function KnowledgePageContent() {
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
    <div className="knowledge-page" data-testid="knowledge-page">
      <aside className="management-panel">
        <KnowledgeSpaceSelector scope="own" onSpaceChange={handleRefresh} />
        <DocumentList />
        <IndexJobList />
        {/* 明确不渲染 AuditList — 对应 tasks 4.3 / personal-knowledge-page spec */}
      </aside>
    </div>
  );
}

export default function KnowledgePage() {
  return (
    <KnowledgeProvider>
      <KnowledgePageContent />
    </KnowledgeProvider>
  );
}
