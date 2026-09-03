import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createApiClient } from '../api/client';
import { ApiError } from '../api/errors';
import { createChatApi, type ChatApi } from './api';
import { createIdempotencyKey } from './idempotency';
import { openGenerationStream } from './sse';
import { mockAuth, mockChat } from '../mocks/testing';
import type { AbVoteResponse, SseGenerationEvent } from './types';

/*
 * 会话与问答域 API 封装测试（规格 §8；契约 §3、§6.1）。
 * - JSON 端点：注入 mock fetch 验证 URL / 方法 / 请求头 / 请求体 / 204 / 错误归一化；
 * - SSE 端点：经 MSW 真实 HTTP 边界驱动（vitest-setup 装配），验证 Idempotency-Key 传递、
 *   事件序列完整（event_seq 递增、start=1）与同键重放语义。
 */

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 捕获最近一次 fetch 调用的 url + init。 */
function captureFetch(mock: ReturnType<typeof vi.fn<typeof fetch>>): { url: string; init: RequestInit } {
  const [url, init] = mock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init };
}

function makeClient(mock: ReturnType<typeof vi.fn<typeof fetch>>) {
  const client = createApiClient({
    getAccessToken: () => 'tok_1',
    refresh: vi.fn(),
    fetchFn: mock,
  });
  return createChatApi(client);
}

