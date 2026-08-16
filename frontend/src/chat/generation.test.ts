import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import type { ChatApi } from './api';
import {
  DEFAULT_GENERATION_CONFIG,
  GenerationSession,
  GENERATION_MESSAGE_KEYS,
  type GenerationSessionDeps,
} from './generation';
import type { SseEventMessage } from './sse';
import type { SseGenerationEvent, StopReason } from './types';

/*
 * 生成控制器行为验证（spec §7）：经 stub ChatApi + fake timers 精确控制事件流与网络失败，
 * 覆盖事件去重、pre-start 同键重试、断线重连（Last-Event-ID 断言）、宽限期语义、停止三态、
 * 重试、认证失效与终态互斥。
 */

interface StubStream {
  readonly kind: 'ask' | 'retry' | 'events';
  readonly generationId: string | null;
  readonly lastEventId: number | null;
  readonly idempotencyKey: string | null;
  readonly body: unknown;
  readonly token: string | null;
  readonly onEvent: (message: SseEventMessage) => void;
  readonly promise: Promise<void>;
  push(id: number, event: SseGenerationEvent): void;
  fail(cause: unknown): void;
  resolve(): void;
}

interface StubApi {
  readonly api: ChatApi;
  readonly streams: StubStream[];
  readonly stopGeneration: ReturnType<typeof vi.fn>;
  readonly retryGenerationCalls: Array<{ failedGenerationId: string; idempotencyKey: string }>;
  readonly eventsCalls: Array<{ generationId: string; lastEventId: number | null }>;
}

function makeStubStream(
  kind: StubStream['kind'],
  generationId: string | null,
  lastEventId: number | null,
  idempotencyKey: string | null,
  body: unknown,
  token: string | null,
  onEvent: (message: SseEventMessage) => void,
): StubStream {
  let resolveFn: () => void = () => {};
  let rejectFn: (cause: unknown) => void = () => {};
  const promise = new Promise<void>((resolve, reject) => {
    resolveFn = resolve;
    rejectFn = reject;
  });
  return {
    kind,
    generationId,
    lastEventId,
    idempotencyKey,
    body,
    token,
    onEvent,
    promise,
    push(id, event) {
      onEvent({ id, event });
    },
    fail(cause) {
      rejectFn(cause);
    },
    resolve() {
      resolveFn();
    },
  };
}

function makeStubApi(): StubApi {
  const streams: StubStream[] = [];
  const retryGenerationCalls: StubApi['retryGenerationCalls'] = [];
  const eventsCalls: StubApi['eventsCalls'] = [];
  const api: ChatApi = {
    async listConversations() {
      return { items: [], groups: [] };
    },
    async createConversation() {
      return { id: 'c_1', title: '', pinned: false, group_id: null, last_active_at: '' };
    },
    async getConversation() {
      return { id: 'c_1', title: '', effort_level: 'quick', scope: { space_ids: [], document_ids: [] }, messages: [] };
    },
    async patchConversation() {
      return { id: 'c_1', title: '', pinned: false, group_id: null, last_active_at: '' };
    },
    async deleteConversation() {},
    async createConversationGroup() {
      return { id: 'g', name: '' };
    },
    async patchConversationGroup() {
      return { id: 'g', name: '' };
    },
    async deleteConversationGroup() {},
    ask(_conversationId, body, idempotencyKey, token, onEvent) {
      const stream = makeStubStream('ask', null, null, idempotencyKey, body, token, onEvent);
      streams.push(stream);
      return stream.promise;
    },
    async stopGeneration() {
      return { generation_id: 'g_1', message_id: 'm_1', status: 'stop_requested' };
    },
    retryGeneration(failedGenerationId, idempotencyKey, token, onEvent) {
      const stream = makeStubStream('retry', failedGenerationId, null, idempotencyKey, null, token, onEvent);
      streams.push(stream);
      retryGenerationCalls.push({ failedGenerationId, idempotencyKey });
      return stream.promise;
    },
    getGenerationEvents(generationId, lastEventId, token, onEvent) {
      const stream = makeStubStream('events', generationId, lastEventId, null, null, token, onEvent);
      streams.push(stream);
      eventsCalls.push({ generationId, lastEventId });
      return stream.promise;
    },
    async submitFeedback() {},
    async submitAbVote() {
      return { pair_id: 'p', voted: true, choice: '0' };
    },
    async listSpaces() {
      return { items: [] };
    },
    async listDocuments() {
      return { items: [], total: 0, page: 1, page_size: 50 };
    },
  };
  const stopGeneration = vi.fn(api.stopGeneration);
  api.stopGeneration = stopGeneration;
  return { api, streams, stopGeneration, retryGenerationCalls, eventsCalls };
}

