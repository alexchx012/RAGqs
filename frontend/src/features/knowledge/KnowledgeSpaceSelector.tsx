import React, { useState, useCallback } from 'react';
import { useKnowledge } from './KnowledgeContext';
import { apiJson } from '../../api/client';

interface Props {
  onSpaceChange: () => void;
  scope: 'own' | 'all';
}

export default function KnowledgeSpaceSelector({ onSpaceChange, scope }: Props) {
  const { selectedSpaceId, setSelectedSpaceId, knowledgeSpaces, refreshSpaces, spaceIdOf } = useKnowledge();
  const [newSpaceId, setNewSpaceId] = useState('');
  const [newSpaceName, setNewSpaceName] = useState('');
  const [statusMsg, setStatusMsg] = useState('');
  const [statusType, setStatusType] = useState<'info' | 'success' | 'error'>('info');

  const handleSelect = useCallback(async (e: React.ChangeEvent<HTMLSelectElement>) => {
    setSelectedSpaceId(e.target.value);
    onSpaceChange();
  }, [setSelectedSpaceId, onSpaceChange]);

  const handleCreate = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const sid = newSpaceId.trim();
    if (!sid) return;
    const name = newSpaceName.trim() || sid;
    try {
      await apiJson('/knowledge-spaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ space_id: sid, name }),
      });
      setSelectedSpaceId(sid);
      setNewSpaceId('');
      setNewSpaceName('');
      await refreshSpaces();
      setStatusMsg(`知识空间 ${sid} 已创建`);
      setStatusType('success');
      onSpaceChange();
    } catch (err: unknown) {
      setStatusMsg(err instanceof Error ? err.message : '创建失败');
      setStatusType('error');
    }
  }, [newSpaceId, newSpaceName, setSelectedSpaceId, refreshSpaces, onSpaceChange]);

  const handleRefresh = useCallback(async () => {
    setStatusMsg('');
    try {
      const spaces = await refreshSpaces();
      if (spaces.length > 0 && !spaces.some(s => spaceIdOf(s) === selectedSpaceId)) {
        setSelectedSpaceId(spaceIdOf(spaces[0]));
        onSpaceChange();
      }
    } catch (err: unknown) {
      setStatusMsg(err instanceof Error ? err.message : '刷新失败');
      setStatusType('error');
    }
  }, [refreshSpaces, selectedSpaceId, spaceIdOf, setSelectedSpaceId, onSpaceChange]);

  return (
    <section className="ops-section">
      <div className="ops-section-header">
        <span>知识空间</span>
        <button type="button" className="ops-icon-btn" title="刷新知识空间" onClick={handleRefresh}>↻</button>
      </div>
      <select
        className="space-selector"
        value={knowledgeSpaces.length === 0 ? '' : selectedSpaceId}
        onChange={handleSelect}
        disabled={knowledgeSpaces.length === 0}
      >
        {knowledgeSpaces.length === 0 ? (
          <option value="">暂无可用知识空间</option>
        ) : (
          knowledgeSpaces.map(space => (
            <option key={spaceIdOf(space)} value={spaceIdOf(space)}>{space.name || spaceIdOf(space)}</option>
          ))
        )}
      </select>
      {scope === 'all' && (
        <form className="space-form" onSubmit={handleCreate}>
          <input type="text" placeholder="space id" value={newSpaceId} onChange={e => setNewSpaceId(e.target.value)} />
          <input type="text" placeholder="显示名称" value={newSpaceName} onChange={e => setNewSpaceName(e.target.value)} />
          <button type="submit">创建</button>
        </form>
      )}
      {statusMsg && <div className={`management-status ${statusType}`} style={{ marginTop: 8 }}>{statusMsg}</div>}
    </section>
  );
}
