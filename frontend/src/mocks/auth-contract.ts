/*
 * 契约 mock 核心（规格 §1）：严格实现《前端接口需求.md》§1–§2 本 change 范围的服务端行为。
 * 与传输层无关（handlers.ts 负责 MSW 接线），真实模拟：
 * - refresh Cookie 与 CSRF Cookie 的设置 / 清除、refresh 轮换（单调 sequence + 原子单次消费）；
 * - 刚消费的直接前驱 token 5 秒内并发重用返回同一后继结果，超过 5 秒判 refresh_reuse_detected 并撤销会话；
 * - 连续登录失败限流（429 + retry_after_seconds）；
 * - 四类认证失效码：session_revoked / invalid_refresh / refresh_reuse_detected / csrf_failed；
 * - pending_delete / deleted 账号的登录与轮换一律被拒（前端沿用清理凭证流程，无恢复入口）。
 */

import type { DeviceSession, Role, User } from '../auth/types';

export type AccountLifecycle = 'active' | 'pending_delete' | 'deleted';

export interface MockUserRecord {
  readonly user: User;
  readonly password: string;
  readonly lifecycle: AccountLifecycle;
}

export class MockHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(code);
    this.name = 'MockHttpError';
  }
}

interface AccessTokenRecord {
  readonly token: string;
  readonly sessionId: string;
  readonly userId: string;
  readonly issuedAt: number;
}

interface RefreshFamily {
  readonly sessionId: string;
  readonly userId: string;
  sequence: number;
  currentToken: string;
  /** 刚消费的直接前驱：5 秒内并发重用返回同一后继结果。 */
  lastConsumed: { token: string; successor: string; at: number } | null;
}

interface SessionRecord {
  readonly id: string;
  readonly userId: string;
  readonly device: string;
  readonly createdAt: number;
  lastActiveAt: number;
  revokedAt: number | null;
}

interface LoginThrottle {
  consecutiveFailures: number;
  lockedUntil: number;
}

export interface MockAuthConfig {
  /** access token 固定 15 分钟（契约 §1）。 */
  accessTokenTtlMs: number;
  /** 连续失败达到该次数后限流。 */
  rateLimitThreshold: number;
  /** 限流锁定时长（秒），随 error.details.retry_after_seconds 下发。 */
  rateLimitSeconds: number;
  /** 前驱 refresh token 重用宽限（契约 §2.10 固定 5 秒）。 */
  reuseWindowMs: number;
}

export const DEFAULT_MOCK_AUTH_CONFIG: MockAuthConfig = {
  accessTokenTtlMs: 15 * 60_000,
  rateLimitThreshold: 5,
  rateLimitSeconds: 30,
  reuseWindowMs: 5_000,
};

function fixtureUsers(): MockUserRecord[] {
  const make = (
    id: string,
    username: string,
    role: Role,
    department: User['department'],
    lifecycle: AccountLifecycle = 'active',
  ): MockUserRecord => ({
    user: {
      id,
      username,
      display_name: username,
      real_name: username,
      department,
      role,
      avatar_url: null,
    },
    password: 'password123',
    lifecycle,
  });
  return [
    make('u_user', 'zhangsan', 'user', { id: 'd_finance', name: 'Finance' }),
    make('u_minister', 'minister-li', 'minister', { id: 'd_finance', name: 'Finance' }),
    make('u_ops', 'ops-wang', 'ops', null),
    make('u_admin', 'admin', 'admin', null),
    make('u_ghost', 'ghost', 'user', null, 'pending_delete'),
  ];
}

export interface LoginResult {
  readonly user: User;
  readonly accessToken: string;
  readonly refreshToken: string;
  readonly csrfToken: string;
}

export interface RefreshResult {
  readonly accessToken: string;
  readonly refreshToken: string;
}

/**
 * mock 服务端状态的可选持久化（规格 §1：页面刷新后凭 refresh Cookie 静默恢复会话）。
 * 浏览器开发环境经 localStorage 持久化，页面刷新后服务端状态不丢；业务代码永不接触。
 * 测试环境默认不注入（内存态，reset() 即复位）。
 */
export interface MockAuthPersistence {
  load(): string | null;
  save(snapshot: string): void;
}

interface MockAuthSnapshot {
  users: MockUserRecord[];
  accessTokens: [string, AccessTokenRecord][];
  families: [string, RefreshFamily][];
  sessions: [string, SessionRecord][];
  csrfBySession: [string, string][];
  throttles: [string, LoginThrottle][];
  seq: number;
}

