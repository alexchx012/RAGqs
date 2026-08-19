/*
 * 会话与问答契约 mock 核心（fe-chat-home 规格 §8；契约《前端接口需求.md》§3、§6.1）。
 * 与传输层无关（chat-handlers.ts 负责 MSW 接线），真实模拟：
 * - §3.1–§3.6 会话与分组 CRUD（内存数据集；会话不分页、排序一次给全，q 按标题过滤）；
 * - §3.7 提问 SSE：start→stage/step/notice→answer→终态完整序列，事件 id=event_seq 单调递增（start=1）、
 *   深度档含多条 step、A/B 序列含 ab_start+双 answer；error / stopped 终态经夹具触发；
 *   GET /generations/{id}/events 支持 Last-Event-ID 重放（只发之后的事件再接实时）；
 * - Idempotency-Key 语义：同键同请求重放原结果、同键不同请求 409 idempotency_key_conflict；
 *   反馈新键重复 409 feedback_already_submitted；A/B 换键重复 409 ab_vote_already_submitted、过期 409 ab_pair_expired；
 * - §3.3 读模型：A/B open / voted 两态、feedback 已投态、重试链、generating 状态消息（供刷新恢复）；
 * - §6.1 三种 usage 返回集（retrieval/upload/manage；权限与可见性按角色推导，前端只透传）。
 * 事件序列在提问时一次性物化（模拟「先持久化再发送」）；心跳 comment 由 handler 在流内发送，不占 event_seq。
 */

import type { Role } from '../auth/types';
import type {
  AbCandidate,
  AbChoice,
  AbState,
  AnswerMode,
  AskRequest,
  AssistantMessageStatus,
  Citation,
  ConversationDetail,
  ConversationGroup,
  ConversationMessage,
  ConversationScope,
  ConversationSummary,
  EffortLevel,
  FeedbackState,
  GenerationStatus,
  Notice,
  SpaceItem,
  SpaceKind,
  SpacePermission,
  SpaceUsage,
  SseGenerationEvent,
  StopReason,
} from '../chat/types';
import { MockHttpError } from './auth-contract';

export { MockHttpError };

/** 鉴权注入：装配处用 MockAuthController.me 实现；无有效 Bearer 时抛 MockHttpError(401)。 */
export interface ValidateChatAuth {
  (header: string | null): { userId: string; role: Role; departmentId: string | null };
}

interface StoredSseEvent {
  readonly seq: number;
  readonly event: SseGenerationEvent['event'];
  readonly data: Record<string, unknown>;
}

interface IdemRecord {
  readonly kind: 'ask' | 'retry' | 'feedback' | 'ab-vote';
  readonly normalized: string;
  /** ask/retry 重放目标。 */
  readonly generationId?: string;
  /** feedback/ab-vote 重放目标（读取 messageId 上已存结果）。 */
  readonly messageId?: string;
}

interface MockUserRow {
  readonly id: string;
  readonly role: 'user';
  readonly content: string;
  readonly created_at: string;
}

interface MockAssistantRow {
  readonly id: string;
  readonly role: 'assistant';
  readonly created_at: string;
  readonly generation_id: string;
  readonly root_generation_id: string;
  readonly retry_of_generation_id: string | null;
  readonly attempt_number: number;
  readonly feedback: FeedbackState;
}

interface MockConversation {
  readonly id: string;
  readonly ownerUserId: string;
  title: string;
  pinned: boolean;
  groupId: string | null;
  lastActiveAt: string;
  effortLevel: EffortLevel;
  scope: ConversationScope;
  messages: Array<MockUserRow | MockAssistantRow>;
}

interface MockGroup {
  readonly id: string;
  name: string;
}

interface MockGeneration {
  readonly id: string;
  readonly conversationId: string;
  readonly userMessageId: string;
  readonly messageId: string;
  readonly attemptNumber: number;
  readonly rootGenerationId: string;
  readonly retryOfGenerationId: string | null;
  readonly effortLevel: EffortLevel;
  readonly answerMode: AnswerMode;
  /** 普通（非 A/B）回答的稳定正文；A/B 回答正文在 candidates。 */
  readonly answerContent: string;
  /** 普通回答的引用与 notice。 */
  readonly answerCitations: readonly Citation[];
  readonly notices: readonly Notice[];
  status: GenerationStatus;
  stopReason: StopReason | null;
  /** M12：事件可变追加（运行中 generation 后续补发 stopped 等事件）。 */
  events: StoredSseEvent[];
  readonly pairId: string | null;
  /** M15：'terminal' = 终态（error/stopped）A/B pair——不可投，前端按普通回答处理。 */
  abStatus: 'none' | 'pending' | 'open' | 'voted' | 'terminal';
  abChoice: AbChoice | null;
  abExpired: boolean;
  readonly candidates: AbCandidate[];
}

/* ---------- §6.1 空间数据集 ---------- */

interface MockSpaceDef {
  readonly id: string;
  readonly kind: SpaceKind;
  readonly name: string;
  readonly documentCount: number;
  readonly ownerUserId?: string;
  readonly departmentId?: string;
  readonly departmentStatus?: 'active' | 'inactive';
  readonly isPublic?: boolean;
}

const SPACE_DEFS: readonly MockSpaceDef[] = [
  { id: 'personal:u_user', kind: 'personal', name: '个人库', documentCount: 12, ownerUserId: 'u_user' },
  { id: 'personal:u_minister', kind: 'personal', name: '个人库', documentCount: 5, ownerUserId: 'u_minister' },
  { id: 'personal:u_ops', kind: 'personal', name: '个人库', documentCount: 3, ownerUserId: 'u_ops' },
  { id: 'personal:u_admin', kind: 'personal', name: '个人库', documentCount: 2, ownerUserId: 'u_admin' },
  { id: 'department:d_finance', kind: 'department', name: '财务部', documentCount: 40, departmentId: 'd_finance', departmentStatus: 'active' },
  { id: 'department:d_hr', kind: 'department', name: '人事部', documentCount: 18, departmentId: 'd_hr', departmentStatus: 'active' },
  { id: 'department:d_archived', kind: 'department', name: '已归档部门', documentCount: 7, departmentId: 'd_archived', departmentStatus: 'inactive' },
  // 管理端部门库下钻（§7.3）：与 admin 部门种子一致（d_empty 空库、d_legacy 已停用）
  { id: 'department:d_empty', kind: 'department', name: '空壳部', documentCount: 0, departmentId: 'd_empty', departmentStatus: 'active' },
  { id: 'department:d_legacy', kind: 'department', name: '档案部', documentCount: 4, departmentId: 'd_legacy', departmentStatus: 'inactive' },
  { id: 'public', kind: 'public', name: '公共库', documentCount: 300, isPublic: true },
];

interface MockDocumentDef {
  readonly id: string;
  readonly spaceId: string;
  readonly documentVersionId: string;
  readonly version: number;
  readonly name: string;
  readonly mediaKind: string;
  readonly uploadedAt: string;
  readonly usage: { pages: number; images: number };
}

