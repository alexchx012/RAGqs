/*
 * SSE 客户端（契约 §3.7；spec §7）。
 * 基于 @microsoft/fetch-event-source 的 fetch-stream，可携带 Bearer（原生 EventSource 不能），
 * 支持 POST 请求体与自定义请求头（Idempotency-Key / Last-Event-ID）。
 * 超时看门狗（A14）：fetch-event-source 无任何超时——半开 TCP 连接（NAT/断网静默死亡）既不
 * reject 也不 EOF，重连链路依赖「流结束」因此永不触发。此处落实 timeoutMs：fetch 发起即计时
 * （覆盖建连），收到响应头、任一流字节或契约事件即重置；超时 abort 底层 fetch，流以结束语义
 * 返回，交由 generation.ts 既有重连链路恢复（重连带 Last-Event-ID 续传，无重复事件）。
 * 心跳语义选择：空闲计时按「comment 也重置」——后端流中继每 30s 下发 `: keep-alive` comment，
 * 而契约事件之间可静默数分钟（think/deep 各阶段由 worker 异步落库，事件到流有间隔）；
 * 若只按契约事件重置，健康的长静默生成会被 60s 误杀成重连循环直至 reconnect_failed。
 * comment 在 fetch-event-source 解析层被丢弃、不进 onmessage，故经 fetch 包装在字节层感知：
 * 任何到达的字节（comment 或事件帧）都证明链路在传送，一律重置计时。默认 60s > 2×30s 心跳间隔，
 * 丢一次心跳仍不误判。
 * 错误语义（契约 §3.7）：建连前任何非 2xx / 非 text/event-stream 响应按 HTTP 请求级错误处理——
 * 本封装在 onerror 内抛错使 promise reject，不伪装为 SSE 事件、不自动重试（重试/退避/refresh
 * 由调用方接管）；401 同理由调用方走会话层 refresh。仅成功建立的流逐事件回调 onEvent。
 */

import { fetchEventSource, type EventSourceMessage } from '@microsoft/fetch-event-source';
import { resolveUrl } from '../api/client';
import { normalizeApiError } from '../api/errors';
import type { SseGenerationEvent } from './types';

export interface SseStreamOptions {
  /** 相对 /v1 前缀路径，如 /conversations/c_1/messages。 */
  readonly path: string;
  readonly method?: 'POST' | 'GET';
  readonly body?: unknown;
  readonly headers: Record<string, string>;
  readonly token: string;
  /** 收到 open（HTTP 2xx + text/event-stream）时回调。 */
  readonly onOpen?: () => void;
  /** 每条已解析的契约事件（含标准 id 字段 = event_seq；心跳 comment 无 id → null）。 */
  readonly onEvent: (message: SseEventMessage) => void;
  /** 连接被服务端关闭（或 fetch 失败）时回调，交由调用方决定重连。 */
  readonly onError?: (cause: unknown) => void;
  /**
   * 建连与流空闲超时（默认 60s）：fetch 发起即计时，响应头/任一流字节（含心跳 comment）/
   * 契约事件到达即重置；超时 abort 连接、流按结束语义返回，由调用方重连。
   * 默认值必须大于后端心跳间隔（30s），否则健康连接会被误判。
   */
  readonly timeoutMs?: number;
  readonly signal?: AbortSignal;
  /** 测试注入：默认 window.fetch。 */
  readonly fetchFn?: typeof fetch;
}

/** 已解析的 SSE 事件：id 为 generation 内单调递增 event_seq（start=1；心跳 comment 不计序号 → null）。 */
export interface SseEventMessage {
  readonly id: number | null;
  readonly event: SseGenerationEvent;
}

/** 未识别的 SSE 事件（§1 SSE 兼容性：未知事件忽略处理）。 */
export function isGenerationEvent(event: EventSourceMessage): boolean {
  return (
    event.event === 'start' ||
    event.event === 'stage' ||
    event.event === 'step' ||
    event.event === 'notice' ||
    event.event === 'ab_start' ||
    event.event === 'delta' ||
    event.event === 'answer' ||
    event.event === 'done' ||
    event.event === 'error' ||
    event.event === 'stopped'
  );
}

