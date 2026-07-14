import React, { useCallback, useEffect } from 'react';
import { KnowledgeProvider, useKnowledge } from '../features/knowledge/KnowledgeContext';
import KnowledgeSpaceSelector from '../features/knowledge/KnowledgeSpaceSelector';
import DocumentList from '../features/knowledge/DocumentList';
import IndexJobList from '../features/knowledge/IndexJobList';
import AuditList from '../features/knowledge/AuditList';
import UserManagementPanel from '../features/admin/UserManagementPanel';

function AdminProjectsContent() {
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
    <div className="admin-projects-page" data-testid="admin-projects-page">
      <section className="admin-governance">
        <h2>知识空间治理</h2>
        <aside className="management-panel">
          <KnowledgeSpaceSelector scope="all" onSpaceChange={handleRefresh} />
          <DocumentList />
          <IndexJobList />
          <AuditList />
        </aside>
      </section>
      <section className="admin-users">
        <UserManagementPanel />
      </section>
    </div>
  );
}

export default function AdminProjectsPage() {
  return (
    <KnowledgeProvider>
      <AdminProjectsContent />
    </KnowledgeProvider>
  );
}