/** §6.2 文档种子：个人库（zhangsan）文档级收窄样例 + 公共库样例。 */
const DOCUMENT_DEFS: readonly MockDocumentDef[] = [
  { id: 'doc_1', spaceId: 'personal:u_user', documentVersionId: 'dv_1_3', version: 3, name: '员工手册.pdf', mediaKind: 'pdf', uploadedAt: '2026-07-20T02:00:00Z', usage: { pages: 50, images: 40 } },
  { id: 'doc_2', spaceId: 'personal:u_user', documentVersionId: 'dv_2_1', version: 1, name: '报销制度.docx', mediaKind: 'word', uploadedAt: '2026-07-18T09:30:00Z', usage: { pages: 12, images: 0 } },
  { id: 'doc_3', spaceId: 'personal:u_user', documentVersionId: 'dv_3_2', version: 2, name: '年假政策.md', mediaKind: 'md', uploadedAt: '2026-07-10T04:00:00Z', usage: { pages: 3, images: 0 } },
  { id: 'doc_9', spaceId: 'public', documentVersionId: 'dv_9_3', version: 3, name: '公共制度汇编.pdf', mediaKind: 'pdf', uploadedAt: '2026-06-01T00:00:00Z', usage: { pages: 200, images: 10 } },
];

/** 空间权限 / 可见性按角色推导（契约 §6.1；前端只渲染返回项，不自行推导）。 */
function permissionOf(def: MockSpaceDef, role: Role, userId: string, departmentId: string | null): SpacePermission | null {
  if (def.isPublic === true) {
    return role === 'ops' || role === 'admin' ? 'manage' : 'contribute';
  }
  if (def.kind === 'personal') {
    // 他人个人库只读（manage 集合才返回）；本人个人库 manage
    return def.ownerUserId === userId ? 'manage' : 'read';
  }
  if (def.kind === 'department') {
    if (def.departmentStatus === 'inactive') {
      // inactive 部门库只对 ops/admin 返回且固定 read
      return role === 'ops' || role === 'admin' ? 'read' : null;
    }
    if (role === 'ops' || role === 'admin') {
      return 'manage';
    }
    if (departmentId !== null && def.departmentId === departmentId) {
      // 本部门：部长 manage、普通用户 read
      return role === 'minister' ? 'manage' : 'read';
    }
    return null;
  }
  return null;
}

/* ---------- 控制器 ---------- */

/** 种子样例标题（后端下发文案）：提取为常量供 e2e 引用同一数据源，避免测试硬编码重复。 */
export const CHAT_SEED_TITLES = {
  abCompare: 'A/B 对比示例',
} as const;

/** 种子引用文档名（后端下发文档元数据）：e2e 断言悬停卡时引用，不硬编码中文。 */
export const CHAT_SEED_DOCUMENT_NAMES = {
  employeeHandbook: '员工手册.pdf',
} as const;

