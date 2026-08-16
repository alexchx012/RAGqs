/*
 * 生成控制器（fe-chat-home 规格 §7；契约《前端接口需求.md》§3.3、§3.7–3.9）。
 * 对一次提问 / 重试 / 断线恢复的完整生命周期建模，纯 TS（React 无关），store 与 UI 经 subscribe/getView 消费：
 * - 提问：发送前生成并保存 Idempotency-Key；收到 start 前网络失败以相同内容相同键重试（有界次数+退避），
 *   不产生重复用户消息；409 idempotency_key_conflict 按请求错误上抛，不自动换键。
 * - 事件应用：按 generation_id 保存已应用 event_seq，忽略 ≤ 已应用序号的重复事件（恢复重放去重）；
 *   start 保存四字段；stage/step/notice/ab_start/answer 更新状态；done/error/stopped 互斥终态只认先到者。
 * - 断线恢复：连接断开后在宽限期（默认 60s）内自动重连 GET /generations/{id}/events + Last-Event-ID
 *   （最后已应用 event_seq），带抖动指数退避并受页面会话截止时间限制；恢复前先经认证层完成 refresh
 *   （single-flight），refresh 失败停止重连按认证失效处理（交回会话层，不自行登出）；宽限期内重连不调用 stop；
 *   重连失败到尽头进入 reconnect_failed，等待服务端 stopped（client_disconnected）终态。
 * - 停止：点击即「正在停止」禁重复；POST stop 202 后等 stopped 终态；409 generation_already_terminal
 *   不覆盖已收终态；stopped 保留已收稳定 answer，按 stop_reason 映射。
 * - 失败重试：POST retry + 新 Idempotency-Key（同一网络重试复用该键）；复用原 user_message_id；
 *   新 generation 事件流接入同一控制器；attempt_number 链内递增。
 * - authorization_revoked：保留已收稳定 answer、不再自动恢复（store 层负责丢弃未完成模拟状态）。
 */

import { ApiError } from '../api/errors';
import type { ChatApi } from './api';
import { createIdempotencyKey } from './idempotency';
import type { SseEventMessage } from './sse';
import type {
  AbChoice,
  AnswerMode,
  AskRequest,
  Citation,
  EffortLevel,
  Notice,
  SseAnswerEventData,
  SseStagePhase,
  StopReason,
} from './types';

export type GenerationSessionKind = 'ask' | 'retry' | 'recover';

export type GenerationPhase =
  | 'idle'
  | 'connecting'
  | 'running'
  | 'reconnecting'
  | 'reconnect_failed'
  | 'stopping'
  | 'completed'
  | 'failed'
  | 'stopped';

export interface GenerationAbCandidateView {
  readonly candidate: 0 | 1;
  readonly content: string;
  readonly citations: readonly Citation[];
  readonly answer_mode: AnswerMode;
  readonly effort_level: EffortLevel | null;
  readonly upgraded_from: string | null;
}

export interface GenerationAbView {
  readonly status: 'none' | 'pending' | 'open' | 'voted';
  readonly pair_id: string | null;
  readonly candidates: readonly GenerationAbCandidateView[];
  readonly choice: AbChoice | null;
}

export type GenerationTerminalView =
  | { readonly kind: 'done'; readonly generationId: string; readonly messageId: string }
  | {
      readonly kind: 'error';
      readonly code: string;
      readonly message: string;
      readonly requestId: string | null;
    }
  | {
      readonly kind: 'stopped';
      readonly generationId: string;
      readonly messageId: string;
      readonly stopReason: StopReason;
    };

export interface GenerationSessionView {
  /** generation_id（收到 start 后）；start 前为本地临时 id。 */
  readonly id: string;
  readonly kind: GenerationSessionKind;
  readonly phase: GenerationPhase;
  readonly conversationId: string | null;
  /** start 事件四字段（未收到 start 前为 null）。 */
  readonly start: {
    readonly generationId: string;
    readonly messageId: string;
    readonly userMessageId: string;
    readonly attemptNumber: number;
  } | null;
  /** 最后已应用 event_seq（start=1；恢复重放据此去重）。 */
  readonly appliedSeq: number;
  readonly stage: SseStagePhase | null;
  readonly steps: readonly { readonly index: number; readonly label: string; readonly state: 'active' | 'done' }[];
  readonly notices: readonly Notice[];
  readonly ab: GenerationAbView;
  /** 普通回答（非 A/B）的稳定正文；A/B 时正文在 ab.candidates。 */
  readonly answer: {
    readonly content: string;
    readonly citations: readonly Citation[];
    readonly answer_mode: AnswerMode;
    readonly effort_level: EffortLevel | null;
    readonly upgraded_from: string | null;
  } | null;
  readonly terminal: GenerationTerminalView | null;
  /** 请求级错误（409 idempotency_key_conflict / 406 / 网络耗尽等）；messageKey 指向 src/copy。 */
  readonly requestError: { readonly code: string; readonly messageKey: string } | null;
  /** refresh 失败按认证失效处理（交回会话层）。 */
  readonly authFailed: boolean;
  readonly reconnectAttempts: number;
  readonly stopRequested: boolean;
}

