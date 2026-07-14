import type { ApiResponse } from './types';

const API_BASE = '/api';

export class ApiError extends Error {
  constructor(
    message: string,
    public status?: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let onUnauthorized: (() => void) | null = null;

export function registerUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

export async function apiJson<T = unknown>(
  path: string,
  options?: RequestInit,
): Promise<ApiResponse<T>> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...options,
  });
  if (!res.ok) {
    if (res.status === 401) {
      onUnauthorized?.();
    }
    let detail: string;
    try {
      const errorData: ApiResponse<T> = await res.json();
      detail = errorData.detail || errorData.message || `HTTP ${res.status}`;
    } catch {
      detail = res.statusText || `HTTP ${res.status}`;
    }
    throw new ApiError(detail, res.status);
  }
  const data: ApiResponse<T> = await res.json();
  if (data.code && data.code >= 400) {
    throw new ApiError(
      data.detail || data.message || '请求失败',
      res.status,
    );
  }
  return data;
}
