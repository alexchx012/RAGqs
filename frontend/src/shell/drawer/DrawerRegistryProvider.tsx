/*
 * DrawerRegistry 的 React 绑定：单例随应用创建并注册内置模块；
 * 各业务 change 已在构建期内完成真实模块接入。
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