export interface GenerationConfig {
  /** 断线后自动重连宽限期（spec §7 默认 60s）。 */
  readonly gracePeriodMs: number;
  /** 重连最大尝试次数（指数退避之外的兜底上界）。 */
  readonly maxReconnectAttempts: number;
  /** 重连基础退避（指数翻倍，带抖动）。 */
  readonly baseReconnectDelayMs: number;
  readonly maxReconnectDelayMs: number;
  /** 收到 start 前的网络重试次数上限（相同内容相同键）。 */
  readonly preStartMaxRetries: number;
  readonly preStartBaseDelayMs: number;
  /** 页面会话截止时间；超过则停止重连（null = 仅受宽限期约束）。 */
  readonly sessionDeadlineMs: number | null;
  /** 抖动随机源（测试注入）。 */
  readonly random: () => number;
}

export const DEFAULT_GENERATION_CONFIG: GenerationConfig = {
  gracePeriodMs: 60_000,
  maxReconnectAttempts: 10,
  baseReconnectDelayMs: 1_000,
  maxReconnectDelayMs: 8_000,
  preStartMaxRetries: 3,
  preStartBaseDelayMs: 800,
  sessionDeadlineMs: null,
  random: Math.random,
};

export interface GenerationSessionDeps {
  readonly api: ChatApi;
  readonly getToken: () => string | null;
  /** 认证层 single-flight refresh（auth/session.ts 公开接口）。 */
  readonly refresh: () => Promise<string>;
  readonly now?: () => number;
  readonly config?: Partial<GenerationConfig>;
}

/** 状态层下发的展示文案 key（一律指向 src/copy/zh-CN.ts）。 */
export const GENERATION_MESSAGE_KEYS = {
  requestError: 'chat.requestError',
  reconnectFailed: 'chat.reconnectFailed',
} as const;

type LaunchMode =
  | { kind: 'ask'; conversationId: string; body: AskRequest }
  | { kind: 'retry'; failedGenerationId: string }
  | { kind: 'recover'; conversationId: string; generationId: string; lastEventId: number | null };

let localSeq = 0;

export class GenerationSession {
  private readonly config: Required<GenerationConfig>;
  private readonly deps: GenerationSessionDeps;
  private readonly now: () => number;

  private view: GenerationSessionView;
  private readonly listeners = new Set<() => void>();
  private disposed = false;
  private mode: LaunchMode | null = null;
  private idempotencyKey: string | null = null;
  private preStartAttempts = 0;
  private reconnectAttempts = 0;
  private disconnectAt: number | null = null;
  private startedAt: number;
  private backoffTimer: ReturnType<typeof setTimeout> | undefined;
  private inflightRefresh: Promise<string> | null = null;
  private activeStream: Promise<void> | null = null;
  private stopRetriedAfterRefresh = false;
  private readonly abortController = new AbortController();

  private constructor(deps: GenerationSessionDeps) {
    this.deps = deps;
    this.config = { ...DEFAULT_GENERATION_CONFIG, ...deps.config };
    this.now = deps.now ?? Date.now;
    this.startedAt = this.now();
    this.view = {
      id: `pending_${++localSeq}`,
      kind: 'ask',
      phase: 'idle',
      conversationId: null,
      start: null,
      appliedSeq: 0,
      stage: null,
      steps: [],
      notices: [],
      ab: { status: 'none', pair_id: null, candidates: [], choice: null },
      answer: null,
      terminal: null,
      requestError: null,
      authFailed: false,
      reconnectAttempts: 0,
      stopRequested: false,
    };
  }