export class MockAuthController {
  /** 可变以便测试调整限流等参数；reset() 恢复默认值。 */
  readonly config: MockAuthConfig = { ...DEFAULT_MOCK_AUTH_CONFIG };
  private users: MockUserRecord[] = [];
  private accessTokens = new Map<string, AccessTokenRecord>();
  private families = new Map<string, RefreshFamily>();
  private sessions = new Map<string, SessionRecord>();
  private csrfBySession = new Map<string, string>();
  private throttles = new Map<string, LoginThrottle>();
  private seq = 0;

  constructor(
    config: Partial<MockAuthConfig> = {},
    private readonly persistence: MockAuthPersistence | null = null,
  ) {
    Object.assign(this.config, config);
    // 先读出已存快照再 reset（reset 会持久化空库）；浏览器多标签页场景下，
    // 每个请求前 rehydrate 保证本页内存库与其他标签页的轮换结果一致
    const pending = this.persistence?.load() ?? null;
    this.reset();
    if (pending !== null) {
      this.hydrateFrom(pending);
    }
  }

  reset(): void {
    this.users = fixtureUsers();
    Object.assign(this.config, DEFAULT_MOCK_AUTH_CONFIG);
    this.accessTokens.clear();
    this.families.clear();
    this.sessions.clear();
    this.csrfBySession.clear();
    this.throttles.clear();
    this.seq = 0;
    // 注意：不在此持久化。构造路径 reset → hydrateFrom 依赖存储中的旧快照；
    // reset 持久化空库会先把它冲掉（测试无持久层，浏览器 dev 复位请清 localStorage）。
  }

  private snapshot(): MockAuthSnapshot {
    return {
      users: this.users,
      accessTokens: [...this.accessTokens],
      families: [...this.families],
      sessions: [...this.sessions],
      csrfBySession: [...this.csrfBySession],
      throttles: [...this.throttles],
      seq: this.seq,
    };
  }

  private persist(): void {
    this.persistence?.save(JSON.stringify(this.snapshot()));
  }

  /** 请求前从持久层重读（无持久层时 no-op）：多标签页 / 页面刷新后与服务端状态保持一致。 */
  private rehydrate(): void {
    const raw = this.persistence?.load();
    if (raw !== null && raw !== undefined) {
      this.hydrateFrom(raw);
    }
  }

  private hydrateFrom(raw: string): void {
    try {
      const snapshot = JSON.parse(raw) as MockAuthSnapshot;
      if (Array.isArray(snapshot.users)) {
        this.users = snapshot.users;
      }
      this.accessTokens = new Map(snapshot.accessTokens);
      this.families = new Map(snapshot.families);
      this.sessions = new Map(snapshot.sessions);
      this.csrfBySession = new Map(snapshot.csrfBySession);
      this.throttles = new Map(snapshot.throttles);
      this.seq = snapshot.seq;
    } catch {
      // 损坏的快照视为空库（mock 开发便利，不上报）
    }
  }

  private nextId(prefix: string): string {
    this.seq += 1;
    return `${prefix}_${this.seq.toString(36)}${Date.now().toString(36)}`;
  }

  private now(): number {
    return Date.now();
  }

  private activeSession(sessionId: string): SessionRecord {
    const session = this.sessions.get(sessionId);
    if (session === undefined || session.revokedAt !== null) {
      throw new MockHttpError(401, 'session_revoked');
    }
    return session;
  }

  private requireUser(userId: string): MockUserRecord {
    const record = this.users.find((candidate) => candidate.user.id === userId);
    if (record === undefined || record.lifecycle !== 'active') {
      // pending_delete / deleted 账号的 token 使用与轮换均被拒绝（契约 §1）
      throw new MockHttpError(401, 'session_revoked');
    }
    return record;
  }

  private replaceUserRecord(userId: string, next: MockUserRecord): void {
    const index = this.users.findIndex((candidate) => candidate.user.id === userId);
    if (index !== -1) {
      this.users[index] = next;
    }
  }

  private issueAccessToken(sessionId: string, userId: string): string {
    const token = this.nextId('mat');
    this.accessTokens.set(token, { token, sessionId, userId, issuedAt: this.now() });
    return token;
  }

