/*
 * 会话状态机（fe-chat-home 规格 §2–§6；契约 §3.3、§3.7–3.9、§6.1）。纯 TS，React 绑定在 chat-context.tsx。
 * - 会话列表：按需分页（A25：首页 50 条，「加载更多」递增 limit，后端上限 200）；排序（置顶 →
 *   自定义分组 → 今天/本周/更早）；q 实时过滤已加载列表，已加载量超阈值（默认 500）走服务端 q。
 * - 当前会话读模型：消息 / 引用 / 反馈 / A/B 状态 / 重试链恢复；generating 消息自动恢复订阅。
 * - 活动 generation 会话（ask/retry/recover）叠加到消息视图：模拟流式正文、阶段/步骤、A/B 双候选、
 *   终态与 stop_reason；重试链（同 root_generation_id）呈现为同一链，失败保留、后继追加。
 * - A/B 状态机：ab_start→双 answer 分别模拟→open 可投票；投票 0/1/neither 按 candidate 序号（与左右解耦）；
 *   409 ab_vote_already_submitted / ab_pair_expired / idempotency_key_conflict 刷新读模型保留服务端首次结果。
 * - 反馈：Idempotency-Key 幂等（网络未知复用同键不换键）；409 feedback_already_submitted /
 *   idempotency_key_conflict 刷新读模型保留首次事实；已投不可改。
 * 展示文案一律经状态码 / copy key 表达，状态层不持有中文文案。
 */

import { ApiError } from '../api/errors';
import type { ChatApi, SpaceDocumentsResponse } from './api';
import {
  GenerationSession,
  type GenerationConfig,
  type GenerationPhase,
  type GenerationSessionDeps,
} from './generation';
import { createIdempotencyKey } from './idempotency';
import { createStreamingSimulator, type StreamingSimulator } from './streaming';
import type {
  AbChoice,
  AbState,
  AnswerMode,
  AssistantMessage,
  AssistantMessageStatus,
  Citation,
  ConversationDetail,
  ConversationGroup,
  ConversationScope,
  ConversationSummary,
  EffortLevel,
  FeedbackVoteRequest,
  Notice,
  SseStagePhase,
  UserMessage,
} from './types';

export type ChatListStatus = 'idle' | 'loading' | 'ready' | 'error';
export type ChatConversationStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface ChatStoreDeps {
  readonly api: ChatApi;
  readonly getToken: () => string | null;
  /** 认证层 single-flight refresh（auth/session.ts 公开接口）。 */
  readonly refresh: () => Promise<string>;
  /** 已加载会话数超过该阈值且有关键词时走服务端 q（spec §2 暂定 500）。 */
  readonly serverSearchThreshold?: number;
  readonly getReducedMotion?: () => boolean;
  readonly now?: () => number;
  readonly config?: Partial<GenerationConfig>;
}

/** assistant 消息视图：读模型 + 活动 generation 会话叠加（模拟流式正文 / A/B 候选）。 */
export interface AssistantMessageView extends AssistantMessage {
  readonly generation: {
    /** 有活动会话时为会话阶段；无活动会话为 null。 */
    readonly phase: GenerationPhase | null;
    /** 普通回答当前已展示正文（模拟流式进度）。 */
    readonly content: string;
    /** A/B 双候选各自已展示正文与引用（普通回答为 null）。 */
    readonly abContents: readonly {
      readonly candidate: 0 | 1;
      readonly content: string;
      readonly citations: readonly Citation[];
    }[] | null;
    /** 模拟是否已完整（含 reduced-motion 直出与停止保留）。 */
    readonly complete: boolean;
    /** 思考档阶段状态行（生成中）。 */
    readonly stage: SseStagePhase | null;
    /** 深度研究步骤（生成中）。 */
    readonly steps: readonly { readonly index: number; readonly label: string; readonly state: 'active' | 'done' }[];
    /** 系统提示条（生成中 / 已持久化）。 */
    readonly notices: readonly Notice[];
    /** 请求级错误（409 幂等键冲突等；按请求错误上抛）。 */
    readonly requestError: { readonly code: string; readonly messageKey: string } | null;
  };
}

export type ChatMessageView = UserMessage | AssistantMessageView;

export type ActionNotice =
  | { readonly type: 'feedback_conflict'; readonly messageId: string }
  | { readonly type: 'ab_conflict'; readonly messageId: string };

export interface ChatStoreState {
  readonly listStatus: ChatListStatus;
  /** 已加载会话（置顶在前、last_active_at 降序；分页按需加载，搜索过滤见 visibleConversations）。 */
  readonly conversations: readonly ConversationSummary[];
  /** 客户端实时过滤后的可见列表（服务端 q 模式下与 conversations 一致）。 */
  readonly visibleConversations: readonly ConversationSummary[];
  readonly groups: readonly ConversationGroup[];
  readonly searchQuery: string;
  readonly searchStatus: 'idle' | 'loading';
  /** 是否已走服务端 q（已加载量超阈值）。 */
  readonly serverFiltered: boolean;
  /** 当前列表请求使用的 limit（A25：「加载更多」按页递增，后端上限 200）。 */
  readonly listLimit: number;
  /** 「加载更多」是否还能取到更多（上次响应满页且 limit 未达上限）。 */
  readonly hasMoreConversations: boolean;
  /** 「加载更多」请求进行中。 */
  readonly loadingMore: boolean;

  readonly conversationStatus: ChatConversationStatus;
  readonly conversationId: string | null;
  readonly conversation: ConversationDetail | null;
  /** 合并读模型与活动会话后的消息视图。 */
  readonly messages: readonly ChatMessageView[];