/** 建连与流空闲超时默认值（A14）：> 2× 后端心跳间隔 30s，丢一次心跳不误判。 */
const DEFAULT_SSE_IDLE_TIMEOUT_MS = 60_000;

export async function openGenerationStream(options: SseStreamOptions): Promise<void> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_SSE_IDLE_TIMEOUT_MS;
  // 超时看门狗与外部 signal 共用同一 controller：任一触发即中止底层 fetch
  const controller = new AbortController();
  const externalSignal = options.signal;
  const onExternalAbort = () => controller.abort();
  if (externalSignal !== undefined) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', onExternalAbort, { once: true });
    }
  }
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  const clearIdleTimer = () => {
    if (idleTimer !== undefined) {
      clearTimeout(idleTimer);
      idleTimer = undefined;
    }
  };
  const rearmIdleTimer = () => {
    clearIdleTimer();
    idleTimer = setTimeout(() => controller.abort(), timeoutMs);
  };
  rearmIdleTimer(); // 建连计时起点：响应头到达前一直有效
  // 字节层活动感知（心跳语义）：包装注入/默认 fetch，响应体经透传 TransformStream，
  // 任一 chunk 到达即重置空闲计时——comment 与事件帧都证明链路在传送
  const watchedFetch: typeof fetch = async (input, init) => {
    const response = await (options.fetchFn ?? fetch)(input, init);
    if (!response.ok || response.body === null) {
      return response; // 错误响应经 onopen 归一化上抛，无需活动感知
    }
    rearmIdleTimer(); // 响应头到达：建连完成，空闲窗口重新起算
    const activity = new TransformStream<Uint8Array, Uint8Array>({
      transform: (chunk, passthrough) => {
        rearmIdleTimer();
        passthrough.enqueue(chunk);
      },
    });
    return new Response(response.body.pipeThrough(activity), {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };
  const headers: Record<string, string> = {
    ...options.headers,
    // 小写键：fetch-event-source 检查的是 headers.accept，避免双写同名头
    accept: 'text/event-stream',
    Authorization: `Bearer ${options.token}`,
  };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  try {
    await fetchEventSource(resolveUrl(`/v1${options.path}`), {
      method: options.method ?? 'POST',
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
      openWhenHidden: true,
      fetch: watchedFetch,
      onopen: async (response) => {
        if (!response.ok) {
          // 建连前任何非 2xx 按 §1 HTTP 请求级错误对象处理（406 streaming_response_required、
          // 409 idempotency_key_conflict、认证 401 等），与 JSON 端点同一归一化；
          // 401 由调用方走会话层 refresh
          const body: unknown = await response.json().catch(() => null);
          throw normalizeApiError(body, response.status);
        }
        const contentType = response.headers.get('Content-Type')?.split(';')[0]?.trim().toLowerCase();
        if (contentType !== 'text/event-stream') {
          throw new Error('sse_open_failed:not_event_stream');
        }
        options.onOpen?.();
      },
      onmessage: (message) => {
        rearmIdleTimer(); // 契约事件到达：空闲窗口重置（与字节层重置冗余，语义显式化）
        if (!isGenerationEvent(message)) {
          return; // 未知事件忽略；心跳 comment 无 event 名，不进入此处
        }
        try {
          const rawId = Number(message.id);
          const id = message.id !== null && message.id !== undefined && Number.isFinite(rawId) ? rawId : null;
          options.onEvent({
            id,
            // 判别联合的 event/data 相互关联，无法分别断言后组合；整体一次断言保留类型可见性
            event: { event: message.event, data: JSON.parse(message.data) } as SseGenerationEvent,
          });
        } catch {
          // 数据非 JSON：忽略该事件（契约未知字段忽略规则）
        }
      },
      onerror: (error) => {
        options.onError?.(error);
        // 抛错使 promise reject：非 2xx / 网络失败均按请求级错误上抛，不自动重试
        throw error;
      },
    });
  } finally {
    clearIdleTimer();
    externalSignal?.removeEventListener('abort', onExternalAbort);
  }
}
