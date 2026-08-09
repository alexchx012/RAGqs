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
import type { AuthBus } from './channel';
import type { User } from './types';

export type AuthStatus = 'unknown' | 'authenticated' | 'unauthenticated';

export interface AuthState {
  readonly status: AuthStatus;
  readonly token: string | null;
  readonly user: User | null;
}

/** 设置域仅可同步当前用户的展示字段，不能覆盖身份或权限数据。 */
export type CurrentUserPresentationPatch = Readonly<Partial<Pick<User, 'display_name' | 'avatar_url'>>>;

/** 发起保存时声明其影响的当前用户展示字段。 */
export type CurrentUserPresentationField = keyof CurrentUserPresentationPatch;

/** 发起保存时捕获的受控提交能力；账号已切换或登出时静默失效。 */
export type CurrentUserPresentationSync = (patch: CurrentUserPresentationPatch) => void;

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

/** 仅在本 store 内部标识认证生命周期，绝不进入状态、持久化或总线。 */
interface LifecycleKey {
  readonly authSessionId: string | null;
  readonly lifecycleEpoch: number;
}

interface RefreshOutcome {
  readonly token: string;
  readonly applied: boolean;
}

interface RefreshFlight {
  readonly key: LifecycleKey;
  readonly promise: Promise<RefreshOutcome>;
}

export class AuthSessionStore {
  private state: AuthState = { status: 'unknown', token: null, user: null };
  private readonly listeners = new Set<() => void>();
  private inflightRefresh: RefreshFlight | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | undefined;
  private bootstrapped = false;
  /** 与展示补丁计数器分离的私有认证生命周期版本。 */
  private lifecycleEpoch = 0;
  /** 仅供展示补丁绑定本地认证会话实例；普通已认证 refresh 不推进。 */
  private presentationSessionInstance = 0;
  /**
   * 逻辑认证会话 identity：在 login（或从 unauthenticated 恢复）时绑定，
   * 同会话普通 refresh 保持不变，用于改密/all-sessions 跨会话 race 防护。
   */
  private authSessionId: string | null = null;
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

