/*
 * API 错误归一化（规格 §2；契约《前端接口需求.md》§1 错误格式）。
 * HTTP 请求级错误统一为 { code, message, details, request_id }：
 * 界面行为由 code 驱动，message 仅兜底展示，details 始终为对象。
 */

export interface ApiErrorInit {
  readonly status: number | null;
  readonly code: string;
  readonly message: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
}

export class ApiError extends Error {
  readonly status: number | null;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.details = init.details;
    this.requestId = init.requestId;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** 把任意响应体归一化为 ApiError；非契约形态兜底为 unknown_error。 */
export function normalizeApiError(body: unknown, status: number): ApiError {
  const envelope = asRecord(body);
  const error = asRecord(envelope['error']);
  const code = typeof error['code'] === 'string' ? error['code'] : 'unknown_error';
  const message = typeof error['message'] === 'string' ? error['message'] : '';
  const requestId = typeof error['request_id'] === 'string' ? error['request_id'] : null;
  return new ApiError({ status, code, message, details: asRecord(error['details']), requestId });
}

/** 网络层失败（无响应）。 */
export function networkError(cause: unknown): ApiError {
  return new ApiError({
    status: null,
    code: 'network_error',
    message: cause instanceof Error ? cause.message : '',
    details: {},
    requestId: null,
  });
}

/** 客户端超时。 */
export function timeoutError(): ApiError {
  return new ApiError({ status: null, code: 'timeout', message: '', details: {}, requestId: null });
}
