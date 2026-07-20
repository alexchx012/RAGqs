import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import React from 'react';
import { ChatProvider, useChat } from './ChatContext';
import { useChatStream } from './useChatStream';

// Mock @microsoft/fetch-event-source
vi.mock('@microsoft/fetch-event-source', () => ({
  fetchEventSource: vi.fn(),
}));

import { fetchEventSource } from '@microsoft/fetch-event-source';

const mockFetchEventSource = fetchEventSource as unknown as ReturnType<typeof vi.fn>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type FetchEventSourceOptions = Record<string, any>;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(ChatProvider, null, children);
}

describe('useChatStream', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns sendStream and abort functions', () => {
    const { result } = renderHook(() => useChatStream(), { wrapper });

    expect(result.current.sendStream).toBeTypeOf('function');
    expect(result.current.abort).toBeTypeOf('function');
  });

  it('sendStream calls fetchEventSource with correct parameters', async () => {
    mockFetchEventSource.mockImplementation(
      (_url: string, _options: FetchEventSourceOptions) => Promise.resolve(),
    );

    const { result } = renderHook(() => useChatStream(), { wrapper });
    const onError = vi.fn();

    await act(async () => {
      await result.current.sendStream('Hello world', 'space-1', onError);
    });

    expect(mockFetchEventSource).toHaveBeenCalledTimes(1);
    const callArgs = mockFetchEventSource.mock.calls[0] as [string, FetchEventSourceOptions];
    expect(callArgs[0]).toBe('/api/chat_stream');
    expect(callArgs[1].method).toBe('POST');
    expect(callArgs[1].headers).toEqual({ 'Content-Type': 'application/json' });

    const body = JSON.parse(callArgs[1].body as string);
    expect(body.Question).toBe('Hello world');
    expect(body.spaceId).toBe('space-1');
    expect(body.Id).toBeDefined();
  });

  it('accumulates content chunks and calls addMessage on completion', async () => {
    mockFetchEventSource.mockImplementation(
      async (_url: string, options: FetchEventSourceOptions) => {
        await options.onopen({ ok: true } as Response);
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'content', data: 'Hello ' }),
        });
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'content', data: 'World' }),
        });
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'done' }),
        });
      },
    );

    const { result } = renderHook(() => useChatStream(), { wrapper });
    const onError = vi.fn();

    await act(async () => {
      await result.current.sendStream('Hi', 'space-1', onError);
    });

    expect(onError).not.toHaveBeenCalled();
  });

  it('calls onError when HTTP response is not ok', async () => {
    mockFetchEventSource.mockImplementation(
      async (_url: string, options: FetchEventSourceOptions) => {
        await options.onopen({
          ok: false,
          status: 500,
          text: async () => 'Internal Server Error',
        } as unknown as Response);
      },
    );

    const { result } = renderHook(() => useChatStream(), { wrapper });
    const onError = vi.fn();

    await act(async () => {
      await result.current.sendStream('Hi', 'space-1', onError);
    });

    expect(onError).toHaveBeenCalled();
  });

  it('calls onError when stream emits error type message', async () => {
    mockFetchEventSource.mockImplementation(
      async (_url: string, options: FetchEventSourceOptions) => {
        await options.onopen({ ok: true } as Response);
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'error', data: 'Server processing error' }),
        });
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'done' }),
        });
      },
    );

    const { result } = renderHook(() => useChatStream(), { wrapper });
    const onError = vi.fn();

    await act(async () => {
      await result.current.sendStream('Hi', 'space-1', onError);
    });

    expect(onError).toHaveBeenCalledWith('Server processing error');
  });

  it('abort cancels the active AbortController', async () => {
    let capturedSignal: AbortSignal | undefined;

    mockFetchEventSource.mockImplementation(
      async (_url: string, options: FetchEventSourceOptions) => {
        capturedSignal = options.signal as AbortSignal;
        await (options.onopen as (r: Response) => void)({ ok: true } as Response);
        return new Promise(() => {}); // never resolves
      },
    );

    const { result } = renderHook(() => useChatStream(), { wrapper });
    const onError = vi.fn();

    act(() => {
      result.current.sendStream('Hi', 'space-1', onError);
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(capturedSignal).toBeDefined();
    expect(capturedSignal!.aborted).toBe(false);

    act(() => {
      result.current.abort();
    });

    expect(capturedSignal!.aborted).toBe(true);
  });

  it('stores answerMode and usedToolsWithoutKnowledgeBase on the current assistant message', async () => {
    mockFetchEventSource.mockImplementation(
      async (_url: string, options: FetchEventSourceOptions) => {
        await options.onopen({ ok: true } as Response);
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'content', data: '测试回答' }),
        });
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({
            type: 'answer_mode',
            data: {
              mode: 'grounded',
              usedToolsWithoutKnowledgeBase: false,
            },
          }),
        });
        (options.onmessage as (ev: { data: string }) => void)({
          data: JSON.stringify({ type: 'done' }),
        });
      },
    );

    const { result } = renderHook(
      () => ({
        stream: useChatStream(),
        chat: useChat(),
      }),
      { wrapper },
    );
    const onError = vi.fn();

    await act(async () => {
      await result.current.stream.sendStream('Hi', 'space-1', onError);
    });

    expect(onError).not.toHaveBeenCalled();

    const assistantMessages = result.current.chat.currentChatHistory.filter(
      (msg) => msg.type === 'assistant',
    );
    expect(assistantMessages).toHaveLength(1);
    expect(assistantMessages[0]).toMatchObject({
      type: 'assistant',
      content: '测试回答',
      answerMode: 'grounded',
      usedToolsWithoutKnowledgeBase: false,
    });
  });
});
