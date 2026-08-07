/*
 * 会话与问答域契约类型（《前端接口需求.md》§1、§3、§6.1）。
 * 仅描述 HTTP/SSE 契约形状；snake_case 只用于 JSON 契约字段，内部标识一律 camelCase。
 * 未知枚举按 §1 兜底规则用 `string & {}` 收窄（不丢弃、不崩溃）。
 */

/* ---------- §1 通用 ---------- */

/** 努力档位：请求字段 effort_level，不复用 answer_mode。 */
export type EffortLevel = 'quick' | 'think' | 'deep';

/** 服务端输出的回答形态（仅输出，不是请求字段）。 */
export type AnswerMode = 'no_context' | 'grounded' | 'direct';

/** generation 生命周期；终态互斥。 */
export type GenerationStatus = 'running' | 'stop_requested' | 'completed' | 'failed' | 'stopped';

/** assistant 历史消息 status 字段，取代独立 stopped 布尔。 */
export type AssistantMessageStatus = 'generating' | 'completed' | 'failed' | 'stopped';

/** 仅 stopped 的 SSE 终态与 assistant 历史读模型返回；其他状态固定为 null。 */
export type StopReason = 'manual_request' | 'client_disconnected' | 'authorization_revoked';

/** 知识空间类型（§1 枚举）。 */
export type SpaceKind = 'personal' | 'department' | 'public';

/** 空间权限（后端按角色/所有者/部门状态计算；前端只渲染不推导）。 */
export type SpacePermission = 'manage' | 'read' | 'contribute';

/** §6.1 三种 usage：只决定返回哪些空间，不改变任一空间的权限值。 */
export type SpaceUsage = 'retrieval' | 'upload' | 'manage';

/** §1 错误对象：HTTP 请求级错误固定为 { error: { code, message, details, request_id } }。 */
export interface ApiErrorEnvelope {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly details: Record<string, unknown>;
    readonly request_id: string;
  };
}

/* ---------- §3.1–§3.6 会话与分组 ---------- */

/** GET /conversations 的单行摘要。 */
export interface ConversationSummary {
  readonly id: string;
  readonly title: string;
  readonly pinned: boolean;
  /** 自定义分组 id；null = 未分组。 */
  readonly group_id: string | null;
  readonly last_active_at: string;
}

export interface ConversationGroup {
  readonly id: string;
  readonly name: string;
}

/** GET /conversations：不分页，排序数据一次给全。 */
export interface ConversationsListResponse {
  readonly items: readonly ConversationSummary[];
  readonly groups: readonly ConversationGroup[];
}

/** POST /conversations：标题由后端据首条提问生成，新建时为空串。 */
export interface CreateConversationResponse {
  readonly id: string;
  readonly title: string;
  readonly pinned: boolean;
  readonly group_id: string | null;
  readonly last_active_at: string;
}

/** PATCH /conversations/{id}：重命名 / 置顶 / 移入分组三合一。 */
export interface PatchConversationRequest {
  readonly title?: string;
  readonly pinned?: boolean;
  readonly group_id?: string | null;
}

/** 检索范围；省略 = 全部范围（个人库 + 本部门部门库 + 公共库）。 */
export interface ConversationScope {
  readonly space_ids: string[];
  /** 仅收窄到个人库文档级时携带，可多选。 */
  readonly document_ids: string[];
}

/* ---------- §3.3 消息读模型 ---------- */

/** 用户消息。 */
export interface UserMessage {
  readonly id: string;
  readonly role: 'user';
  readonly content: string;
  readonly created_at: string;
}

/** 反馈已投状态（未投为 null）；A/B 投票仍只走 §3.9，互不覆盖。 */
export type FeedbackDownReason = 'no_grounding' | 'wrong_citation';

export interface FeedbackUp {
  readonly vote: 'up';
}

export interface FeedbackDown {
  readonly vote: 'down';
  readonly down_reason: FeedbackDownReason;
}

export type FeedbackState = FeedbackUp | FeedbackDown | null;

/** notice 已知值 + 未知值兜底（§1）；未知 kind 保留记录并显示通用提示。 */
export type NoticeKind =
  | 'effort_upgraded'
  | 'retrieval_degraded'
  | 'rerank_degraded'
  | (string & {});

