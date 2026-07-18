import React, { useState, useCallback, useEffect, useRef } from 'react';
import { apiJson } from '../../api/client';
import type { IndexJob, IndexJobsData, PanelState } from '../../api/types';

export default function IndexJobList() {
  const [panelState, setPanelState] = useState<PanelState<IndexJob>>({ status: 'loading' });
  const latestRequestRef = useRef(0);

  const fetchJobs = useCallback(async () => {
    const requestId = ++latestRequestRef.current;
    setPanelState({ status: 'loading' });
    try {
      const data = await apiJson<IndexJobsData>('/index-jobs');
      if (requestId !== latestRequestRef.current) return;
      const jobs = Array.isArray(data.data?.jobs) ? data.data.jobs : [];
      setPanelState(jobs.length === 0 ? { status: 'empty' } : { status: 'ready', items: jobs });
    } catch (err: unknown) {
      if (requestId !== latestRequestRef.current) return;
      const message = err instanceof Error ? err.message : '加载索引任务失败';
      setPanelState({ status: 'error', message });
    }
  }, []);

  useEffect(() => { fetchJobs(); }, [fetchJobs]);

  const handleRetry = useCallback(async (jobId: string) => {
    try {
      await apiJson(`/index-jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' });
      await fetchJobs();
    } catch { /* error shown on next fetch */ }
  }, [fetchJobs]);

  return (
    <section className="ops-section">
      <div className="ops-section-header">
        <span>索引任务</span>
        <button type="button" className="ops-icon-btn" title="刷新任务" onClick={fetchJobs}>↻</button>
      </div>
      <div className="ops-list">
        {panelState.status === 'loading' && <div>加载中...</div>}
        {panelState.status === 'error' && (
          <div style={{ color: '#b3261e' }}>
            加载失败: {panelState.message}{' '}
            <button type="button" onClick={fetchJobs} style={{ background: 'none', border: 'none', color: '#1a73e8', cursor: 'pointer', textDecoration: 'underline' }}>↻ 重试</button>
          </div>
        )}
        {panelState.status === 'empty' && <div>暂无索引任务</div>}
        {panelState.status === 'ready' && panelState.items.map(job => (
          <div key={job.job_id} className="ops-row">
            <div className="ops-row-main">{job.job_id} · {job.status}</div>
            <div className="ops-row-meta">{job.document_id || job.source_path || ''}</div>
            <div className="ops-row-actions">
              <button type="button" onClick={() => handleRetry(job.job_id)}>重试</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
