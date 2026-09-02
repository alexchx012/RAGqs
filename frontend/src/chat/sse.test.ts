import { describe, expect, it, vi } from 'vitest';
import { openGenerationStream } from './sse';

/*
 * SSE 超时看门狗测试（A14）：fetch-event-source 自身无超时——半开 TCP 连接既不 reject
 * 也不 EOF。验证 timeoutMs 落实为「建连 + 空闲」双超时：
 * - 建连：响应头一直未到 → 到期 abort；
 * - 空闲：契约事件 / 心跳 comment 字节到达即重置，静默超过 timeoutMs 才 abort；
 * - 超时与外部中止都以「流结束」语义返回（generation.ts 既有重连链路的触发条件）。
 */

interface ControllableSse {
  readonly response: Response;
  send(text: string): void;
}

/** 受控 SSE 响应：测试手动 enqueue 字节（事件帧或 comment），不主动关闭。 */
function sseResponse(): ControllableSse {
  let streamController!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
    },
  });
  const encoder = new TextEncoder();
  return {
    response: new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
    send: (text) => streamController.enqueue(encoder.encode(text)),
  };
}

function lastFetchInit(mock: ReturnType<typeof vi.fn<typeof fetch>>): RequestInit {
  const [, init] = mock.mock.calls.at(-1) as [string, RequestInit];
  return init;
}

const START_FRAME =
  'id: 1\nevent: start\ndata: {"generation_id":"g_1","message_id":"m_1","user_message_id":"u_1","attempt_number":1}\n\n';

describe('SSE 超时看门狗（A14）', () => {
  it('建连超时：响应头一直未到 → 到期 abort 底层 fetch signal，流以结束语义 resolve', async () => {
    vi.useFakeTimers();
    try {
      const fetchFn = vi.fn<typeof fetch>(() => new Promise<Response>(() => {}));
      let resolved = false;
      let rejected = false;
      const pending = openGenerationStream({
        path: '/conversations/c_1/messages',
        method: 'POST',
        headers: {},
        token: 'tok_1',
        timeoutMs: 60_000,
        fetchFn,
        onEvent: () => {},
      }).then(
        () => {
          resolved = true;
        },
        () => {
          rejected = true;
        },
      );
      await vi.advanceTimersByTimeAsync(59_999);
      expect(resolved).toBe(false);
      expect((lastFetchInit(fetchFn).signal as AbortSignal).aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect((lastFetchInit(fetchFn).signal as AbortSignal).aborted).toBe(true);
      await pending;
      expect(resolved).toBe(true); // 干净结束语义：generation.ts 按断线重连处理
      expect(rejected).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('空闲超时：心跳 comment 字节到达重置计时；静默超过 timeoutMs 才 abort', async () => {
    vi.useFakeTimers();
    try {
      const sse = sseResponse();
      const fetchFn = vi.fn<typeof fetch>(async () => sse.response);
      const events: string[] = [];
      let resolved = false;
      const pending = openGenerationStream({
        path: '/generations/g_1/events',
        method: 'GET',
        headers: {},
        token: 'tok_1',
        timeoutMs: 60_000,
        fetchFn,
        onEvent: ({ event }) => events.push(event.event),
      }).then(() => {
        resolved = true;
      });
      await vi.advanceTimersByTimeAsync(0);
      sse.send(START_FRAME);
      await vi.advanceTimersByTimeAsync(35_000);
      expect(events).toEqual(['start']); // 契约事件照常解析（包装透传不影响事件流）
      expect(resolved).toBe(false);
      // 健康但静默的流：后端每 30s 心跳 comment，空闲计时持续重置，60s 超时不误杀
      sse.send(': keep-alive\n\n');
      await vi.advanceTimersByTimeAsync(35_000);
      expect(resolved).toBe(false);
      sse.send(': keep-alive\n\n');
      await vi.advanceTimersByTimeAsync(35_000);
      expect(resolved).toBe(false);
      // 半开连接：之后无任何字节 → 超过 60s 空闲窗口 → abort 触发结束语义
      await vi.advanceTimersByTimeAsync(60_001);
      await pending;
      expect(resolved).toBe(true);
      expect((lastFetchInit(fetchFn).signal as AbortSignal).aborted).toBe(true);
      // 心跳 comment 不进 onmessage：仅 start 一条契约事件
      expect(events).toEqual(['start']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('契约事件到达同样重置空闲计时（事件间静默 < timeoutMs 不触发）', async () => {
    vi.useFakeTimers();
    try {
      const sse = sseResponse();
      const fetchFn = vi.fn<typeof fetch>(async () => sse.response);
      const events: string[] = [];
      let resolved = false;
      const pending = openGenerationStream({
        path: '/generations/g_1/events',
        method: 'GET',
        headers: {},
        token: 'tok_1',
        timeoutMs: 60_000,
        fetchFn,
        onEvent: ({ event }) => events.push(event.event),
      }).then(() => {
        resolved = true;
      });
      await vi.advanceTimersByTimeAsync(0);
      sse.send(START_FRAME);
      await vi.advanceTimersByTimeAsync(50_000);
      // 事件间 50s 静默后新事件到达：计时重置，不触发
      sse.send(
        'id: 2\nevent: done\ndata: {"generation_id":"g_1","message_id":"m_1","status":"completed"}\n\n',
      );
      await vi.advanceTimersByTimeAsync(50_000);
      expect(resolved).toBe(false);
      expect(events).toEqual(['start', 'done']);
      void pending;
    } finally {
      vi.useRealTimers();
    }
  });

  it('外部 signal 中止：透传到底层 fetch 并以结束语义 resolve', async () => {
    const fetchFn = vi.fn<typeof fetch>(() => new Promise<Response>(() => {}));
    const external = new AbortController();
    const pending = openGenerationStream({
      path: '/generations/g_1/events',
      method: 'GET',
      headers: {},
      token: 'tok_1',
      fetchFn,
      signal: external.signal,
      onEvent: () => {},
    });
    external.abort();
    expect((lastFetchInit(fetchFn).signal as AbortSignal).aborted).toBe(true);
    await expect(pending).resolves.toBeUndefined();
  });
});
