/*
 * 会话与问答域 API 封装（契约《前端接口需求.md》§3、§6.1）。
 * 复用 src/api/client.ts 的 ApiClient（/v1 前缀 + Bearer + 401 自动 refresh + 错误归一化）。
 * 需要自定义请求头的端点（Idempotency-Key / Last-Event-ID / text/event-stream）走本模块封装，
 * 调用方负责生成并保存幂等键；SSE 流事件消费在 Batch B。
 */

import type { ApiClient } from '../api/client';
import { openGenerationStream, type SseStreamOptions } from './sse';
import type {
  AbVoteRequest,
  AbVoteResponse,
  AskRequest,
  CitationClickRequest,
  ConversationDetail,
  ConversationGroup,
  ConversationsListResponse,
  ConversationSummary,
  CreateConversationResponse,
  FeedbackVoteRequest,
  PatchConversationRequest,
  PromptEnhanceResponse,
  SpacesResponse,
  SpaceUsage,
  StopGenerationResponse,
} from './types';

export interface ChatApi {
  /* ---------- §3.1–§3.6 会话与分组 CRUD ---------- */
  listConversations(q?: string): Promise<ConversationsListResponse>;
  createConversation(): Promise<CreateConversationResponse>;
  getConversation(id: string): Promise<ConversationDetail>;
  patchConversation(id: string, patch: PatchConversationRequest): Promise<ConversationSummary>;
  deleteConversation(id: string): Promise<void>;
  createConversationGroup(name: string): Promise<ConversationGroup>;
  patchConversationGroup(id: string, name: string): Promise<ConversationGroup>;
  deleteConversationGroup(id: string): Promise<void>;

  /* ---------- §3.7 提问 / 停止 / 重试 / 恢复（SSE） ---------- */
  ask(
    conversationId: string,
    body: AskRequest,
    idempotencyKey: string,
    token: string,
    onEvent: SseStreamOptions['onEvent'],
    options?: Pick<SseStreamOptions, 'onOpen' | 'onError' | 'signal'>,
  ): Promise<void>;
  stopGeneration(generationId: string): Promise<StopGenerationResponse>;
  retryGeneration(
    failedGenerationId: string,
    idempotencyKey: string,
    token: string,
    onEvent: SseStreamOptions['onEvent'],
    options?: Pick<SseStreamOptions, 'onOpen' | 'onError' | 'signal'>,
  ): Promise<void>;
  getGenerationEvents(
    generationId: string,
    lastEventId: number | null,
    token: string,
    onEvent: SseStreamOptions['onEvent'],
    options?: Pick<SseStreamOptions, 'onOpen' | 'onError' | 'signal'>,
  ): Promise<void>;

  /* ---------- §3.8 / §3.9 反馈与 A/B 投票 ---------- */
  submitFeedback(
    messageId: string,
    body: FeedbackVoteRequest,
    idempotencyKey: string,
  ): Promise<void>;
  reportCitationClick(messageId: string, body: CitationClickRequest): Promise<void>;
  submitAbVote(
    messageId: string,
    body: AbVoteRequest,
    idempotencyKey: string,
  ): Promise<AbVoteResponse>;

  /* ---------- §6.1 知识空间 ---------- */
  listSpaces(usage: SpaceUsage): Promise<SpacesResponse>;
  /** §6.2 空间文档列表（检索范围 chip 个人库文档级收窄；q 按文档名过滤）。 */
  listDocuments(spaceId: string, q?: string): Promise<SpaceDocumentsResponse>;

  /* ---------- prompt-enhancements：输入优化（非流式单次，无聊天副作用） ---------- */
  /**
   * POST /prompt-enhancements：返回 enhanced_prompt 优化文本。
   * 30s 超时与端点 enhance_timeout_seconds 默认对齐；signal 由 composer 中止（还原/卸载）透传，
   * 中止以 AbortError 拒绝（不算失败）。
   */
  enhancePrompt(prompt: string, signal?: AbortSignal): Promise<string>;
}

/** §6.2 文档行（检索范围 chip 只消费 id + name；其余字段透传读模型/预览用）。 */
export interface SpaceDocumentItem {
  readonly id: string;
  readonly document_version_id: string;
  readonly version: number;
  readonly name: string;
  readonly media_kind: string;
  readonly version_status: string;
  readonly active_operation: unknown;
  readonly uploaded_at: string;
  readonly usage: { readonly pages: number; readonly images: number };
}

export interface SpaceDocumentsResponse {
  readonly items: readonly SpaceDocumentItem[];
  readonly total: number;
  readonly page: number;
  readonly page_size: number;
}

