/*
 * 认证状态的 React 绑定。
 * AuthSessionStore 与框架无关，本模块经 useSyncExternalStore 订阅；
 * 应用启动时触发一次 bootstrap（静默 refresh，StrictMode 双调用由 single-flight 幂等）。
 */

import { createContext, useContext, useEffect, useSyncExternalStore, type ReactNode } from 'react';
import type { AuthSessionStore, AuthState } from './session';

const AuthContext = createContext<AuthSessionStore | null>(null);

export function AuthProvider({ store, children }: { store: AuthSessionStore; children: ReactNode }) {
  useEffect(() => {
    void store.bootstrap();
  }, [store]);
  return <AuthContext.Provider value={store}>{children}</AuthContext.Provider>;
}

export function useAuthStore(): AuthSessionStore {
  const store = useContext(AuthContext);
  if (store === null) {
    throw new Error('useAuthStore must be used within AuthProvider');
  }
  return store;
}

export function useAuthState(): AuthState {
  const store = useAuthStore();
  return useSyncExternalStore(
    (listener) => store.subscribe(listener),
    () => store.getState(),
  );
}
