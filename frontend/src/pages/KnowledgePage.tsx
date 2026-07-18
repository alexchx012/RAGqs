import React, { useCallback, useEffect } from 'react';
import { KnowledgeProvider, useKnowledge } from '../features/knowledge/KnowledgeContext';
import KnowledgeSpaceSelector from '../features/knowledge/KnowledgeSpaceSelector';
import DocumentList from '../features/knowledge/DocumentList';
import IndexJobList from '../features/knowledge/IndexJobList';

function KnowledgePageContent() {
  const { refreshSpaces } = useKnowledge();

  // Selection correction (stale/unauthorized space → first available) already
  // happens inside refreshSpaces itself via a race-safe functional state update.
  const handleRefresh = useCallback(async () => {
    try {
      await refreshSpaces();
    } catch {
      /* silent */
    }
  }, [refreshSpaces]);

  useEffect(() => {
    refreshSpaces().catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="knowledge-page" data-testid="knowledge-page">
      <header className="page-header">
        <h1 className="page-title">知识库</h1>
        <p className="page-subtitle">浏览与管理你有权限的知识空间</p>
      </header>
      <aside className="management-panel knowledge-panel-surface">
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
