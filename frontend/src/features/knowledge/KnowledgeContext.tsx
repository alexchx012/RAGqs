import React, { createContext, useContext, useState, useCallback, useRef } from 'react';
import type { KnowledgeSpace, KnowledgeSpacesData } from '../../api/types';
import { apiJson } from '../../api/client';

function getStoredSpaceId(): string {
  try { return localStorage.getItem('ragSelectedSpaceId') || 'default'; }
  catch { return 'default'; }
}

function spaceIdOfSpace(space: KnowledgeSpace): string {
  return space?.space_id || space?.spaceId || space?.id || '';
}

export interface KnowledgeContextValue {
  selectedSpaceId: string;
  setSelectedSpaceId: (id: string) => void;
  knowledgeSpaces: KnowledgeSpace[];
  /** True after the first successful refreshSpaces(); gates document fetches. */
  spacesReady: boolean;
  refreshSpaces: () => Promise<KnowledgeSpace[]>;
  spaceIdOf: (space: KnowledgeSpace) => string;
}

const KnowledgeContext = createContext<KnowledgeContextValue | null>(null);

export function KnowledgeProvider({ children }: { children: React.ReactNode }) {
  const [selectedSpaceId, setSelectedSpaceIdState] = useState(getStoredSpaceId);
  const [knowledgeSpaces, setKnowledgeSpaces] = useState<KnowledgeSpace[]>([]);
  const [spacesReady, setSpacesReady] = useState(false);
  const refreshRequestIdRef = useRef(0);

  const spaceIdOf = useCallback((space: KnowledgeSpace): string => {
    return spaceIdOfSpace(space) || 'default';
  }, []);

  const setSelectedSpaceId = useCallback((id: string) => {
    const normalized = (id || 'default').trim() || 'default';
    setSelectedSpaceIdState(normalized);
    try { localStorage.setItem('ragSelectedSpaceId', normalized); }
    catch { /* localStorage unavailable */ }
  }, []);

  const refreshSpaces = useCallback(async (): Promise<KnowledgeSpace[]> => {
    const requestId = ++refreshRequestIdRef.current;
    const data = await apiJson<KnowledgeSpacesData>('/knowledge-spaces');
    const spaces = Array.isArray(data.data?.spaces) ? data.data.spaces : [];

    // A newer refreshSpaces() call has already started (or finished) — this
    // response is stale. Skip applying it so it can't clobber fresher state
    // (e.g. a space created after this call started).
    if (requestId !== refreshRequestIdRef.current) {
      return spaces;
    }

    setKnowledgeSpaces(spaces);

    // Correct stale/unauthorized selection only after a successful load.
    setSelectedSpaceIdState((current) => {
      if (spaces.length === 0) {
        try { localStorage.removeItem('ragSelectedSpaceId'); }
        catch { /* localStorage unavailable */ }
        return '';
      }
      const ids = spaces.map(spaceIdOfSpace).filter(Boolean);
      if (current && ids.includes(current)) {
        return current;
      }
      const first = ids[0] || '';
      try { localStorage.setItem('ragSelectedSpaceId', first); }
      catch { /* localStorage unavailable */ }
      return first;
    });

    setSpacesReady(true);
    return spaces;
  }, []);

  return (
    <KnowledgeContext.Provider value={{ selectedSpaceId, setSelectedSpaceId, knowledgeSpaces, spacesReady, refreshSpaces, spaceIdOf }}>
      {children}
    </KnowledgeContext.Provider>
  );
}

export function useKnowledge(): KnowledgeContextValue {
  const ctx = useContext(KnowledgeContext);
  if (!ctx) throw new Error('useKnowledge must be used within KnowledgeProvider');
  return ctx;
}
