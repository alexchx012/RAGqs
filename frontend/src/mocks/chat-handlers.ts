/*
 * 会话与问答 MSW handlers（fe-chat-home 规格 §8；契约《前端接口需求.md》§3、§6.1）。
 * 把 §3.1–§3.6 会话与分组 CRUD、§3.7 提问/停止/重试/恢复（SSE）、§3.8 反馈、§3.9 A/B 投票、
 * §6.1 GET /spaces 接到 MockChatController；prompt-enhancements 输入优化为 mock 环境演示接缝
 * （约 2.5s 延迟返回固定演示文本，供增强动效审查；测试经 enhanceDelayMs=0 跳过）。
 * SSE 流：事件带标准 id 字段 = generation 内单调递增 event_seq（start=1）；
 * 流首发送心跳 comment（`: heartbeat`），不计入事件序号；Last-Event-ID 只重放其后事件。
 * 非 2xx（含 406 streaming_response_required、幂等键冲突等）一律按 §1 HTTP 请求级错误对象返回。
 */

import { delay, http, HttpResponse } from 'msw';
import type { AskRequest } from '../chat/types';
import { MockHttpError, sseEventFrame, type MockChatController } from './chat-contract';

export { MockHttpError, sseEventFrame };

let requestSeq = 0;

function errorResponse(error: unknown) {
  const normalized =
    error instanceof MockHttpError ? error : new MockHttpError(500, 'internal_error');
  requestSeq += 1;
  return HttpResponse.json(
    {
      error: {
        code: normalized.code,
        message: normalized.code,
        details: normalized.details,
        request_id: `req_mock_${requestSeq}`,
      },
    },
    { status: normalized.status },
  );
}

/** 请求明确排除 text/event-stream 时按 406 streaming_response_required 处理（契约 §3.7）。 */
function acceptsEventStream(request: Request): boolean {
  const accept = request.headers.get('Accept');
  if (accept === null || accept === '' || accept === '*/*') {
    return true;
  }
  return accept
    .split(',')
    .some((part) => part.trim().split(';')[0]?.toLowerCase() === 'text/event-stream');
}

