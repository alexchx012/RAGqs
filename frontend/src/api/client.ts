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

export async function apiJson<T = unknown>(
  path: string,
  options?: RequestInit,
): Promise<ApiResponse<T>> {
  const res = await fetch(`${API_BASE}${path}`, options);
  const data: ApiResponse<T> = await res.json();
  if (!res.ok || (data.code && data.code >= 400)) {
    throw new ApiError(
      data.detail || data.message || '请求失败',
      res.status,
    );
  }
  return data;
}
