/*
 * 同源多标签页协调总线（规格 §3；契约 §1、§2.10）。
 * 一个标签页完成 refresh / login 后同步新 access token 与登录状态；
 * logout、指定设备撤销、全部设备撤销结果同样同步。
 * 真实环境经 BroadcastChannel；测试注入内存实现（发送方不收自己的消息，与 BroadcastChannel 一致）。
 */

import type { User } from './types';

export type AuthBusMessage =
  | { readonly type: 'login'; readonly token: string; readonly user: User }
  | { readonly type: 'refresh'; readonly token: string }
  | { readonly type: 'logout' }
  | { readonly type: 'session-revoked'; readonly id: string; readonly current: boolean }
  | { readonly type: 'sessions-revoked-all' };

export type AuthBusListener = (message: AuthBusMessage) => void;

export interface AuthBus {
  post(message: AuthBusMessage): void;
  subscribe(listener: AuthBusListener): () => void;
  close(): void;
}

export const AUTH_CHANNEL_NAME = 'ragqs-auth';

/** BroadcastChannel 实现；环境不支持（或已关闭）时退化为静默 no-op，不影响单标签页功能。 */
export function createBroadcastAuthBus(channelName: string = AUTH_CHANNEL_NAME): AuthBus {
  const listeners = new Set<AuthBusListener>();
  let channel: BroadcastChannel | null = null;
  if (typeof BroadcastChannel === 'function') {
    try {
      channel = new BroadcastChannel(channelName);
      channel.onmessage = (event: MessageEvent<AuthBusMessage>) => {
        for (const listener of listeners) {
          listener(event.data);
        }
      };
    } catch {
      channel = null;
    }
  }
  return {
    post(message) {
      channel?.postMessage(message);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    close() {
      listeners.clear();
      channel?.close();
      channel = null;
    },
  };
}

/** 测试用内存枢纽：每个 createBus() 得到一个标签页，post 只投递给其他标签页。 */
export function createMemoryAuthHub(): { createBus(): AuthBus } {
  const buses = new Set<{ listeners: Set<AuthBusListener> }>();
  return {
    createBus(): AuthBus {
      const self = { listeners: new Set<AuthBusListener>() };
      buses.add(self);
      return {
        post(message) {
          for (const bus of buses) {
            if (bus === self) {
              continue;
            }
            for (const listener of bus.listeners) {
              listener(message);
            }
          }
        },
        subscribe(listener) {
          self.listeners.add(listener);
          return () => self.listeners.delete(listener);
        },
        close() {
          self.listeners.clear();
          buses.delete(self);
        },
      };
    },
  };
}
