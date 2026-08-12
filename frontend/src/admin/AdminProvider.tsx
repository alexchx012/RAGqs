/*
 * 管理面板域依赖注入边界（无 UI）。
 * api 由 App 装配层注入（与 settingsApi 同一 ApiClient，Bearer/会话守卫一致）。
 * metricsWindow 在总览 dashboard（§9.1）与系统运维指标看板（§9.2）间共享：
 * 指标看板沿用 dashboard 所选时间窗口（《运维端设计.md》§7.5），默认 7d。
 * summariesVersion / invalidateSummaries：抽屉左栏项右侧摘要（徽标 / 状态点）的刷新机制——
 * 摘要组件挂载自加载并随 summariesVersion 重取；审批 / 任务 / 校准等写操作成功后调用
 * invalidateSummaries() 刷新计数（spec §1）。
 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import type { AdminApi } from './api';
import type { MetricsWindow } from './types';

export interface AdminContextValue {
  readonly api: AdminApi;
  readonly metricsWindow: MetricsWindow;
  readonly setMetricsWindow: (window: MetricsWindow) => void;
  /** 左栏摘要读序列代际：递增触发各摘要组件重取。 */
  readonly summariesVersion: number;
  /** 写操作成功后调用：刷新左栏徽标 / 状态点计数。 */
  readonly invalidateSummaries: () => void;
}

const AdminContext = createContext<AdminContextValue | null>(null);

export interface AdminProviderProps {
  readonly api: AdminApi;
  readonly children: ReactNode;
}

export function AdminProvider({ api, children }: AdminProviderProps) {
  const [metricsWindow, setMetricsWindow] = useState<MetricsWindow>('7d');
  const [summariesVersion, setSummariesVersion] = useState(0);
  const invalidateSummaries = useCallback(() => {
    setSummariesVersion((version) => version + 1);
  }, []);
  const value = useMemo<AdminContextValue>(
    () => ({ api, metricsWindow, setMetricsWindow, summariesVersion, invalidateSummaries }),
    [api, metricsWindow, summariesVersion, invalidateSummaries],
  );
  return <AdminContext.Provider value={value}>{children}</AdminContext.Provider>;
}

export function useAdmin(): AdminContextValue {
  const value = useContext(AdminContext);
  if (value === null) {
    throw new Error('useAdmin must be used within AdminProvider');
  }
  return value;
}