  private userByAccessToken(header: string | null): { record: MockUserRecord; session: SessionRecord } {
    if (header === null || !header.startsWith('Bearer ')) {
      throw new MockHttpError(401, 'invalid_token');
    }
    const token = header.slice('Bearer '.length);
    const entry = this.accessTokens.get(token);
    if (entry === undefined) {
      throw new MockHttpError(401, 'invalid_token');
    }
    const session = this.activeSession(entry.sessionId);
    const record = this.requireUser(entry.userId);
    session.lastActiveAt = this.now();
    this.persist();
    return { record, session };
  }

  private revokeFamily(sessionId: string): void {
    for (const family of this.families.values()) {
      if (family.sessionId === sessionId) {
        this.families.delete(family.currentToken);
      }
    }
  }

  private revokeSessionRecord(session: SessionRecord): void {
    session.revokedAt = this.now();
    this.revokeFamily(session.id);
    this.csrfBySession.delete(session.id);
  }

  login(username: string, password: string, device: string): LoginResult {
    this.rehydrate();
    const throttle = this.throttles.get(username);
    const now = this.now();
    if (throttle !== undefined && throttle.lockedUntil > now) {
      const retryAfter = Math.ceil((throttle.lockedUntil - now) / 1000);
      throw new MockHttpError(429, 'too_many_attempts', { retry_after_seconds: retryAfter });
    }
    const record = this.users.find((candidate) => candidate.user.username === username);
    const valid =
      record !== undefined && record.password === password && record.lifecycle === 'active';
    if (!valid) {
      const current = this.throttles.get(username) ?? { consecutiveFailures: 0, lockedUntil: 0 };
      current.consecutiveFailures += 1;
      if (current.consecutiveFailures >= this.config.rateLimitThreshold) {
        current.lockedUntil = now + this.config.rateLimitSeconds * 1000;
        current.consecutiveFailures = 0;
      }
      this.throttles.set(username, current);
      this.persist();
      throw new MockHttpError(401, 'invalid_credentials');
    }
    this.throttles.delete(username);

    const sessionId = this.nextId('sess');
    const refreshToken = this.nextId('mrt');
    const csrfToken = this.nextId('mcsrf');
    const session: SessionRecord = {
      id: sessionId,
      userId: record.user.id,
      device,
      createdAt: now,
      lastActiveAt: now,
      revokedAt: null,
    };
    this.sessions.set(sessionId, session);
    this.families.set(refreshToken, {
      sessionId,
      userId: record.user.id,
      sequence: 1,
      currentToken: refreshToken,
      lastConsumed: null,
    });
    this.csrfBySession.set(sessionId, csrfToken);
    // access token 必须在 persist 前签发：me 每请求 rehydrate，快照不含内存新 token 会 401
    const accessToken = this.issueAccessToken(sessionId, record.user.id);
    this.persist();
    return {
      user: record.user,
      accessToken,
      refreshToken,
      csrfToken,
    };
  }

  refresh(refreshToken: string | null, csrfHeader: string | null, csrfCookie: string | null): RefreshResult {
    this.rehydrate();
    const family = refreshToken === null ? undefined : this.families.get(refreshToken);
    const replay =
      family === undefined && refreshToken !== null
        ? [...this.families.values()].find((candidate) => candidate.lastConsumed?.token === refreshToken)
        : undefined;
    const effective = family ?? replay;

    if (effective !== undefined) {
      // CSRF 校验：Cookie、请求头、会话绑定一致（契约 §2.10）
      const bound = this.csrfBySession.get(effective.sessionId);
      if (
        csrfHeader === null ||
        csrfCookie === null ||
        bound === undefined ||
        csrfHeader !== csrfCookie ||
        csrfHeader !== bound
      ) {
        throw new MockHttpError(403, 'csrf_failed');
      }
      // pending_delete / deleted 账号的轮换被拒（契约 §1；前端按认证失效处理）
      const record = this.users.find((candidate) => candidate.user.id === effective.userId);
      if (record === undefined || record.lifecycle !== 'active') {
        throw new MockHttpError(401, 'invalid_refresh');
      }
      const session = this.sessions.get(effective.sessionId);
      if (session === undefined || session.revokedAt !== null) {
        // refresh token 已撤销
        throw new MockHttpError(401, 'invalid_refresh');
      }
    }

    if (family === undefined) {
      if (replay !== undefined && replay.lastConsumed !== null) {
        const consumed = replay.lastConsumed;
        const session = this.sessions.get(replay.sessionId) as SessionRecord;
        if (this.now() - consumed.at < this.config.reuseWindowMs) {
          // 5 秒内并发重用同一前驱：返回第一次轮换产生的同一后继结果，不视为失败
          session.lastActiveAt = this.now();
          // 同 login：access token 先于 persist 签发
          const accessToken = this.issueAccessToken(session.id, replay.userId);
          this.persist();
          return {
            accessToken,
            refreshToken: consumed.successor,
          };
        }
        // 超过 5 秒重用已消费 token：撤销该设备会话
        this.revokeSessionRecord(session);
        this.persist();
        throw new MockHttpError(401, 'refresh_reuse_detected');
      }
      // refresh token 过期、未知或已撤销
      throw new MockHttpError(401, 'invalid_refresh');
    }

    const session = this.sessions.get(family.sessionId) as SessionRecord;
    const successor = this.nextId('mrt');
    family.sequence += 1;
    family.lastConsumed = { token: refreshToken as string, successor, at: this.now() };
    family.currentToken = successor;
    this.families.delete(refreshToken as string);
    this.families.set(successor, family);
    session.lastActiveAt = this.now();
    const accessToken = this.issueAccessToken(session.id, family.userId);
    this.persist();
    return { accessToken, refreshToken: successor };
  }

