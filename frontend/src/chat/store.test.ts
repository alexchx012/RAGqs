import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { createApiClient } from '../api/client';
import { createChatApi, type ChatApi } from './api';
import {
  ChatStore,
  groupConversationList,
  sortConversations,
  type AssistantMessageView,
  type ConversationSectionKind,
} from './store';
import { mockAuth, mockChat } from '../mocks/testing';
import type { SseEventMessage } from './sse';
import type { ConversationSummary, SseGenerationEvent } from './types';

/*
 * 会话状态机行为验证（spec §2–§6）：经 MSW 契约 mock 真实 HTTP 边界驱动（reduced-motion 直出模拟），
 * 覆盖列表排序/分组/搜索阈值、提问全流程、反馈幂等与 409 刷新、A/B 投票与 409、
 * 重试链与 stopped 终态呈现。
 */

/** 让出事件循环（真实 macrotask），使 MSW/undici 的 SSE 流送达。 */
async function flush(times = 8): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
}

/** 轮询等待条件成立（真实定时器；SSE 流经 MSW/undici 异步送达）。 */
async function waitFor(predicate: () => boolean, timeoutMs = 3_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise<void>((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`waitFor timeout: condition not met within ${timeoutMs}ms`);
}

function makeStore(overrides: Partial<ConstructorParameters<typeof ChatStore>[0]> = {}) {
  const token = mockAuth.login('zhangsan', 'password123', 'vitest').accessToken;
  const client = createApiClient({ getAccessToken: () => token, refresh: async () => token });
  const api = createChatApi(client);
  const store = new ChatStore({
    api,
    getToken: () => token,
    refresh: async () => token,
    getReducedMotion: () => true, // 直出全文：测试聚焦状态机而非模拟节奏
    ...overrides,
  });
  return { store, token, api };
}

describe('会话状态机（ChatStore）', () => {
  beforeEach(() => {
    // 契约 mock 状态经 vitest-setup beforeEach 复位（mockChat.reset）
  });

  describe('会话列表：排序 / 分组 / 搜索', () => {
    it('sortConversations：置顶优先，同置顶按 last_active_at 降序', () => {
      const items: ConversationSummary[] = [
        { id: 'a', title: 'A', pinned: false, group_id: null, last_active_at: '2026-01-02T00:00:00Z' },
        { id: 'b', title: 'B', pinned: true, group_id: null, last_active_at: '2026-01-01T00:00:00Z' },
        { id: 'c', title: 'C', pinned: false, group_id: null, last_active_at: '2026-01-03T00:00:00Z' },
      ];
      const sorted = sortConversations(items).map((item) => item.id);
      expect(sorted).toEqual(['b', 'c', 'a']);
    });

    it('groupConversationList：置顶 → 自定义分组 → 今天 / 本周 / 更早', () => {
      const now = new Date('2026-08-06T12:00:00Z').getTime();
      const group = { id: 'g_1', name: '工作' };
      const iso = (hoursAgo: number) => new Date(now - hoursAgo * 3600_000).toISOString();
      const items: ConversationSummary[] = [
        { id: 'pinned', title: 'P', pinned: true, group_id: null, last_active_at: iso(0) },
        { id: 'in-group', title: 'G', pinned: false, group_id: 'g_1', last_active_at: iso(1) },
        { id: 'today', title: 'T', pinned: false, group_id: null, last_active_at: iso(2) },
        { id: 'yesterday', title: 'Y', pinned: false, group_id: null, last_active_at: iso(30) },
        { id: 'old', title: 'O', pinned: false, group_id: null, last_active_at: iso(30 * 24) },
      ];
      const sections = groupConversationList(items, [group], now);
      expect(sections.map((section) => section.kind as ConversationSectionKind)).toEqual([
        'pinned',
        'group',
        'today',
        'week',
        'earlier',
      ]);
      expect(sections[1]?.items[0]?.id).toBe('in-group');
    });

    it('列表加载与客户端实时过滤：setSearchQuery 过滤已加载列表（未超阈值）', async () => {
      const { store } = makeStore({ serverSearchThreshold: 100 });
      await store.loadConversationList();
      expect(store.getState().listStatus).toBe('ready');
      expect(store.getState().conversations.length).toBeGreaterThan(0);

      store.setSearchQuery('年假');
      const visible = store.getState().visibleConversations;
      expect(visible.length).toBeGreaterThan(0);
      expect(visible.every((item) => item.title.includes('年假'))).toBe(true);
      expect(store.getState().serverFiltered).toBe(false);

      store.setSearchQuery('不存在关键词');
      expect(store.getState().visibleConversations).toHaveLength(0);
    });

    it('已加载量超阈值：setSearchQuery 走服务端 q（serverFiltered=true）', async () => {
      const { store } = makeStore({ serverSearchThreshold: 2 });
      // 经 API 新建两条并命名，使已加载量 > 阈值
      const a = await store.createConversation();
      const b = await store.createConversation();
      await store.patchConversation(a?.id ?? '', { title: 'Alpha 甲' });
      await store.patchConversation(b?.id ?? '', { title: 'Beta 乙' });
      await store.loadConversationList();
      expect(store.getState().conversations.length).toBeGreaterThan(2);

      store.setSearchQuery('甲');
      await waitFor(() => store.getState().serverFiltered === true);
      const visible = store.getState().visibleConversations;
      expect(visible.length).toBeGreaterThan(0);
      expect(visible.every((item) => item.title.includes('甲'))).toBe(true);
    });

    /* ---------- 列表加载竞态（评审 #14）：旧响应不得覆盖新结果 ---------- */

    function summary(id: string, title: string): ConversationSummary {
      return { id, title, pinned: false, group_id: null, last_active_at: '2026-08-18T00:00:00Z' };
    }

    /** 可控延迟的列表加载：按次序返回 deferred，手动决定解析顺序模拟慢旧响应。 */
    function makeDeferredListApi() {
      const pending: {
        q: string | undefined;
        resolve: (value: { items: ConversationSummary[]; groups: never[] }) => void;
      }[] = [];
      const api = {
        listConversations: (q?: string) =>
          new Promise<{ items: ConversationSummary[]; groups: never[] }>((resolve) => {
            pending.push({ q, resolve });
          }),
      } as unknown as ChatApi;
      const take = () => {
        const entry = pending.shift();
        if (entry === undefined) throw new Error('no pending list request');
        return entry;
      };
      return { api, take };
    }

    it('并发加载：慢的旧搜索子集后返回，不覆盖新查询结果（评审 #14）', async () => {
      const { api, take } = makeDeferredListApi();
      const { store } = makeStore({ api, serverSearchThreshold: 1 });

      // 预置全量列表，使已加载量超阈值（setSearchQuery 进入服务端 q 模式）
      const p0 = store.loadConversationList();
      take().resolve({ items: [summary('a', 'A'), summary('b', 'B')], groups: [] });
      await p0;

      // 快速连续输入两个关键词：'甲'（旧，慢）与 '乙'（新，快）各触发一次服务端加载
      store.setSearchQuery('甲');
      store.setSearchQuery('乙');
      const staleReq = take(); // '甲' 的请求（先入队，后到）
      const newReq = take(); // '乙' 的请求（新，先回）

      newReq.resolve({ items: [summary('s_yi', '乙会话')], groups: [] });
      await waitFor(() => store.getState().conversations.length === 1);
      staleReq.resolve({ items: [summary('s_jia', '甲会话')], groups: [] }); // 旧响应后到，必须丢弃
      await flush();

      const state = store.getState();
      expect(state.conversations.map((item) => item.id)).toEqual(['s_yi']);
      expect(state.serverFiltered).toBe(true);
      expect(state.listStatus).toBe('ready');
    });

    it('并发加载：旧全量响应后返回，不覆盖新加载结果', async () => {
      const { api, take } = makeDeferredListApi();
      const { store } = makeStore({ api, serverSearchThreshold: 1 });

      // 两次直接加载：旧的（慢）与新的（快）交错，新响应先回
      const pOld = store.loadConversationList();
      const pNew = store.loadConversationList();
      const oldReq = take();
      const newReq = take();

      newReq.resolve({ items: [summary('a', 'A'), summary('b', 'B')], groups: [] });
      await pNew;
      oldReq.resolve({ items: [summary('a', 'A'), summary('b', 'B'), summary('c', 'C')], groups: [] });
      await pOld;

      const state = store.getState();
      expect(state.conversations.map((item) => item.id)).toEqual(['a', 'b']);
      expect(state.listStatus).toBe('ready');
    });
  });

  describe('提问与消息合并视图', () => {
    it('提问全流程：start→answer→done，reduced-motion 直出，finalize 后 session 清空、读模型 completed', async () => {
      const { store } = makeStore();
      await store.openConversation('c_1');
      await store.ask('什么是年假', 'quick');
      // 终态后 handleTerminal finalize：session 清空，读模型刷新为 completed
      await waitFor(() => store.getState().session === null);
      const state = store.getState();
      const assistants = state.messages.filter((message) => message.role === 'assistant');
      const latest = assistants.at(-1);
      expect(latest?.role).toBe('assistant');
      if (latest?.role === 'assistant') {
        expect(latest.status).toBe('completed');
        expect(latest.content.length).toBeGreaterThan(0);
        expect(latest.generation.phase).toBeNull(); // 读模型收敛，无活动 overlay
      }
    });

    it('生成失败：error 终态 finalize 后可重试；重试链同 root_generation_id', async () => {
      const { store } = makeStore();
      await store.openConversation('c_1');
      mockChat.setNextError('source_scope_changed');
      await store.ask('会失败的问题', 'quick');
      // error 终态 finalize：session 清空，读模型 failed 消息保留
      await waitFor(() => store.getState().session === null);
      const failedMessage = store.getState().messages.filter((m) => m.role === 'assistant').at(-1);
      expect(failedMessage?.role).toBe('assistant');
      if (failedMessage?.role !== 'assistant') return;
      expect(failedMessage.status).toBe('failed');
      const failedId = failedMessage.id;
      const failedGenerationId = failedMessage.generation_id;

      // 重试：新 generation，attempt 链内递增，原失败保留
      await store.retry(failedId);
      await waitFor(() => store.getState().session === null);
      const state = store.getState();
      const assistants = state.messages.filter((m) => m.role === 'assistant');
      const original = assistants.find((m) => m.role === 'assistant' && m.generation_id === failedGenerationId);
      const successor = assistants.find(
        (m) => m.role === 'assistant' && m.retry_of_generation_id === failedGenerationId,
      );
      expect(original?.role).toBe('assistant');
      expect(successor?.role).toBe('assistant');
      if (original?.role === 'assistant' && successor?.role === 'assistant') {
        expect(original.status).toBe('failed');
        expect(original.root_generation_id).toBe(successor.root_generation_id);
        expect(successor.attempt_number).toBe(2);
        expect(successor.status).toBe('completed');
      }
    });

    it('authorization_revoked：finalize 后 session 清空，读模型 stopped 保留已收 answer', async () => {
      const { store } = makeStore();
      await store.openConversation('c_1');
      mockChat.setNextStopped('authorization_revoked');
      await store.ask('会撤销的问题', 'quick');
      await waitFor(() => store.getState().session === null);
      const latest = store.getState().messages.filter((m) => m.role === 'assistant').at(-1);
      expect(latest?.role).toBe('assistant');
      if (latest?.role === 'assistant') {
        expect(latest.status).toBe('stopped');
        expect(latest.stop_reason).toBe('authorization_revoked');
        expect(latest.content.length).toBeGreaterThan(0); // 保留已收稳定 answer
      }
    });

    it('手动停止：点击即 stopping（POST stop 调用），禁重复，已完成不生效', async () => {
      // 用 stub ChatApi 控制活动 generation（真实 MSW mock 即时完成，无法停留在 running）
      let captured: { onEvent: (m: SseEventMessage) => void; idempotencyKey: string } | null = null;
      const token = mockAuth.login('zhangsan', 'password123', 'vitest').accessToken;
      const stopGeneration = vi.fn(async () => ({
        generation_id: 'g_1',
        message_id: 'm_1',
        status: 'stop_requested' as const,
      }));
      const client = createApiClient({ getAccessToken: () => token, refresh: async () => token });
      const realApi = createChatApi(client);
      const api: ChatApi = {
        ...realApi,
        async ask(_conversationId, _body, idempotencyKey, _tok, onEvent) {
          captured = { onEvent, idempotencyKey };
          return new Promise(() => {}); // 挂起：模拟仍在运行的流
        },
        stopGeneration,
        async getGenerationEvents() {
          return new Promise(() => {});
        },
      };
      const store = new ChatStore({
        api,
        getToken: () => token,
        refresh: async () => token,
        getReducedMotion: () => true,
      });
      await store.openConversation('c_1');
      await store.ask('可停止的问题', 'quick');
      await waitFor(() => captured !== null);
      // start 事件送达后 stop（waitFor 保证 captured 已赋值；closure 赋值对直线流程不可见，用非空断言）
      captured!.onEvent({
        id: 1,
        event: {
          event: 'start',
          data: { generation_id: 'g_1', message_id: 'm_1', user_message_id: 'u_1', attempt_number: 1 },
        },
      });
      expect(store.getState().session?.start).not.toBeNull();

      store.stop();
      expect(store.getState().session?.phase).toBe('stopping');
      expect(store.getState().session?.stopRequested).toBe(true);
      await waitFor(() => stopGeneration.mock.calls.length > 0);
      expect(stopGeneration).toHaveBeenCalledWith('g_1');
      store.stop(); // 禁重复：不再调用 POST stop
      expect(stopGeneration.mock.calls.length).toBe(1);
    });

    it('已完成 generation 上调用 stop：不生效（不影响已展示结果）', async () => {
      const { store } = makeStore();
      await store.openConversation('c_1');
      await store.ask('已完成的问题', 'quick');
      await waitFor(() => store.getState().session === null);
      store.stop();
      // finalize 后 session 已空，stop 无目标
      expect(store.getState().session).toBeNull();
    });

    it('stopped 终态经 store 呈现：finalize 后 session 清空、读模型 stop_reason 映射', async () => {
      const { store } = makeStore();
      await store.openConversation('c_1');
      mockChat.setNextStopped('manual_request');
      await store.ask('手动停止的问题', 'quick');
      await waitFor(() => store.getState().session === null);
      const latest = store.getState().messages.filter((m) => m.role === 'assistant').at(-1);
      expect(latest?.role).toBe('assistant');
      if (latest?.role === 'assistant') {
        expect(latest.status).toBe('stopped');
        expect(latest.stop_reason).toBe('manual_request');
        expect(latest.content.length).toBeGreaterThan(0); // 保留已收稳定 answer
      }
    });
  });

  describe('反馈（§3.8）', () => {
    it('反馈幂等 + 409 刷新：首次成功、新键重复 409 后读模型保留首次投票事实', async () => {
      const { store } = makeStore();
      await store.openConversation('c_ab');
      const message = store.getState().messages.find((m) => m.role === 'assistant');
      expect(message?.role).toBe('assistant');
      if (message?.role !== 'assistant') return;

      await store.submitFeedback(message.id, { vote: 'down', reason: 'no_grounding' });
      await flush();
      let state = store.getState();
      expect(state.actionNotice).toBeNull();
      let voted = state.messages.find((m) => m.id === message.id);
      expect(voted?.role).toBe('assistant');
      if (voted?.role === 'assistant') {
        expect(voted.feedback).toEqual({ vote: 'down', down_reason: 'no_grounding' });
      }

      // 新键重复提交 → 409 feedback_already_submitted → 刷新读模型保留首次结果
      await store.submitFeedback(message.id, { vote: 'up' });
      await flush();
      state = store.getState();
      expect(state.actionNotice).toMatchObject({ type: 'feedback_conflict', messageId: message.id });
      voted = state.messages.find((m) => m.id === message.id);
      expect(voted?.role).toBe('assistant');
      if (voted?.role === 'assistant') {
        expect(voted.feedback).toEqual({ vote: 'down', down_reason: 'no_grounding' }); // 未改票
      }
    });
  });

  describe('A/B 投票（§3.9）', () => {
    it('投票 0：读模型 voted:true、保留所选候选正文；换键重复 409 后不改票', async () => {
      const { store } = makeStore();
      await store.openConversation('c_ab');
      const message = store.getState().messages.find((m) => m.role === 'assistant');
      expect(message?.role).toBe('assistant');
      if (message?.role !== 'assistant') return;
      expect(message.ab).toMatchObject({ status: 'open', voted: false, choice: null });

      await store.submitAbVote(message.id, '0');
      await flush();
      let state = store.getState();
      expect(state.actionNotice).toBeNull();
      let voted = state.messages.find((m) => m.id === message.id);
      expect(voted?.role).toBe('assistant');
      if (voted?.role === 'assistant') {
        expect(voted.ab).toMatchObject({ status: 'voted', voted: true, choice: '0', candidates: null });
        expect(voted.content).toBe('Seeded candidate 0 content.');
      }

      // 换键重复投票 → 409 ab_vote_already_submitted → 刷新读模型不改票
      await store.submitAbVote(message.id, '1');
      await flush();
      state = store.getState();
      expect(state.actionNotice).toMatchObject({ type: 'ab_conflict', messageId: message.id });
      voted = state.messages.find((m) => m.id === message.id);
      expect(voted?.role).toBe('assistant');
      if (voted?.role === 'assistant') {
        expect(voted.ab).toMatchObject({ status: 'voted', choice: '0' }); // 未改票
      }
    });

    it('投票 neither：读模型无候选正文、feedback 不渲染', async () => {
      const { store } = makeStore();
      await store.openConversation('c_ab');
      const message = store.getState().messages.find((m) => m.role === 'assistant');
      expect(message?.role).toBe('assistant');
      if (message?.role !== 'assistant') return;

      await store.submitAbVote(message.id, 'neither');
      await flush();
      const state = store.getState();
      const voted = state.messages.find((m) => m.id === message.id);
      expect(voted?.role).toBe('assistant');
      if (voted?.role === 'assistant') {
        expect(voted.ab).toMatchObject({ status: 'voted', voted: true, choice: 'neither' });
        expect(voted.content).toBe('');
        expect(voted.feedback).toBeNull();
      }
    });
  });

  describe('页面刷新恢复（读模型 generating）', () => {
    it('打开会话：读模型中 generating 消息自动恢复订阅（launchRecover）', async () => {
      const { store } = makeStore();
      // 用 mock 夹具创建一个 running generation + generating 占位消息（start 事件已种子化）
      const { accessToken } = mockAuth.login('zhangsan', 'password123', 'vitest');
      const pending = mockChat.startPendingGeneration(`Bearer ${accessToken}`, 'c_1', {
        content: '在跑问题',
        effort_level: 'think',
      });
      void pending;
      await store.openConversation('c_1');
      await flush();
      // 恢复流保持打开并重放 start：generating 消息在场、phase=running（M12）
      const recovering = store.getState().messages.find(
        (m): m is AssistantMessageView => m.role === 'assistant' && m.status === 'generating',
      );
      expect(recovering?.role).toBe('assistant');
      if (recovering?.role === 'assistant') {
        expect(recovering.generation.phase).toBe('running');
      }
    });
  });

  describe('会话与分组 CRUD 经状态机', () => {
    it('新建 / 重命名 / 删除会话，列表收敛', async () => {
      const { store } = makeStore();
      const created = await store.createConversation();
      expect(created).not.toBeNull();
      const id = created?.id ?? '';
      await store.patchConversation(id, { title: '新会话' });
      await flush();
      let state = store.getState();
      expect(state.conversations.some((item) => item.id === id && item.title === '新会话')).toBe(true);
      await store.deleteConversation(id);
      state = store.getState();
      expect(state.conversations.some((item) => item.id === id)).toBe(false);
    });

    it('分组 CRUD：创建分组 → 移入 → 删除归未分组', async () => {
      const { store } = makeStore();
      await store.createGroup('新分组');
      await flush();
      const group = store.getState().groups.find((g) => g.name === '新分组');
      expect(group).toBeDefined();
      const groupId = group?.id ?? '';
      await store.patchConversation('c_1', { group_id: groupId });
      await flush();
      let inGroup = store.getState().conversations.find((item) => item.id === 'c_1');
      expect(inGroup?.group_id).toBe(groupId);
      await store.deleteGroup(groupId);
      await flush();
      inGroup = store.getState().conversations.find((item) => item.id === 'c_1');
      expect(inGroup?.group_id).toBeNull();
    });
  });

  it('读模型合并：completed 历史消息 generation.phase=null', async () => {
    const { store } = makeStore();
    await store.openConversation('c_1');
    const state = store.getState();
    expect(state.conversationStatus).toBe('ready');
    expect(state.conversation).toBeTruthy();
    const assistant = state.messages.find((m) => m.role === 'assistant');
    expect(assistant?.role).toBe('assistant');
    if (assistant?.role === 'assistant') {
      expect(assistant.generation.phase).toBeNull();
      expect(assistant.generation.complete).toBe(true);
    }
  });
});

/* ---------- 审计补强：stub API 承载的实时路径（真实 MSW 即时完成无法停留在 running） ---------- */

interface HeldStream {
  onEvent: (message: SseEventMessage) => void;
  push: (id: number, event: SseGenerationEvent) => void;
}

function makeHeldStore() {
  const token = mockAuth.login('zhangsan', 'password123', 'vitest').accessToken;
  const held = new Map<string, HeldStream>();
  const client = createApiClient({ getAccessToken: () => token, refresh: async () => token });
  const realApi = createChatApi(client);
  // submit / stop 用纯 stub（C1 断言调用参数；M6 断言 stop 调用；不走 MSW 的 message 查找）
  const submitAbVote = vi.fn(async () => ({ pair_id: 'pair_live', voted: true as const, choice: '0' as const }));
  const submitFeedback = vi.fn(async () => {});
  const stopGeneration = vi.fn(async () => ({ generation_id: 'g', message_id: 'm', status: 'stop_requested' as const }));
  const api: ChatApi = {
    ...realApi,
    async ask(_conversationId, _body, _idempotencyKey, _tok, onEvent) {
      const stream: HeldStream = {
        onEvent,
        push: (id, event) => onEvent({ id, event }),
      };
      held.set('ask', stream);
      return new Promise(() => {}); // 挂起：模拟仍在运行的流
    },
    // getGenerationEvents 走真实 MSW live handler：恢复流保持打开并注册到 mockChat，
    // pushStopped 才能把 stopped 帧送达 store 会话（M12 语义）
    submitAbVote,
    submitFeedback,
    stopGeneration,
  };
  const store = new ChatStore({
    api,
    getToken: () => token,
    refresh: async () => token,
    getReducedMotion: () => true,
  });
  return { store, held, submitAbVote, submitFeedback, stopGeneration, token };
}

function startEvt(generationId: string, messageId: string, userMessageId: string, attemptNumber = 1): SseGenerationEvent {
  return { event: 'start', data: { generation_id: generationId, message_id: messageId, user_message_id: userMessageId, attempt_number: attemptNumber } };
}

function answerEvt(candidate: 0 | 1, content: string): SseGenerationEvent {
  return { event: 'answer', data: { candidate, content, citations: [], answer_mode: 'grounded', effort_level: 'quick', upgraded_from: null } };
}

describe('审计补强（C1/M2/M5/M6/M12/M16）', () => {
  it('实时 A/B 投票：live overlay 未 finalize 时从合并视图取 pair_id 投票成功（C1）', async () => {
    const { store, held, submitAbVote } = makeHeldStore();
    await store.openConversation('c_1');
    await store.ask('实时对比问题', 'think');
    const stream = held.get('ask');
    expect(stream).toBeDefined();
    stream?.push(1, startEvt('g_live', 'm_live', 'u_1'));
    stream?.push(2, { event: 'ab_start', data: { pair_id: 'pair_live', message_id: 'm_live', candidates: [0, 1] } });
    stream?.push(3, answerEvt(0, '候选甲'));
    stream?.push(4, answerEvt(1, '候选乙'));
    await waitFor(() => store.getState().session?.ab.status === 'open');

    // 此时 overlay 仍在（generation 未终态、读模型不刷新）——从合并视图取 pair_id 投票
    const message = store.getState().messages.find((m) => m.role === 'assistant' && m.id === 'm_live');
    expect(message?.role).toBe('assistant');
    if (message?.role !== 'assistant') return;
    expect(message.ab).toMatchObject({ status: 'open', voted: false, pair_id: 'pair_live' });

    await store.submitAbVote('m_live', '0');
    await waitFor(() => submitAbVote.mock.calls.length > 0);
    expect(submitAbVote).toHaveBeenCalledWith('m_live', { pair_id: 'pair_live', choice: '0' }, expect.any(String));
  });

  it('跨会话 overlay 隔离：A 生成中切到 B，A 合成消息不出现（M5）', async () => {
    const { store, held } = makeHeldStore();
    // 新建会话 A 并打开提问（overlay 生成于 A）
    await store.createConversation();
    await store.ask('A 会话问题', 'quick');
    const stream = held.get('ask');
    stream?.push(1, startEvt('g_a', 'm_a', 'u_a'));
    await waitFor(() => store.getState().session?.start !== null);

    // 切到种子会话 c_1（B）
    await store.openConversation('c_1');
    const state = store.getState();
    // A 的合成 assistant 消息（m_a）不得出现在 B
    expect(state.messages.some((m) => m.id === 'm_a')).toBe(false);
    expect(state.messages.some((m) => m.role === 'assistant' && m.id === 'm_a')).toBe(false);
  });

  it('请求级错误可见且可恢复提问（M2）：409 上抛后 requestError 入视图，再次提问不受软锁', async () => {
    const token = mockAuth.login('zhangsan', 'password123', 'vitest').accessToken;
    const client = createApiClient({ getAccessToken: () => token, refresh: async () => token });
    const realApi = createChatApi(client);
    let askCount = 0;
    const held = new Map<string, HeldStream>();
    const api: ChatApi = {
      ...realApi,
      async ask(_conversationId, _body, _idempotencyKey, _tok, onEvent) {
        askCount += 1;
        if (askCount === 1) {
          throw new ApiError({
            status: 409,
            code: 'idempotency_key_conflict',
            message: '',
            details: {},
            requestId: 'req_x',
          });
        }
        const stream: HeldStream = {
          onEvent,
          push: (id, event) => onEvent({ id, event }),
        };
        held.set('ask', stream);
        return new Promise(() => {});
      },
      async getGenerationEvents() {
        return new Promise(() => {});
      },
    };
    const store = new ChatStore({
      api,
      getToken: () => token,
      refresh: async () => token,
      getReducedMotion: () => true,
    });
    await store.openConversation('c_1');
    await store.ask('会被 409 的问题', 'quick');
    await waitFor(() => store.getState().session?.requestError !== null);
    expect(store.getState().session?.requestError?.code).toBe('idempotency_key_conflict');
    // 合并视图可见错误（pre-start 合成 assistant 行带 requestError）
    await waitFor(() =>
      store.getState().messages.some(
        (m) => m.role === 'assistant' && m.generation.requestError !== null,
      ),
    );
    const errMsg = store
      .getState()
      .messages.find(
        (m): m is AssistantMessageView =>
          m.role === 'assistant' && m.generation.requestError !== null,
      );
    expect(errMsg?.generation.requestError?.code).toBe('idempotency_key_conflict');

    // 同一 store 再次提问：disposeStaleSessions 清残留，可发起新流（不受软锁）
    await store.ask('可恢复的问题', 'quick');
    await waitFor(() => held.get('ask') !== undefined);
    expect(askCount).toBe(2);
    expect(store.getState().session?.requestError).toBeNull();
    expect(store.getState().session?.phase).toBe('connecting');
  });

  it('刷新恢复订阅 + 恢复后 stop 由 mock 自动补发 stopped 终态（M6/M12，无需 pushStopped）', async () => {
    // 真实 MSW stop handler：stopGeneration 会 queueMicrotask 推 stopped，不依赖测试手调 pushStopped
    const { store } = makeStore();
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'vitest');
    const pending = mockChat.startPendingGeneration(`Bearer ${accessToken}`, 'c_1', {
      content: '恢复中的问题',
      effort_level: 'think',
    });
    await store.openConversation('c_1');
    await waitFor(() => {
      const msg = store
        .getState()
        .messages.find(
          (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
        );
      return msg !== undefined && msg.generation.phase === 'running';
    });
    // live stream 已注册
    expect(mockChat.liveStreamCount(pending.generationId)).toBeGreaterThan(0);
    store.stop();
    // stop POST → mock 自动补发 stopped(manual_request) → finalize → 读模型收敛
    await waitFor(() => {
      const msg = store
        .getState()
        .messages.find(
          (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
        );
      return msg !== undefined && msg.status === 'stopped';
    });
    expect(store.getState().session).toBeNull();
    const stopped = store
      .getState()
      .messages.find(
        (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
      );
    expect(stopped?.stop_reason).toBe('manual_request');
    // stop 推送后 live stream 已 close 并清理
    expect(mockChat.liveStreamCount(pending.generationId)).toBe(0);
  });

  it('手动停止保留已收稳定 answer 并收敛读模型（M1，stop 自动补发，无需 pushStopped）', async () => {
    const { store } = makeStore();
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'vitest');
    const pending = mockChat.startPendingGeneration(`Bearer ${accessToken}`, 'c_1', {
      content: '可停止的问题',
      effort_level: 'quick',
    });
    await store.openConversation('c_1');
    await waitFor(() => {
      const msg = store
        .getState()
        .messages.find(
          (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
        );
      return msg !== undefined && msg.generation.phase === 'running';
    });
    // 补发 answer（已收稳定正文）经 live 流送达
    mockChat.pushAnswer(`Bearer ${accessToken}`, pending.generationId, '已收稳定正文');
    await waitFor(() => {
      const msg = store
        .getState()
        .messages.find(
          (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
        );
      return msg !== undefined && msg.content === '已收稳定正文';
    });
    // store.stop → POST stop → mock 自动 stopped，无需 pushStopped
    store.stop();
    await waitFor(() => {
      const msg = store
        .getState()
        .messages.find(
          (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
        );
      return msg !== undefined && msg.status === 'stopped';
    });
    expect(store.getState().session).toBeNull();
    const msg = store
      .getState()
      .messages.find(
        (m): m is AssistantMessageView => m.role === 'assistant' && m.id === pending.messageId,
      );
    if (msg !== undefined) {
      expect(msg.content).toBe('已收稳定正文');
      expect(msg.stop_reason).toBe('manual_request');
    }
  });

  it('连发两条相同提问：本地气泡按 id 去重（M16）', async () => {
    const { store, held } = makeHeldStore();
    await store.openConversation('c_1');
    await store.ask('相同的提问', 'quick');
    const first = held.get('ask');
    first?.push(1, startEvt('g_1', 'm_1', 'u_1'));
    await waitFor(() => store.getState().session?.start !== null);
    // 用户再次连发相同内容（新 generation）
    await store.ask('相同的提问', 'quick');
    const second = held.get('ask');
    second?.push(1, startEvt('g_2', 'm_2', 'u_2'));
    await waitFor(() => store.getState().session?.start?.generationId === 'g_2');
    // 两条 user 消息都应存在（按 id 去重，不因内容相同被吞）
    const users = store.getState().messages.filter((m) => m.role === 'user');
    expect(users.length).toBe(2);
  });
});

describe('A/B 模拟器时序（N1/N3）', () => {
  it('仅一候选完成 + done：不得按已创建数提前 finalize（N1）', async () => {
    // reduced-motion 直出：候选 0 立即 onDone；旧逻辑 size>=expected 会 complete 并 finalize
    const { store, held } = makeHeldStore();
    await store.openConversation('c_1');
    await store.ask('对比时序', 'think');
    const stream = held.get('ask');
    stream?.push(1, startEvt('g_ab', 'm_ab', 'u_ab'));
    stream?.push(2, {
      event: 'ab_start',
      data: { pair_id: 'pair_n1', message_id: 'm_ab', candidates: [0, 1] },
    });
    stream?.push(3, answerEvt(0, '候选零完整正文'));
    await waitFor(() => {
      const msg = store.getState().messages.find((m) => m.id === 'm_ab');
      return msg?.role === 'assistant' && (msg.generation.abContents?.length ?? 0) >= 1;
    });
    stream?.push(4, {
      event: 'done',
      data: { generation_id: 'g_ab', message_id: 'm_ab', status: 'completed' },
    });
    await flush(6);
    // 仅一候选：不得 finalize（session 仍在，complete=false）
    expect(store.getState().session).not.toBeNull();
    const mid = store.getState().messages.find((m) => m.id === 'm_ab');
    expect(mid?.role).toBe('assistant');
    if (mid?.role === 'assistant') {
      expect(mid.generation.complete).toBe(false);
    }
  });

  it('双候选均到达后：仅一侧模拟完成不 finalize；两侧完成才收敛并清理（N1）', async () => {
    const token = mockAuth.login('zhangsan', 'password123', 'vitest').accessToken;
    const held = new Map<string, HeldStream>();
    const client = createApiClient({ getAccessToken: () => token, refresh: async () => token });
    const realApi = createChatApi(client);
    const api: ChatApi = {
      ...realApi,
      async ask(_conversationId, _body, _idempotencyKey, _tok, onEvent) {
        const stream: HeldStream = {
          onEvent,
          push: (id, event) => onEvent({ id, event }),
        };
        held.set('ask', stream);
        return new Promise(() => {});
      },
      async getGenerationEvents() {
        return new Promise(() => {});
      },
    };
    // 非 reduced-motion：两侧同时创建模拟器后，先完成一侧不得按 size 提前 finalize
    const store = new ChatStore({
      api,
      getToken: () => token,
      refresh: async () => token,
      getReducedMotion: () => false,
    });
    await store.openConversation('c_1');
    await store.ask('双候选时序', 'think');
    const stream = held.get('ask');
    stream?.push(1, startEvt('g_ab2', 'm_ab2', 'u_ab2'));
    stream?.push(2, {
      event: 'ab_start',
      data: { pair_id: 'pair_n1b', message_id: 'm_ab2', candidates: [0, 1] },
    });
    // 候选 0 短、候选 1 很长 → 0 先 isDone，1 仍在播
    stream?.push(3, answerEvt(0, '短'));
    stream?.push(4, answerEvt(1, '长候选正文。'.repeat(40)));
    stream?.push(5, {
      event: 'done',
      data: { generation_id: 'g_ab2', message_id: 'm_ab2', status: 'completed' },
    });
    await waitFor(() => {
      const msg = store.getState().messages.find((m) => m.id === 'm_ab2');
      return msg?.role === 'assistant' && (msg.generation.abContents?.length ?? 0) >= 2;
    });
    // 给候选 0 足够时间直出/短播完成，候选 1 仍未完成
    await new Promise<void>((resolve) => setTimeout(resolve, 80));
    expect(store.getState().session).not.toBeNull();
    const mid = store.getState().messages.find((m) => m.id === 'm_ab2');
    if (mid?.role === 'assistant') {
      // 若错误按 abSimulators.size>=2 会在此 complete
      // 允许 complete 仍为 false（一侧未播完）
      const c1 = mid.generation.abContents?.find((entry) => entry.candidate === 1);
      const stillStreaming = c1 !== undefined && c1.content.length < '长候选正文。'.repeat(40).length;
      if (stillStreaming) {
        expect(mid.generation.complete).toBe(false);
      }
    }
    // 两侧都播完 → finalize，session 清空（模拟器 dispose）
    await waitFor(() => store.getState().session === null, 15_000);
    expect(store.getState().session).toBeNull();
  });

  it('candidate0 answer 先于 ab_start：不双写、不截断，最终 open 可投票（N3）', async () => {
    const { store, held } = makeHeldStore();
    await store.openConversation('c_1');
    await store.ask('先到候选零', 'think');
    const stream = held.get('ask');
    stream?.push(1, startEvt('g_n3', 'm_n3', 'u_n3'));
    // candidate 0 先作为普通 answer
    stream?.push(2, answerEvt(0, '先到的候选零正文'));
    await waitFor(() => {
      const msg = store.getState().messages.find((m) => m.id === 'm_n3');
      return msg?.role === 'assistant' && msg.content.includes('先到的候选零正文');
    });
    // ab_start：迁移为候选 0，正文不消失、不双写
    stream?.push(3, { event: 'ab_start', data: { pair_id: 'pair_n3', message_id: 'm_n3', candidates: [0, 1] } });
    await waitFor(() => store.getState().session?.ab.pair_id === 'pair_n3');
    stream?.push(4, answerEvt(1, '后到的候选一正文'));
    await waitFor(() => store.getState().session?.ab.status === 'open');
    const message = store.getState().messages.find((m) => m.id === 'm_n3');
    expect(message?.role).toBe('assistant');
    if (message?.role !== 'assistant') return;
    expect(message.ab).toMatchObject({ status: 'open', pair_id: 'pair_n3', voted: false });
    const contents = message.generation.abContents ?? [];
    const c0 = contents.find((entry) => entry.candidate === 0);
    const c1 = contents.find((entry) => entry.candidate === 1);
    expect(c0?.content).toContain('先到的候选零正文');
    expect(c1?.content).toContain('后到的候选一正文');
    // 可投票（合并视图有 pair_id）
    await store.submitAbVote('m_n3', '0');
  });
});
