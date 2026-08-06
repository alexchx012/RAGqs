/*
 * DrawerRegistry 的 React 绑定：单例随应用创建并内置占位模块；
 * 后续业务 change 经 useDrawerRegistry() 取得同一实例注册真实模块（替换占位）。
 */

import { createContext, useContext, type ReactNode } from 'react';
import { createPlaceholderModules } from './placeholder-modules';
import { DrawerRegistry } from './registry';

const DrawerRegistryContext = createContext<DrawerRegistry | null>(null);

export function createDrawerRegistry(): DrawerRegistry {
  const registry = new DrawerRegistry();
  for (const module of createPlaceholderModules()) {
    registry.register(module);
  }
  return registry;
}

export function DrawerRegistryProvider({
  registry,
  children,
}: {
  registry: DrawerRegistry;
  children: ReactNode;
}) {
  return <DrawerRegistryContext.Provider value={registry}>{children}</DrawerRegistryContext.Provider>;
}

export function useDrawerRegistry(): DrawerRegistry {
  const registry = useContext(DrawerRegistryContext);
  if (registry === null) {
    throw new Error('useDrawerRegistry must be used within DrawerRegistryProvider');
  }
  return registry;
}
