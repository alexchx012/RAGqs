/*
 * 通知轮询层（shared-shell 规格 §4、§8；契约 §5）。
 * - 未读徽标定时刷新（30s）；打开面板即拉取列表；仅已认证时运行（由调用方 start/stop）。
 * - 未读数以服务端 unread-count 为唯一权威：前端不从列表推算、不自行持久化已读状态。
 * - read-all 语义（§5.3）：成功后当前已渲染条目显示为已读，随后刷新 unread-count 与列表；
 *   新物化未读按服务端结果重新显示红点，不强制清零。
 * - 多标签共享账号级已读状态：不经前端额外同步，以轮询收敛。
 */

import type { NotificationsApi } from './api';
import type { NotificationItem } from './types';

export type NotificationListStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface NotificationsState {
  /** 服务端权威未读数；未拉取过为 null。 */
  readonly unreadCount: number | null;
  /** 当前已拉取的列表（服务端顺序，不重排）；未拉取为 null。 */
  readonly items: readonly NotificationItem[] | null;
  readonly listStatus: NotificationListStatus;
}

export const NOTIFICATION_POLL_INTERVAL_MS = 30_000;

const INITIAL_STATE: NotificationsState = { unreadCount: null, items: null, listStatus: 'idle' };

export class NotificationsStore {
  private state: NotificationsState = INITIAL_STATE;
  private readonly listeners = new Set<() => void>();
  private pollTimer: ReturnType<typeof setInterval> | undefined;
  private running = false;
  private unreadInflight: Promise<void> | null = null;

  constructor(private readonly api: NotificationsApi) {}

  getState(): NotificationsState {
    return this.state;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private setState(next: Partial<NotificationsState>): void {
    this.state = { ...this.state, ...next };
    for (const listener of this.listeners) {
      listener();
    }
  }

  /** 开始轮询（仅已认证时调用）；幂等。 */
  start(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    void this.refreshUnread();
    this.pollTimer = setInterval(() => {
      void this.refreshUnread();
    }, NOTIFICATION_POLL_INTERVAL_MS);
  }

  stop(): void {
    this.running = false;
    if (this.pollTimer !== undefined) {
      clearInterval(this.pollTimer);
      this.pollTimer = undefined;
    }
    this.setState(INITIAL_STATE);
  }

  dispose(): void {
    this.stop();
    this.listeners.clear();
  }

  /** 刷新服务端权威未读数（single-flight）。 */
  async refreshUnread(): Promise<void> {
    if (this.unreadInflight !== null) {
      return this.unreadInflight;
    }
    const inflight = (async () => {
      try {
        const { count } = await this.api.unreadCount();
        this.setState({ unreadCount: count });
      } catch {
        // 轮询失败静默：保持上次权威值，下个周期收敛
      } finally {
        this.unreadInflight = null;
      }
    })();
    this.unreadInflight = inflight;
    return inflight;
  }

  /** 打开面板即拉取列表。 */
  async openPanel(): Promise<void> {
    this.setState({ listStatus: 'loading' });
    try {
      const { items } = await this.api.list();
      this.setState({ items, listStatus: 'ready' });
    } catch {
      this.setState({ listStatus: 'error' });
    }
  }

  /** 点击单条：服务端幂等标已读，本地同步该条并刷新未读数。 */
  async markRead(id: string): Promise<void> {
    await this.api.markRead(id);
    if (this.state.items !== null) {
      this.setState({
        items: this.state.items.map((item) => (item.id === id ? { ...item, read: true } : item)),
      });
    }
    await this.refreshUnread();
  }

  /** read-all：不带请求体；成功后已渲染条目标已读，随后刷新计数与列表（§5.3）。 */
  async markAllRead(): Promise<void> {
    await this.api.markAllRead();
    if (this.state.items !== null) {
      this.setState({ items: this.state.items.map((item) => ({ ...item, read: true })) });
    }
    await this.refreshUnread();
    await this.openPanel();
  }
}