/** SSE 单帧序列化（event_seq + event + data JSON；心跳 comment 由 handler 组帧，不占序号）。 */
export function sseEventFrame(seq: number, event: string, data: Record<string, unknown>): string {
  return `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** 断线恢复流（M12）：推送回调 + 关闭回调。 */
interface LiveStreamHandle {
  readonly push: (frame: string) => void;
  readonly close: () => void;
}

export class MockChatController {
  private conversations = new Map<string, MockConversation>();
  private groups = new Map<string, MockGroup>();
  private generations = new Map<string, MockGeneration>();
  private idemByUser = new Map<string, Map<string, IdemRecord>>();
  private seq = 0;
  /** M12：活动恢复流注册表（generationId → 打开的流句柄；运行中 generation 保持流打开）。 */
  private liveStreams = new Map<string, Set<LiveStreamHandle>>();
  /** 夹具：后续 ask 命中 A/B 采样（§3.7 ab_start）。 */
  abEnabled = false;
  /** 夹具：后续 ask 以 error 终态结束（code 可覆盖）。 */
  private nextErrorCode: string | null = null;
  /** 夹具：后续 ask 以 stopped 终态结束（stop_reason 可覆盖）。 */
  private nextStopReason: StopReason | null = null;

  constructor(
    private readonly validateAuth: ValidateChatAuth,
    private readonly onAssistantMessageCreated?: (
      messageId: string,
      ownerUserId: string,
      citations: readonly Citation[],
    ) => void,
  ) {
    this.reset();
  }

  reset(): void {
    this.conversations.clear();
    this.groups.clear();
    this.generations.clear();
    this.idemByUser.clear();
    this.liveStreams.clear();
    this.seq = 0;
    this.abEnabled = false;
    this.nextErrorCode = null;
    this.nextStopReason = null;
    this.seedFixtures();
  }

  hasMessage(auth: string | null, messageId: string): boolean {
    const { userId } = this.requireAuth(auth);
    return [...this.conversations.values()].some((conversation) =>
      conversation.ownerUserId === userId && conversation.messages.some((message) => message.id === messageId),
    );
  }

  getPreviewCitations(auth: string | null, messageId: string): readonly Citation[] | null {
    const { userId } = this.requireAuth(auth);
    for (const conversation of this.conversations.values()) {
      if (conversation.ownerUserId !== userId) {
        continue;
      }
      const message = conversation.messages.find((candidate) => candidate.id === messageId);
      if (message === undefined) {
        continue;
      }
      const previewMessage = this.toConversationMessage(message);
      return previewMessage.role === 'assistant' ? previewMessage.citations : [];
    }
    return null;
  }

  /* ---------- 夹具 ---------- */

  setNextError(code = 'provider_error'): void {
    this.nextErrorCode = code;
  }

  setNextStopped(reason: StopReason = 'manual_request'): void {
    this.nextStopReason = reason;
  }

  /** 使指定 pair 过期（§3.9 ab_pair_expired）。 */
  expirePair(auth: string | null, pairId: string): void {
    this.requireAuth(auth);
    let found = false;
    for (const generation of this.generations.values()) {
      if (generation.pairId === pairId) {
        generation.abExpired = true;
        found = true;
      }
    }
    if (!found) {
      throw new MockHttpError(404, 'not_found');
    }
  }

  /**
   * 生成一个「仍在运行」的 generation（status=running、无事件）与 generating 占位 assistant 消息，
   * 供刷新恢复读模型测试；不经提问 SSE（§3.3 generation_id + status 恢复在跑流）。
   */
  startPendingGeneration(
    auth: string | null,
    conversationId: string,
    body: Pick<AskRequest, 'content' | 'effort_level'>,
  ): { generationId: string; messageId: string; userMessageId: string } {
    this.requireAuth(auth);
    const conversation = this.conversation(conversationId);
    const userMessageId = this.nextId('m');
    conversation.messages.push({ id: userMessageId, role: 'user', content: body.content, created_at: this.iso() });
    const generationId = this.nextId('g');
    const messageId = this.nextId('m');
    // M12：running generation 的恢复流需重放 start（event_seq=1），否则客户端恢复会话
    // 永远收不到 start、正文/占位/停止全部失真；后续事件经 pushAnswer/pushStopped 补发
    this.generations.set(generationId, {
      id: generationId,
      conversationId,
      userMessageId,
      messageId,
      attemptNumber: 1,
      rootGenerationId: generationId,
      retryOfGenerationId: null,
      effortLevel: body.effort_level,
      answerMode: 'grounded',
      answerContent: '',
      answerCitations: [],
      notices: [],
      status: 'running',
      stopReason: null,
      events: [
        {
          seq: 1,
          event: 'start',
          data: { generation_id: generationId, message_id: messageId, user_message_id: userMessageId, attempt_number: 1 },
        },
      ],
      pairId: null,
      abStatus: 'none',
      abChoice: null,
      abExpired: false,
      candidates: [],
    });
    conversation.messages.push({
      id: messageId,
      role: 'assistant',
      created_at: this.iso(),
      generation_id: generationId,
      root_generation_id: generationId,
      retry_of_generation_id: null,
      attempt_number: 1,
      feedback: null,
    });
    this.announceAssistantMessage(conversation, messageId);
    conversation.lastActiveAt = this.iso();
    return { generationId, messageId, userMessageId };
  }

  /* ---------- §3.1–§3.5 会话 CRUD ---------- */

  listConversations(auth: string | null, q?: string): { items: ConversationSummary[]; groups: ConversationGroup[] } {
    this.requireAuth(auth);
    const keyword = q?.trim().toLowerCase();
    const items = [...this.conversations.values()]
      .filter((conversation) => keyword === undefined || keyword === '' || conversation.title.toLowerCase().includes(keyword))
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.lastActiveAt.localeCompare(a.lastActiveAt))
      .map((conversation) => this.summaryOf(conversation));
    const groups = [...this.groups.values()].map((group) => ({ id: group.id, name: group.name }));
    return { items, groups };
  }

  createConversation(auth: string | null): ConversationSummary {
    const { userId } = this.requireAuth(auth);
    const id = this.nextId('c');
    const conversation: MockConversation = {
      id,
      ownerUserId: userId,
      title: '',
      pinned: false,
      groupId: null,
      lastActiveAt: this.iso(),
      effortLevel: 'quick',
      scope: { space_ids: [], document_ids: [] },
      messages: [],
    };
    this.conversations.set(id, conversation);
    return this.summaryOf(conversation);
  }

  getConversation(auth: string | null, id: string): ConversationDetail {
    this.requireAuth(auth);
    const conversation = this.conversation(id);
    return {
      id: conversation.id,
      title: conversation.title,
      effort_level: conversation.effortLevel,
      scope: { ...conversation.scope, space_ids: [...conversation.scope.space_ids], document_ids: [...conversation.scope.document_ids] },
      messages: conversation.messages.map((message) => this.toConversationMessage(message)),
    };
  }

  patchConversation(auth: string | null, id: string, patch: { title?: string; pinned?: boolean; group_id?: string | null }): ConversationSummary {
    this.requireAuth(auth);
    const conversation = this.conversation(id);
    if (patch.title !== undefined) {
      conversation.title = patch.title;
    }
    if (patch.pinned !== undefined) {
      conversation.pinned = patch.pinned;
    }
    if (patch.group_id !== undefined) {
      if (patch.group_id !== null && !this.groups.has(patch.group_id)) {
        throw new MockHttpError(404, 'not_found');
      }
      conversation.groupId = patch.group_id;
    }
    conversation.lastActiveAt = this.iso();
    return this.summaryOf(conversation);
  }

  deleteConversation(auth: string | null, id: string): void {
    this.requireAuth(auth);
    this.conversation(id);
    this.conversations.delete(id);
  }

  /* ---------- §3.6 会话分组 CRUD ---------- */

  createGroup(auth: string | null, name: string): ConversationGroup {
    this.requireAuth(auth);
    if (typeof name !== 'string' || name.trim() === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'name' });
    }
    const group: MockGroup = { id: this.nextId('g'), name: name.trim() };
    this.groups.set(group.id, group);
    return { id: group.id, name: group.name };
  }

  patchGroup(auth: string | null, id: string, name: string): ConversationGroup {
    this.requireAuth(auth);
    const group = this.groups.get(id);
    if (group === undefined) {
      throw new MockHttpError(404, 'not_found');
    }
    if (typeof name !== 'string' || name.trim() === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'name' });
    }
    group.name = name.trim();
    return { id: group.id, name: group.name };
  }

  /** 删组后组内会话归未分组（§3.6）。 */
  deleteGroup(auth: string | null, id: string): void {
    this.requireAuth(auth);
    if (!this.groups.has(id)) {
      throw new MockHttpError(404, 'not_found');
    }
    this.groups.delete(id);
    for (const conversation of this.conversations.values()) {
      if (conversation.groupId === id) {
        conversation.groupId = null;
      }
    }
  }

  /* ---------- §3.7 提问（SSE）与 generation 生命周期 ---------- */

  /**
   * 提问：校验 → 幂等判定 → 创建 user 消息 + assistant 占位 + generation 并物化事件序列。
   * 返回可流式发送的事件（含 event_seq；start=1）。
   */
  ask(
    auth: string | null,
    conversationId: string,
    body: AskRequest,
    idempotencyKey: string,
  ): { generationId: string; messageId: string; userMessageId: string; attemptNumber: number; events: readonly StoredSseEvent[] } {
    const { userId } = this.requireAuth(auth);
    const conversation = this.conversation(conversationId);
    this.validateAskBody(body);
    const normalized = JSON.stringify({ content: body.content, effort_level: body.effort_level, scope: body.scope ?? null });
    const idem = this.idemRecord(userId, idempotencyKey);
    if (idem !== undefined) {
      return this.replayAsk(idem, normalized);
    }

    const userMessageId = this.nextId('m');
    const generationId = this.nextId('g');
    const messageId = this.nextId('m');
    const attemptNumber = 1;
    conversation.messages.push({ id: userMessageId, role: 'user', content: body.content, created_at: this.iso() });

    const opts = {
      ab: this.abEnabled,
      errorCode: this.nextErrorCode,
      stopReason: this.nextStopReason,
    };
    // 一次性夹具，仅对下一次 ask 生效
    this.nextErrorCode = null;
    this.nextStopReason = null;
    this.abEnabled = false;

    const generation = this.buildGeneration({
      generationId,
      conversation,
      userMessageId,
      messageId,
      attemptNumber,
      rootGenerationId: generationId,
      retryOfGenerationId: null,
      body,
      opts,
    });
    this.generations.set(generationId, generation);
    conversation.messages.push({
      id: messageId,
      role: 'assistant',
      created_at: this.iso(),
      generation_id: generationId,
      root_generation_id: generationId,
      retry_of_generation_id: null,
      attempt_number: 1,
      feedback: null,
    });
    this.announceAssistantMessage(conversation, messageId);
    conversation.lastActiveAt = this.iso();
    if (conversation.title === '') {
      conversation.title = body.content.slice(0, 30);
    }
    this.idemMap(userId).set(idempotencyKey, { kind: 'ask', normalized, generationId });
    return { generationId, messageId, userMessageId, attemptNumber, events: generation.events };
  }

  /** 停止：running 首次 202 stop_requested；stop_requested 重复 202；stopped 200 终态；completed/failed 409。
   *  首次 stop 在返回 202 后向 live stream 补发 stopped(manual_request) 终态并关闭流（与真实后端语义对齐），
   *  测试无需再手调 pushStopped。 */
  stopGeneration(auth: string | null, generationId: string): { generation_id: string; message_id: string; status: GenerationStatus; stop_reason?: StopReason | null } {
    this.requireAuth(auth);
    const generation = this.generation(generationId);
    if (generation.status === 'completed' || generation.status === 'failed') {
      throw new MockHttpError(409, 'generation_already_terminal');
    }
    if (generation.status === 'stopped') {
      return { generation_id: generation.id, message_id: generation.messageId, status: 'stopped', stop_reason: generation.stopReason };
    }
    const firstRequest = generation.status === 'running';
    // running / stop_requested：均推进为 stop_requested 并返回同一 202 形状
    generation.status = 'stop_requested';
    const result = {
      generation_id: generation.id,
      message_id: generation.messageId,
      status: 'stop_requested' as const,
    };
    // 仅首次 stop 调度补发：短异步，保证本响应先以 stop_requested 返回，再推 SSE stopped
    if (firstRequest) {
      queueMicrotask(() => {
        // 仍为 stop_requested 时才补发（避免与外部 pushStopped 竞态双发）
        const current = this.generations.get(generationId);
        if (current !== undefined && current.status === 'stop_requested') {
          this.pushStopped(auth, generationId, 'manual_request');
        }
      });
    }
    return result;
  }

  /**
   * 失败重试（§3.7）：仅 failed generation 可重试；复用原 user_message_id，链内 attempt_number 递增，
   * 新 assistant 消息 / generation_id / 事件序列；原 failed 消息保留，新消息为后继。
   */
  retry(
    auth: string | null,
    failedGenerationId: string,
    idempotencyKey: string,
  ): { generationId: string; messageId: string; userMessageId: string; attemptNumber: number; events: readonly StoredSseEvent[] } {
    const { userId } = this.requireAuth(auth);
    const parent = this.generation(failedGenerationId);
    const normalized = JSON.stringify({ kind: 'retry', of: failedGenerationId });
    const idem = this.idemRecord(userId, idempotencyKey);
    if (idem !== undefined) {
      if (idem.kind !== 'retry' || idem.normalized !== normalized || idem.generationId === undefined) {
        throw new MockHttpError(409, 'idempotency_key_conflict');
      }
      const generation = this.generation(idem.generationId);
      return { generationId: generation.id, messageId: generation.messageId, userMessageId: generation.userMessageId, attemptNumber: generation.attemptNumber, events: generation.events };
    }
    if (parent.status !== 'failed') {
      throw new MockHttpError(409, 'generation_not_retryable');
    }
    // 同一 failed generation 已创建直接重试时，重复操作关联既有重试结果，不创建分叉（§3.7）
    const existingChild = [...this.generations.values()].find(
      (generation) => generation.retryOfGenerationId === parent.id,
    );
    if (existingChild !== undefined) {
      this.idemMap(userId).set(idempotencyKey, { kind: 'retry', normalized, generationId: existingChild.id });
      return { generationId: existingChild.id, messageId: existingChild.messageId, userMessageId: existingChild.userMessageId, attemptNumber: existingChild.attemptNumber, events: existingChild.events };
    }

    const conversation = this.conversation(parent.conversationId);
    const generationId = this.nextId('g');
    const messageId = this.nextId('m');
    const attemptNumber = parent.attemptNumber + 1;
    const body: AskRequest = {
      content: this.userMessage(conversation, parent.userMessageId).content,
      effort_level: parent.effortLevel,
      scope: { space_ids: [...conversation.scope.space_ids], document_ids: [...conversation.scope.document_ids] },
      overrides: null,
    };
    const generation = this.buildGeneration({
      generationId,
      conversation,
      userMessageId: parent.userMessageId,
      messageId,
      attemptNumber,
      rootGenerationId: parent.rootGenerationId,
      retryOfGenerationId: parent.id,
      body,
      opts: { ab: false, errorCode: null, stopReason: null },
    });
    this.generations.set(generationId, generation);
    conversation.messages.push({
      id: messageId,
      role: 'assistant',
      created_at: this.iso(),
      generation_id: generationId,
      root_generation_id: parent.rootGenerationId,
      retry_of_generation_id: parent.id,
      attempt_number: attemptNumber,
      feedback: null,
    });
    this.announceAssistantMessage(conversation, messageId);
    conversation.lastActiveAt = this.iso();
    this.idemMap(userId).set(idempotencyKey, { kind: 'retry', normalized, generationId });
    return { generationId, messageId, userMessageId: parent.userMessageId, attemptNumber, events: generation.events };
  }

  /** 断线恢复：Last-Event-ID 之后的事件（含终态）；未携带从 start 重放。 */
  listEvents(auth: string | null, generationId: string, lastEventId: number | null): readonly StoredSseEvent[] {
    this.requireAuth(auth);
    const generation = this.generation(generationId);
    if (lastEventId === null) {
      return generation.events;
    }
    return generation.events.filter((event) => event.seq > lastEventId);
  }

  /* ---------- M12：恢复流保持打开 + 可编程补发（运行中 generation） ---------- */

  /** 该 generation 是否仍可运行（running / stop_requested）——恢复流据此保持打开。 */
  isRunningGeneration(auth: string | null, generationId: string): boolean {
    this.requireAuth(auth);
    const generation = this.generation(generationId);
    return generation.status === 'running' || generation.status === 'stop_requested';
  }

  /** 注册一个打开的恢复流句柄；关闭时由 handler 调用 unregister。 */
  registerLiveStream(auth: string | null, generationId: string, handle: LiveStreamHandle): void {
    this.requireAuth(auth);
    let set = this.liveStreams.get(generationId);
    if (set === undefined) {
      set = new Set();
      this.liveStreams.set(generationId, set);
    }
    set.add(handle);
  }

  unregisterLiveStream(generationId: string, handle: LiveStreamHandle): void {
    const set = this.liveStreams.get(generationId);
    if (set === undefined) return;
    set.delete(handle);
    if (set.size === 0) {
      this.liveStreams.delete(generationId);
    }
  }

  /** 测试用：当前 generation 仍注册的 live stream 句柄数（验证 cancel 同引用 unregister）。 */
  liveStreamCount(generationId: string): number {
    return this.liveStreams.get(generationId)?.size ?? 0;
  }

  /** 夹具：对运行中 generation 补发 stopped 终态（手动停止 / 断线宽限期届满模拟）。 */
  pushStopped(auth: string | null, generationId: string, reason: StopReason): void {
    this.requireAuth(auth);
    const generation = this.generation(generationId);
    if (generation.status === 'completed' || generation.status === 'failed') {
      throw new MockHttpError(409, 'generation_already_terminal');
    }
    const seq = generation.events.reduce((max, event) => Math.max(max, event.seq), 0) + 1;
    const event: StoredSseEvent = {
      seq,
      event: 'stopped',
      data: { generation_id: generation.id, message_id: generation.messageId, status: 'stopped', stop_reason: reason },
    };
    generation.events.push(event);
    generation.status = 'stopped';
    generation.stopReason = reason;
    const handles = this.liveStreams.get(generationId);
    if (handles !== undefined) {
      const frame = sseEventFrame(event.seq, event.event, event.data);
      for (const handle of handles) {
        handle.push(frame);
        handle.close();
      }
      this.liveStreams.delete(generationId);
    }
  }

  /** 夹具：对运行中 generation 补发普通 answer（M1：停止前已收稳定 answer 的模拟）。 */
  pushAnswer(auth: string | null, generationId: string, content: string): void {
    this.requireAuth(auth);
    const generation = this.generation(generationId);
    if (generation.status === 'completed' || generation.status === 'failed' || generation.status === 'stopped') {
      throw new MockHttpError(409, 'generation_already_terminal');
    }
    const seq = generation.events.reduce((max, event) => Math.max(max, event.seq), 0) + 1;
    const event: StoredSseEvent = {
      seq,
      event: 'answer',
      data: {
        candidate: 0,
        content,
        citations: [],
        answer_mode: 'grounded',
        effort_level: generation.effortLevel,
        upgraded_from: null,
      },
    };
    generation.events.push(event);
    // 读模型收敛需要 answerContent 已固化
    (generation as { answerContent: string }).answerContent = content;
    const handles = this.liveStreams.get(generationId);
    if (handles !== undefined) {
      const frame = sseEventFrame(event.seq, event.event, event.data);
      for (const handle of handles) {
        handle.push(frame);
      }
    }
  }

  /* ---------- §3.8 / §3.9 反馈与 A/B 投票 ---------- */

  submitFeedback(auth: string | null, messageId: string, body: { vote: 'up' } | { vote: 'down'; reason: 'no_grounding' | 'wrong_citation' }, idempotencyKey: string): void {
    const { userId } = this.requireAuth(auth);
    this.validateFeedbackBody(body);
    const target = this.assistantMessage(messageId);
    const generation = this.generation(target.record.generation_id);
    const normalized = JSON.stringify(body);
    const idem = this.idemRecord(userId, idempotencyKey);
    if (idem !== undefined) {
      if (idem.kind !== 'feedback' || idem.messageId !== messageId || idem.normalized !== normalized) {
        throw new MockHttpError(409, 'idempotency_key_conflict');
      }
      return; // 同键同请求重放：幂等 204
    }
    if (target.record.feedback !== null) {
      throw new MockHttpError(409, 'feedback_already_submitted');
    }
    if (generation.pairId !== null && generation.abChoice === 'neither') {
      // choice=neither 的 A/B 回答不渲染常设 👍👎（§3.3），不再接受反馈
      throw new MockHttpError(409, 'feedback_already_submitted');
    }
    const feedback = body.vote === 'up' ? { vote: 'up' as const } : { vote: 'down' as const, down_reason: body.reason };
    (target.record as { feedback: FeedbackState }).feedback = feedback;
    this.idemMap(userId).set(idempotencyKey, { kind: 'feedback', normalized, messageId });
  }

  submitAbVote(
    auth: string | null,
    messageId: string,
    body: { pair_id: string; choice: AbChoice },
    idempotencyKey: string,
  ): { pair_id: string; voted: true; choice: AbChoice } {
    const { userId } = this.requireAuth(auth);
    if (body.choice !== '0' && body.choice !== '1' && body.choice !== 'neither') {
      throw new MockHttpError(422, 'validation_error', { field: 'choice' });
    }
    const target = this.assistantMessage(messageId);
    const generation = this.generation(target.record.generation_id);
    if (generation.pairId === null || generation.pairId !== body.pair_id) {
      throw new MockHttpError(409, 'ab_pair_expired');
    }
    const normalized = JSON.stringify({ pair_id: body.pair_id, choice: body.choice });
    const idem = this.idemRecord(userId, idempotencyKey);
    if (idem !== undefined) {
      if (idem.kind !== 'ab-vote' || idem.messageId !== messageId || idem.normalized !== normalized) {
        throw new MockHttpError(409, 'idempotency_key_conflict');
      }
      return { pair_id: generation.pairId, voted: true, choice: generation.abChoice as AbChoice };
    }
    if (generation.abExpired) {
      throw new MockHttpError(409, 'ab_pair_expired');
    }
    if (generation.abStatus === 'voted') {
      throw new MockHttpError(409, 'ab_vote_already_submitted');
    }
    if (generation.abStatus !== 'open') {
      // pending：候选仍在生成，不提供投票（§3.3）
      throw new MockHttpError(409, 'ab_pair_expired');
    }
    generation.abStatus = 'voted';
    generation.abChoice = body.choice;
    this.idemMap(userId).set(idempotencyKey, { kind: 'ab-vote', normalized, messageId });
    return { pair_id: generation.pairId, voted: true, choice: body.choice };
  }

  /* ---------- §6.1 知识空间 ---------- */

  listSpaces(auth: string | null, usage: SpaceUsage): SpaceItem[] {
    const { userId, role, departmentId } = this.requireAuth(auth);
    if (usage !== 'retrieval' && usage !== 'upload' && usage !== 'manage') {
      throw new MockHttpError(422, 'validation_error', { field: 'usage' });
    }
    const items: SpaceItem[] = [];
    for (const def of SPACE_DEFS) {
      const permission = permissionOf(def, role, userId, departmentId);
      if (permission === null) {
        continue;
      }
      const item: SpaceItem =
        def.kind === 'department' && def.departmentStatus !== undefined
          ? {
              id: def.id,
              kind: def.kind,
              name: def.name,
              permission,
              document_count: def.documentCount,
              department_status: def.departmentStatus,
            }
          : { id: def.id, kind: def.kind, name: def.name, permission, document_count: def.documentCount };
      // retrieval：本人个人库 + active 部门库 + 公共库（不含他人个人库、不含 inactive 部门库）
      const inRetrieval =
        (def.kind === 'personal' && def.ownerUserId === userId) ||
        (def.kind === 'department' && def.departmentStatus === 'active') ||
        def.kind === 'public';
      if (usage === 'retrieval' && !inRetrieval) {
        continue;
      }
      // upload：retrieval 集合中 permission 为 manage / contribute 的空间
      if (usage === 'upload' && permission !== 'manage' && permission !== 'contribute') {
        continue;
      }
      items.push(item);
    }
    return items;
  }

  /** §6.2 空间文档列表：检索范围 chip 个人库文档级收窄；默认上传时间倒序，q 按文档名过滤。 */
  listDocuments(
    auth: string | null,
    spaceId: string,
    q?: string,
  ): { items: Array<{ id: string; document_version_id: string; version: number; name: string; media_kind: string; version_status: string; active_operation: null; uploaded_at: string; usage: { pages: number; images: number } }>; total: number; page: number; page_size: number } {
    const { userId, role, departmentId } = this.requireAuth(auth);
    const def = SPACE_DEFS.find((candidate) => candidate.id === spaceId);
    if (def === undefined || permissionOf(def, role, userId, departmentId) === null) {
      throw new MockHttpError(404, 'space_not_found');
    }
    const keyword = q?.trim().toLowerCase() ?? '';
    const items = DOCUMENT_DEFS
      .filter((doc) => doc.spaceId === spaceId)
      .filter((doc) => keyword === '' || doc.name.toLowerCase().includes(keyword))
      .sort((a, b) => b.uploadedAt.localeCompare(a.uploadedAt))
      .map((doc) => ({
        id: doc.id,
        document_version_id: doc.documentVersionId,
        version: doc.version,
        name: doc.name,
        media_kind: doc.mediaKind,
        version_status: 'active' as const,
        active_operation: null,
        uploaded_at: doc.uploadedAt,
        usage: doc.usage,
      }));
    return { items, total: items.length, page: 1, page_size: 50 };
  }

  /* ---------- 内部 ---------- */

  private requireAuth(auth: string | null): { userId: string; role: Role; departmentId: string | null } {
    return this.validateAuth(auth);
  }

  private nextId(prefix: string): string {
    this.seq += 1;
    return `${prefix}_${this.seq.toString(36)}${Date.now().toString(36)}`;
  }

  private announceAssistantMessage(conversation: MockConversation, messageId: string): void {
    this.onAssistantMessageCreated?.(
      messageId,
      conversation.ownerUserId,
      this.getPreviewCitationsForOwner(conversation.ownerUserId, messageId),
    );
  }

  private getPreviewCitationsForOwner(ownerUserId: string, messageId: string): readonly Citation[] {
    const conversation = [...this.conversations.values()].find(
      (candidate) => candidate.ownerUserId === ownerUserId && candidate.messages.some((message) => message.id === messageId),
    );
    if (conversation === undefined) {
      return [];
    }
    const message = conversation.messages.find((candidate) => candidate.id === messageId);
    if (message === undefined) {
      return [];
    }
    const previewMessage = this.toConversationMessage(message);
    return previewMessage.role === 'assistant' ? previewMessage.citations : [];
  }

  private iso(): string {
    return new Date().toISOString();
  }

  private conversation(id: string): MockConversation {
    const conversation = this.conversations.get(id);
    if (conversation === undefined) {
      throw new MockHttpError(404, 'conversation_not_found');
    }
    return conversation;
  }

  private generation(id: string): MockGeneration {
    const generation = this.generations.get(id);
    if (generation === undefined) {
      throw new MockHttpError(404, 'generation_not_found');
    }
    return generation;
  }

  private summaryOf(conversation: MockConversation): ConversationSummary {
    return {
      id: conversation.id,
      title: conversation.title,
      pinned: conversation.pinned,
      group_id: conversation.groupId,
      last_active_at: conversation.lastActiveAt,
    };
  }

  private idemMap(userId: string): Map<string, IdemRecord> {
    let map = this.idemByUser.get(userId);
    if (map === undefined) {
      map = new Map<string, IdemRecord>();
      this.idemByUser.set(userId, map);
    }
    return map;
  }

  private idemRecord(userId: string, key: string): IdemRecord | undefined {
    return this.idemMap(userId).get(key);
  }

  private replayAsk(idem: IdemRecord, normalized: string): ReturnType<MockChatController['ask']> {
    if (idem.kind !== 'ask' || idem.normalized !== normalized || idem.generationId === undefined) {
      throw new MockHttpError(409, 'idempotency_key_conflict');
    }
    const generation = this.generation(idem.generationId);
    return {
      generationId: generation.id,
      messageId: generation.messageId,
      userMessageId: generation.userMessageId,
      attemptNumber: generation.attemptNumber,
      events: generation.events,
    };
  }

  private validateAskBody(body: AskRequest): void {
    if (typeof body.content !== 'string' || body.content.trim() === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'content' });
    }
    if (body.effort_level !== 'quick' && body.effort_level !== 'think' && body.effort_level !== 'deep') {
      throw new MockHttpError(422, 'validation_error', { field: 'effort_level' });
    }
    if (body.overrides !== null) {
      // 专家模式预留：当前前端固定不传（§3.7）
      throw new MockHttpError(422, 'validation_error', { field: 'overrides' });
    }
    if (body.scope !== undefined) {
      if (!Array.isArray(body.scope.space_ids) || !Array.isArray(body.scope.document_ids)) {
        throw new MockHttpError(422, 'validation_error', { field: 'scope' });
      }
    }
  }

  private validateFeedbackBody(body: unknown): void {
    if (typeof body !== 'object' || body === null) {
      throw new MockHttpError(422, 'validation_error', { field: 'vote' });
    }
    const vote = (body as { vote?: unknown }).vote;
    if (vote !== 'up' && vote !== 'down') {
      throw new MockHttpError(422, 'validation_error', { field: 'vote' });
    }
    if (vote === 'down') {
      const reason = (body as { reason?: unknown }).reason;
      if (reason !== 'no_grounding' && reason !== 'wrong_citation') {
        throw new MockHttpError(422, 'validation_error', { field: 'reason' });
      }
    }
  }

  private userMessage(conversation: MockConversation, id: string): MockUserRow {
    const message = conversation.messages.find((candidate) => candidate.id === id && candidate.role === 'user');
    if (message === undefined || message.role !== 'user') {
      throw new MockHttpError(404, 'not_found');
    }
    return message;
  }

  private assistantMessage(messageId: string): { conversation: MockConversation; record: MockAssistantRow } {
    for (const conversation of this.conversations.values()) {
      const record = conversation.messages.find(
        (candidate): candidate is MockAssistantRow => candidate.id === messageId && candidate.role === 'assistant',
      );
      if (record !== undefined) {
        return { conversation, record };
      }
    }
    throw new MockHttpError(404, 'message_not_found');
  }

  private buildGeneration(input: {
    generationId: string;
    conversation: MockConversation;
    userMessageId: string;
    messageId: string;
    attemptNumber: number;
    rootGenerationId: string;
    retryOfGenerationId: string | null;
    body: AskRequest;
    opts: { ab: boolean; errorCode: string | null; stopReason: StopReason | null };
  }): MockGeneration {
    // pairId 先于脚本生成：ab_start 事件与 generation 记录共用同一值
    const pairId = input.opts.ab ? this.nextId('pair') : null;
    const script = this.buildScript(input, pairId);
    const events = script.map((item, index) => ({ seq: index + 1, event: item.event, data: item.data }));
    const candidates: AbCandidate[] = [];
    let answerMode: AnswerMode = 'grounded';
    let answerContent = '';
    let answerCitations: Citation[] = [];
    const notices: Notice[] = [];
    for (const item of script) {
      if (item.event === 'answer') {
        const answer = item.data as { candidate: 0 | 1; content: string; citations: Citation[]; answer_mode: AnswerMode };
        if (pairId === null) {
          answerMode = answer.answer_mode;
          answerContent = answer.content;
          answerCitations = answer.citations;
        } else {
          candidates.push({ candidate: answer.candidate, content: answer.content, citations: answer.citations, answer_mode: answer.answer_mode });
        }
      } else if (item.event === 'notice') {
        notices.push(item.data as unknown as Notice);
      }
    }
    let status: GenerationStatus;
    let stopReason: StopReason | null = null;
    if (input.opts.errorCode !== null) {
      status = 'failed';
    } else if (input.opts.stopReason !== null) {
      status = 'stopped';
      stopReason = input.opts.stopReason;
    } else {
      status = 'completed';
    }
    return {
      id: input.generationId,
      conversationId: input.conversation.id,
      userMessageId: input.userMessageId,
      messageId: input.messageId,
      attemptNumber: input.attemptNumber,
      rootGenerationId: input.rootGenerationId,
      retryOfGenerationId: input.retryOfGenerationId,
      effortLevel: input.body.effort_level,
      answerMode,
      answerContent,
      answerCitations,
      notices,
      status,
      stopReason,
      events,
      pairId,
      // M15：A/B pair 状态明确化——非 A/B 为 none；终态（error/stopped，未发齐双候选）
      // 为 terminal（不可投、前端按普通回答）；completed 且双候选为 open
      abStatus:
        pairId === null
          ? 'none'
          : input.opts.errorCode !== null || input.opts.stopReason !== null
            ? 'terminal'
            : 'open',
      abChoice: null,
      abExpired: false,
      candidates,
    };
  }

  /** 构造未分配 seq 的事件序列（seq 在物化时按 1..n 分配；start 恒为 1）。pairId 由调用方预先生成。 */
  private buildScript(
    input: {
      generationId: string;
      conversation: MockConversation;
      userMessageId: string;
      messageId: string;
      attemptNumber: number;
      body: AskRequest;
      opts: { ab: boolean; errorCode: string | null; stopReason: StopReason | null };
    },
    pairId: string | null,
  ): Array<{ event: SseGenerationEvent['event']; data: Record<string, unknown> }> {
    const { generationId, messageId, userMessageId, attemptNumber, body, opts } = input;
    const base = {
      generation_id: generationId,
      message_id: messageId,
      user_message_id: userMessageId,
      attempt_number: attemptNumber,
    };
    const answer = (candidate: 0 | 1, content: string, citations: Citation[]): Record<string, unknown> => ({
      candidate,
      content,
      citations,
      answer_mode: 'grounded',
      effort_level: body.effort_level,
      upgraded_from: null,
    });
    const plainContent = `Mock answer for "${body.content}" (effort=${body.effort_level}, attempt=${attemptNumber}).`;
    const citationA: Citation = {
      document_id: 'doc_1',
      document_version_id: 'v_1',
      document_name: CHAT_SEED_DOCUMENT_NAMES.employeeHandbook,
      locator: { page: 1, span: { start: 30, end: 45 } },
      snippet: '5 days per year',
    };
    const citationSecond: Citation = {
      document_id: 'doc_1',
      document_version_id: 'v_1',
      document_name: CHAT_SEED_DOCUMENT_NAMES.employeeHandbook,
      locator: { page: 2 },
      snippet: 'medical certificate',
    };
    const citationB: Citation = { document_id: 'doc_2', document_version_id: 'v_2', document_name: '年假政策.md', locator: { section_path: ['第 4 章', '4.2'], paragraph: 7 } };

    if (opts.errorCode !== null) {
      return [
        { event: 'start', data: base },
        { event: 'stage', data: { phase: 'retrieving' } },
        {
          event: 'error',
          data: { code: opts.errorCode, message: opts.errorCode, details: {}, request_id: `req_mock_chat_${generationId}` },
        },
      ];
    }
    if (opts.stopReason !== null) {
      return [
        { event: 'start', data: base },
        { event: 'stage', data: { phase: 'retrieving' } },
        { event: 'answer', data: answer(0, plainContent, [citationA, citationSecond]) },
        { event: 'stopped', data: { generation_id: generationId, message_id: messageId, status: 'stopped', stop_reason: opts.stopReason } },
      ];
    }
    if (opts.ab === true) {
      return this.abScript(base, generationId, messageId, body.effort_level, plainContent, pairId as string);
    }
    if (body.effort_level === 'deep') {
      return [
        { event: 'start', data: base },
        { event: 'stage', data: { phase: 'retrieving' } },
        { event: 'step', data: { index: 0, label: 'retrieve_round_1', state: 'active' } },
        { event: 'step', data: { index: 0, label: 'retrieve_round_1', state: 'done' } },
        { event: 'step', data: { index: 1, label: 'retrieve_round_2', state: 'active' } },
        { event: 'stage', data: { phase: 'generating' } },
        { event: 'step', data: { index: 1, label: 'retrieve_round_2', state: 'done' } },
        { event: 'notice', data: { kind: 'rerank_degraded', detail: {} } },
        { event: 'answer', data: answer(0, plainContent, [citationA, citationB]) },
        { event: 'done', data: { generation_id: generationId, message_id: messageId, status: 'completed' } },
      ];
    }
    if (body.effort_level === 'think') {
      return [
        { event: 'start', data: base },
        { event: 'stage', data: { phase: 'retrieving' } },
        { event: 'stage', data: { phase: 'generating' } },
        { event: 'notice', data: { kind: 'effort_upgraded', detail: { from: 'quick', to: 'think' } } },
        { event: 'answer', data: answer(0, plainContent, [citationA, citationSecond]) },
        { event: 'done', data: { generation_id: generationId, message_id: messageId, status: 'completed' } },
      ];
    }
    // quick
    return [
      { event: 'start', data: base },
      { event: 'answer', data: answer(0, plainContent, [citationA, citationSecond]) },
      { event: 'done', data: { generation_id: generationId, message_id: messageId, status: 'completed' } },
    ];
  }

  private abScript(
    base: Record<string, unknown>,
    generationId: string,
    messageId: string,
    effortLevel: EffortLevel,
    plainContent: string,
    pairId: string,
  ): Array<{ event: SseGenerationEvent['event']; data: Record<string, unknown> }> {
    const candidateA: Citation = { document_id: 'doc_a', document_version_id: 'va_1', document_name: '候选来源 A.pdf', locator: { page: 3 } };
    const candidateB: Citation = { document_id: 'doc_b', document_version_id: 'vb_1', document_name: '候选来源 B.xlsx', locator: { sheet: 'Q1', a1_range: 'B3:D30' } };
    return [
      { event: 'start', data: base },
      { event: 'stage', data: { phase: 'retrieving' } },
      { event: 'ab_start', data: { pair_id: pairId, message_id: messageId, candidates: [0, 1] } },
      { event: 'stage', data: { phase: 'generating' } },
      { event: 'answer', data: { candidate: 0, content: `${plainContent} [candidate 0]`, citations: [candidateA], answer_mode: 'grounded', effort_level: effortLevel, upgraded_from: null } },
      { event: 'answer', data: { candidate: 1, content: `${plainContent} [candidate 1]`, citations: [candidateB], answer_mode: 'grounded', effort_level: effortLevel, upgraded_from: null } },
      { event: 'done', data: { generation_id: generationId, message_id: messageId, status: 'completed' } },
    ];
  }

  private toConversationMessage(message: MockUserRow | MockAssistantRow): ConversationMessage {
    if (message.role === 'user') {
      return { id: message.id, role: 'user', content: message.content, created_at: message.created_at };
    }
    const generation = this.generation(message.generation_id);
    let content = '';
    let answerMode: AnswerMode = generation.answerMode;
    let citations: readonly Citation[] = generation.answerCitations;
    if (generation.pairId !== null) {
      if (generation.abStatus === 'voted' && generation.abChoice !== null && generation.abChoice !== 'neither') {
        // 投票 0/1：只保留所选回答正文（§3.3）
        const chosen = generation.candidates.find((candidate) => String(candidate.candidate) === generation.abChoice);
        content = chosen?.content ?? '';
        answerMode = chosen?.answer_mode ?? generation.answerMode;
        citations = chosen?.citations ?? [];
      } else if (generation.abStatus === 'terminal') {
        // M15：终态 A/B pair——按唯一稳定候选作为普通回答处理（§3.3）；无候选则空正文
        const single = generation.candidates[0];
        content = single?.content ?? '';
        answerMode = single?.answer_mode ?? generation.answerMode;
        citations = single?.citations ?? generation.answerCitations;
      }
      // open / pending：正文在 ab.candidates，content 保持空串（UI 按候选渲染）
    } else {
      content = generation.answerContent;
    }
    return {
      id: message.id,
      role: 'assistant',
      content,
      created_at: message.created_at,
      answer_mode: answerMode,
      effort_level: generation.effortLevel,
      generation_id: generation.id,
      root_generation_id: message.root_generation_id,
      retry_of_generation_id: message.retry_of_generation_id,
      attempt_number: message.attempt_number,
      status: this.assistantStatus(generation.status),
      stop_reason: generation.stopReason,
      notices: generation.notices,
      citations,
      feedback: message.feedback,
      ab: this.deriveAb(generation),
    };
  }

  private assistantStatus(status: GenerationStatus): AssistantMessageStatus {
    if (status === 'completed') {
      return 'completed';
    }
    if (status === 'failed') {
      return 'failed';
    }
    if (status === 'stopped') {
      return 'stopped';
    }
    return 'generating';
  }

  private deriveAb(generation: MockGeneration): AbState {
    if (generation.pairId === null) {
      return null;
    }
    // M15：终态 A/B pair 不进入对比视图（前端按普通回答处理）
    if (generation.abStatus === 'terminal') {
      return null;
    }
    if (generation.abStatus === 'voted') {
      return { pair_id: generation.pairId, status: 'voted', voted: true, choice: generation.abChoice as AbChoice, candidates: null };
    }
    if (generation.abStatus === 'open') {
      return { pair_id: generation.pairId, status: 'open', voted: false, choice: null, candidates: generation.candidates as [AbCandidate, AbCandidate] };
    }
    return { pair_id: generation.pairId, status: 'pending', voted: false, choice: null, candidates: generation.candidates };
  }

  /* ---------- 种子（开发 / e2e 便利；测试可 reset 后自建） ---------- */

  private seedFixtures(): void {
    const groupId = this.nextId('g');
    this.groups.set(groupId, { id: groupId, name: '工作' });

    // 固定 lastActiveAt：禁止 iso() 同毫秒/跨毫秒导致排序抖动。
    // c_1 必须严格新于 c_ab——登录落地自动打开「最近会话」依赖此序（HomePage 集成 / e2e）。
    const seedActiveC1 = '2026-07-21T12:00:01.000Z';
    const seedActiveCab = '2026-07-21T12:00:00.000Z';
    const seedCreatedC1 = '2026-07-21T11:59:00.000Z';
    const seedCreatedCab = '2026-07-21T11:58:00.000Z';

    // c_1：think 完成会话，含 feedback=up 与一条带页码引用的回答
    const c1 = this.newConversation('c_1', 'u_user', '年假怎么休', false, groupId, 'think');
    c1.lastActiveAt = seedActiveC1;
    const user1 = this.nextId('m');
    c1.messages.push({ id: user1, role: 'user', content: '年假怎么休', created_at: seedCreatedC1 });
    const g1 = this.nextId('g');
    const m1 = 'm_1';
    this.generations.set(g1, {
      id: g1,
      conversationId: c1.id,
      userMessageId: user1,
      messageId: m1,
      attemptNumber: 1,
      rootGenerationId: g1,
      retryOfGenerationId: null,
      effortLevel: 'think',
      answerMode: 'grounded',
      answerContent: 'Mock seeded answer about annual leave.',
      answerCitations: [
        {
          document_id: 'doc_1',
          document_version_id: 'v_1',
          document_name: CHAT_SEED_DOCUMENT_NAMES.employeeHandbook,
          locator: { page: 1, span: { start: 30, end: 45 } },
          snippet: '5 days per year',
        },
        {
          document_id: 'doc_1',
          document_version_id: 'v_1',
          document_name: CHAT_SEED_DOCUMENT_NAMES.employeeHandbook,
          locator: { page: 2 },
          snippet: 'medical certificate',
        },
        {
          document_id: 'doc_1',
          document_version_id: 'v_0',
          document_name: CHAT_SEED_DOCUMENT_NAMES.employeeHandbook,
          locator: { page: 1 },
          snippet: '4 days per year',
        },
        { document_id: 'doc_scan', document_version_id: 'vs_1', document_name: '扫描合同.pdf', locator: { page: 1 } },
        { document_id: 'doc_xlsx', document_version_id: 'vx_1', document_name: '报销明细.xlsx', locator: { sheet: 'Q1 报销', a1_range: 'A2:C2' } },
        { document_id: 'doc_xlsx', document_version_id: 'vx_1', document_name: '报销明细.xlsx', locator: { sheet: 'Q2 报销', a1_range: 'A2' } },
      ],
      notices: [{ kind: 'effort_upgraded', detail: { from: 'quick', to: 'think' } }],
      status: 'completed',
      stopReason: null,
      events: [],
      pairId: null,
      abStatus: 'none',
      abChoice: null,
      abExpired: false,
      candidates: [],
    });
    c1.messages.push({
      id: m1,
      role: 'assistant',
      created_at: seedCreatedC1,
      generation_id: g1,
      root_generation_id: g1,
      retry_of_generation_id: null,
      attempt_number: 1,
      feedback: { vote: 'up' },
    });
    this.announceAssistantMessage(c1, m1);

    // c_ab：未投票 A/B 对比对（voted:false，双候选），供刷新重建与投票测试
    const cab = this.newConversation('c_ab', 'u_user', CHAT_SEED_TITLES.abCompare, false, null, 'think');
    cab.lastActiveAt = seedActiveCab;
    const userAb = this.nextId('m');
    cab.messages.push({ id: userAb, role: 'user', content: '请对比两版回答', created_at: seedCreatedCab });
    const gab = this.nextId('g');
    const mab = this.nextId('m');
    const pairId = this.nextId('pair');
    this.generations.set(gab, {
      id: gab,
      conversationId: cab.id,
      userMessageId: userAb,
      messageId: mab,
      attemptNumber: 1,
      rootGenerationId: gab,
      retryOfGenerationId: null,
      effortLevel: 'think',
      answerMode: 'grounded',
      answerContent: '',
      answerCitations: [],
      notices: [],
      status: 'completed',
      stopReason: null,
      events: [],
      pairId,
      abStatus: 'open',
      abChoice: null,
      abExpired: false,
      candidates: [
        { candidate: 0, content: 'Seeded candidate 0 content.', citations: [], answer_mode: 'grounded' },
        { candidate: 1, content: 'Seeded candidate 1 content.', citations: [], answer_mode: 'grounded' },
      ],
    });
    cab.messages.push({
      id: mab,
      role: 'assistant',
      created_at: seedCreatedCab,
      generation_id: gab,
      root_generation_id: gab,
      retry_of_generation_id: null,
      attempt_number: 1,
      feedback: null,
    });
    this.announceAssistantMessage(cab, mab);
  }

  private newConversation(
    id: string,
    ownerUserId: string,
    title: string,
    pinned: boolean,
    groupId: string | null,
    effortLevel: EffortLevel,
  ): MockConversation {
    const conversation: MockConversation = {
      id,
      ownerUserId,
      title,
      pinned,
      groupId,
      lastActiveAt: this.iso(),
      effortLevel,
      scope: { space_ids: ['personal:u_user', 'department:d_finance', 'public'], document_ids: [] },
      messages: [],
    };
    this.conversations.set(id, conversation);
    return conversation;
  }
}
