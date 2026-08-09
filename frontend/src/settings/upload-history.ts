/*
 * 上传结果历史（会话内存档，review A2）。
 * - 按 auth session/user 隔离：记录与读取都以 sessionKey（`${authSessionId}:${userId}`）为键，
 *   旧会话的异步回调不得写入新会话槽位（调用方在响应落地时用当前 sessionKey 校验并拒绝过期写入）。
 * - 最小订阅机制：挂载中的组件 subscribe，写入/清空时通知重读。
 * - 仅会话内存档（刷新即清空；服务端任务卡是持久记录，本历史是上传响应的即时回显）。
 */

import type { SpaceItem, UploadResponse } from './types';

export interface UploadHistoryEntry {
  readonly response: UploadResponse;
  readonly target: SpaceItem | null;
  readonly at: string;
}

const store = new Map<string, UploadHistoryEntry>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) {
    listener();
  }
}

/** 写入最近一次上传结果（按会话隔离）；返回是否落库（sessionKey 为空或过期写入被拒时 false）。 */
export function recordUploadHistory(entry: UploadHistoryEntry, sessionKey: string | null): boolean {
  if (sessionKey === null || sessionKey === '') {
    return false;
  }
  store.set(sessionKey, entry);
  notify();
  return true;
}

/** 读取当前会话最近一次上传结果；无记录返回 null。 */
export function readUploadHistory(sessionKey: string | null): UploadHistoryEntry | null {
  if (sessionKey === null || sessionKey === '') {
    return null;
  }
  return store.get(sessionKey) ?? null;
}

/** 清空指定会话的历史（测试/登出清理用）。 */
export function clearUploadHistory(sessionKey: string | null = null): void {
  if (sessionKey === null) {
    store.clear();
  } else {
    store.delete(sessionKey);
  }
  notify();
}

/** 最小订阅：写入/清空时通知；返回取消函数。 */
export function subscribeUploadHistory(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
