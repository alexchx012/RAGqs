import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, render, screen } from '@testing-library/react';
import React from 'react';

// Mock the api client before importing the module under test
vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

import { apiJson } from '../../api/client';
import {
  KnowledgeProvider,
  useKnowledge,
} from './KnowledgeContext';
import type { KnowledgeSpace } from '../../api/types';

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(KnowledgeProvider, null, children);
}

describe('KnowledgeContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('useKnowledge', () => {
    it('throws when used outside KnowledgeProvider', () => {
      // Suppress console.error for this expected error test
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

      expect(() => {
        renderHook(() => useKnowledge());
      }).toThrow('useKnowledge must be used within KnowledgeProvider');

      consoleSpy.mockRestore();
    });

    it('provides selectedSpaceId, setSelectedSpaceId, knowledgeSpaces, refreshSpaces, and spaceIdOf', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      expect(result.current).toBeDefined();
      expect(result.current.selectedSpaceId).toBeDefined();
      expect(result.current.setSelectedSpaceId).toBeTypeOf('function');
      expect(result.current.knowledgeSpaces).toEqual([]);
      expect(result.current.refreshSpaces).toBeTypeOf('function');
      expect(result.current.spaceIdOf).toBeTypeOf('function');
    });

    it('defaults selectedSpaceId to "default" when localStorage is empty', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      expect(result.current.selectedSpaceId).toBe('default');
    });

    it('reads selectedSpaceId from localStorage on initialization', () => {
      localStorage.setItem('ragSelectedSpaceId', 'custom-space');

      const { result } = renderHook(() => useKnowledge(), { wrapper });

      expect(result.current.selectedSpaceId).toBe('custom-space');
    });
  });

  describe('setSelectedSpaceId', () => {
    it('updates selectedSpaceId and persists to localStorage', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      act(() => {
        result.current.setSelectedSpaceId('my-space');
      });

      expect(result.current.selectedSpaceId).toBe('my-space');
      expect(localStorage.getItem('ragSelectedSpaceId')).toBe('my-space');
    });

    it('normalizes empty or falsy values to "default"', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      act(() => {
        result.current.setSelectedSpaceId('');
      });

      expect(result.current.selectedSpaceId).toBe('default');
      expect(localStorage.getItem('ragSelectedSpaceId')).toBe('default');
    });
  });

  describe('spaceIdOf', () => {
    it('extracts space_id from a KnowledgeSpace object', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      const space: KnowledgeSpace = {
        space_id: 'sp-001',
        name: 'Test Space',
      };

      expect(result.current.spaceIdOf(space)).toBe('sp-001');
    });

    it('falls back to spaceId when space_id is absent', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      const space: KnowledgeSpace = {
        space_id: '',
        name: 'Test Space',
        spaceId: 'fallback-id',
      };

      expect(result.current.spaceIdOf(space)).toBe('fallback-id');
    });

    it('falls back to id when space_id and spaceId are absent', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      const space: KnowledgeSpace = {
        space_id: '',
        name: 'Test Space',
        id: 'another-id',
      };

      expect(result.current.spaceIdOf(space)).toBe('another-id');
    });

    it('returns "default" when no id field is present', () => {
      const { result } = renderHook(() => useKnowledge(), { wrapper });

      const space: KnowledgeSpace = {
        space_id: '',
        name: 'Test Space',
      };

      expect(result.current.spaceIdOf(space)).toBe('default');
    });
  });

  describe('refreshSpaces', () => {
    it('calls apiJson with /knowledge-spaces and updates knowledgeSpaces', async () => {
      const mockSpaces: KnowledgeSpace[] = [
        { space_id: 'sp-1', name: 'Space 1' },
        { space_id: 'sp-2', name: 'Space 2' },
      ];

      mockApiJson.mockResolvedValue({
        code: 200,
        data: { spaces: mockSpaces },
      });

      const { result } = renderHook(() => useKnowledge(), { wrapper });

      let returnedSpaces: KnowledgeSpace[] = [];
      await act(async () => {
        returnedSpaces = await result.current.refreshSpaces();
      });

      expect(mockApiJson).toHaveBeenCalledWith('/knowledge-spaces');
      expect(result.current.knowledgeSpaces).toEqual(mockSpaces);
      expect(returnedSpaces).toEqual(mockSpaces);
    });

    it('handles null/undefined data gracefully', async () => {
      mockApiJson.mockResolvedValue({
        code: 200,
        data: null,
      });

      const { result } = renderHook(() => useKnowledge(), { wrapper });

      await act(async () => {
        await result.current.refreshSpaces();
      });

      expect(result.current.knowledgeSpaces).toEqual([]);
    });

    it('handles missing spaces field gracefully', async () => {
      mockApiJson.mockResolvedValue({
        code: 200,
        data: {},
      });

      const { result } = renderHook(() => useKnowledge(), { wrapper });

      await act(async () => {
        await result.current.refreshSpaces();
      });

      expect(result.current.knowledgeSpaces).toEqual([]);
    });
  });

  describe('KnowledgeProvider', () => {
    it('renders children', () => {
      render(
        <KnowledgeProvider>
          <div data-testid="child">Hello</div>
        </KnowledgeProvider>,
      );

      expect(screen.getByTestId('child')).toBeDefined();
    });
  });
});