  /** 当前活动 generation 会话（ask/retry/recover；含重连/停止/终态）。 */
  readonly session: ReturnType<GenerationSession['getView']> | null;
  /** 反馈 / A/B 冲突提示（409 后刷新读模型保留首次结果；一次性展示）。 */
  readonly actionNotice: ActionNotice | null;
  /** 提交中的反馈 / A/B 投票（m2：结果未知前锁定控件，禁连击同键不同请求体）。 */
  readonly pendingSubmits: readonly { readonly kind: 'feedback' | 'ab-vote'; readonly messageId: string }[];
}

interface PendingAsk {
  readonly localId: string;
  readonly content: string;
  readonly createdAt: string;
  /** start 事件下发后绑定真实 user_message_id（M16：按 id 去重本地气泡）。 */
  userMessageId: string | null;
  /** 本地气泡视图构建一次、跨 tick 透传同一引用（A28）。 */
  readonly localView: UserMessage;
}

interface MessageOverlay {
  readonly messageId: string;
  readonly session: GenerationSession;
  readonly conversationId: string;
  /** 合成视图（读模型未含该消息时）的回答时间戳：overlay 创建时刻，稳定不变。 */
  readonly localCreatedAt: string;
  /** 重试链：链首 generation；ask 为自身 generation_id。 */
  readonly rootGenerationId: string | null;
  readonly retryOfGenerationId: string | null;
  simulator: StreamingSimulator | null;
  readonly abSimulators: Map<0 | 1, StreamingSimulator>;
  /** 每候选引用（M7：实时 A/B 候选引用角标需按候选携带）。 */
  readonly abCitationMap: Map<0 | 1, readonly Citation[]>;
  simulatedText: string;
  readonly abTexts: Map<0 | 1, string>;
  complete: boolean;
  terminalHandled: boolean;
}

const DEFAULT_SERVER_SEARCH_THRESHOLD = 500;
/** A25 分页：首页与「加载更多」单次递增量；后端 le=200 上限，达到后不再展示加载更多。 */
const CONVERSATIONS_PAGE_SIZE = 50;
const CONVERSATIONS_MAX_LIMIT = 200;

const INITIAL_STATE: ChatStoreState = {
  listStatus: 'idle',
  conversations: [],
  visibleConversations: [],
  groups: [],
  searchQuery: '',
  searchStatus: 'idle',
  serverFiltered: false,
  listLimit: CONVERSATIONS_PAGE_SIZE,
  hasMoreConversations: false,
  loadingMore: false,
  conversationStatus: 'idle',
  conversationId: null,
  conversation: null,
  messages: [],
  session: null,
  actionNotice: null,
  pendingSubmits: [],
};

let pendingSeq = 0;

export class ChatStore {
  private state: ChatStoreState = INITIAL_STATE;
  private readonly listeners = new Set<() => void>();
  private readonly generationDeps: GenerationSessionDeps;
  private readonly serverSearchThreshold: number;
  private sessions = new Map<string, GenerationSession>();
  private overlays = new Map<string, MessageOverlay>();
  /**
   * A28：无 overlay 的 assistant 消息视图缓存——读模型消息对象引用不变时透传同一视图，
   * 流式 tick 不再全量重建数组元素（这是 AssistantMessage memo 生效的前提）。
   * 仅缓存 attachEmptyGeneration 的确定性产物；有 overlay 的活动消息每 tick 更新引用。
   */
  private assistantViews = new Map<string, { base: AssistantMessage; view: AssistantMessageView }>();
  private activeSession: GenerationSession | null = null;
  private pendingAsk: PendingAsk | null = null;
  private feedbackKeys = new Map<string, string>();
  private abKeys = new Map<string, string>();
  private openSeq = 0;
  private listSeq = 0;
  /** 新会话入口单飞（openOrCreateNewConversation）：连点/多入口并发共享同一在飞请求，禁止重复创建。 */
  private newConversationInflight: Promise<ConversationSummary | null> | null = null;

  constructor(private readonly deps: ChatStoreDeps) {
    this.serverSearchThreshold = deps.serverSearchThreshold ?? DEFAULT_SERVER_SEARCH_THRESHOLD;
    this.generationDeps = {
      api: deps.api,
      getToken: deps.getToken,
      refresh: deps.refresh,
      now: deps.now,
      config: deps.config,
    };
  }