  logout(authorization: string | null): void {
    // 幂等 204：token 无效也视为已登出
    this.rehydrate();
    try {
      const { session } = this.userByAccessToken(authorization);
      this.revokeSessionRecord(session);
      this.persist();
    } catch {
      // ignore
    }
  }

  me(authorization: string | null): User {
    this.rehydrate();
    return this.userByAccessToken(authorization).record.user;
  }

  /** Settings mock 通过同一认证域用户记录更新唯一允许的展示字段。 */
  updateCurrentUserDisplayName(authorization: string | null, displayName: string): User {
    this.rehydrate();
    const { record } = this.userByAccessToken(authorization);
    const next: MockUserRecord = {
      ...record,
      user: { ...record.user, display_name: displayName },
    };
    this.replaceUserRecord(record.user.id, next);
    this.persist();
    return next.user;
  }

  /** Settings mock 通过同一认证域用户记录保存头像展示 URL。 */
  updateCurrentUserAvatar(authorization: string | null, avatarUrl: string): User {
    this.rehydrate();
    const { record } = this.userByAccessToken(authorization);
    const next: MockUserRecord = {
      ...record,
      user: { ...record.user, avatar_url: avatarUrl },
    };
    this.replaceUserRecord(record.user.id, next);
    this.persist();
    return next.user;
  }

  /** 密码更新成功后服务端立即撤销该用户的全部设备会话。 */
  changeCurrentUserPasswordAndRevokeAll(
    authorization: string | null,
    oldPassword: string,
    newPassword: string,
  ): void {
    this.rehydrate();
    const { record } = this.userByAccessToken(authorization);
    if (record.password !== oldPassword) {
      throw new MockHttpError(403, 'wrong_old_password');
    }
    this.replaceUserRecord(record.user.id, { ...record, password: newPassword });
    for (const candidate of this.sessions.values()) {
      if (candidate.userId === record.user.id && candidate.revokedAt === null) {
        this.revokeSessionRecord(candidate);
      }
    }
    this.persist();
  }

  listSessions(authorization: string | null): DeviceSession[] {
    this.rehydrate();
    const { session } = this.userByAccessToken(authorization);
    const items: DeviceSession[] = [];
    for (const candidate of this.sessions.values()) {
      if (candidate.userId !== session.userId || candidate.revokedAt !== null) {
        continue;
      }
      items.push({
        id: candidate.id,
        device: candidate.device,
        last_active_at: new Date(candidate.lastActiveAt).toISOString(),
        current: candidate.id === session.id,
      });
    }
    return items;
  }

  /** 返回目标是否为当前设备（handlers 据此清除当前浏览器 Cookie）。 */
  revokeSession(authorization: string | null, id: string): { current: boolean } {
    this.rehydrate();
    const { session } = this.userByAccessToken(authorization);
    const target = this.sessions.get(id);
    if (target !== undefined && target.userId === session.userId && target.revokedAt === null) {
      this.revokeSessionRecord(target);
      this.persist();
    }
    // 重复撤销幂等 204
    return { current: id === session.id };
  }

  revokeAllSessions(authorization: string | null): void {
    this.rehydrate();
    const { session } = this.userByAccessToken(authorization);
    for (const candidate of this.sessions.values()) {
      if (candidate.userId === session.userId && candidate.revokedAt === null) {
        this.revokeSessionRecord(candidate);
      }
    }
    this.persist();
  }
}