function sseFrame(seq: number, event: string, data: Record<string, unknown>): string {
  return `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sseResponse(frames: readonly string[]) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
  return new HttpResponse(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

/** 提问 / 重试共享：校验 Idempotency-Key 与 Accept，然后按事件序列组流（流首心跳 comment）。 */
function eventStreamResponse(events: readonly { seq: number; event: string; data: Record<string, unknown> }[], withHeartbeat: boolean) {
  const frames: string[] = [];
  if (withHeartbeat) {
    frames.push(': heartbeat\n\n');
  }
  for (const event of events) {
    frames.push(sseFrame(event.seq, event.event, event.data));
  }
  return sseResponse(frames);
}

/** M12：运行中 generation 的恢复流——先重放事件，随后保持打开并注册到 controller，
 *  等待可编程补发（pushStopped / stop POST 触发）；心跳注释不计入事件序号。
 *  cancel 必须用与 register 同一 handle 引用，否则 Set.delete 不命中导致泄漏。 */
function liveEventStreamResponse(
  events: readonly { seq: number; event: string; data: Record<string, unknown> }[],
  controller: MockChatController,
  auth: string | null,
  generationId: string,
) {
  const encoder = new TextEncoder();
  let registeredHandle: { push: (frame: string) => void; close: () => void } | null = null;
  const stream = new ReadableStream<Uint8Array>({
    start(streamController) {
      // 重放历史事件
      for (const event of events) {
        streamController.enqueue(encoder.encode(sseFrame(event.seq, event.event, event.data)));
      }
      // 心跳注释（不计入序号）
      streamController.enqueue(encoder.encode(': heartbeat\n\n'));
      const handle = {
        push(frame: string) {
          try {
            streamController.enqueue(encoder.encode(frame));
          } catch {
            // 流已关闭
          }
        },
        close() {
          try {
            streamController.close();
          } catch {
            // 已关闭
          }
        },
      };
      // 保持打开：注册到 controller，由 stop/pushStopped 补发终态并 close
      controller.registerLiveStream(auth, generationId, handle);
      registeredHandle = handle;
    },
    cancel() {
      if (registeredHandle !== null) {
        controller.unregisterLiveStream(generationId, registeredHandle);
        registeredHandle = null;
      }
    },
  });
  return new HttpResponse(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function requireIdempotencyKey(request: Request): string {
  const key = request.headers.get('Idempotency-Key');
  if (key === null) {
    throw new MockHttpError(422, 'validation_error', { field: 'idempotency_key' });
  }
  return key;
}

export function createChatHandlers(controller: MockChatController) {
  return [
    /* ---------- §3.1–§3.5 会话 CRUD ---------- */

    http.get('/v1/conversations', ({ request }) => {
      try {
        const q = new URL(request.url).searchParams.get('q') ?? undefined;
        return HttpResponse.json(controller.listConversations(request.headers.get('Authorization'), q));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/conversations', ({ request }) => {
      try {
        return HttpResponse.json(controller.createConversation(request.headers.get('Authorization')), { status: 201 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/conversations/:id', ({ request, params }) => {
      try {
        return HttpResponse.json(
          controller.getConversation(request.headers.get('Authorization'), String(params['id'])),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.patch('/v1/conversations/:id', async ({ request, params }) => {
      try {
        const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
        const patch = {
          title: typeof body['title'] === 'string' ? body['title'] : undefined,
          pinned: typeof body['pinned'] === 'boolean' ? body['pinned'] : undefined,
          group_id: 'group_id' in body ? (body['group_id'] as string | null) : undefined,
        };
        return HttpResponse.json(
          controller.patchConversation(request.headers.get('Authorization'), String(params['id']), patch),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/conversations/:id', ({ request, params }) => {
      try {
        controller.deleteConversation(request.headers.get('Authorization'), String(params['id']));
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §3.6 会话分组 CRUD ---------- */

    http.post('/v1/conversation-groups', async ({ request }) => {
      try {
        const body = (await request.json().catch(() => ({}))) as { name?: unknown };
        return HttpResponse.json(
          controller.createGroup(request.headers.get('Authorization'), String(body.name ?? '')),
          { status: 201 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.patch('/v1/conversation-groups/:id', async ({ request, params }) => {
      try {
        const body = (await request.json().catch(() => ({}))) as { name?: unknown };
        return HttpResponse.json(
          controller.patchGroup(request.headers.get('Authorization'), String(params['id']), String(body.name ?? '')),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/conversation-groups/:id', ({ request, params }) => {
      try {
        controller.deleteGroup(request.headers.get('Authorization'), String(params['id']));
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §3.7 提问（SSE）/ 停止 / 重试 / 恢复 ---------- */

    http.post('/v1/conversations/:id/messages', async ({ request, params }) => {
      try {
        if (!acceptsEventStream(request)) {
          throw new MockHttpError(406, 'streaming_response_required');
        }
        const idempotencyKey = requireIdempotencyKey(request);
        const body = (await request.json().catch(() => null)) as AskRequest | null;
        if (body === null) {
          throw new MockHttpError(422, 'validation_error', { field: 'body' });
        }
        const result = controller.ask(
          request.headers.get('Authorization'),
          String(params['id']),
          body,
          idempotencyKey,
        );
        return eventStreamResponse(result.events, true);
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/generations/:id/stop', ({ request, params }) => {
      try {
        const result = controller.stopGeneration(
          request.headers.get('Authorization'),
          String(params['id']),
        );
        return HttpResponse.json(result, { status: result.status === 'stop_requested' ? 202 : 200 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/generations/:id/retry', async ({ request, params }) => {
      try {
        if (!acceptsEventStream(request)) {
          throw new MockHttpError(406, 'streaming_response_required');
        }
        const idempotencyKey = requireIdempotencyKey(request);
        const result = controller.retry(
          request.headers.get('Authorization'),
          String(params['id']),
          idempotencyKey,
        );
        return eventStreamResponse(result.events, true);
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/generations/:id/events', ({ request, params }) => {
      try {
        const raw = request.headers.get('Last-Event-ID');
        const lastEventId = raw === null ? null : Number(raw);
        const generationId = String(params['id']);
        const events = controller.listEvents(
          request.headers.get('Authorization'),
          generationId,
          lastEventId,
        );
        // M12：运行中 generation 的恢复流保持打开（心跳 + 可编程补发 stopped）；终态流发完即关
        if (!controller.isRunningGeneration(request.headers.get('Authorization'), generationId)) {
          return eventStreamResponse(events, true);
        }
        return liveEventStreamResponse(events, controller, request.headers.get('Authorization'), generationId);
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §3.8 / §3.9 反馈与 A/B 投票 ---------- */

    http.post('/v1/messages/:id/feedback', async ({ request, params }) => {
      try {
        const idempotencyKey = requireIdempotencyKey(request);
        const body = (await request.json().catch(() => ({}))) as never;
        controller.submitFeedback(
          request.headers.get('Authorization'),
          String(params['id']),
          body,
          idempotencyKey,
        );
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/messages/:id/citation-clicks', async ({ request, params }) => {
      try {
        const body = (await request.json().catch(() => ({}))) as never;
        controller.recordCitationClick(
          request.headers.get('Authorization'),
          String(params['id']),
          body,
        );
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/messages/:id/ab-vote', async ({ request, params }) => {
      try {
        const idempotencyKey = requireIdempotencyKey(request);
        const body = (await request.json().catch(() => ({}))) as never;
        const result = controller.submitAbVote(
          request.headers.get('Authorization'),
          String(params['id']),
          body,
          idempotencyKey,
        );
        return HttpResponse.json(result, { status: 200 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.1 知识空间 ---------- */

    http.get('/v1/spaces', ({ request }) => {
      try {
        const usage = new URL(request.url).searchParams.get('usage') ?? 'manage';
        return HttpResponse.json({ items: controller.listSpaces(request.headers.get('Authorization'), usage as never) });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/spaces/:id/documents', ({ request, params }) => {
      try {
        const q = new URL(request.url).searchParams.get('q') ?? undefined;
        return HttpResponse.json(
          controller.listDocuments(request.headers.get('Authorization'), String(params['id']), q),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- prompt-enhance：输入优化演示（仅 mock 环境；延迟供动效审查，测试置 0 跳过） ---------- */

    http.post('/v1/prompt-enhancements', async ({ request }) => {
      try {
        const body = (await request.json().catch(() => ({}))) as { prompt?: unknown };
        const result = controller.enhancePrompt(
          request.headers.get('Authorization'),
          String(body.prompt ?? ''),
        );
        // 校验通过后才延迟（与真实端点一致：校验错误立即返回）
        if (controller.enhanceDelayMs > 0) {
          await delay(controller.enhanceDelayMs);
        }
        return HttpResponse.json(result);
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}