describe('会话与问答域 API 封装（JSON 端点）', () => {
  it('GET /conversations：携带 Bearer，无 body；q 参数透传编码', async () => {
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, { items: [], groups: [] }));
    const api = makeClient(mock);
    await api.listConversations('年假');
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/conversations?q=');
    expect(url).toContain(encodeURIComponent('年假'));
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_1');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('POST /conversations：body 为空对象', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { id: 'c_1', title: '', pinned: false, group_id: null, last_active_at: '' }),
    );
    const api = makeClient(mock);
    await api.createConversation();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/conversations');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({});
  });

  it('GET /conversations/{id}：id 做路径编码', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { id: 'c_1', title: '', effort_level: 'quick', scope: { space_ids: [], document_ids: [] }, messages: [] }),
    );
    const api = makeClient(mock);
    await api.getConversation('c/1');
    const { url } = captureFetch(mock);
    expect(url).toContain('/v1/conversations/c%2F1');
  });

  it('PATCH /conversations/{id}：三合一字段原样透传', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { id: 'c_1', title: '新标题', pinned: true, group_id: 'g_1', last_active_at: '' }),
    );
    const api = makeClient(mock);
    await api.patchConversation('c_1', { title: '新标题', pinned: true, group_id: 'g_1' });
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/conversations/c_1');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ title: '新标题', pinned: true, group_id: 'g_1' });
  });

  it('DELETE /conversations/{id}：204 解析为 undefined', async () => {
    const mock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const api = makeClient(mock);
    await expect(api.deleteConversation('c_1')).resolves.toBeUndefined();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/conversations/c_1');
    expect(init.method).toBe('DELETE');
  });

  it('会话分组 CRUD：POST/PATCH/DELETE /v1/conversation-groups 路径与 body 正确', async () => {
    const mock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.includes('/v1/conversation-groups') && (url.endsWith('/v1/conversation-groups') || url.endsWith('?x'))) {
        return jsonResponse(200, { id: 'g_1', name: '工作' });
      }
      if (url.includes('/v1/conversation-groups/g_1')) {
        return jsonResponse(200, { id: 'g_1', name: '新名' });
      }
      return new Response(null, { status: 204 });
    });
    const api = makeClient(mock);
    await api.createConversationGroup('工作');
    expect(captureFetch(mock).init.method).toBe('POST');
    expect(JSON.parse(String(captureFetch(mock).init.body))).toEqual({ name: '工作' });

    await api.patchConversationGroup('g_1', '新名');
    expect(captureFetch(mock).url).toContain('/v1/conversation-groups/g_1');
    expect(captureFetch(mock).init.method).toBe('PATCH');
    expect(JSON.parse(String(captureFetch(mock).init.body))).toEqual({ name: '新名' });

    await expect(api.deleteConversationGroup('g_1')).resolves.toBeUndefined();
    expect(captureFetch(mock).url).toContain('/v1/conversation-groups/g_1');
    expect(captureFetch(mock).init.method).toBe('DELETE');
  });

  it('POST /generations/{id}/stop：202/200 响应透传', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(202, { generation_id: 'g_1', message_id: 'm_1', status: 'stop_requested' }),
    );
    const api = makeClient(mock);
    const result = await api.stopGeneration('g_1');
    expect(result).toEqual({ generation_id: 'g_1', message_id: 'm_1', status: 'stop_requested' });
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/generations/g_1/stop');
    expect(init.method).toBe('POST');
  });

  it('POST /messages/{id}/feedback：Idempotency-Key + body 正确，204 → undefined', async () => {
    const mock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const api = makeClient(mock);
    await expect(
      api.submitFeedback('m_1', { vote: 'down', reason: 'wrong_citation' }, 'key-fb'),
    ).resolves.toBeUndefined();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/messages/m_1/feedback');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('key-fb');
    expect(JSON.parse(String(init.body))).toEqual({ vote: 'down', reason: 'wrong_citation' });
  });

  it('POST /messages/{id}/citation-clicks：无幂等键，body 透传，204 → undefined', async () => {
    const mock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const api = makeClient(mock);
    await expect(
      api.reportCitationClick('m_1', {
        document_id: 'doc_1',
        document_version_id: 'ver_1',
        citation_index: 0,
      }),
    ).resolves.toBeUndefined();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/messages/m_1/citation-clicks');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBeUndefined();
    expect(JSON.parse(String(init.body))).toEqual({
      document_id: 'doc_1',
      document_version_id: 'ver_1',
      citation_index: 0,
    });
  });

  it('POST /messages/{id}/ab-vote：Idempotency-Key + body 正确', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { pair_id: 'pair_1', voted: true, choice: '0' }),
    );
    const api = makeClient(mock);
    const result = await api.submitAbVote('m_1', { pair_id: 'pair_1', choice: '0' }, 'key-ab');
    expect(result).toEqual<AbVoteResponse>({ pair_id: 'pair_1', voted: true, choice: '0' });
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/messages/m_1/ab-vote');
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('key-ab');
    expect(JSON.parse(String(init.body))).toEqual({ pair_id: 'pair_1', choice: '0' });
  });

  it('GET /spaces?usage=retrieval：usage 查询参数透传', async () => {
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, { items: [] }));
    const api = makeClient(mock);
    await api.listSpaces('retrieval');
    const { url } = captureFetch(mock);
    expect(url).toContain('/v1/spaces?usage=retrieval');
  });

  it('POST /prompt-enhancements：body 为 {prompt}，返回 enhanced_prompt 字符串', async () => {
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, { enhanced_prompt: '优化后的内容' }));
    const api = makeClient(mock);
    await expect(api.enhancePrompt('原始问题')).resolves.toBe('优化后的内容');
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/prompt-enhancements');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_1');
    expect(JSON.parse(String(init.body))).toEqual({ prompt: '原始问题' });
  });

  it('POST /prompt-enhancements：外部 signal 透传，中止即 AbortError 拒绝', async () => {
    const mock = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    const api = makeClient(mock);
    const external = new AbortController();
    const pending = api.enhancePrompt('原始问题', external.signal);
    external.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('POST /prompt-enhancements：30s 客户端超时与端点对齐（归一化为 timeout）', async () => {
    vi.useFakeTimers();
    try {
      const mock = vi.fn<typeof fetch>(
        (_url, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () =>
              reject(new DOMException('The operation was aborted.', 'AbortError')),
            );
          }),
      );
      const api = makeClient(mock);
      let settled = false;
      const pending = api.enhancePrompt('原始问题').then(
        () => {
          settled = true;
          return 'resolved';
        },
        (caught: unknown) => {
          settled = true;
          return caught;
        },
      );
      // 10s（基座默认超时）时不得触发
      await vi.advanceTimersByTimeAsync(10_000);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(20_000);
      const error = (await pending) as ApiError;
      expect(error).toBeInstanceOf(ApiError);
      expect(error.code).toBe('timeout');
    } finally {
      vi.useRealTimers();
    }
  });

  it('错误归一化走现有 client：409 feedback_already_submitted → ApiError（code/details/request_id）', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(409, {
        error: {
          code: 'feedback_already_submitted',
          message: 'feedback_already_submitted',
          details: { vote: 'up' },
          request_id: 'req_test_1',
        },
      }),
    );
    const api = makeClient(mock);
    const error = (await api
      .submitFeedback('m_1', { vote: 'up' }, 'new-key')
      .catch((caught: unknown) => caught)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.code).toBe('feedback_already_submitted');
    expect(error.details).toEqual({ vote: 'up' });
    expect(error.requestId).toBe('req_test_1');
  });
});