  getState(): ChatStoreState {
    return this.state;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  dispose(): void {
    for (const session of this.sessions.values()) {
      session.dispose();
    }
    for (const overlay of this.overlays.values()) {
      overlay.simulator?.dispose();
      for (const simulator of overlay.abSimulators.values()) {
        simulator.dispose();
      }
    }
    this.sessions.clear();
    this.overlays.clear();
    this.assistantViews.clear();
    this.listeners.clear();
  }

  /* ---------- 会话列表 / 搜索 ---------- */

  async loadConversationList(): Promise<void> {
    const seq = ++this.listSeq; // 服务端过滤模式下并发加载，慢的旧响应不得覆盖新结果
    this.setState({ listStatus: 'loading', searchStatus: this.state.searchQuery.trim() !== '' ? 'loading' : 'idle' });
    try {
      const q = this.shouldServerSearch(this.state.searchQuery) ? this.state.searchQuery : undefined;
      const result = await this.deps.api.listConversations(q, CONVERSATIONS_PAGE_SIZE);
      if (seq !== this.listSeq) return; // 已有更新的加载请求
      const sorted = sortConversations(result.items);
      const serverFiltered = q !== undefined;
      this.setState({
        conversations: sorted,
        groups: result.groups,
        listStatus: 'ready',
        searchStatus: 'idle',
        serverFiltered,
        listLimit: CONVERSATIONS_PAGE_SIZE,
        hasMoreConversations: result.items.length >= CONVERSATIONS_PAGE_SIZE,
        loadingMore: false,
      });
      this.recomputeVisible();
    } catch {
      if (seq !== this.listSeq) return;
      // A25：刷新与「加载更多」共用代际——刷新结果落地时一并解除加载更多的 in-flight 锁
      this.setState({ listStatus: 'error', searchStatus: 'idle', loadingMore: false });
    }
  }

  /**
   * A25「加载更多」：后端仅支持 limit（无 offset/cursor），按页递增 limit 重取并整体替换——
   * 服务端按 last_active_at 降序返回，旧前缀不变。失败静默保留已加载数据，可再次点击。
   */
  async loadMoreConversations(): Promise<void> {
    if (this.state.loadingMore || !this.state.hasMoreConversations) return;
    const nextLimit = Math.min(this.state.listLimit + CONVERSATIONS_PAGE_SIZE, CONVERSATIONS_MAX_LIMIT);
    const seq = ++this.listSeq; // 与首屏加载共用代际；「加载更多」与刷新互斥，后完成者为准
    this.setState({ loadingMore: true });
    try {
      const q = this.shouldServerSearch(this.state.searchQuery) ? this.state.searchQuery : undefined;
      const result = await this.deps.api.listConversations(q, nextLimit);
      if (seq !== this.listSeq) return; // 已有更新的加载请求
      const sorted = sortConversations(result.items);
      this.setState({
        conversations: sorted,
        groups: result.groups,
        listLimit: nextLimit,
        hasMoreConversations: result.items.length >= nextLimit && nextLimit < CONVERSATIONS_MAX_LIMIT,
        loadingMore: false,
      });
      this.recomputeVisible();
    } catch {
      if (seq !== this.listSeq) return;
      this.setState({ loadingMore: false });
    }
  }

  setSearchQuery(query: string): void {
    this.setState({ searchQuery: query });
    // M8：服务端过滤模式下，关键词清空/变化时重新全量拉取（子集不能当全量）
    if (this.state.serverFiltered) {
      void this.loadConversationList();
      return;
    }
    if (this.shouldServerSearch(query)) {
      void this.loadConversationList();
    } else {
      this.recomputeVisible();
    }
  }

  private shouldServerSearch(query: string): boolean {
    return query.trim() !== '' && this.state.conversations.length > this.serverSearchThreshold;
  }

  private recomputeVisible(): void {
    const query = this.state.searchQuery.trim().toLowerCase();
    if (query === '' || this.state.serverFiltered) {
      this.setState({ visibleConversations: this.state.conversations });
      return;
    }
    this.setState({
      visibleConversations: this.state.conversations.filter((item) =>
        item.title.toLowerCase().includes(query),
      ),
    });
  }

  /* ---------- 会话与分组 CRUD ---------- */

  async createConversation(): Promise<ConversationSummary | null> {
    try {
      const summary = await this.deps.api.createConversation();
      await this.loadConversationList();
      // 新建会话：直接打开空会话（新会话默认「快速」由 UI 本地状态负责，store 不记忆档位）
      this.pendingAsk = null;
      this.activeSession = null;
      this.setState({
        conversationStatus: 'ready',
        conversationId: summary.id,
        conversation: {
          id: summary.id,
          title: summary.title,
          effort_level: 'quick',
          scope: { space_ids: [], document_ids: [] },
          messages: [],
        },
        messages: [],
        session: null,
        actionNotice: null,
      });
      return summary;
    } catch {
      return null;
    }
  }

  /**
   * 新会话唯一入口（新登录落地 / 侧栏「新建会话」 / 空会话首问）。
   * 已有未命名会话（title === ''，首条提问生成标题前）→ 指向它（已在该会话则无操作）；
   * 没有 → 创建并打开。任何时刻最多一个新会话；并发调用单飞，禁止重复创建。
   */
  openOrCreateNewConversation(): Promise<ConversationSummary | null> {
    this.newConversationInflight ??= this.doOpenOrCreateNewConversation().finally(() => {
      this.newConversationInflight = null;
    });
    return this.newConversationInflight;
  }

  private async doOpenOrCreateNewConversation(): Promise<ConversationSummary | null> {
    const fresh = this.state.conversations.find((item) => item.title === '');
    if (fresh !== undefined) {
      if (this.state.conversationId !== fresh.id) {
        await this.openConversation(fresh.id);
      }
      return fresh;
    }
    return this.createConversation();
  }

  async patchConversation(id: string, patch: { title?: string; pinned?: boolean; group_id?: string | null }): Promise<boolean> {
    try {
      await this.deps.api.patchConversation(id, patch);
      await this.loadConversationList();
      if (this.state.conversationId === id && (patch.title !== undefined || patch.pinned !== undefined)) {
        await this.refreshConversation();
      }
      return true;
    } catch {
      // 失败不再静默：返回 false，由侧栏调用方轻提示（A38），列表待下次 load 收敛
      return false;
    }
  }

  async deleteConversation(id: string): Promise<boolean> {
    try {
      await this.deps.api.deleteConversation(id);
      if (this.state.conversationId === id) {
        this.setState({ conversation: null, conversationId: null, messages: [], session: null });
      }
      await this.loadConversationList();
      return true;
    } catch {
      return false;
    }
  }

  /** 创建分组并返回新分组 id（失败返回 null）；m5：移入分组→新建分组后直接移入。 */
  async createGroup(name: string): Promise<string | null> {
    try {
      const group = await this.deps.api.createConversationGroup(name);
      await this.loadConversationList();
      return group.id;
    } catch {
      return null;
    }
  }

  async patchGroup(id: string, name: string): Promise<boolean> {
    try {
      await this.deps.api.patchConversationGroup(id, name);
      await this.loadConversationList();
      return true;
    } catch {
      return false;
    }
  }

  async deleteGroup(id: string): Promise<boolean> {
    try {
      await this.deps.api.deleteConversationGroup(id);
      await this.loadConversationList();
      return true;
    } catch {
      return false;
    }
  }

  /* ---------- 当前会话 ---------- */

  async openConversation(id: string): Promise<void> {
    const seq = ++this.openSeq; // m7：快速连点旧响应覆盖新会话的竞态丢弃
    this.activeSession = null;
    this.pendingAsk = null;
    this.setState({
      conversationStatus: 'loading',
      conversationId: id,
      session: null,
      actionNotice: null,
    });
    try {
      const conversation = await this.deps.api.getConversation(id);
      if (seq !== this.openSeq) return; // 已有更新的打开请求
      this.setState({ conversation, conversationStatus: 'ready' });
      this.resumeGenerating(conversation);
      this.recomputeMessages();
    } catch {
      if (seq !== this.openSeq) return;
      this.setState({ conversationStatus: 'error' });
    }
  }

  /** 刷新读模型（409 反馈/A-B 冲突、终态后权威收敛、UI 主动刷新）。 */
  async refreshMessage(_messageId: string): Promise<void> {
    await this.refreshConversation();
  }

  private async refreshConversation(): Promise<void> {
    const id = this.state.conversationId;
    if (id === null) return;
    try {
      const conversation = await this.deps.api.getConversation(id);
      this.setState({ conversation, conversationStatus: 'ready' });
      this.recomputeMessages();
    } catch {
      // 保留现有数据
    }
  }

  /** 打开会话时：对读模型中 status=generating 的 assistant 消息恢复订阅（页面刷新恢复）。 */
  private resumeGenerating(conversation: ConversationDetail): void {
    for (const message of conversation.messages) {
      if (message.role !== 'assistant' || message.status !== 'generating') continue;
      if (this.overlays.has(message.id)) continue;
      const session = GenerationSession.launchRecover(
        this.generationDeps,
        conversation.id,
        message.generation_id,
        null,
      );
      this.trackSession(session, message.id, conversation.id, message.root_generation_id, null);
    }
  }

  /* ---------- 提问 / 停止 / 重试 ---------- */

  async ask(content: string, effortLevel: EffortLevel, scope?: ConversationScope): Promise<boolean> {
    const conversationId = this.state.conversationId;
    if (conversationId === null || content.trim() === '') return false;
    // M2：清掉同会话 requestError 残留会话（已无活动流），避免泄漏与二次提问软锁
    this.disposeStaleSessions(conversationId);
    const session = GenerationSession.launchAsk(this.generationDeps, conversationId, {
      content,
      effort_level: effortLevel,
      scope,
      overrides: null,
    });
    this.activeSession = session;
    const localId = `local_user_${++pendingSeq}`;
    const createdAt = new Date().toISOString();
    this.pendingAsk = {
      localId,
      content,
      createdAt,
      userMessageId: null,
      localView: { id: localId, role: 'user', content, created_at: createdAt },
    };
    this.setState({ actionNotice: null });
    this.trackSession(session, null, conversationId, null, null);
    return true;
  }

  /** 清理指定会话内已 requestError（无活动流、非终态）的残留 generation 会话。 */
  private disposeStaleSessions(conversationId: string): void {
    for (const [id, session] of this.sessions) {
      const view = session.getView();
      if (view.conversationId !== conversationId) continue;
      if (view.terminal !== null || view.requestError === null) continue;
      session.dispose();
      this.sessions.delete(id);
    }
  }

  /** 停止当前会话正在运行的 generation（含刷新恢复的会话）；点击即「正在停止」禁重复。 */
  stop(): void {
    this.currentGenerationSession()?.requestStop();
  }

  /** 当前会话的运行中 generation（ask/retry/recover 任一来源；已终态/已停止不算）。 */
  private currentGenerationSession(): GenerationSession | null {
    const conversationId = this.state.conversationId;
    if (conversationId === null) return null;
    for (const session of this.sessions.values()) {
      const view = session.getView();
      if (view.conversationId === conversationId && view.terminal === null && view.start !== null) {
        return session;
      }
    }
    return null;
  }

  /** 失败重试：仅 failed 消息可重试；复用原 user_message_id（服务端）；新 Idempotency-Key 由会话层生成。 */
  async retry(messageId: string): Promise<void> {
    // 从合并消息视图查找：失败消息可能尚未回到读模型（finalize 后的异步 refresh 有滞后），
    // UI 始终从渲染的消息上触发重试，与读模型一致地携带 generation 字段链。
    const message = this.state.messages.find(
      (candidate): candidate is AssistantMessageView =>
        candidate.id === messageId && candidate.role === 'assistant',
    );
    if (message === undefined || message.status !== 'failed') return;
    const session = GenerationSession.launchRetry(this.generationDeps, message.generation_id);
    this.activeSession = session;
    this.setState({ actionNotice: null });
    this.trackSession(session, null, this.state.conversationId ?? '', message.root_generation_id, message.generation_id);
  }

  /* ---------- 反馈 / A/B 投票 ---------- */

  /** 反馈：Idempotency-Key 幂等；网络未知复用同键（不换键）；409 刷新读模型保留首次事实。 */
  async submitFeedback(messageId: string, vote: FeedbackVoteRequest): Promise<void> {
    const key = this.feedbackKeys.get(messageId) ?? createIdempotencyKey();
    this.feedbackKeys.set(messageId, key);
    this.addPendingSubmit('feedback', messageId);
    try {
      await this.deps.api.submitFeedback(messageId, vote, key);
      this.feedbackKeys.delete(messageId);
      await this.refreshConversation();
    } catch (error) {
      if (
        error instanceof ApiError &&
        (error.code === 'feedback_already_submitted' || error.code === 'idempotency_key_conflict')
      ) {
        this.feedbackKeys.delete(messageId);
        this.setState({ actionNotice: { type: 'feedback_conflict', messageId } });
        await this.refreshConversation();
        return;
      }
      // 网络失败：保留键供 UI 以原请求体重试（不换键）
      throw error;
    } finally {
      this.removePendingSubmit('feedback', messageId);
    }
  }

  /** A/B 投票：choice 0/1/neither 按 answer 事件 candidate 序号计（与左右解耦）；409 刷新读模型不改票。 */
  async submitAbVote(messageId: string, choice: AbChoice): Promise<void> {
    // C1：从合并视图取 pair_id——实时 A/B 会话的读模型不刷新，合并视图（state.messages）才是权威
    const message = this.state.messages.find(
      (candidate): candidate is AssistantMessageView =>
        candidate.id === messageId && candidate.role === 'assistant',
    );
    const pairId = message?.ab?.pair_id ?? null;
    if (pairId === null) return;
    const key = this.abKeys.get(pairId) ?? createIdempotencyKey();
    this.abKeys.set(pairId, key);
    this.addPendingSubmit('ab-vote', messageId);
    try {
      await this.deps.api.submitAbVote(messageId, { pair_id: pairId, choice }, key);
      this.abKeys.delete(pairId);
      await this.refreshConversation();
    } catch (error) {
      if (
        error instanceof ApiError &&
        (error.code === 'ab_vote_already_submitted' ||
          error.code === 'ab_pair_expired' ||
          error.code === 'idempotency_key_conflict')
      ) {
        this.abKeys.delete(pairId);
        this.setState({ actionNotice: { type: 'ab_conflict', messageId } });
        await this.refreshConversation();
        return;
      }
      throw error;
    } finally {
      this.removePendingSubmit('ab-vote', messageId);
    }
  }

  private addPendingSubmit(kind: 'feedback' | 'ab-vote', messageId: string): void {
    this.setState({
      pendingSubmits: [...this.state.pendingSubmits, { kind, messageId }],
    });
  }

  private removePendingSubmit(kind: 'feedback' | 'ab-vote', messageId: string): void {
    this.setState({
      pendingSubmits: this.state.pendingSubmits.filter(
        (item) => !(item.kind === kind && item.messageId === messageId),
      ),
    });
  }

  /* ---------- §6.1/§6.2 知识空间与文档（检索范围 chip） ---------- */

  /** 拉取检索空间（usage=retrieval；消费方为输入区 scope chip）。 */
  async fetchSpaces(): Promise<readonly import('./types').SpaceItem[] | null> {
    try {
      const response = await this.deps.api.listSpaces('retrieval');
      return response.items;
    } catch {
      return null;
    }
  }

  /** 拉取空间文档列表（scope chip 下钻）；失败返回 null 由 UI 就地降级。 */
  async fetchSpaceDocuments(spaceId: string, q?: string): Promise<SpaceDocumentsResponse | null> {
    try {
      return await this.deps.api.listDocuments(spaceId, q);
    } catch {
      return null;
    }
  }

  /* ---------- 会话层内部 ---------- */

  private trackSession(
    session: GenerationSession,
    messageId: string | null,
    conversationId: string,
    rootGenerationId: string | null,
    retryOfGenerationId: string | null,
  ): void {
    session.subscribe(() =>
      this.onSessionChanged(session, messageId, conversationId, rootGenerationId, retryOfGenerationId),
    );
    this.sessions.set(session.getView().id, session);
    if (messageId !== null) {
      this.overlays.set(messageId, this.createOverlay(session, messageId, conversationId, rootGenerationId, retryOfGenerationId));
    }
    this.setState({ session: session.getView() });
    this.recomputeMessages();
  }

  private createOverlay(
    session: GenerationSession,
    messageId: string,
    conversationId: string,
    rootGenerationId: string | null,
    retryOfGenerationId: string | null,
  ): MessageOverlay {
    return {
      messageId,
      session,
      conversationId,
      localCreatedAt: new Date().toISOString(),
      rootGenerationId,
      retryOfGenerationId,
      simulator: null,
      abSimulators: new Map(),
      abCitationMap: new Map(),
      simulatedText: '',
      abTexts: new Map(),
      complete: false,
      terminalHandled: false,
    };
  }

  private onSessionChanged(
    session: GenerationSession,
    knownMessageId: string | null,
    conversationId: string,
    rootGenerationId: string | null,
    retryOfGenerationId: string | null,
  ): void {
    const view = session.getView();
    const messageId = view.start?.messageId ?? knownMessageId;
    // M16：start 到达后把本地气泡绑定到真实 user_message_id（id 去重，不再按内容匹配）
    if (view.start !== null && this.pendingAsk !== null && this.pendingAsk.userMessageId === null) {
      this.pendingAsk = { ...this.pendingAsk, userMessageId: view.start.userMessageId };
    }
    let overlay = messageId === null ? undefined : this.overlays.get(messageId);
    if (messageId !== null && overlay === undefined) {
      overlay = this.createOverlay(session, messageId, conversationId, rootGenerationId, retryOfGenerationId);
      this.overlays.set(messageId, overlay);
    }
    if (overlay !== undefined) {
      this.feedSimulators(overlay);
    }
    // state.session 呈现「当前会话的运行中 generation」（ask/retry/recover 任一来源），
    // 刷新恢复的会话同样驱动停止键（M6 的 UI 可见性）
    if (this.activeSession === session || view.conversationId === this.state.conversationId) {
      this.setState({ session: view });
    }
    this.recomputeMessages();
    if (overlay !== undefined) {
      this.handleTerminal(overlay);
    }
  }

  private feedSimulators(overlay: MessageOverlay): void {
    const view = overlay.session.getView();
    // N3：ab_start 后（generation 层已把 answer 迁入 candidates[0]）接管主模拟器为 candidate 0，
    // 保留已模拟进度，禁止再为 candidate 0 新建第二个模拟器。
    if (view.ab.pair_id !== null && overlay.simulator !== null) {
      if (!overlay.abSimulators.has(0)) {
        overlay.abSimulators.set(0, overlay.simulator);
        overlay.abTexts.set(0, overlay.simulatedText);
        const migrated = view.ab.candidates.find((candidate) => candidate.candidate === 0);
        if (migrated !== undefined) {
          overlay.abCitationMap.set(0, migrated.citations);
        }
      }
      overlay.simulator = null;
      // 主模拟器 onDone 可能已把 complete 置 true；A/B 固定双候选，需等两边都完成
      if (!this.allAbCandidatesComplete(overlay)) {
        overlay.complete = false;
      }
    }
    if (
      view.answer !== null &&
      overlay.simulator === null &&
      !overlay.abSimulators.has(0) &&
      overlay.simulatedText.length === 0 &&
      view.ab.pair_id === null
    ) {
      const target = overlay;
      overlay.simulator = this.createSimulator(
        (text) => {
          // M13/N3：ab_start 后仍在模拟的普通回答把进度写入候选 0
          if (target.session.getView().ab.pair_id !== null) {
            target.abTexts.set(0, text);
          } else {
            target.simulatedText = text;
          }
          this.recomputeMessages();
        },
        () => this.onSimulationDone(target),
        () => this.recomputeMessages(),
      );
      overlay.simulator.feed(view.answer.content);
    }
    for (const candidate of view.ab.candidates) {
      // 已接管 / 已创建的候选模拟器只同步引用，不双写
      if (overlay.abSimulators.has(candidate.candidate)) {
        overlay.abCitationMap.set(candidate.candidate, candidate.citations);
        continue;
      }
      const candidateKey = candidate.candidate;
      const sim = this.createSimulator(
        (text) => {
          overlay.abTexts.set(candidateKey, text);
          this.recomputeMessages();
        },
        () => {
          // C1/N1：按「已完成」候选数判断 complete，禁止先完成者提前 finalize
          this.onCandidateSimulationDone(overlay);
        },
        () => this.recomputeMessages(),
      );
      overlay.abSimulators.set(candidateKey, sim);
      overlay.abCitationMap.set(candidateKey, candidate.citations);
      sim.feed(candidate.content);
    }
  }

  private createSimulator(onText: (text: string) => void, onDone: () => void, onStop: () => void): StreamingSimulator {
    return createStreamingSimulator({
      reducedMotion: this.deps.getReducedMotion?.() ?? false,
      onText,
      onDone,
      onStop,
    });
  }

  private onSimulationDone(overlay: MessageOverlay): void {
    const view = overlay.session.getView();
    // N3：若 ab_start 已到（主模拟器可能已被/即将被接管为 candidate 0），不单独 complete
    if (view.ab.pair_id !== null) {
      this.onCandidateSimulationDone(overlay);
      return;
    }
    overlay.complete = true;
    if (view.terminal !== null) {
      this.finalizeOverlay(overlay);
    }
    this.recomputeMessages();
  }

  /** N1：A/B 候选模拟完成——按 isDone 计数；盲测固定双候选，两侧都播完才 complete/finalize。 */
  private onCandidateSimulationDone(overlay: MessageOverlay): void {
    const view = overlay.session.getView();
    if (view.ab.pair_id === null) {
      this.recomputeMessages();
      return;
    }
    if (this.allAbCandidatesComplete(overlay)) {
      overlay.complete = true;
      if (view.terminal !== null) {
        this.finalizeOverlay(overlay);
      }
    }
    this.recomputeMessages();
  }

  /** A/B 固定 2 候选：两个模拟器均已创建且 isDone 才算完成（禁止按「已创建数」提前 finalize）。 */
  private allAbCandidatesComplete(overlay: MessageOverlay): boolean {
    if (overlay.abSimulators.size < 2) return false;
    for (const simulator of overlay.abSimulators.values()) {
      if (!simulator.isDone()) return false;
    }
    return true;
  }

  private handleTerminal(overlay: MessageOverlay): void {
    // 必须先确认已有终态再置 terminalHandled——否则 start/answer 等中间事件会抢占标志，
    // 后续 done/error/stopped 被跳过，overlay/session 永不 finalize。
    const view = overlay.session.getView();
    const terminal = view.terminal;
    if (terminal === null) return;
    if (overlay.terminalHandled) return;
    overlay.terminalHandled = true;
    if (terminal.kind === 'stopped') {
      // M1：手动停止/断线/client 断开统一收敛到「已收稳定 answer」并 finalize 刷新读模型
      // （spec §4：停止仅保留已收到的稳定回答；authorization_revoked 同语义）
      overlay.simulator?.dispose();
      for (const simulator of overlay.abSimulators.values()) {
        simulator.dispose();
      }
      overlay.simulatedText = view.answer?.content ?? overlay.simulatedText;
      overlay.complete = true;
      this.finalizeOverlay(overlay);
    } else if (terminal.kind === 'error') {
      overlay.simulator?.stop();
      for (const simulator of overlay.abSimulators.values()) {
        simulator.stop();
      }
      overlay.complete = true;
      this.finalizeOverlay(overlay);
    } else if (overlay.complete) {
      // done：模拟已播完则直接收敛到读模型；否则等 onSimulationDone / onCandidateSimulationDone
      this.finalizeOverlay(overlay);
    }
    this.recomputeMessages();
  }

  private finalizeOverlay(overlay: MessageOverlay): void {
    // N1：finalize 时 dispose 主模拟器与全部候选模拟器，避免计时器泄漏
    overlay.simulator?.dispose();
    for (const simulator of overlay.abSimulators.values()) {
      simulator.dispose();
    }
    overlay.simulator = null;
    overlay.abSimulators.clear();
    this.overlays.delete(overlay.messageId);
    // sessions Map 可能以 pending id 入表、start 后 view.id 变为 generation_id——按 value 扫描删除
    this.removeSession(overlay.session);
    const view = overlay.session.getView();
    if (this.activeSession === overlay.session) {
      this.activeSession = null;
    }
    // recover 路径不设 activeSession，但仍把 view 写入 state.session——按 id 对齐清空，避免终态残留
    const displayed = this.state.session;
    const sameDisplayed =
      displayed !== null &&
      (displayed.id === view.id ||
        (view.start !== null &&
          (displayed.id === view.start.generationId ||
            displayed.start?.generationId === view.start.generationId)));
    if (sameDisplayed) {
      this.setState({ session: null });
    }
    overlay.session.dispose();
    void this.refreshConversation();
  }

  /** 按 session 对象引用删除（避免 pending id → generation id 键漂移导致删不掉）。 */
  private removeSession(target: GenerationSession): void {
    for (const [id, session] of this.sessions) {
      if (session === target) {
        this.sessions.delete(id);
      }
    }
  }

  /* ---------- 消息视图合并 ---------- */

  private recomputeMessages(): void {
    const conversation = this.state.conversation;
    const base = conversation?.messages ?? [];
    const out: ChatMessageView[] = [];
    const knownIds = new Set<string>();
    for (const message of base) {
      knownIds.add(message.id);
      if (message.role === 'user') {
        out.push(message);
        continue;
      }
      const overlay = this.overlays.get(message.id);
      if (overlay !== undefined) {
        // A28：活动流式消息是唯一每 tick 更新引用的元素
        out.push(this.mergeAssistant(message, overlay));
        continue;
      }
      // A28：无 overlay 时确定性视图按读模型引用缓存，透传同一对象（memo 生效前提）
      const cached = this.assistantViews.get(message.id);
      if (cached !== undefined && cached.base === message) {
        out.push(cached.view);
        continue;
      }
      const view = this.attachEmptyGeneration(message);
      this.assistantViews.set(message.id, { base: message, view });
      out.push(view);
    }
    if (this.pendingAsk !== null && !knownIds.has(this.pendingAsk.localId)) {
      // M16：本地气泡按 id 去重——start 已绑定真实 user_message_id 且读模型已含该消息时收起
      const realLanded =
        this.pendingAsk.userMessageId !== null && knownIds.has(this.pendingAsk.userMessageId);
      if (!realLanded) {
        out.push(this.pendingAsk.localView);
      }
    }
    for (const [messageId, overlay] of this.overlays) {
      if (knownIds.has(messageId)) continue;
      // M5：overlay 必须属于当前会话——A 生成中切到 B，A 的合成消息不得出现在 B
      if (overlay.conversationId !== this.state.conversationId) continue;
      if (overlay.session.getView().start === null) continue; // start 前由 state.session 呈现连接态
      out.push(this.synthesizeAssistant(overlay));
    }
    // M2：pre-start requestError 无 message_id / overlay——合成临时错误行，避免不可达
    this.appendPreStartRequestError(out, knownIds);
    // A28：切换会话后修剪不在当前读模型中的缓存条目（条目仅在本循环写入，量级≤当前消息数）
    if (this.assistantViews.size > knownIds.size) {
      for (const id of this.assistantViews.keys()) {
        if (!knownIds.has(id)) this.assistantViews.delete(id);
      }
    }
    this.setState({ messages: out });
  }

  /** pre-start 请求级错误：无 start 时合成 assistant 错误行（仅当前会话、仅活动 session）。 */
  private appendPreStartRequestError(out: ChatMessageView[], knownIds: Set<string>): void {
    const session = this.activeSession;
    if (session === null) return;
    const view = session.getView();
    if (view.requestError === null || view.start !== null) return;
    if (view.conversationId !== this.state.conversationId) return;
    const errorId = `local_reqerr_${view.id}`;
    if (knownIds.has(errorId) || out.some((message) => message.id === errorId)) return;
    out.push({
      id: errorId,
      role: 'assistant',
      content: '',
      created_at: this.pendingAsk?.createdAt ?? new Date().toISOString(),
      answer_mode: 'grounded',
      effort_level: 'quick',
      generation_id: view.id,
      root_generation_id: view.id,
      retry_of_generation_id: null,
      attempt_number: 1,
      status: 'failed',
      stop_reason: null,
      notices: [],
      citations: [],
      feedback: null,
      ab: null,
      generation: {
        phase: view.phase,
        content: '',
        abContents: null,
        complete: true,
        stage: null,
        steps: [],
        notices: [],
        requestError: view.requestError,
      },
    });
  }

  private attachEmptyGeneration(message: AssistantMessage): AssistantMessageView {
    return {
      ...message,
      generation: {
        phase: null,
        content: message.content,
        abContents: null,
        complete: true,
        stage: null,
        steps: [],
        notices: message.notices,
        requestError: null,
      },
    };
  }

  private mergeAssistant(message: AssistantMessage, overlay: MessageOverlay): AssistantMessageView {
    const view = overlay.session.getView();
    const status = this.sessionStatus(view);
    const isAb = view.ab.pair_id !== null;
    const content = isAb ? '' : overlay.simulatedText;
    const abContents = isAb
      ? [...overlay.abTexts.entries()].map(([candidate, text]) => ({
          candidate,
          content: text,
          citations: overlay.abCitationMap.get(candidate) ?? [],
        }))
      : null;
    return {
      ...message,
      content,
      status,
      stop_reason: view.terminal?.kind === 'stopped' ? view.terminal.stopReason : message.stop_reason,
      citations: view.answer !== null ? view.answer.citations : message.citations,
      notices: view.notices.length > 0 ? view.notices : message.notices,
      ab: isAb ? this.sessionAbToReadModel(view.ab) : message.ab,
      generation: {
        phase: view.phase,
        content,
        abContents,
        complete: overlay.complete,
        stage: view.stage,
        steps: view.steps,
        notices: view.notices.length > 0 ? view.notices : message.notices,
        requestError: view.requestError,
      },
    };
  }

  private synthesizeAssistant(overlay: MessageOverlay): AssistantMessageView {
    const view = overlay.session.getView();
    const start = view.start;
    const generationId = start?.generationId ?? overlay.messageId;
    const isAb = view.ab.pair_id !== null;
    const content = isAb ? '' : overlay.simulatedText;
    const abContents = isAb
      ? [...overlay.abTexts.entries()].map(([candidate, text]) => ({
          candidate,
          content: text,
          citations: overlay.abCitationMap.get(candidate) ?? [],
        }))
      : null;
    const base: AssistantMessage = {
      id: overlay.messageId,
      role: 'assistant',
      content,
      created_at: overlay.localCreatedAt,
      answer_mode: view.answer?.answer_mode ?? 'grounded',
      effort_level: view.answer?.effort_level ?? 'quick',
      generation_id: generationId,
      root_generation_id: overlay.rootGenerationId ?? generationId,
      retry_of_generation_id: overlay.retryOfGenerationId ?? null,
      attempt_number: start?.attemptNumber ?? 1,
      status: this.sessionStatus(view),
      stop_reason: view.terminal?.kind === 'stopped' ? view.terminal.stopReason : null,
      notices: view.notices,
      citations: view.answer?.citations ?? [],
      feedback: null,
      ab: isAb ? this.sessionAbToReadModel(view.ab) : null,
    };
    return {
      ...base,
      generation: {
        phase: view.phase,
        content,
        abContents,
        complete: overlay.complete,
        stage: view.stage,
        steps: view.steps,
        notices: view.notices,
        requestError: view.requestError,
      },
    };
  }

  private sessionStatus(view: ReturnType<GenerationSession['getView']>): AssistantMessageStatus {
    const terminal = view.terminal;
    if (terminal === null) return 'generating';
    if (terminal.kind === 'done') return 'completed';
    if (terminal.kind === 'error') return 'failed';
    return 'stopped';
  }

  private sessionAbToReadModel(ab: ReturnType<GenerationSession['getView']>['ab']): AbState {
    if (ab.pair_id === null) return null;
    const toCandidate = (candidate: { candidate: 0 | 1; content: string; citations: readonly Citation[]; answer_mode: AnswerMode }) => ({
      candidate: candidate.candidate,
      content: candidate.content,
      citations: candidate.citations,
      answer_mode: candidate.answer_mode,
    });
    if (ab.status === 'voted') {
      return { pair_id: ab.pair_id, status: 'voted', voted: true, choice: ab.choice ?? '0', candidates: null };
    }
    if (ab.status === 'open') {
      const first = ab.candidates[0];
      const second = ab.candidates[1];
      if (first === undefined || second === undefined) {
        return { pair_id: ab.pair_id, status: 'pending', voted: false, choice: null, candidates: ab.candidates.map(toCandidate) };
      }
      return {
        pair_id: ab.pair_id,
        status: 'open',
        voted: false,
        choice: null,
        candidates: [toCandidate(first), toCandidate(second)],
      };
    }
    return { pair_id: ab.pair_id, status: 'pending', voted: false, choice: null, candidates: ab.candidates.map(toCandidate) };
  }

  private setState(patch: Partial<ChatStoreState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) {
      listener();
    }
  }
}