function makeDeps(api: StubApi, overrides: Partial<GenerationSessionDeps> = {}): GenerationSessionDeps {
  return {
    api: api.api,
    getToken: () => 'tok',
    refresh: vi.fn(async () => 'tok_new'),
    config: {
      random: () => 1, // 退避取满值（确定性）
      gracePeriodMs: 60_000,
      maxReconnectAttempts: 10,
      baseReconnectDelayMs: 100,
      maxReconnectDelayMs: 400,
      preStartMaxRetries: 3,
      preStartBaseDelayMs: 100,
    },
    ...overrides,
  };
}

function startEvt(generationId = 'g_1', messageId = 'm_1', userMessageId = 'u_1', attemptNumber = 1): SseGenerationEvent {
  return { event: 'start', data: { generation_id: generationId, message_id: messageId, user_message_id: userMessageId, attempt_number: attemptNumber } };
}

function answerEvt(content = 'Mock answer.', candidate: 0 | 1 = 0): SseGenerationEvent {
  return { event: 'answer', data: { candidate, content, citations: [], answer_mode: 'grounded', effort_level: 'quick', upgraded_from: null } };
}

function doneEvt(generationId = 'g_1', messageId = 'm_1'): SseGenerationEvent {
  return { event: 'done', data: { generation_id: generationId, message_id: messageId, status: 'completed' } };
}

function stoppedEvt(stopReason: StopReason, generationId = 'g_1', messageId = 'm_1'): SseGenerationEvent {
  return { event: 'stopped', data: { generation_id: generationId, message_id: messageId, status: 'stopped', stop_reason: stopReason } };
}

function networkError(): Error {
  return new Error('Failed to fetch');
}

function httpError(status: number, code: string): ApiError {
  return new ApiError({ status, code, message: code, details: {}, requestId: 'req_1' });
}