  /** 提问：发送前生成并保存 Idempotency-Key。 */
  static launchAsk(deps: GenerationSessionDeps, conversationId: string, body: AskRequest): GenerationSession {
    const session = new GenerationSession(deps);
    session.mode = { kind: 'ask', conversationId, body };
    session.idempotencyKey = createIdempotencyKey();
    session.update({ kind: 'ask', conversationId });
    session.begin();
    return session;
  }

  /** 失败重试：新 Idempotency-Key（同一网络重试复用该键）。 */
  static launchRetry(deps: GenerationSessionDeps, failedGenerationId: string): GenerationSession {
    const session = new GenerationSession(deps);
    session.mode = { kind: 'retry', failedGenerationId };
    session.idempotencyKey = createIdempotencyKey();
    session.update({ kind: 'retry' });
    session.begin();
    return session;
  }

  /** 页面刷新恢复：重新订阅 events 端点（未携带已持久化序号时从 start 重放，事件去重兜底）。 */
  static launchRecover(
    deps: GenerationSessionDeps,
    conversationId: string,
    generationId: string,
    lastEventId: number | null,
  ): GenerationSession {
    const session = new GenerationSession(deps);
    session.mode = { kind: 'recover', conversationId, generationId, lastEventId };
    session.update({ kind: 'recover', conversationId });
    session.begin();
    return session;
  }