/* ---------- 列表排序与分组（数据一次给全，渲染顺序固定：置顶 → 自定义分组 → 今天/本周/更早） ---------- */

export function sortConversations(items: readonly ConversationSummary[]): ConversationSummary[] {
  return [...items].sort(
    (a, b) => Number(b.pinned) - Number(a.pinned) || b.last_active_at.localeCompare(a.last_active_at),
  );
}

export type ConversationSectionKind = 'pinned' | 'group' | 'today' | 'week' | 'earlier';

export interface ConversationSection {
  readonly kind: ConversationSectionKind;
  readonly group: ConversationGroup | null;
  readonly items: readonly ConversationSummary[];
}

export function groupConversationList(
  items: readonly ConversationSummary[],
  groups: readonly ConversationGroup[],
  now: number = Date.now(),
): ConversationSection[] {
  const sections: ConversationSection[] = [];
  const pinned = items.filter((item) => item.pinned);
  const rest = items.filter((item) => !item.pinned);
  if (pinned.length > 0) {
    sections.push({ kind: 'pinned', group: null, items: pinned });
  }
  for (const group of groups) {
    const groupItems = rest.filter((item) => item.group_id === group.id);
    if (groupItems.length > 0) {
      sections.push({ kind: 'group', group, items: groupItems });
    }
  }
  const ungrouped = rest.filter((item) => item.group_id === null);
  const today = ungrouped.filter((item) => isSameDay(item.last_active_at, now));
  const week = ungrouped.filter((item) => !isSameDay(item.last_active_at, now) && isSameWeek(item.last_active_at, now));
  const earlier = ungrouped.filter((item) => !isSameDay(item.last_active_at, now) && !isSameWeek(item.last_active_at, now));
  if (today.length > 0) sections.push({ kind: 'today', group: null, items: today });
  if (week.length > 0) sections.push({ kind: 'week', group: null, items: week });
  if (earlier.length > 0) sections.push({ kind: 'earlier', group: null, items: earlier });
  return sections;
}

function isSameDay(iso: string, now: number): boolean {
  const date = new Date(iso);
  const ref = new Date(now);
  return (
    date.getFullYear() === ref.getFullYear() &&
    date.getMonth() === ref.getMonth() &&
    date.getDate() === ref.getDate()
  );
}

/** 本周：与参考时间同一自然周（周一为起点）。 */
function isSameWeek(iso: string, now: number): boolean {
  const date = new Date(iso);
  const ref = new Date(now);
  const startOfWeek = (value: Date): number => {
    const day = value.getDay();
    const diff = day === 0 ? 6 : day - 1;
    const start = new Date(value);
    start.setHours(0, 0, 0, 0);
    start.setDate(value.getDate() - diff);
    return start.getTime();
  };
  const weekStart = startOfWeek(ref);
  const weekEnd = weekStart + 7 * 24 * 60 * 60 * 1000;
  const time = date.getTime();
  return time >= weekStart && time < weekEnd;
}
