import React, { useState, useCallback, useEffect, useRef } from 'react';
import { useKnowledge } from './KnowledgeContext';
import { apiJson } from '../../api/client';
import type { AuditRecord, AuditData, PanelState } from '../../api/types';

export default function AuditList() {
  const { selectedSpaceId } = useKnowledge();
  const [panelState, setPanelState] = useState<PanelState<AuditRecord>>({ status: 'loading' });
  const latestRequestRef = useRef(0);

  const fetchAudits = useCallback(async () => {
    const requestId = ++latestRequestRef.current;
    setPanelState({ status: 'loading' });
    try {
      const data = await apiJson<AuditData>(`/chat/audits?space_id=${encodeURIComponent(selectedSpaceId)}`);
      if (requestId !== latestRequestRef.current) return;
      const audits = Array.isArray(data.data?.audits) ? data.data.audits : [];
      setPanelState(audits.length === 0 ? { status: 'empty' } : { status: 'ready', items: audits });
    } catch (err: unknown) {
      if (requestId !== latestRequestRef.current) return;
      const message = err instanceof Error ? err.message : '加载审计记录失败';
      setPanelState({ status: 'error', message });
    }
  }, [selectedSpaceId]);

  useEffect(() => { fetchAudits(); }, [fetchAudits]);

  return (
    <section className="ops-section">
      <div className="ops-section-header">
        <span>检索审计</span>
        <button type="button" className="ops-icon-btn" title="刷新审计" onClick={fetchAudits}>↻</button>
      </div>
      <div className="ops-list">
        {panelState.status === 'loading' && <div>加载中...</div>}
        {panelState.status === 'error' && (
          <div style={{ color: '#b3261e' }}>
            加载失败: {panelState.message}{' '}
            <button type="button" onClick={fetchAudits} style={{ background: 'none', border: 'none', color: '#1a73e8', cursor: 'pointer', textDecoration: 'underline' }}>↻ 重试</button>
          </div>
        )}
        {panelState.status === 'empty' && <div>暂无审计记录</div>}
        {panelState.status === 'ready' && panelState.items.map((audit, i) => (
          <div key={audit.id || audit.traceId || String(i)} className="ops-row">
            <div className="ops-row-main">{audit.question || audit.traceId || audit.id || 'audit'}</div>
            <div className="ops-row-meta">{(audit.sources || []).length} sources · {audit.createdAt || ''}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
