/*
 * 配额契约 mock 核心（契约《前端接口需求.md》§7.1–7.2）。
 * 单一权威状态，settings-contract 的 /quota/me 与 /quota-requests 和知识库上传的
 * quota_exceeded 判定共用同一实例，避免两处配额读数分叉。
 * - 普通 user/minister：used/base_limit/extra_granted/effective_limit，unlimited=false；
 *   ops/admin 固定 unlimited=true（不设页面上限，无申请入口）。
 * - pending_request 单账号同时只允许一条；201 后经 mock 审批夹具 resolvePending 清空
 *   （通过时按申请页数叠加 extra_granted，次月语义由夹具显式触发）。
 * - 写操作幂等：同一 Idempotency-Key 同 payload 返回同一结果；不同 payload 409
 *   idempotency_key_conflict；已存在 pending 时 409 pending_request_exists。
 */

import type { QuotaRequestResult, QuotaSnapshot } from '../settings/types';
import { MockHttpError } from './auth-contract';

export const MOCK_QUOTA_PERIOD = '2026-08';
export const MOCK_QUOTA_RESET_AT = '2026-09-01T00:00:00+08:00';
export const MOCK_QUOTA_TIMEZONE = 'Asia/Shanghai';
export const MOCK_QUOTA_CALENDAR_VERSION = 'calendar_mock_v1';

export interface QuotaAccountSeed {
  readonly userId: string;
  /** 角色决定 unlimited 与申请权限（由装配方按角色推导）。 */
  readonly unlimited: boolean;
  readonly used: number;
  readonly baseLimit: number;
}

interface StoredQuotaRequest {
  readonly requestedPages: number;
  readonly result: QuotaRequestResult;
}

export class MockQuotaStore {
  private readonly accounts = new Map<string, QuotaAccountSeed>();
  private readonly extraGranted = new Map<string, number>();
  private readonly pending = new Map<string, QuotaRequestResult>();
  private readonly idempotency = new Map<string, Map<string, StoredQuotaRequest>>();
  private readonly usage = new Map<string, number>();
  private requestSequence = 0;

  constructor(seeds: readonly QuotaAccountSeed[]) {
    for (const seed of seeds) {
      this.accounts.set(seed.userId, seed);
    }
  }

  reset(): void {
    this.extraGranted.clear();
    this.pending.clear();
    this.idempotency.clear();
    this.usage.clear();
    this.requestSequence = 0;
  }

  private account(userId: string): QuotaAccountSeed | null {
    return this.accounts.get(userId) ?? null;
  }

  snapshot(userId: string): QuotaSnapshot {
    const account = this.account(userId);
    const used = this.usage.get(userId) ?? account?.used ?? 0;
    const unlimited = account?.unlimited ?? false;
    const extra = this.extraGranted.get(userId) ?? 0;
    const baseLimit = account?.baseLimit ?? 500;
    const pending = this.pending.get(userId);
    return {
      used,
      base_limit: baseLimit,
      extra_granted: unlimited ? 0 : extra,
      effective_limit: unlimited ? 0 : baseLimit + extra,
      unlimited,
      reset_at: MOCK_QUOTA_RESET_AT,
      business_timezone: MOCK_QUOTA_TIMEZONE,
      quota_period: MOCK_QUOTA_PERIOD,
      business_calendar_version_id: MOCK_QUOTA_CALENDAR_VERSION,
      pending_request:
        pending === undefined
          ? null
          : {
              id: pending.id,
              version: pending.version,
              requested_pages: pending.requested_pages,
              quota_period: pending.quota_period,
              created_at: pending.created_at,
            },
    };
  }

  /** 知识库上传入库时累计用量（页面计入配额）；返回累计后 used。 */
  addUsage(userId: string, pages: number): number {
    const next = (this.usage.get(userId) ?? this.account(userId)?.used ?? 0) + pages;
    this.usage.set(userId, next);
    return next;
  }

  /** 知识库上传配额校验：返回剩余可入库页数（unlimited 返回 Infinity）。 */
  remaining(userId: string): number {
    const snapshot = this.snapshot(userId);
    return snapshot.unlimited
      ? Number.POSITIVE_INFINITY
      : Math.max(0, snapshot.effective_limit - snapshot.used);
  }

  /** 测试夹具：直接把某账号 used 抬高（触发 quota_exceeded 路径）。 */
  raiseUsage(userId: string, pages: number): void {
    const next = (this.usage.get(userId) ?? this.account(userId)?.used ?? 0) + pages;
    this.usage.set(userId, next);
  }

  request(userId: string, requestedPages: number, idempotencyKey: string): QuotaRequestResult {
    const account = this.account(userId);
    if (account?.unlimited === true) {
      throw new MockHttpError(403, 'quota_request_forbidden');
    }
    if (!Number.isInteger(requestedPages) || requestedPages < 1 || requestedPages > 500) {
      throw new MockHttpError(422, 'validation_error');
    }
    let records = this.idempotency.get(userId);
    if (records === undefined) {
      records = new Map<string, StoredQuotaRequest>();
      this.idempotency.set(userId, records);
    }
    const previous = records.get(idempotencyKey);
    if (previous !== undefined) {
      if (previous.requestedPages !== requestedPages) {
        throw new MockHttpError(409, 'idempotency_key_conflict');
      }
      return { ...previous.result };
    }
    if (this.pending.has(userId)) {
      throw new MockHttpError(409, 'pending_request_exists');
    }
    this.requestSequence += 1;
    const result: QuotaRequestResult = {
      id: `quota_request_${this.requestSequence}`,
      version: 1,
      status: 'pending',
      requested_pages: requestedPages,
      quota_period: MOCK_QUOTA_PERIOD,
      created_at: '2026-08-01T00:00:00Z',
    };
    this.pending.set(userId, result);
    records.set(idempotencyKey, { requestedPages, result });
    return { ...result };
  }

  /** 运维审批夹具：通过时清空 pending 并按申请页数叠加 extra_granted；驳回仅清空。 */
  resolvePending(userId: string, approved: boolean): void {
    const pending = this.pending.get(userId);
    if (pending === undefined) {
      return;
    }
    this.pending.delete(userId);
    this.idempotency.delete(userId);
    if (approved) {
      this.extraGranted.set(userId, (this.extraGranted.get(userId) ?? 0) + pending.requested_pages);
    }
  }

  /** 次月夹具：清空 pending 与 extra_granted，恢复月初基数（计数器回落到 base 形态）。 */
  rollToNextPeriod(userId: string): void {
    this.pending.delete(userId);
    this.extraGranted.delete(userId);
    this.usage.delete(userId);
  }
}
