/*
 * SSE 客户端（契约 §3.7；spec §7）。
 * 基于 @microsoft/fetch-event-source 的 fetch-stream，可携带 Bearer（原生 EventSource 不能），
 * 支持 POST 请求体与自定义请求头（Idempotency-Key / Last-Event-ID）。
 * 本批只封装「发起请求拿到流」的粒度：open/onmessage/onerror 事件消费在 Batch B 落地。
 * 错误语义（契约 §3.7）：建连前任何非 2xx / 非 text/event-stream 响应按 HTTP 请求级错误处理——
 * 本封装在 onerror 内抛错使 promise reject，不伪装为 SSE 事件、不自动重试（重试/退避/refresh
 * 由 Batch B 的调用方接管）；401 同理由调用方走会话层 refresh。仅成功建立的流逐事件回调 onEvent。
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
  /** 默认 60s。 */
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
    event.event === 'answer' ||
    event.event === 'done' ||
    event.event === 'error' ||
    event.event === 'stopped'
  );
}

export async function openGenerationStream(options: SseStreamOptions): Promise<void> {
  const headers: Record<string, string> = {
    ...options.headers,
    // 小写键：fetch-event-source 检查的是 headers.accept，避免双写同名头
    accept: 'text/event-stream',
    Authorization: `Bearer ${options.token}`,
  };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  await fetchEventSource(resolveUrl(`/v1${options.path}`), {
    method: options.method ?? 'POST',
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
    openWhenHidden: true,
    fetch: options.fetchFn,
    onopen: async (response) => {
      if (!response.ok) {
        // 建连前任何非 2xx 按 §1 HTTP 请求级错误对象处理（406 streaming_response_required、
        // 409 idempotency_key_conflict、认证 401 等），与 JSON 端点同一归一化；
        // 401 由 Batch B 调用方走会话层 refresh
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
}