/** 提取 SSE 流式封装的公共参数；auth token 由调用方传入（走同一会话层刷新链路）。 */
interface SseCallParams {
  readonly path: string;
  readonly method: 'POST' | 'GET';
  readonly body?: unknown;
  readonly headers: Record<string, string>;
  readonly token: string;
  readonly onEvent: SseStreamOptions['onEvent'];
  readonly options?: Pick<SseStreamOptions, 'onOpen' | 'onError' | 'signal'>;
}

/** prompt-enhance 端点客户端超时（与后端 chat.enhance_timeout_seconds 默认 30s 对齐）。 */
const PROMPT_ENHANCE_TIMEOUT_MS = 30_000;

function stream(params: SseCallParams): Promise<void> {
  // SSE 流不经过 ApiClient 的 JSON 请求/响应归一化，仅复用其路径前缀约定
  return openGenerationStream({
    path: params.path,
    method: params.method,
    body: params.body,
    headers: params.headers,
    token: params.token,
    onEvent: params.onEvent,
    onOpen: params.options?.onOpen,
    onError: params.options?.onError,
    signal: params.options?.signal,
  });
}

export function createChatApi(client: ApiClient): ChatApi {
  return {
    listConversations(q) {
      const query = q === undefined ? '' : `?q=${encodeURIComponent(q)}`;
      return client.request<ConversationsListResponse>(`/conversations${query}`);
    },
    createConversation() {
      return client.request<CreateConversationResponse>('/conversations', {
        method: 'POST',
        body: {},
      });
    },
    getConversation(id) {
      return client.request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`);
    },
    patchConversation(id, patch) {
      return client.request<ConversationSummary>(`/conversations/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: patch,
      });
    },
    async deleteConversation(id) {
      await client.request<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },
    createConversationGroup(name) {
      return client.request<ConversationGroup>('/conversation-groups', {
        method: 'POST',
        body: { name },
      });
    },
    patchConversationGroup(id, name) {
      return client.request<ConversationGroup>(`/conversation-groups/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: { name },
      });
    },
    async deleteConversationGroup(id) {
      await client.request<void>(`/conversation-groups/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      });
    },

    ask(conversationId, body, idempotencyKey, token, onEvent, options) {
      return stream({
        path: `/conversations/${encodeURIComponent(conversationId)}/messages`,
        method: 'POST',
        body,
        headers: { 'Idempotency-Key': idempotencyKey },
        token,
        onEvent,
        options,
      });
    },
    stopGeneration(generationId) {
      return client.request<StopGenerationResponse>(
        `/generations/${encodeURIComponent(generationId)}/stop`,
        { method: 'POST' },
      );
    },
    retryGeneration(failedGenerationId, idempotencyKey, token, onEvent, options) {
      return stream({
        path: `/generations/${encodeURIComponent(failedGenerationId)}/retry`,
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        token,
        onEvent,
        options,
      });
    },
    getGenerationEvents(generationId, lastEventId, token, onEvent, options) {
      const headers: Record<string, string> = {};
      if (lastEventId !== null) {
        headers['Last-Event-ID'] = String(lastEventId);
      }
      return stream({
        path: `/generations/${encodeURIComponent(generationId)}/events`,
        method: 'GET',
        headers,
        token,
        onEvent,
        options,
      });
    },

    async submitFeedback(messageId, body, idempotencyKey) {
      await client.request<void>(`/messages/${encodeURIComponent(messageId)}/feedback`, {
        method: 'POST',
        body,
        headers: { 'Idempotency-Key': idempotencyKey },
      });
    },
    async reportCitationClick(messageId, body) {
      await client.request<void>(`/messages/${encodeURIComponent(messageId)}/citation-clicks`, {
        method: 'POST',
        body,
      });
    },
    submitAbVote(messageId, body, idempotencyKey) {
      return client.request<AbVoteResponse>(`/messages/${encodeURIComponent(messageId)}/ab-vote`, {
        method: 'POST',
        body,
        headers: { 'Idempotency-Key': idempotencyKey },
      });
    },

    listSpaces(usage) {
      return client.request<SpacesResponse>(`/spaces?usage=${encodeURIComponent(usage)}`);
    },

    listDocuments(spaceId, q) {
      const query = q === undefined ? '' : `?q=${encodeURIComponent(q)}`;
      return client.request<SpaceDocumentsResponse>(
        `/spaces/${encodeURIComponent(spaceId)}/documents${query}`,
      );
    },

    async enhancePrompt(prompt, signal) {
      const result = await client.request<PromptEnhanceResponse>('/prompt-enhancements', {
        method: 'POST',
        body: { prompt },
        timeoutMs: PROMPT_ENHANCE_TIMEOUT_MS,
        signal,
      });
      return result.enhanced_prompt;
    },
  };
}
