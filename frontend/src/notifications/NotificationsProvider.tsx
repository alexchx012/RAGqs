/*
 * 通知轮询层的 React 绑定（fe-shared-shell 规格 §4）。
 * store 单例由应用装配创建（App.tsx）；start/stop 由 AppShell（RequireAuth 之下）
 * 承担，仅已认证时运行。
 */

import { createContext, useContext, type ReactNode } from 'react';
import type { NotificationsStore } from './store';

const NotificationsContext = createContext<NotificationsStore | null>(null);

export function NotificationsProvider({
  store,
  children,
}: {
  store: NotificationsStore;
  children: ReactNode;
}) {
  return <NotificationsContext.Provider value={store}>{children}</NotificationsContext.Provider>;
}

export function useNotifications(): NotificationsStore {
  const store = useContext(NotificationsContext);
  if (store === null) {
    throw new Error('useNotifications must be used within NotificationsProvider');
  }
  return store;
}
