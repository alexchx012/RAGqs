/*
 * 认证状态层（规格 §2–§3；契约 §1、§2.10）。
 * - access token 固定 15 分钟、只存内存（本类的私有字段），不进 localStorage / cookie。
 * - 页面刷新后 access token 丢失：bootstrap() 在首个业务请求前完成静默 refresh，
 *   用户档案缺失时随后用 GET /auth/me 拉取。
 * - 到期前自动 refresh（single-flight）；认证失效（session_revoked / invalid_refresh /
 *   refresh_reuse_detected / csrf_failed，含 pending_delete / deleted 账号被拒）统一清理：
 *   清除内存 token、停止自动 refresh、进入未认证态（路由守卫负责回登录页，无恢复入口）。
 * - 多标签页经 AuthBus 协调：login / refresh / logout / 设备撤销结果同步其他标签页。
 */

import type { AuthApi } from './api';
import type { AuthBus, AuthBusMessage } from './channel';
import type { User } from './types';

export type AuthStatus = 'unknown' | 'authenticated' | 'unauthenticated';

export interface AuthState {
  readonly status: AuthStatus;
  readonly token: string | null;
  readonly user: User | null;
}

export interface AuthSessionDeps {
  readonly api: AuthApi;
  readonly bus: AuthBus;
  /** access token 固定 15 分钟（契约 §1）。 */
  readonly accessTokenTtlMs?: number;
  /** 到期前提前量，默认 60s（即第 14 分钟自动续期）。 */
  readonly refreshLeadMs?: number;
}

const ACCESS_TOKEN_TTL_MS = 15 * 60_000;
const REFRESH_LEAD_MS = 60_000;

export class AuthSessionStore {
  private state: AuthState = { status: 'unknown', token: null, user: null };
  private readonly listeners = new Set<() => void>();
  private inflightRefresh: Promise<string> | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | undefined;
  private bootstrapped = false;
  private readonly unsubscribeBus: () => void;
  private readonly ttlMs: number;
  private readonly leadMs: number;

  constructor(private readonly deps: AuthSessionDeps) {
    this.ttlMs = deps.accessTokenTtlMs ?? ACCESS_TOKEN_TTL_MS;
    this.leadMs = deps.refreshLeadMs ?? REFRESH_LEAD_MS;
    this.unsubscribeBus = deps.bus.subscribe((message) => this.onBusMessage(message));
  }

  getState(): AuthState {
    return this.state;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private setState(next: Partial<AuthState>): void {
    this.state = { ...this.state, ...next };
    for (const listener of this.listeners) {
      listener();
    }
  }

  /** 应用启动时调用一次：静默 refresh 恢复会话；失败则进入未认证态。幂等。 */
  async bootstrap(): Promise<void> {
    if (this.bootstrapped) {
      return;
    }
    this.bootstrapped = true;
    try {
      await this.refresh();
      await this.ensureUser();
    } catch {
      // refresh 失败已在 refresh() 内按认证失效清理
    }
  }

  async login(username: string, password: string): Promise<User> {
    const { token, user } = await this.deps.api.login(username, password);
    this.setState({ status: 'authenticated', token, user });
    this.scheduleRefresh();
    this.deps.bus.post({ type: 'login', token, user });
    return user;
  }

  /** 只退出当前设备（契约 §2.2）；服务端幂等 204，本地无论如何都清理。 */
  async logout(): Promise<void> {
    try {
      await this.deps.api.logout();
    } catch {
      // 登出入口无二次确认、无错误界面：服务端失败不阻塞本地清理
    }
    this.clearAuth();
    this.deps.bus.post({ type: 'logout' });
  }

  /** single-flight refresh：并发调用等待同一次结果（契约 §2.10）。 */
  refresh(): Promise<string> {
    if (this.inflightRefresh !== null) {
      return this.inflightRefresh;
    }
    const inflight = (async () => {
      try {
        const { token } = await this.deps.api.refresh();
        this.setState({ status: 'authenticated', token });
        this.scheduleRefresh();
        this.deps.bus.post({ type: 'refresh', token });
        return token;
      } catch (error) {
        // refresh 失败（含四类认证失效码与网络失败）按认证失效处理
        this.clearAuth();
        throw error;
      } finally {
        this.inflightRefresh = null;
      }
    })();
    this.inflightRefresh = inflight;
    return inflight;
  }

  /** 用户档案缺失时用 GET /auth/me 拉取。 */
  async ensureUser(): Promise<User> {
    if (this.state.user !== null) {
      return this.state.user;
    }
    const user = await this.deps.api.me();
    this.setState({ user });
    return user;
  }

  /** 撤销指定设备会话；目标为当前设备时本地等同登出（契约 §2.8）。 */
  async revokeSession(id: string, options: { current?: boolean } = {}): Promise<void> {
    await this.deps.api.revokeSession(id);
    const current = options.current ?? false;
    this.deps.bus.post({ type: 'session-revoked', id, current });
    if (current) {
      this.clearAuth();
    }
  }

  /** 退出全部设备：清理认证状态并回登录页（契约 §2.8）。 */
  async revokeAllSessions(): Promise<void> {
    await this.deps.api.revokeAllSessions();
    this.deps.bus.post({ type: 'sessions-revoked-all' });
    this.clearAuth();
  }

  dispose(): void {
    this.stopAutoRefresh();
    this.unsubscribeBus();
    this.listeners.clear();
  }

  private clearAuth(): void {
    this.stopAutoRefresh();
    this.setState({ status: 'unauthenticated', token: null, user: null });
  }

  private scheduleRefresh(): void {
    this.stopAutoRefresh();
    this.refreshTimer = setTimeout(() => {
      void this.refresh().catch(() => undefined);
    }, this.ttlMs - this.leadMs);
  }

  private stopAutoRefresh(): void {
    if (this.refreshTimer !== undefined) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = undefined;
    }
  }

  private onBusMessage(message: AuthBusMessage): void {
    switch (message.type) {
      case 'login':
        this.setState({ status: 'authenticated', token: message.token, user: message.user });
        this.scheduleRefresh();
        break;
      case 'refresh':
        // 其他标签页完成轮换：采纳新 token 并重排自动 refresh，避免各标签页各自轮换
        this.setState({ status: 'authenticated', token: message.token });
        this.scheduleRefresh();
        void this.ensureUser().catch(() => undefined);
        break;
      case 'logout':
      case 'sessions-revoked-all':
        this.clearAuth();
        break;
      case 'session-revoked':
        if (message.current) {
          this.clearAuth();
        }
        break;
    }
  }
}