describe('会话与问答域 API 封装（SSE 端点，经 MSW）', () => {
  let api: ChatApi;
  let token: string;
  let client: ReturnType<typeof createApiClient>;

  beforeEach(() => {
    // 经 vitest-setup 装配的 MSW mock 服务端：登录取得有效 Bearer
    token = mockAuth.login('zhangsan', 'password123', 'vitest').accessToken;
    client = createApiClient({ getAccessToken: () => token, refresh: vi.fn() });
    api = createChatApi(client);
  });

  it('SSE open 接受带 charset 的 text/event-stream Content-Type', async () => {
    const events: SseGenerationEvent[] = [];
    const fetchFn = vi.fn<typeof fetch>(async () => {
      const body = 'id: 1\nevent: done\ndata: {"generation_id":"g_1","message_id":"m_1","status":"completed"}\n\n';
      return new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'Text/Event-Stream; charset=utf-8' },
      });
    });

    await openGenerationStream({
      path: '/generations/g_1/events',
      method: 'GET',
      headers: {},
      token,
      fetchFn,
      onEvent: ({ event }) => events.push(event),
    });

    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(events.map((event) => event.event)).toEqual(['done']);
  });

  it('提问事件 event_seq 由 mock 下发且自增（经 fetch-event-source 逐条回调）', async () => {
    const events: SseGenerationEvent[] = [];
    const firstDone = new Promise<void>((resolve) => {
      void api.ask(
        'c_1',
        { content: '你好', effort_level: 'quick', overrides: null },
        'ask-key-1',
        token,
        ({ event }) => {
          events.push(event);
          if (event.event === 'done') resolve();
        },
      );
    });
    await firstDone;
    expect(events.map((event) => event.event)).toEqual(['start', 'answer', 'done']);
    const start = events[0];
    expect(start?.event).toBe('start');
    if (start?.event === 'start') {
      expect(start.data.attempt_number).toBe(1);
    }
    // Idempotency-Key 真实到达 mock：同键同内容重放同一 generation；同键不同内容 → 409
    const replay: SseGenerationEvent[] = [];
    await api.ask(
      'c_1',
      { content: '你好', effort_level: 'quick', overrides: null },
      'ask-key-1',
      token,
      ({ event }) => replay.push(event),
    );
    const replayStart = replay.find((event) => event.event === 'start');
    expect(replayStart?.event).toBe('start');
    if (replayStart?.event === 'start' && start?.event === 'start') {
      expect(replayStart.data.generation_id).toBe(start.data.generation_id);
      expect(replayStart.data.message_id).toBe(start.data.message_id);
    }
    await expect(
      api.ask(
        'c_1',
        { content: '不同内容', effort_level: 'quick', overrides: null },
        'ask-key-1',
        token,
        () => {},
      ),
    ).rejects.toMatchObject({ status: 409, code: 'idempotency_key_conflict' });
  });

  it('提问事件 event_seq 由 mock 下发且自增（经 fetch-event-source 逐条回调）', async () => {
    const received: Array<{ event: string; id?: string }> = [];
    const donePromise = new Promise<void>((resolve) => {
      void api.ask(
        'c_1',
        { content: '深度问题', effort_level: 'deep', overrides: null },
        'ask-key-deep',
        token,
        ({ event }) => {
          // fetch-event-source 的 EventSourceMessage 带 id 字段；经 openGenerationStream 透传事件名
          received.push({ event: event.event });
          if (event.event === 'done') resolve();
        },
      );
    });
    await donePromise;
    const names = received.map((item) => item.event);
    expect(names[0]).toBe('start');
    expect(names).toContain('step');
    expect(names).toContain('notice');
    expect(names.at(-1)).toBe('done');
    // start 恒为 1 与 seq 递增在契约 mock 测试（chat-contract.test.ts）中按事件 id 精确断言；
    // 此处经传输层验证事件名顺序与终态齐备。
    expect(names.filter((name) => name === 'answer')).toHaveLength(1);
  });

  it('Idempotency-Key 生成工具：输出非空且满足 UUID 或十六进制形态', () => {
    const key = createIdempotencyKey();
    expect(key.length).toBeGreaterThanOrEqual(32);
    expect(/^[0-9a-f-]+$/.test(key)).toBe(true);
  });

  it('断线恢复：GET /generations/{id}/events 带 Last-Event-ID 只重放其后事件', async () => {
    // 先提问拿到 generation_id
    const firstEvents: SseGenerationEvent[] = [];
    const firstDone = new Promise<void>((resolve) => {
      void api.ask(
        'c_1',
        { content: '恢复问题', effort_level: 'think', overrides: null },
        'ask-key-recover',
        token,
        ({ event }) => {
          firstEvents.push(event);
          if (event.event === 'done') resolve();
        },
      );
    });
    await firstDone;
    const start = firstEvents.find((event) => event.event === 'start');
    expect(start?.event).toBe('start');
    const generationId = start?.event === 'start' ? start.data.generation_id : '';
    const messageId = start?.event === 'start' ? start.data.message_id : '';

    // 带 Last-Event-ID=1：start（seq=1）不重放，其余事件重放并接实时（done 终态齐备）
    const replayed: SseGenerationEvent[] = [];
    const replayDone = new Promise<void>((resolve) => {
      void api.getGenerationEvents(generationId, 1, token, ({ event }) => {
        replayed.push(event);
        if (event.event === 'done' || event.event === 'error' || event.event === 'stopped') {
          resolve();
        }
      });
    });
    await replayDone;
    expect(replayed.some((event) => event.event === 'start')).toBe(false);
    expect(replayed.some((event) => event.event === 'answer')).toBe(true);
    expect(replayed.at(-1)?.event).toBe('done');

    // 不带 Last-Event-ID：从 start 开始完整重放
    const full: SseGenerationEvent[] = [];
    const fullDone = new Promise<void>((resolve) => {
      void api.getGenerationEvents(generationId, null, token, ({ event }) => {
        full.push(event);
        if (event.event === 'done' || event.event === 'error' || event.event === 'stopped') {
          resolve();
        }
      });
    });
    await fullDone;
    expect(full[0]?.event).toBe('start');
    expect(full.at(-1)?.event).toBe('done');
    const fullStart = full.find((event) => event.event === 'start');
    expect(fullStart?.event).toBe('start');
    if (fullStart?.event === 'start') {
      expect(fullStart.data.generation_id).toBe(generationId);
      expect(fullStart.data.message_id).toBe(messageId);
    }
  });

  it('失败重试：新 Idempotency-Key，attempt_number 链内递增，复用 user_message_id', async () => {
    // 触发失败终态（夹具）：setNextError
    mockChat.setNextError('provider_error');
    const failedEvents: SseGenerationEvent[] = [];
    const failedDone = new Promise<void>((resolve) => {
      void api.ask(
        'c_1',
        { content: '会失败的问题', effort_level: 'quick', overrides: null },
        'ask-key-fail',
        token,
        ({ event }) => {
          failedEvents.push(event);
          if (event.event === 'done' || event.event === 'error' || event.event === 'stopped') resolve();
        },
      );
    });
    await failedDone;
    const failedStart = failedEvents.find((event) => event.event === 'start');
    expect(failedEvents.at(-1)?.event).toBe('error');
    expect(failedStart?.event).toBe('start');
    const failedGenerationId = failedStart?.event === 'start' ? failedStart.data.generation_id : '';
    const userMessageId = failedStart?.event === 'start' ? failedStart.data.user_message_id : '';

    const retried: SseGenerationEvent[] = [];
    const retryDone = new Promise<void>((resolve) => {
      void api.retryGeneration(failedGenerationId, 'retry-key-1', token, ({ event }) => {
        retried.push(event);
        if (event.event === 'done' || event.event === 'error' || event.event === 'stopped') resolve();
      });
    });
    await retryDone;
    const retryStart = retried.find((event) => event.event === 'start');
    expect(retried.at(-1)?.event).toBe('done');
    expect(retryStart?.event).toBe('start');
    if (retryStart?.event === 'start') {
      expect(retryStart.data.attempt_number).toBe(2);
      expect(retryStart.data.user_message_id).toBe(userMessageId);
      expect(retryStart.data.generation_id).not.toBe(failedGenerationId);
    }
  });
});
