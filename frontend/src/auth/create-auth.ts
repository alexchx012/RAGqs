/*
 * 认证层默认装配（浏览器入口使用）。
 * client 经惰性闭包引用 store，打破 client → refresh → store → api → client 的循环依赖。
 */

import { createApiClient, type ApiClient } from '../api/client';
import { createAuthApi } from './api';
import { createBroadcastAuthBus } from './channel';
import { AuthSessionStore } from './session';

export interface AuthAssembly {
  readonly store: AuthSessionStore;
  readonly client: ApiClient;
}

export function createAuth(): AuthAssembly {
  let store: AuthSessionStore;
  const client = createApiClient({
    getAccessToken: () => store.getState().token,
    getAuthSessionId: () => store.getAuthSessionId(),
    refresh: () => store.refresh(),
  });
  store = new AuthSessionStore({
    api: createAuthApi(client),
    bus: createBroadcastAuthBus(),
  });
  return { store, client };
}