export interface Notice {
  readonly kind: NoticeKind;
  readonly detail: Record<string, unknown>;
}

/**
 * Citation.locator 四种载体形态（只带载体固有单位，前端不做位置推断）：
 * - 文本层 PDF：page + 可选 span（页内字符偏移，消歧用）；
 * - 扫描件 PDF：仅 page；
 * - 建树文档：section_path + 可选 paragraph；
 * - basic 文档与图片：空对象（悬停卡只有文档名）。
 */
export type CitationLocator =
  | { readonly page: number; readonly span?: { readonly start: number; readonly end: number } }
  | { readonly section_path: readonly string[]; readonly paragraph?: number }
  | { readonly sheet: string; readonly a1_range: string }
  | Record<string, never>;

export interface Citation {
  /** 引用生成时固化；文档替换后不改绑。 */
  readonly document_id: string;
  readonly document_version_id: string;
  /**
   * 文档名（基座 §3.4「引自《文档名》」要求，契约 §3.7 的 Citation schema 未明确该字段）。
   * 契约假设登记：后端应在 Citation 中下发 document_name（§1 容忍新增字段、前端未知字段忽略）；
   * 前端缺失时回退通用「引自文档」措辞，不显示不透明 ID。待后端确认。
   */
  readonly document_name?: string;
  readonly locator: CitationLocator;
  /** 检索命中文本层片段，供前端匹配高亮。 */
  readonly snippet?: string;
}

/** A/B 候选：candidate 序号即 §3.9 投票所需入参（与界面左右解耦）。 */
export interface AbCandidate {
  readonly candidate: 0 | 1;
  readonly content: string;
  readonly citations: readonly Citation[];
  readonly answer_mode: AnswerMode;
}

/**
 * ab 字段（非 A/B 为 null）三态：
 * - pending：候选仍在生成（可空/单项），前端按普通回答全宽渲染，不提供投票；
 * - open：未投票且两候选均已发布，可恢复并保留投票能力；
 * - voted：投票完成，只保留所选回答正文（choice=neither 时 content 为空）。
 */
export type AbState =
  | null
  | {
      readonly pair_id: string;
      readonly status: 'pending';
      readonly voted: false;
      readonly choice: null;
      readonly candidates: readonly AbCandidate[];
    }
  | {
      readonly pair_id: string;
      readonly status: 'open';
      readonly voted: false;
      readonly choice: null;
      readonly candidates: readonly [AbCandidate, AbCandidate];
    }
  | {
      readonly pair_id: string;
      readonly status: 'voted';
      readonly voted: true;
      readonly choice: '0' | '1' | 'neither';
      readonly candidates: null;
    };

/**
 * assistant 消息：generation 字段链用于重试链与刷新恢复。
 * stop_reason 仅在 status=stopped 时非空；其他状态固定为 null。
 */
export interface AssistantMessage {
  readonly id: string;
  readonly role: 'assistant';
  readonly content: string;
  readonly answer_mode: AnswerMode;
  readonly effort_level: EffortLevel;
  readonly generation_id: string;
  /** 重试链标识：链首即首次提问的 generation。 */
  readonly root_generation_id: string;
  /** 指向上一环；链首为 null。 */
  readonly retry_of_generation_id: string | null;
  /** 链内递增。 */
  readonly attempt_number: number;
  readonly status: AssistantMessageStatus;
  readonly stop_reason: StopReason | null;
  readonly notices: readonly Notice[];
  readonly citations: readonly Citation[];
  readonly feedback: FeedbackState;
  readonly ab: AbState;
}

export type ConversationMessage = UserMessage | AssistantMessage;

/** GET /conversations/{id}：打开会话恢复消息历史。 */
export interface ConversationDetail {
  readonly id: string;
  readonly title: string;
  readonly effort_level: EffortLevel;
  readonly scope: ConversationScope;
  readonly messages: readonly ConversationMessage[];
}

/* ---------- §3.7 提问（SSE） ---------- */