  getView(): GenerationSessionView {
    return this.view;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 用户点击停止：仅收到 start 后可停止；禁重复；对已完成/失败不生效（不影响已展示结果）。 */
  requestStop(): void {
    if (this.disposed) return;
    if (this.view.terminal !== null) return; // 已完成/失败/已停：不影响已展示结果
    if (this.view.start === null) return; // 收到 start 前不可停止（spec §3）
    if (this.phase() === 'stopping') return; // 禁重复
    this.update({ phase: 'stopping', stopRequested: true });
    void this.driveStop();
  }

  dispose(): void {
    this.disposed = true;
    this.abortController.abort();
    if (this.backoffTimer !== undefined) {
      clearTimeout(this.backoffTimer);
      this.backoffTimer = undefined;
    }
    this.listeners.clear();
  }

  /* ---------- 启动与主驱动 ---------- */

  private phase(): GenerationPhase {
    return this.view.phase;
  }

  private begin(): void {
    this.startedAt = this.now();
    this.update({ phase: 'connecting' });
    void this.driveInitial();
  }

  /** 初始流（ask / retry / recover）打开；start 前的网络失败以相同内容相同键重试（有界+退避）。 */
  private async driveInitial(): Promise<void> {
    while (!this.disposed) {
      if (this.phase() !== 'stopping') {
        this.update({ phase: 'connecting' });
      }
      try {
        await this.openInitialStream();
      } catch (cause) {
        if (this.disposed || this.view.terminal !== null) return;
        if (this.view.start !== null) {
          this.enterReconnect();
          return;
        }
        const result = await this.handlePreStartFailure(cause);
        if (result !== 'retry') return;
        this.preStartAttempts += 1;
        if (this.preStartAttempts > this.config.preStartMaxRetries) {
          this.setRequestError('network_error', GENERATION_MESSAGE_KEYS.requestError);
          return;
        }
        if (!(await this.sleep(this.backoffDelay(this.preStartAttempts, this.config.preStartBaseDelayMs)))) return;
        continue;
      }
      // 流正常结束
      if (this.disposed) return;
      if (this.view.terminal !== null) return;
      if (this.view.start !== null) {
        this.enterReconnect();
        return;
      }
      // start 前流被服务端关闭且无终态：按 pre-start 失败处理（同键重试）
      this.preStartAttempts += 1;
      if (this.preStartAttempts > this.config.preStartMaxRetries) {
        this.setRequestError('network_error', GENERATION_MESSAGE_KEYS.requestError);
        return;
      }
      if (!(await this.sleep(this.backoffDelay(this.preStartAttempts, this.config.preStartBaseDelayMs)))) return;
    }
  }

  /** 收到 start 前失败分类：401 刷新后重试；409/406 等按请求错误上抛；网络失败重试。 */
  private async handlePreStartFailure(cause: unknown): Promise<'fatal' | 'auth' | 'retry'> {
    if (cause instanceof ApiError) {
      if (cause.status === 401) {
        try {
          await this.refreshToken();
          return 'retry';
        } catch {
          this.failAuth();
          return 'auth';
        }
      }
      this.setRequestError(cause.code, GENERATION_MESSAGE_KEYS.requestError);
      return 'fatal';
    }
    // 网络 / 超时（无 HTTP 状态）：重试同键
    return 'retry';
  }

  /* ---------- 断线恢复 ---------- */

  private enterReconnect(): void {
    if (this.disposed || this.view.authFailed) return;
    this.disconnectAt = this.now();
    this.reconnectAttempts = 0;
    this.update({ phase: 'reconnecting', reconnectAttempts: 0 });
    void this.driveReconnect();
  }

  private async driveReconnect(): Promise<void> {
    // 恢复前先完成认证层 refresh（single-flight）；失败按认证失效处理，停止重连
    try {
      await this.refreshToken();
    } catch {
      this.failAuth();
      return;
    }
    while (!this.disposed) {
      if (this.view.terminal !== null || this.view.authFailed) return;
      if (this.phase() === 'stopping') {
        this.ensureStopTerminalListener();
        return;
      }
      if (!this.withinGrace()) {
        this.failReconnect();
        return;
      }
      if (this.config.sessionDeadlineMs !== null && this.now() - this.startedAt >= this.config.sessionDeadlineMs) {
        this.failReconnect();
        return;
      }
      this.update({ phase: 'reconnecting' });
      try {
        await this.openEventsStream();
        // 恢复流正常结束：仅在已达终态 / 已 dispose / 认证失效时退出。
        // 代理或服务端 idle timeout 可能以干净 EOF 关闭且无 done/error/stopped，
        // 此时不能直接 return，否则会卡在 reconnecting 且无后续重试。
        if (this.disposed || this.view.terminal !== null || this.view.authFailed) return;
        if (this.phase() === 'stopping') {
          this.ensureStopTerminalListener();
          return;
        }
      } catch (cause) {
        if (this.disposed || this.view.terminal !== null || this.view.authFailed) return;
        if (cause instanceof ApiError && cause.status === 401) {
          try {
            await this.refreshToken();
            this.reconnectAttempts += 1;
            if (!(await this.sleep(this.backoffDelay(this.reconnectAttempts, this.config.baseReconnectDelayMs)))) return;
            continue;
          } catch {
            this.failAuth();
            return;
          }
        }
      }
      // 网络失败或干净 EOF 但未达终态：按既有退避策略继续重连
      this.reconnectAttempts += 1;
      this.update({ reconnectAttempts: this.reconnectAttempts });
      // 已超出宽限期 / 会话截止 / 次数上限时立即 fail，避免无意义 sleep 后再在下一轮失败
      if (
        !this.withinGrace() ||
        this.reconnectAttempts >= this.config.maxReconnectAttempts ||
        (this.config.sessionDeadlineMs !== null && this.now() - this.startedAt >= this.config.sessionDeadlineMs)
      ) {
        this.failReconnect();
        return;
      }
      if (!(await this.sleep(this.backoffDelay(this.reconnectAttempts, this.config.baseReconnectDelayMs)))) return;
    }
  }

  private withinGrace(): boolean {
    if (this.disconnectAt === null) return true;
    return this.now() - this.disconnectAt < this.config.gracePeriodMs;
  }

  /** 重连失败到尽头：等待服务端 stopped（client_disconnected）终态；保留一条一次性监听流。 */
  private failReconnect(): void {
    if (this.disposed) return;
    this.update({ phase: 'reconnect_failed' });
    void this.awaitTerminalOnce();
  }

  private async awaitTerminalOnce(): Promise<void> {
    if (this.disposed || this.view.terminal !== null) return;
    try {
      await this.openEventsStream();
    } catch {
      // 等待失败：留在 reconnect_failed，由 store 读模型刷新收敛
    }
    if (this.disposed || this.view.terminal !== null || this.phase() !== 'stopping') return;
    // 停止后的终态流也可能被代理以 EOF 关闭；不能留下没有监听器的 stopping 状态。
    this.update({
      phase: 'failed',
      stopRequested: false,
      requestError: { code: 'network_error', messageKey: GENERATION_MESSAGE_KEYS.requestError },
    });
  }

  private ensureStopTerminalListener(): void {
    if (this.disposed || this.view.terminal !== null || this.activeStream !== null) return;
    void this.awaitTerminalOnce();
  }

  /* ---------- 停止 ---------- */

  private async driveStop(): Promise<void> {
    const generationId = this.view.start?.generationId;
    if (generationId === undefined) return;
    try {
      await this.deps.api.stopGeneration(generationId);
      // 202 stop_requested → 等 stopped 终态；若当前无监听流则开一条
      this.ensureStopTerminalListener();
    } catch (cause) {
      if (this.disposed) return;
      if (cause instanceof ApiError && cause.status === 409) {
        // generation_already_terminal：不覆盖已收终态（保留已展示结果）
        return;
      }
      if (cause instanceof ApiError && cause.status === 401) {
        if (this.stopRetriedAfterRefresh) {
          this.update({
            phase: 'failed',
            stopRequested: false,
            requestError: { code: cause.code, messageKey: GENERATION_MESSAGE_KEYS.requestError },
          });
          return;
        }
        this.stopRetriedAfterRefresh = true;
        try {
          await this.refreshToken();
        } catch {
          this.failAuth();
          return;
        }
        if (this.view.terminal === null) {
          void this.driveStop();
        }
        return;
      }
      // 网络失败：保持 stopping，等待 stopped 终态
      if (this.view.terminal === null && this.activeStream === null) {
        void this.openEventsStream();
      }
    }
  }

  /* ---------- 流打开 ---------- */

  private openInitialStream(): Promise<void> {
    const token = this.deps.getToken();
    if (token === null) {
      this.failAuth();
      return Promise.reject(this.authError());
    }
    const options = {
      onOpen: undefined as (() => void) | undefined,
      signal: this.abortController.signal,
    };
    const onEvent = (message: SseEventMessage) => this.applyEvent(message);
    let promise: Promise<void>;
    switch (this.mode?.kind) {
      case 'ask':
        promise = this.deps.api.ask(
          this.mode.conversationId,
          this.mode.body,
          this.idempotencyKey as string,
          token,
          onEvent,
          options,
        );
        break;
      case 'retry':
        promise = this.deps.api.retryGeneration(
          this.mode.failedGenerationId,
          this.idempotencyKey as string,
          token,
          onEvent,
          options,
        );
        break;
      case 'recover':
        promise = this.deps.api.getGenerationEvents(
          this.mode.generationId,
          this.mode.lastEventId,
          token,
          onEvent,
          options,
        );
        break;
      default:
        return Promise.reject(new Error('generation_session_not_launched'));
    }
    return this.withActiveStream(promise);
  }

  private openEventsStream(): Promise<void> {
    const token = this.deps.getToken();
    if (token === null) {
      this.failAuth();
      return Promise.reject(this.authError());
    }
    const generationId = this.view.start?.generationId ?? (this.mode?.kind === 'recover' ? this.mode.generationId : null);
    if (generationId === null) {
      return Promise.resolve();
    }
    return this.withActiveStream(
      this.deps.api.getGenerationEvents(
        generationId,
        this.view.appliedSeq > 0 ? this.view.appliedSeq : null,
        token,
        (message) => this.applyEvent(message),
        { signal: this.abortController.signal },
      ),
    );
  }

  private async withActiveStream(promise: Promise<void>): Promise<void> {
    this.activeStream = promise;
    try {
      await promise;
    } finally {
      if (this.activeStream === promise) {
        this.activeStream = null;
      }
    }
  }

  /* ---------- 事件应用（去重 + 终态互斥） ---------- */

  private applyEvent(message: SseEventMessage): void {
    const { id, event } = message;
    if (this.view.terminal !== null) return; // 终态互斥：只认先到者
    if (id !== null) {
      if (id <= this.view.appliedSeq) return; // 忽略 ≤ 已应用序号（恢复重放去重）
      this.update({ appliedSeq: id });
    }
    switch (event.event) {
      case 'start':
        this.update({
          id: event.data.generation_id,
          start: {
            generationId: event.data.generation_id,
            messageId: event.data.message_id,
            userMessageId: event.data.user_message_id,
            attemptNumber: event.data.attempt_number,
          },
          phase: 'running',
        });
        break;
      case 'stage':
        this.update({ stage: event.data.phase });
        break;
      case 'step': {
        const steps = [...this.view.steps];
        const index = steps.findIndex((step) => step.index === event.data.index && step.label === event.data.label);
        const entry = { index: event.data.index, label: event.data.label, state: event.data.state };
        if (index >= 0) {
          steps[index] = entry;
        } else {
          steps.push(entry);
        }
        this.update({ steps });
        break;
      }
      case 'notice':
        // 未知 kind 保留记录（§1 未知枚举兜底），不使 generation 失败
        this.update({ notices: [...this.view.notices, event.data] });
        break;
      case 'ab_start':
        // M13：candidate 0 answer 先于 ab_start 到达时，把已渲染的普通 answer 迁移为候选 0
        // （契约只保证 ab_start 先于 candidate 1 的 answer；正文不因 ab_start 到达而消失）
        if (this.view.answer !== null) {
          const migrated = this.view.answer;
          this.update({
            answer: null,
            ab: {
              status: 'pending',
              pair_id: event.data.pair_id,
              candidates: [
                {
                  candidate: 0,
                  content: migrated.content,
                  citations: migrated.citations,
                  answer_mode: migrated.answer_mode,
                  effort_level: migrated.effort_level,
                  upgraded_from: migrated.upgraded_from,
                },
              ],
              choice: null,
            },
          });
          break;
        }
        this.update({
          ab: { status: 'pending', pair_id: event.data.pair_id, candidates: [], choice: null },
        });
        break;
      case 'answer':
        this.applyAnswer(event.data);
        break;
      case 'done':
        this.update({
          terminal: { kind: 'done', generationId: event.data.generation_id, messageId: event.data.message_id },
          phase: 'completed',
        });
        break;
      case 'error':
        this.update({
          terminal: {
            kind: 'error',
            code: event.data.code,
            message: event.data.message,
            requestId: event.data.request_id ?? null,
          },
          phase: 'failed',
        });
        break;
      case 'stopped':
        this.update({
          terminal: {
            kind: 'stopped',
            generationId: event.data.generation_id,
            messageId: event.data.message_id,
            stopReason: event.data.stop_reason,
          },
          phase: 'stopped',
        });
        break;
    }
  }

  private applyAnswer(data: SseAnswerEventData): void {
    if (this.view.ab.pair_id === null) {
      this.update({
        answer: {
          content: data.content,
          citations: data.citations,
          answer_mode: data.answer_mode,
          effort_level: data.effort_level,
          upgraded_from: data.upgraded_from,
        },
      });
      return;
    }
    const candidates = [...this.view.ab.candidates];
    const index = candidates.findIndex((candidate) => candidate.candidate === data.candidate);
    const entry = {
      candidate: data.candidate,
      content: data.content,
      citations: data.citations,
      answer_mode: data.answer_mode,
      effort_level: data.effort_level,
      upgraded_from: data.upgraded_from,
    };
    if (index >= 0) {
      candidates[index] = entry;
    } else {
      candidates.push(entry);
    }
    this.update({
      ab: { ...this.view.ab, candidates, status: candidates.length >= 2 ? 'open' : 'pending' },
    });
  }

  /* ---------- 认证 / 工具 ---------- */

  private refreshToken(): Promise<string> {
    if (this.inflightRefresh !== null) return this.inflightRefresh;
    const inflight = this.deps.refresh();
    this.inflightRefresh = inflight;
    return inflight.finally(() => {
      if (this.inflightRefresh === inflight) this.inflightRefresh = null;
    });
  }

  private failAuth(): void {
    if (this.disposed) return;
    this.update({ authFailed: true });
  }

  private authError(): ApiError {
    return new ApiError({
      status: 401,
      code: 'invalid_token',
      message: '',
      details: {},
      requestId: null,
    });
  }

  private setRequestError(code: string, messageKey: string): void {
    if (this.disposed) return;
    this.update({ requestError: { code, messageKey } });
  }

  private backoffDelay(attempt: number, base: number): number {
    const exp = Math.min(base * 2 ** (attempt - 1), this.config.maxReconnectDelayMs);
    return Math.round(exp * (0.5 + this.config.random() * 0.5));
  }

  private sleep(ms: number): Promise<boolean> {
    return new Promise((resolve) => {
      if (this.disposed) {
        resolve(false);
        return;
      }
      this.backoffTimer = setTimeout(() => {
        this.backoffTimer = undefined;
        resolve(!this.disposed);
      }, ms);
    });
  }

  private update(patch: Partial<GenerationSessionView>): void {
    this.view = { ...this.view, ...patch };
    for (const listener of this.listeners) {
      listener();
    }
  }
}
