import React, { createContext, useContext, useState, useCallback } from 'react';
import type { KnowledgeSpace, KnowledgeSpacesData } from '../../api/types';
import { apiJson } from '../../api/client';

function getStoredSpaceId(): string {
  try { return localStorage.getItem('ragSelectedSpaceId') || 'default'; }
  catch { return 'default'; }
}

export interface KnowledgeContextValue {
  selectedSpaceId: string;
  setSelectedSpaceId: (id: string) => void;
  knowledgeSpaces: KnowledgeSpace[];
  refreshSpaces: () => Promise<KnowledgeSpace[]>;
  spaceIdOf: (space: KnowledgeSpace) => string;
}

const KnowledgeContext = createContext<KnowledgeContextValue | null>(null);

export function KnowledgeProvider({ children }: { children: React.ReactNode }) {
  const [selectedSpaceId, setSelectedSpaceIdState] = useState(getStoredSpaceId);
  const [knowledgeSpaces, setKnowledgeSpaces] = useState<KnowledgeSpace[]>([]);

  const spaceIdOf = useCallback((space: KnowledgeSpace): string => {
    return space?.space_id || space?.spaceId || space?.id || 'default';
  }, []);

  const setSelectedSpaceId = useCallback((id: string) => {
    const normalized = (id || 'default').trim() || 'default';
    setSelectedSpaceIdState(normalized);
    try { localStorage.setItem('ragSelectedSpaceId', normalized); }
    catch { /* localStorage unavailable */ }
  }, []);

  const refreshSpaces = useCallback(async (): Promise<KnowledgeSpace[]> => {
    const data = await apiJson<KnowledgeSpacesData>('/knowledge-spaces');
    const spaces = Array.isArray(data.data?.spaces) ? data.data.spaces : [];
    setKnowledgeSpaces(spaces);
    return spaces;
  }, []);

  return (
    <KnowledgeContext.Provider value={{ selectedSpaceId, setSelectedSpaceId, knowledgeSpaces, refreshSpaces, spaceIdOf }}>
      {children}
    </KnowledgeContext.Provider>
  );
}

export function useKnowledge(): KnowledgeContextValue {
  const ctx = useContext(KnowledgeContext);
  if (!ctx) throw new Error('useKnowledge must be used within KnowledgeProvider');
  return ctx;
}