describe('生成控制器（spec §7）', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('提问全流程：connecting→running（start 四字段）→answer→done 终态', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: '你好', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    expect(stream?.kind).toBe('ask');
    expect(stream?.body).toMatchObject({ content: '你好', effort_level: 'quick' });
    expect(session.getView().phase).toBe('connecting');

    stream?.push(1, startEvt());
    expect(session.getView().phase).toBe('running');
    expect(session.getView().start).toEqual({ generationId: 'g_1', messageId: 'm_1', userMessageId: 'u_1', attemptNumber: 1 });
    expect(session.getView().appliedSeq).toBe(1);
    expect(session.getView().id).toBe('g_1');

    stream?.push(2, answerEvt('Hello answer.'));
    expect(session.getView().answer?.content).toBe('Hello answer.');

    stream?.push(3, doneEvt());
    expect(session.getView().terminal).toEqual({ kind: 'done', generationId: 'g_1', messageId: 'm_1' });
    expect(session.getView().phase).toBe('completed');
  });

  it('事件去重：忽略 ≤ 已应用 event_seq 的重复事件', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, answerEvt('first'));
    stream?.push(2, answerEvt('duplicate')); // 重复 seq=2：忽略
    expect(session.getView().answer?.content).toBe('first');
    stream?.push(1, startEvt()); // 重复 start：忽略
    expect(session.getView().start?.generationId).toBe('g_1');
    stream?.push(3, doneEvt());
    expect(session.getView().terminal?.kind).toBe('done');
  });

  it('终态互斥：done 先到后，stopped 不再覆盖', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, doneEvt());
    stream?.push(3, stoppedEvt('manual_request'));
    expect(session.getView().terminal).toEqual({ kind: 'done', generationId: 'g_1', messageId: 'm_1' });
    expect(session.getView().phase).toBe('completed');
  });

  it('未知 notice kind：保留记录，不使 generation 失败', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, { event: 'notice', data: { kind: 'future_unknown_kind', detail: { x: 1 } } });
    expect(session.getView().notices).toHaveLength(1);
    expect(session.getView().notices[0]?.kind).toBe('future_unknown_kind');
    expect(session.getView().phase).toBe('running');
  });

  it('收到 start 前网络失败：相同内容相同键重试（不换键、不新增用户消息）', async () => {
    const api = makeStubApi();
    const deps = makeDeps(api);
    const session = GenerationSession.launchAsk(deps, 'c_1', { content: '重试问题', effort_level: 'think', overrides: null });
    const first = api.streams[0];
    first?.fail(networkError());
    await vi.advanceTimersByTimeAsync(100); // pre-start 退避 100ms
    const second = api.streams[1];
    expect(second?.kind).toBe('ask');
    expect(second?.idempotencyKey).toBe(first?.idempotencyKey); // 同键
    expect(second?.body).toEqual(first?.body); // 同内容
    second?.push(1, startEvt());
    expect(session.getView().phase).toBe('running');
    second?.push(2, doneEvt());
    expect(session.getView().terminal?.kind).toBe('done');
  });

  it('409 idempotency_key_conflict：按请求错误上抛，不自动换键重试', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    api.streams[0]?.fail(httpError(409, 'idempotency_key_conflict'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.streams).toHaveLength(1); // 未重试
    expect(session.getView().requestError?.code).toBe('idempotency_key_conflict');
    expect(session.getView().requestError?.messageKey).toBe(GENERATION_MESSAGE_KEYS.requestError);
    expect(session.getView().phase).toBe('connecting');
  });

  it('pre-start 401：先 refresh 再以同键重试', async () => {
    const api = makeStubApi();
    let token = 'tok';
    const deps = makeDeps(api, {
      getToken: () => token,
      refresh: vi.fn(async () => {
        token = 'tok_new';
        return 'tok_new';
      }),
    });
    const session = GenerationSession.launchAsk(deps, 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    api.streams[0]?.fail(httpError(401, 'invalid_token'));
    await vi.advanceTimersByTimeAsync(100); // 401 → refresh → pre-start 退避 100ms 后同键重试
    expect((deps.refresh as ReturnType<typeof vi.fn>)).toHaveBeenCalledTimes(1);
    const second = api.streams[1];
    expect(second?.idempotencyKey).toBe(api.streams[0]?.idempotencyKey);
    expect(second?.token).toBe('tok_new');
    second?.push(1, startEvt());
    expect(session.getView().phase).toBe('running');
  });

  it('断线恢复：进入 reconnecting 并以 Last-Event-ID=最后已应用序号重连（不重放 start）', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const first = api.streams[0];
    first?.push(1, startEvt());
    first?.push(2, answerEvt('partial'));
    first?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().phase).toBe('reconnecting');
    const events = api.streams[1];
    expect(events?.kind).toBe('events');
    expect(events?.generationId).toBe('g_1');
    expect(events?.lastEventId).toBe(2); // Last-Event-ID = 最后已应用 event_seq
    // 恢复流重放 seq>2；重复 seq=2 被去重
    events?.push(2, answerEvt('partial-dup'));
    expect(session.getView().answer?.content).toBe('partial');
    events?.push(3, doneEvt());
    expect(session.getView().terminal?.kind).toBe('done');
  });

  it('recovery 流正常 EOF 但无终态：继续退避重连，不卡在 reconnecting', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const first = api.streams[0];
    first?.push(1, startEvt());
    first?.push(2, answerEvt('partial'));
    first?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().phase).toBe('reconnecting');

    // 代理/服务端 idle timeout：恢复流干净关闭，无 done/error/stopped
    const recovery1 = api.streams[1];
    expect(recovery1?.kind).toBe('events');
    recovery1?.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().terminal).toBeNull();
    expect(session.getView().phase).toBe('reconnecting');
    expect(session.getView().authFailed).toBe(false);

    // 按既有退避（base=100, random=1 → 100ms）继续重连，不应 stall
    await vi.advanceTimersByTimeAsync(100);
    const recovery2 = api.streams[2];
    expect(recovery2?.kind).toBe('events');
    expect(recovery2?.generationId).toBe('g_1');
    expect(recovery2?.lastEventId).toBe(2);
    expect(session.getView().reconnectAttempts).toBeGreaterThanOrEqual(1);
    expect(session.getView().phase).toBe('reconnecting');

    // 第二次恢复流送达终态后正常完成
    recovery2?.push(3, doneEvt());
    recovery2?.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().terminal?.kind).toBe('done');
    expect(session.getView().phase).toBe('completed');
  });

  it('recovery 流 EOF 时已超 sessionDeadline：立即 reconnect_failed，不先 sleep 再失败', async () => {
    const api = makeStubApi();
    let current = 1_000;
    const deps = makeDeps(api, {
      now: () => current,
      config: {
        random: () => 1,
        gracePeriodMs: 60_000,
        maxReconnectAttempts: 10,
        baseReconnectDelayMs: 100,
        maxReconnectDelayMs: 400,
        preStartMaxRetries: 3,
        preStartBaseDelayMs: 100,
        sessionDeadlineMs: 500,
      },
    });
    const session = GenerationSession.launchAsk(deps, 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    api.streams[0]?.push(1, startEvt());
    api.streams[0]?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().phase).toBe('reconnecting');

    const recovery = api.streams[1];
    expect(recovery?.kind).toBe('events');
    // 流仍打开期间会话截止时间已到；EOF 后应立即 fail，而不是 sleep 100ms 后再在下一轮失败
    current = 1_000 + 500;
    recovery?.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().phase).toBe('reconnect_failed');
    expect(session.getView().terminal).toBeNull();

    const streamsAfterFail = api.streams.length;
    // failReconnect 会开一条 awaitTerminalOnce 监听流；不应再有退避后的重连
    await vi.advanceTimersByTimeAsync(1_000);
    expect(session.getView().phase).toBe('reconnect_failed');
    expect(api.streams.length).toBe(streamsAfterFail);
  });

  it('宽限期语义：重连在宽限期内进行、不调用 stop；届满后进入 reconnect_failed', async () => {
    const api = makeStubApi();
    const deps = makeDeps(api, { config: { ...DEFAULT_GENERATION_CONFIG, random: () => 1, gracePeriodMs: 1_000, maxReconnectAttempts: 10, baseReconnectDelayMs: 100, maxReconnectDelayMs: 400, preStartBaseDelayMs: 100 } });
    const session = GenerationSession.launchAsk(deps, 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    api.streams[0]?.push(1, startEvt());
    api.streams[0]?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().phase).toBe('reconnecting');
    // 每次恢复流都失败：退避 100→200→400→400…；宽限期 1000ms 内累计
    for (let index = 0; index < 10 && session.getView().phase === 'reconnecting'; index += 1) {
      const stream = api.streams.at(-1);
      stream?.fail(networkError());
      await vi.advanceTimersByTimeAsync(500);
    }
    expect(session.getView().phase).toBe('reconnect_failed');
    expect(api.stopGeneration).not.toHaveBeenCalled(); // 宽限期内不调用 stop
    expect(session.getView().reconnectAttempts).toBeGreaterThan(0);
  });

  it('refresh 失败：停止重连并按认证失效处理（authFailed=true）', async () => {
    const api = makeStubApi();
    const deps = makeDeps(api, {
      refresh: vi.fn(async () => {
        throw new ApiError({ status: 401, code: 'invalid_refresh', message: '', details: {}, requestId: null });
      }),
    });
    const session = GenerationSession.launchAsk(deps, 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    api.streams[0]?.push(1, startEvt());
    api.streams[0]?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getView().authFailed).toBe(true);
    // 不再发起恢复流
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.streams.filter((stream) => stream.kind === 'events')).toHaveLength(0);
  });

  it('停止：点击即 stopping 禁重复；POST stop 一次；stopped 终态按 stop_reason 映射', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, answerEvt('已展示正文'));
    expect(session.getView().phase).toBe('running');

    session.requestStop();
    expect(session.getView().phase).toBe('stopping');
    expect(session.getView().stopRequested).toBe(true);
    expect(api.stopGeneration).toHaveBeenCalledWith('g_1');
    session.requestStop(); // 禁重复
    expect(api.stopGeneration).toHaveBeenCalledTimes(1);

    stream?.push(3, stoppedEvt('manual_request'));
    expect(session.getView().terminal).toEqual({ kind: 'stopped', generationId: 'g_1', messageId: 'm_1', stopReason: 'manual_request' });
    expect(session.getView().phase).toBe('stopped');
    expect(session.getView().answer?.content).toBe('已展示正文'); // 保留已收稳定 answer
  });

  it('重连中的停止在恢复流 EOF 后重新监听 stopped 终态', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const initial = api.streams[0];
    initial?.push(1, startEvt());
    initial?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);

    const recovery = api.streams[1];
    expect(recovery?.kind).toBe('events');
    session.requestStop();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.stopGeneration).toHaveBeenCalledTimes(1);

    recovery?.resolve();
    await vi.advanceTimersByTimeAsync(0);

    const terminalStream = api.streams[2];
    expect(terminalStream?.kind).toBe('events');
    terminalStream?.push(2, stoppedEvt('manual_request'));
    expect(session.getView().phase).toBe('stopped');
    expect(session.getView().terminal?.kind).toBe('stopped');
  });

  it('停止后的终态监听再次 EOF 时退出 stopping，进入可操作错误状态', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const initial = api.streams[0];
    initial?.push(1, startEvt());
    initial?.fail(networkError());
    await vi.advanceTimersByTimeAsync(0);

    session.requestStop();
    await vi.advanceTimersByTimeAsync(0);
    api.streams[1]?.resolve();
    await vi.advanceTimersByTimeAsync(0);

    api.streams[2]?.resolve();
    await vi.advanceTimersByTimeAsync(0);

    expect(session.getView()).toMatchObject({
      phase: 'failed',
      stopRequested: false,
      requestError: { code: 'network_error', messageKey: GENERATION_MESSAGE_KEYS.requestError },
    });
  });

  it('停止请求持续 401 时只刷新并重试一次，然后回到可操作错误状态', async () => {
    const api = makeStubApi();
    api.stopGeneration.mockRejectedValueOnce(httpError(401, 'invalid_token')).mockRejectedValueOnce(httpError(401, 'invalid_token'));
    const refresh = vi.fn(async () => 'tok_new');
    const session = GenerationSession.launchAsk(makeDeps(api, { refresh }), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    api.streams[0]?.push(1, startEvt());

    session.requestStop();
    await vi.advanceTimersByTimeAsync(0);

    expect(api.stopGeneration).toHaveBeenCalledTimes(2);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(session.getView()).toMatchObject({
      phase: 'failed',
      stopRequested: false,
      requestError: { code: 'invalid_token', messageKey: GENERATION_MESSAGE_KEYS.requestError },
    });
  });

  it('停止：收到 start 前不可停止（不调用 POST stop）', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    session.requestStop();
    expect(api.stopGeneration).not.toHaveBeenCalled();
    expect(session.getView().phase).toBe('connecting');
  });

  it('停止：已完成/失败 generation 的 stop 不生效，不影响已展示结果', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, doneEvt());
    session.requestStop();
    expect(api.stopGeneration).not.toHaveBeenCalled();
    expect(session.getView().terminal?.kind).toBe('done');
  });

  it('409 generation_already_terminal：stop 返回 409 不覆盖已收终态', async () => {
    const api = makeStubApi();
    api.stopGeneration.mockRejectedValueOnce(httpError(409, 'generation_already_terminal'));
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    session.requestStop();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.stopGeneration).toHaveBeenCalledTimes(1);
    expect(session.getView().phase).toBe('stopping'); // 等待 stopped 终态
    stream?.push(2, stoppedEvt('manual_request'));
    expect(session.getView().terminal?.kind).toBe('stopped');
  });

  it('authorization_revoked：保留已收稳定 answer，phase=stopped', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, answerEvt('稳定答案'));
    stream?.push(3, stoppedEvt('authorization_revoked'));
    expect(session.getView().terminal).toMatchObject({ kind: 'stopped', stopReason: 'authorization_revoked' });
    expect(session.getView().answer?.content).toBe('稳定答案');
  });

  it('失败重试：新 Idempotency-Key + retry 端点；attempt_number 链内递增', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchRetry(makeDeps(api), 'g_failed');
    const stream = api.streams[0];
    expect(stream?.kind).toBe('retry');
    expect(stream?.generationId).toBe('g_failed');
    expect(stream?.idempotencyKey).toBeTruthy();
    expect(session.getView().phase).toBe('connecting');
    stream?.push(1, startEvt('g_retry', 'm_retry', 'u_orig', 2));
    expect(session.getView().start?.attemptNumber).toBe(2);
    expect(session.getView().start?.userMessageId).toBe('u_orig'); // 复用原 user_message_id
    stream?.push(2, doneEvt('g_retry', 'm_retry'));
    expect(session.getView().terminal?.kind).toBe('done');
  });

  it('断线恢复 launchRecover：从指定 Last-Event-ID 或 null 订阅 events 端点', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchRecover(makeDeps(api), 'c_1', 'g_running', null);
    expect(api.streams[0]?.kind).toBe('events');
    expect(api.streams[0]?.lastEventId).toBeNull();
    api.streams[0]?.push(1, startEvt('g_running', 'm_running', 'u_1', 1));
    expect(session.getView().phase).toBe('running');

    const api2 = makeStubApi();
    GenerationSession.launchRecover(makeDeps(api2), 'c_1', 'g_running', 5);
    expect(api2.streams[0]?.lastEventId).toBe(5);
  });

  it('A/B：ab_start 后双 answer 分别记录，open 后可投票（choice 按 candidate 序号）', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'think', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    stream?.push(2, { event: 'ab_start', data: { pair_id: 'pair_1', message_id: 'm_1', candidates: [0, 1] } });
    expect(session.getView().ab.status).toBe('pending');
    expect(session.getView().ab.pair_id).toBe('pair_1');
    stream?.push(3, answerEvt('候选 A', 0));
    expect(session.getView().ab.candidates).toHaveLength(1);
    expect(session.getView().ab.status).toBe('pending');
    stream?.push(4, answerEvt('候选 B', 1));
    expect(session.getView().ab.candidates).toHaveLength(2);
    expect(session.getView().ab.status).toBe('open');
    // 普通 answer 字段在 A/B 下不设置
    expect(session.getView().answer).toBeNull();
  });

  it('candidate 0 answer 先于 ab_start 到达：迁移为候选 0，正文不消失（M13）', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'think', overrides: null });
    const stream = api.streams[0];
    stream?.push(1, startEvt());
    // 契约只保证 ab_start 先于 candidate 1；candidate 0 可能先到（作为普通 answer）
    stream?.push(2, answerEvt('先到的候选 0', 0));
    expect(session.getView().answer?.content).toBe('先到的候选 0');
    // ab_start 到达：已渲染正文迁移为 ab.candidates[0]，不消失
    stream?.push(3, { event: 'ab_start', data: { pair_id: 'pair_1', message_id: 'm_1', candidates: [0, 1] } });
    expect(session.getView().answer).toBeNull();
    expect(session.getView().ab.status).toBe('pending');
    expect(session.getView().ab.candidates[0]?.candidate).toBe(0);
    expect(session.getView().ab.candidates[0]?.content).toBe('先到的候选 0');
    // candidate 1 后到：进入对比
    stream?.push(4, answerEvt('候选 1', 1));
    expect(session.getView().ab.status).toBe('open');
    expect(session.getView().ab.candidates).toHaveLength(2);
  });

  it('dispose 中止活动流并清理监听', async () => {
    const api = makeStubApi();
    const session = GenerationSession.launchAsk(makeDeps(api), 'c_1', { content: 'q', effort_level: 'quick', overrides: null });
    const listener = vi.fn();
    session.subscribe(listener);
    session.dispose();
    const stream = api.streams[0];
    // dispose 后 abort：流失败被忽略，不再 emit
    stream?.fail(networkError());
    await vi.advanceTimersByTimeAsync(10_000);
    expect(session.getView().phase).toBe('connecting');
  });
});
