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
      <header className="page-header">
        <h1 className="page-title">项目管理</h1>
        <p className="page-subtitle">治理知识空间与系统用户</p>
      </header>
      <div className="admin-projects-grid">
        <section className="admin-governance admin-section" aria-labelledby="admin-governance-title">
          <h2 id="admin-governance-title" className="admin-section-title">
            知识空间治理
          </h2>
          <aside className="management-panel admin-panel-surface">
            <KnowledgeSpaceSelector scope="all" onSpaceChange={handleRefresh} />
            <DocumentList />
            <IndexJobList />
            <AuditList />
          </aside>
        </section>
        <section className="admin-users admin-section" aria-labelledby="admin-users-title">
          <h2 id="admin-users-title" className="admin-section-title visually-hidden">
            用户
          </h2>
          <UserManagementPanel />
        </section>
      </div>
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
