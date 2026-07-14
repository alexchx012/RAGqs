import React, { useState, useCallback, useEffect } from 'react';
import { useKnowledge } from './KnowledgeContext';
import { apiJson } from '../../api/client';
import type { DocumentRecord, DocumentsData, PanelState } from '../../api/types';

export default function DocumentList() {
  const { selectedSpaceId } = useKnowledge();
  const [panelState, setPanelState] = useState<PanelState<DocumentRecord>>({ status: 'loading' });

  const fetchDocuments = useCallback(async () => {
    setPanelState({ status: 'loading' });
    try {
      const data = await apiJson<DocumentsData>(
        `/knowledge-spaces/${encodeURIComponent(selectedSpaceId)}/documents`,
      );
      const docs = Array.isArray(data.data?.documents) ? data.data.documents : [];
      setPanelState(docs.length === 0 ? { status: 'empty' } : { status: 'ready', items: docs });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '加载文档失败';
      setPanelState({ status: 'error', message });
    }
  }, [selectedSpaceId]);

  useEffect(() => { fetchDocuments(); }, [fetchDocuments]);

  const handleDelete = useCallback(async (documentId: string) => {
    try {
      await apiJson(
        `/knowledge-spaces/${encodeURIComponent(selectedSpaceId)}/documents/${encodeURIComponent(documentId)}`,
        { method: 'DELETE' },
      );
      await fetchDocuments();
    } catch { /* error shown on next fetch */ }
  }, [selectedSpaceId, fetchDocuments]);

  const handleRebuild = useCallback(async (documentId: string) => {
    try {
      await apiJson(
        `/knowledge-spaces/${encodeURIComponent(selectedSpaceId)}/documents/${encodeURIComponent(documentId)}/rebuild`,
        { method: 'POST' },
      );
      await fetchDocuments();
    } catch { /* error shown on next fetch */ }
  }, [selectedSpaceId, fetchDocuments]);

  return (
    <section className="ops-section">
      <div className="ops-section-header">
        <span>文档</span>
        <button type="button" className="ops-icon-btn" title="刷新文档" onClick={fetchDocuments}>↻</button>
      </div>
      <div className="ops-list">
        {panelState.status === 'loading' && <div>加载中...</div>}
        {panelState.status === 'error' && (
          <div style={{ color: '#b3261e' }}>
            加载失败: {panelState.message}{' '}
            <button type="button" onClick={fetchDocuments} style={{ background: 'none', border: 'none', color: '#1a73e8', cursor: 'pointer', textDecoration: 'underline' }}>↻ 重试</button>
          </div>
        )}
        {panelState.status === 'empty' && <div>暂无文档</div>}
        {panelState.status === 'ready' && panelState.items.map(doc => (
          <div key={doc.document_id} className="ops-row">
            <div className="ops-row-main">{doc.file_name || doc.document_id} · {doc.status || 'unknown'}</div>
            <div className="ops-row-meta">{doc.indexed_chunks ?? 0}/{doc.total_chunks ?? 0} chunks</div>
            <div className="ops-row-actions">
              <button type="button" onClick={() => handleRebuild(doc.document_id)}>重建</button>
              <button type="button" onClick={() => handleDelete(doc.document_id)}>删除</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