  /** 当前逻辑认证会话 identity；未认证时为 null。同会话 refresh 不改变该值。 */
  getAuthSessionId(): string | null {
    return this.authSessionId;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /**
   * 在保存请求发起时绑定当前 user id 和本地认证会话实例。只接受展示字段，且账号切换/登出后不再写入。
   * 同一认证会话的普通 refresh 不推进实例，因此不干扰正在进行的个人资料保存。
   */
  createCurrentUserPresentationSync(): CurrentUserPresentationSync {
    const capturedUser = this.state.user;
    const capturedSessionInstance = this.presentationSessionInstance;
    if (this.state.status !== 'authenticated' || capturedUser === null) {
      return () => undefined;
    }
    return (patch) => {
      const currentUser = this.state.user;
      if (
        this.state.status !== 'authenticated' ||
        currentUser === null ||
        currentUser.id !== capturedUser.id ||
        this.presentationSessionInstance !== capturedSessionInstance ||
        (patch.display_name === undefined && patch.avatar_url === undefined)
      ) {
        return;
      }
      this.setState({
        user: {
          ...currentUser,
          ...(patch.display_name === undefined ? {} : { display_name: patch.display_name }),
          ...(patch.avatar_url === undefined ? {} : { avatar_url: patch.avatar_url }),
        },
      });
    };
  }

  private setState(next: Partial<AuthState>): void {
    this.state = { ...this.state, ...next };
    for (const listener of this.listeners) {
      listener();
    }
  }

  private advancePresentationSessionInstance(): void {
    this.presentationSessionInstance += 1;
  }

  private advanceLifecycleEpoch(): void {
    this.lifecycleEpoch += 1;
  }

  private captureLifecycleKey(): LifecycleKey {
    return { authSessionId: this.authSessionId, lifecycleEpoch: this.lifecycleEpoch };
  }

  private matchesLifecycleKey(key: LifecycleKey): boolean {
    return this.authSessionId === key.authSessionId && this.lifecycleEpoch === key.lifecycleEpoch;
  }

  private sameLifecycleKey(left: LifecycleKey, right: LifecycleKey): boolean {
    return left.authSessionId === right.authSessionId && left.lifecycleEpoch === right.lifecycleEpoch;
  }

  /** True only when still authenticated under the given lifecycle key (post-setState fence). */
  private isAuthenticatedLifecycle(key: LifecycleKey): boolean {
    return this.state.status === 'authenticated' && this.matchesLifecycleKey(key);
  }

  /** 应用启动时调用一次：静默 refresh 恢复会话；失败则进入未认证态。幂等。 */
  async bootstrap(): Promise<void> {
    if (this.bootstrapped) {
      return;
    }
    this.bootstrapped = true;
    try {
      const outcome = await this.refreshForCurrentLifecycle();
      if (outcome.applied) {
        await this.ensureUser();
      }
    } catch {
      // refresh 失败已在 refreshForCurrentLifecycle() 内按认证失效清理
    }
  }

  async login(username: string, password: string): Promise<User> {
    const { token, user } = await this.deps.api.login(username, password);
    const authSessionId = token;
    this.advancePresentationSessionInstance();
    this.advanceLifecycleEpoch();
    this.authSessionId = authSessionId;
    this.setState({ status: 'authenticated', token, user });
    this.scheduleRefresh();
    this.deps.bus.post({ type: 'login', token, user, authSessionId });
    return user;
  }

  /** 只退出当前设备（契约 §2.2）；服务端幂等 204，本地在仍匹配发起 identity 时清理。 */
  async logout(): Promise<void> {
    // 必须在请求前捕获 identity：延迟完成时若已切到 B / 新 A，不得清理新会话。
    const initiatedAuthSessionId = this.authSessionId;
    try {
      await this.deps.api.logout();
    } catch {
      // 登出入口无二次确认、无错误界面：服务端失败不阻塞本地清理（仍按发起 identity 判定）
    }
    if (initiatedAuthSessionId === null) {
      // 未认证发起：仅当完成时仍未登录才本地清理；期间若已登录 B 则不得 clearAuth，也不发无 id 广播。
      if (this.authSessionId === null) {
        this.clearAuth();
      }
      return;
    }
    this.deps.bus.post({ type: 'logout', authSessionId: initiatedAuthSessionId });
    if (this.authSessionId === initiatedAuthSessionId) {
      this.clearAuth();
    }
  }

  /** 保持公共 refresh 返回 token；内部 outcome 供 bootstrap 判断该 flight 是否实际落地。 */
  refresh(): Promise<string> {
    return this.refreshForCurrentLifecycle().then(({ token }) => token);
  }

  /** 以完整生命周期 key 分区的 single-flight refresh。 */
  private refreshForCurrentLifecycle(): Promise<RefreshOutcome> {
    const key = this.captureLifecycleKey();
    const existingFlight = this.inflightRefresh;
    if (existingFlight !== null && this.sameLifecycleKey(existingFlight.key, key)) {
      return existingFlight.promise;
    }

    let resolveOutcome!: (outcome: RefreshOutcome) => void;
    let rejectOutcome!: (reason?: unknown) => void;
    const promise = new Promise<RefreshOutcome>((resolve, reject) => {
      resolveOutcome = resolve;
      rejectOutcome = reject;
    });
    const flight: RefreshFlight = { key, promise };

    // 必须先注册 flight，再同步启动 worker；api.refresh() 同步 throw 时也不会遗留已结束的 slot。
    this.inflightRefresh = flight;
    void this.runRefreshFlight(flight).then(resolveOutcome, rejectOutcome);
    return promise;
  }

  private async runRefreshFlight(flight: RefreshFlight): Promise<RefreshOutcome> {
    try {
      const { token } = await this.deps.api.refresh();
      if (!this.matchesLifecycleKey(flight.key)) {
        // 旧调用者仍可取得自己的 token，但不得把它应用到新生命周期。
        return { token, applied: false };
      }

      // 非认证态建立会话会推进 epoch；fence 必须用推进后的 key，而非 flight 的 pre-transition key。
      const establishingFromNonAuth = this.state.status !== 'authenticated';
      if (establishingFromNonAuth) {
        // 从 unknown/unauthenticated 建立认证是一次新的本地生命周期。
        this.advancePresentationSessionInstance();
        this.advanceLifecycleEpoch();
        this.authSessionId = token;
      }
      const authSessionId = this.authSessionId ?? token;
      this.authSessionId = authSessionId;
      const appliedKey = establishingFromNonAuth ? this.captureLifecycleKey() : flight.key;
      this.setState({ status: 'authenticated', token });
      // setState 同步通知订阅者；重入 clearAuth 后不得 schedule / 广播 / 标记 applied。
      if (!this.isAuthenticatedLifecycle(appliedKey)) {
        return { token, applied: false };
      }
      this.scheduleRefresh();
      this.deps.bus.post({ type: 'refresh', token, authSessionId });
      return { token, applied: true };
    } catch (error) {
      // 仅当前仍是本 flight 的完整生命周期时才清理；始终 rethrow 原错误。
      if (this.matchesLifecycleKey(flight.key)) {
        this.clearAuth();
      }
      throw error;
    } finally {
      // A 的 finally 不得清掉后来注册的 B flight。
      if (this.inflightRefresh === flight) {
        this.inflightRefresh = null;
      }
    }
  }

  /** 用户档案缺失时用 GET /auth/me 拉取。 */
  async ensureUser(): Promise<User> {
    if (this.state.user !== null) {
      return this.state.user;
    }
    const key = this.captureLifecycleKey();
    const user = await this.deps.api.me();
    if (this.state.status === 'authenticated' && this.matchesLifecycleKey(key)) {
      this.setState({ user });
    }
    return user;
  }

  /** 设置页读取当前账号的活跃设备会话（会话端点仍归认证域）。 */
  listSessions(): ReturnType<AuthApi['listSessions']> {
    return this.deps.api.listSessions();
  }

  /** 撤销指定设备会话；目标为当前设备时本地等同登出（契约 §2.8）。 */
  async revokeSession(id: string, options: { current?: boolean } = {}): Promise<void> {
    const current = options.current ?? false;
    // current 设备撤销与 logout / revoke-all 相同：在 await 前捕获 identity，避免延迟完成清新会话。
    const initiatedAuthSessionId = current ? this.authSessionId : null;
    await this.deps.api.revokeSession(id);
    if (current) {
      if (initiatedAuthSessionId === null) {
        // 未认证发起：仅当完成时仍 null 才本地清理；绝不广播无 id 的 current-revoke wildcard。
        if (this.authSessionId === null) {
          this.clearAuth();
        }
        return;
      }
      this.deps.bus.post({
        type: 'session-revoked',
        id,
        current: true,
        authSessionId: initiatedAuthSessionId,
      });
      if (this.authSessionId === initiatedAuthSessionId) {
        this.clearAuth();
      }
      return;
    }
    this.deps.bus.post({ type: 'session-revoked', id, current: false });
  }

  /** 退出全部设备：先请求 DELETE /auth/sessions，再清理认证状态（契约 §2.8）。 */
  async revokeAllSessions(): Promise<void> {
    // 必须在 DELETE 发起前捕获 identity：响应延迟期间若已切到 B / 新 A，不得按响应时的可变 identity 清理。
    const initiatedAuthSessionId = this.authSessionId;
    await this.deps.api.revokeAllSessions();
    this.handleServerAllSessionsRevoked(initiatedAuthSessionId);
  }

  /**
   * 服务端已在其他成功操作中撤销全部会话（例如修改密码）后的本地收尾。
   * 此路径绝不能再调用 DELETE /auth/sessions：当前 Bearer 此时已可能失效。
   *
   * @param expectedAuthSessionId 发起操作时捕获的逻辑会话 identity。
   *   传入时仅当仍匹配当前 identity 才清理本地；无论是否匹配都广播该 identity，供仍绑定旧会话的 peer 清理。
   *   省略时使用当前 identity（兼容即时收尾调用）。
   */
  handleServerAllSessionsRevoked(expectedAuthSessionId?: string | null): void {
    const targetId = expectedAuthSessionId === undefined ? this.authSessionId : expectedAuthSessionId;
    if (targetId === null || targetId === undefined) {
      return;
    }
    if (this.authSessionId !== targetId) {
      // 当前 tab 已切到其他逻辑会话：不清理本地；仍广播旧 identity，供仍匹配的 peer 清理。
      this.deps.bus.post({ type: 'sessions-revoked-all', authSessionId: targetId });
      return;
    }
    this.deps.bus.post({ type: 'sessions-revoked-all', authSessionId: targetId });
    this.clearAuth();
  }

  dispose(): void {
    this.stopAutoRefresh();
    this.unsubscribeBus();
    this.listeners.clear();
  }

  private clearAuth(): void {
    this.advancePresentationSessionInstance();
    this.advanceLifecycleEpoch();
    this.authSessionId = null;
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

  /**
   * BroadcastChannel / 测试总线 payload 在运行时不可信。
   * 入口按 unknown 处理：先拒绝 null/非对象/非 string type，再在各分支校验字段。
   * 保持内部实现；不改 channel.ts 的编译期 AuthBusMessage 契约。
   */
  private onBusMessage(message: unknown): void {
    if (message === null || typeof message !== 'object') {
      return;
    }
    const payload = message as Record<string, unknown>;
    if (typeof payload.type !== 'string') {
      return;
    }

    switch (payload.type) {
      case 'login': {
        const token = payload.token;
        const authSessionId = payload.authSessionId;
        const user = payload.user;
        // 缺/空/非法凭据不得写入状态；合法既有 login 消息继续生效。
        if (
          typeof token !== 'string' ||
          token.length === 0 ||
          typeof authSessionId !== 'string' ||
          authSessionId.length === 0 ||
          !this.isBusUser(user)
        ) {
          break;
        }
        this.advancePresentationSessionInstance();
        this.advanceLifecycleEpoch();
        this.authSessionId = authSessionId;
        this.setState({ status: 'authenticated', token, user });
        this.scheduleRefresh();
        break;
      }
      case 'refresh': {
        // refresh 必须带非空 string token 与 authSessionId（含 id 匹配 / unknown 准入路径）。
        if (
          typeof payload.token !== 'string' ||
          payload.token.length === 0 ||
          typeof payload.authSessionId !== 'string' ||
          payload.authSessionId.length === 0
        ) {
          break;
        }
        const token = payload.token;
        const authSessionId = payload.authSessionId;
        if (this.state.status === 'unknown' && this.authSessionId === null) {
          // 初始 unknown 才可由 peer refresh 建立新生命周期。
          this.advancePresentationSessionInstance();
          this.advanceLifecycleEpoch();
          this.authSessionId = authSessionId;
          const appliedKey = this.captureLifecycleKey();
          this.setState({ status: 'authenticated', token });
          // 重入 clear 后不得 schedule / ensureUser。
          if (!this.isAuthenticatedLifecycle(appliedKey)) {
            break;
          }
          this.scheduleRefresh();
          void this.ensureUser().catch(() => undefined);
          break;
        }
        if (this.state.status === 'authenticated' && this.authSessionId === authSessionId) {
          // 同一逻辑会话的 refresh 仅轮换 token，保留 user/id/epoch。
          const appliedKey = this.captureLifecycleKey();
          this.setState({ token });
          if (!this.isAuthenticatedLifecycle(appliedKey)) {
            break;
          }
          this.scheduleRefresh();
          if (this.state.user === null) {
            void this.ensureUser().catch(() => undefined);
          }
        }
        break;
      }
      case 'logout': {
        // 仅非空 string id 才参与比较；延迟 logout 不得清已切换的新会话。
        if (typeof payload.authSessionId !== 'string' || payload.authSessionId.length === 0) {
          break;
        }
        if (this.authSessionId === payload.authSessionId) {
          this.clearAuth();
        }
        break;
      }
      case 'sessions-revoked-all': {
        // 仅非空 string id 才参与比较；已切到 B / 新 A 的 tab 忽略旧事件。
        if (typeof payload.authSessionId !== 'string' || payload.authSessionId.length === 0) {
          break;
        }
        if (this.authSessionId === payload.authSessionId) {
          this.clearAuth();
        }
        break;
      }
      case 'session-revoked': {
        // Fail-closed：BroadcastChannel payload 可畸形，不依赖 TS 类型保证。
        // 仅 current===true 且非空 string authSessionId 与当前 logical id 匹配时才 clear。
        if (
          payload.current === true &&
          typeof payload.authSessionId === 'string' &&
          payload.authSessionId.length > 0 &&
          this.authSessionId === payload.authSessionId
        ) {
          this.clearAuth();
        }
        break;
      }
      default:
        break;
    }
  }

  /** 运行时粗校验 bus login 的 user；缺字段/非对象不得写入。 */
  private isBusUser(value: unknown): value is User {
    if (value === null || typeof value !== 'object') {
      return false;
    }
    const candidate = value as Record<string, unknown>;
    return (
      typeof candidate.id === 'string' &&
      typeof candidate.username === 'string' &&
      typeof candidate.display_name === 'string' &&
      typeof candidate.real_name === 'string' &&
      typeof candidate.role === 'string' &&
      (candidate.avatar_url === null || typeof candidate.avatar_url === 'string') &&
      (candidate.department === null ||
        (typeof candidate.department === 'object' && candidate.department !== null))
    );
  }
}