export interface AskRequest {
  readonly content: string;
  readonly effort_level: EffortLevel;
  /** 省略 = 全部范围；space_ids 一律透传 §6.1 返回的实际值。 */
  readonly scope?: ConversationScope;
  /** 专家模式预留：当前前端固定不传。 */
  readonly overrides: null;
}

/** SSE start 事件：固定首事件，先于其他一切事件与终态。 */
export interface SseStartEventData {
  readonly generation_id: string;
  readonly message_id: string;
  readonly user_message_id: string;
  readonly attempt_number: number;
}

export type SseStagePhase = 'retrieving' | 'retrieving_again' | 'generating';

export interface SseStageEventData {
  readonly phase: SseStagePhase;
}

/** 深度研究步骤：label 为后端稳定机读名（如 retrieve_round_2），展示措辞前端定。 */
export interface SseStepEventData {
  readonly index: number;
  readonly label: string;
  readonly state: 'active' | 'done';
}

/** ab_start：通知前端准备第二条回答；不携带配置身份或候选正文。 */
export interface SseAbStartEventData {
  readonly pair_id: string;
  readonly message_id: string;
  readonly candidates: readonly [0, 1];
}

/** answer：一次性携带完整稳定答案；普通回答 candidate=0，A/B 按候选各发布一次。 */
export interface SseAnswerEventData {
  readonly candidate: 0 | 1;
  readonly content: string;
  readonly citations: readonly Citation[];
  readonly answer_mode: AnswerMode;
  readonly effort_level: EffortLevel;
  readonly upgraded_from: string | null;
}

export interface SseDoneEventData {
  readonly generation_id: string;
  readonly message_id: string;
  readonly status: 'completed';
}

/** error：生成失败终态；data 直接是错误对象（无 error 包装，§1）。 */
export interface SseErrorEventData {
  readonly code: string;
  readonly message: string;
  readonly details: Record<string, unknown>;
  readonly request_id: string;
}

export interface SseStoppedEventData {
  readonly generation_id: string;
  readonly message_id: string;
  readonly status: 'stopped';
  readonly stop_reason: StopReason;
}

/** SSE 事件联合：done / error / stopped 互斥终态，一次 generation 只收其一。 */
export type SseGenerationEvent =
  | { readonly event: 'start'; readonly data: SseStartEventData }
  | { readonly event: 'stage'; readonly data: SseStageEventData }
  | { readonly event: 'step'; readonly data: SseStepEventData }
  | { readonly event: 'notice'; readonly data: Notice }
  | { readonly event: 'ab_start'; readonly data: SseAbStartEventData }
  | { readonly event: 'answer'; readonly data: SseAnswerEventData }
  | { readonly event: 'done'; readonly data: SseDoneEventData }
  | { readonly event: 'error'; readonly data: SseErrorEventData }
  | { readonly event: 'stopped'; readonly data: SseStoppedEventData };

/** 停止响应：running 首次 202 stop_requested；已 stopped 200 当前终态。 */
export interface StopGenerationResponse {
  readonly generation_id: string;
  readonly message_id: string;
  readonly status: GenerationStatus;
  readonly stop_reason?: StopReason | null;
}

/* ---------- §3.8 / §3.9 反馈与 A/B 投票 ---------- */

export type FeedbackVoteRequest =
  | { readonly vote: 'up' }
  | { readonly vote: 'down'; readonly reason: FeedbackDownReason };

export type AbChoice = '0' | '1' | 'neither';

export interface AbVoteRequest {
  readonly pair_id: string;
  readonly choice: AbChoice;
}

export interface AbVoteResponse {
  readonly pair_id: string;
  readonly voted: true;
  readonly choice: AbChoice;
}

/* ---------- §6.1 知识空间 ---------- */

export interface SpaceItem {
  /** 实际 space_id；前端只透传服务端返回值，不拼接、不替换、不简写。 */
  readonly id: string;
  readonly kind: SpaceKind;
  readonly name: string;
  readonly permission: SpacePermission;
  readonly document_count: number;
  /** 仅部门库返回。 */
  readonly department_status?: 'active' | 'inactive';
}

export interface SpacesResponse {
  readonly items: readonly SpaceItem[];
}
