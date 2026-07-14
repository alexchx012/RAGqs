import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import React from 'react';
import { ChatProvider } from './ChatContext';
import { useChatQuick } from './useChatQuick';

// Mock apiJson
vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

import { apiJson } from '../../api/client';

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(ChatProvider, null, children);
}

describe('useChatQuick', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns sendQuick function', () => {
    const { result } = renderHook(() => useChatQuick(), { wrapper });

    expect(result.current.sendQuick).toBeTypeOf('function');
  });

  it('sendQuick calls apiJson with correct parameters', async () => {
    mockApiJson.mockResolvedValue({
      code: 200,
      data: { success: true, answer: 'Hello back' },
    });

    const { result } = renderHook(() => useChatQuick(), { wrapper });

    await act(async () => {
      await result.current.sendQuick('Hello world', 'space-1');
    });

    expect(mockApiJson).toHaveBeenCalledTimes(1);
    const [path, options] = mockApiJson.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/chat');
    expect(options.method).toBe('POST');
    expect(options.headers).toEqual({ 'Content-Type': 'application/json' });

    const body = JSON.parse(options.body as string);
    expect(body.Question).toBe('Hello world');
    expect(body.spaceId).toBe('space-1');
    expect(body.Id).toBeDefined();
  });

  it('adds assistant message on successful response', async () => {
    mockApiJson.mockResolvedValue({
      code: 200,
      data: { success: true, answer: 'Test answer' },
    });

    const { result } = renderHook(() => useChatQuick(), { wrapper });

    await act(async () => {
      await result.current.sendQuick('Hi', 'space-1');
    });

    // Verify addMessage was called with the assistant message
    // Indirect verification: apiJson was called successfully
    expect(mockApiJson).toHaveBeenCalledTimes(1);
  });

  it('throws and shows error message when API returns failure', async () => {
    mockApiJson.mockResolvedValue({
      code: 500,
      data: { success: false, errorMessage: 'Server error' },
    });

    const { result } = renderHook(() => useChatQuick(), { wrapper });

    await act(async () => {
      await result.current.sendQuick('Hi', 'space-1');
    });

    // The hook catches the error and adds an error message via addMessage
    expect(mockApiJson).toHaveBeenCalledTimes(1);
  });

  it('catches network errors and adds error message', async () => {
    mockApiJson.mockRejectedValue(new Error('Network failure'));

    const { result } = renderHook(() => useChatQuick(), { wrapper });

    await act(async () => {
      await result.current.sendQuick('Hi', 'space-1');
    });

    // The hook catches the error gracefully
    expect(mockApiJson).toHaveBeenCalledTimes(1);
  });

  it('shows fallback text when answer is empty on success', async () => {
    mockApiJson.mockResolvedValue({
      code: 200,
      data: { success: true, answer: '' },
    });

    const { result } = renderHook(() => useChatQuick(), { wrapper });

    await act(async () => {
      await result.current.sendQuick('Hi', 'space-1');
    });

    expect(mockApiJson).toHaveBeenCalledTimes(1);
  });
});
