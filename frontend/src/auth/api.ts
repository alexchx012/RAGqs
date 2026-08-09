/*
 * 认证域 API 封装（规格 §6；契约 §2.1–§2.3、§2.7–§2.8、§2.10）。
 * 本 change 只提供可调用封装；设置页「活跃会话」卡 UI 在 fe-settings-personal 落地时调用。
 * users/me/profile、avatar、password、preferences 的封装随使用它们的 change 落地（非目标）。
 */

import type { ApiClient } from '../api/client';
import { readCsrfToken } from './cookies';
import type { DeviceSession, User } from './types';

export interface AuthApi {
  login(username: string, password: string): Promise<{ token: string; user: User }>;
  logout(): Promise<void>;
  refresh(): Promise<{ token: string }>;
  me(): Promise<User>;
  listSessions(): Promise<readonly DeviceSession[]>;
  revokeSession(id: string): Promise<void>;
  revokeAllSessions(): Promise<void>;
}

interface RefreshResponse {
  readonly token: string;
}

interface SessionsResponse {
  readonly items: readonly DeviceSession[];
}

export function createAuthApi(client: ApiClient): AuthApi {
  return {
    login(username, password) {
      return client.request<{ token: string; user: User }>('/auth/login', {
        method: 'POST',
        auth: false,
        body: { username, password },
      });
    },
    logout() {
      const authSessionGuard = client.captureAuthSessionGuard();
      return client.request<void>('/auth/logout', { method: 'POST', authSessionGuard });
    },
    refresh() {
      // refresh 不携带 Bearer、无 body；从 CSRF Cookie 读值经 X-CSRF-Token 原样回传
      const csrf = readCsrfToken();
      return client.request<RefreshResponse>('/auth/refresh', {
        method: 'POST',
        auth: false,
        headers: csrf === null ? {} : { 'X-CSRF-Token': csrf },
      });
    },
    me() {
      return client.request<User>('/auth/me');
    },
    async listSessions() {
      const authSessionGuard = client.captureAuthSessionGuard();
      const response = await client.request<SessionsResponse>('/auth/sessions', { authSessionGuard });
      return response.items;
    },
    revokeSession(id) {
      const authSessionGuard = client.captureAuthSessionGuard();
      return client.request<void>(`/auth/sessions/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        authSessionGuard,
      });
    },
    revokeAllSessions() {
      const authSessionGuard = client.captureAuthSessionGuard();
      return client.request<void>('/auth/sessions', { method: 'DELETE', authSessionGuard });
    },
  };
}
