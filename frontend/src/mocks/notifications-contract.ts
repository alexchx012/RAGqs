/*
 * 站内提醒契约 mock 核心（shared-shell 规格 §8；契约《前端接口需求.md》§5、§13）。
 * 与传输层无关（handlers.ts 负责 MSW 接线），真实模拟：
 * - 每账号在线保留最新 50 条（保留窗口），送达序号倒序返回；limit 仅 1–50，非法 422；
 * - 未读数权威计算：未 read 且送达序号大于 read-all 水位且对应事件未 ack；
 * - read-all 水位语义：不提交请求体，水位取事务时最大送达序号；之后新物化的仍可未读；
 * - 单条 read 幂等 204；ack（event_id + 当前用户天然幂等）成功/重复 204、
 *   事件不存在或非接收者 404、非可 ack 类型 409 notification_event_not_acknowledgeable；
 *   ack 可早于物化：后到的对应通知直接为已读、不进未读数。
 * - 已删除文档元数据：种子内含后端固定脱敏文案样例，前端原样展示，mock 不提供任何恢复。
 */

import type { NotificationItem, NotificationType } from '../notifications/types';
import { MockHttpError } from './auth-contract';

export { MockHttpError };

interface NotificationRecord extends NotificationItem {
  readonly recipientId: string;
  readonly seq: number;
  /** ack 语义关联的事件 id（内部字段，契约 §5.1 不下发）。 */
  readonly eventId: string | null;
}

interface SeedInput {
  readonly type: NotificationType;
  readonly title: string;
  readonly payload?: NotificationItem['payload'];
  readonly read?: boolean;
  readonly minutesAgo?: number;
  readonly eventId?: string;
}

const ACKABLE_TYPES = new Set(['ingestion_completed', 'ocr_low_confidence']);
const RETENTION_LIMIT = 50;

/*
 * 种子样例标题（后端下发文案）：提取为常量供 e2e 引用同一数据源，避免测试硬编码重复。
 * 均为「后端 title」语义：前端原样展示（脱敏文案为后端固定文案，不恢复文件名）。
 */
export const NOTIFICATION_SEED_TITLES = {
  ingestionDone: '《员工报销制度》已解析完成并入库',
  redacted: '相关文档已删除',
  unknownType: '你的深度研究报告已生成',
} as const;

export interface MockNotificationsPersistence {
  load(): string | null;
  save(snapshot: string): void;
}

interface MockNotificationsSnapshot {
  items: NotificationRecord[];
  watermarks: [string, number][];
  ackedEvents: [string, string][];
  pendingEvents: [string, PendingEvent][];
  seq: number;
}

interface PendingEvent {
  readonly recipientId: string;
  readonly type: NotificationType;
}

/** 鉴权注入：装配处用 MockAuthController.me 实现；无有效 Bearer 时抛 MockHttpError(401)。 */
export type ValidateAuth = (header: string | null) => { userId: string };

export class MockNotificationsController {
  private items: NotificationRecord[] = [];
  /** read-all 水位：userId → 已覆盖的最大送达序号。 */
  private watermarks = new Map<string, number>();
  /** 已 ack 事件：userId → eventId 集合（ack 可早于物化）。 */
  private ackedEvents = new Map<string, Set<string>>();
  /** 已知事件注册表（outbox 事实）：eventId → 接收者与类型；通知物化与否不影响 ack 判定。 */
  private pendingEvents = new Map<string, PendingEvent>();
  private seq = 0;

  constructor(
    private readonly validateAuth: ValidateAuth,
    private readonly persistence: MockNotificationsPersistence | null = null,
  ) {
    const pending = this.persistence?.load() ?? null;
    this.reset();
    if (pending !== null) {
      this.hydrateFrom(pending);
    }
  }

  reset(): void {
    this.items = [];
    this.watermarks.clear();
    this.ackedEvents.clear();
    this.pendingEvents.clear();
    this.seq = 0;
    this.seedFixtures();
    // 同 auth：不在此持久化空库，避免冲掉构造路径 rehydrate 依赖的旧快照
  }

  private snapshot(): MockNotificationsSnapshot {
    const ackedEvents: [string, string][] = [];
    for (const [user, events] of this.ackedEvents) {
      for (const event of events) {
        ackedEvents.push([user, event]);
      }
    }
    return {
      items: this.items,
      watermarks: [...this.watermarks],
      ackedEvents,
      pendingEvents: [...this.pendingEvents],
      seq: this.seq,
    };
  }

  private persist(): void {
    this.persistence?.save(JSON.stringify(this.snapshot()));
  }

  private rehydrate(): void {
    const raw = this.persistence?.load();
    if (raw !== null && raw !== undefined) {
      this.hydrateFrom(raw);
    }
  }

  private hydrateFrom(raw: string): void {
    try {
      const snapshot = JSON.parse(raw) as MockNotificationsSnapshot;
      this.items = snapshot.items;
      this.watermarks = new Map(snapshot.watermarks);
      this.ackedEvents = new Map<string, Set<string>>();
      for (const [user, event] of snapshot.ackedEvents) {
        const set = this.ackedEvents.get(user) ?? new Set<string>();
        set.add(event);
        this.ackedEvents.set(user, set);
      }
      this.pendingEvents = new Map(snapshot.pendingEvents ?? []);
      this.seq = snapshot.seq;
    } catch {
      // 损坏快照视为空库（mock 开发便利，不上报）
    }
  }

  /** 测试 / 开发夹具：登记一个 outbox 已知事件（通知可能尚未物化，ack 判定以此为据）。 */
  registerPendingEvent(userId: string, eventId: string, type: NotificationType): void {
    this.pendingEvents.set(eventId, { recipientId: userId, type });
    this.persist();
  }

  /** 测试 / 开发夹具：为指定账号物化一条通知（保留窗口外最旧条目直接消失）。 */
  addNotification(userId: string, input: SeedInput): NotificationItem {
    this.seq += 1;
    const eventId = input.eventId ?? null;
    if (eventId !== null) {
      this.pendingEvents.delete(eventId); // 物化后不再是「未物化」事件
    }
    const acked = eventId !== null && (this.ackedEvents.get(userId)?.has(eventId) ?? false);
    const record: NotificationRecord = {
      id: `ntf_${this.seq.toString(36)}`,
      type: input.type,
      title: input.title,
      payload: input.payload ?? {},
      read: input.read ?? acked,
      event_occurred_at: new Date(Date.now() - (input.minutesAgo ?? 0) * 60_000).toISOString(),
      recipientId: userId,
      seq: this.seq,
      eventId,
    };
    this.items.push(record);
    // 保留窗口：每账号只留送达序号最新 50 条
    const mine = this.items.filter((item) => item.recipientId === userId);
    if (mine.length > RETENTION_LIMIT) {
      const keep = new Set(
        [...mine].sort((a, b) => b.seq - a.seq).slice(0, RETENTION_LIMIT).map((item) => item.id),
      );
      this.items = this.items.filter((item) => item.recipientId !== userId || keep.has(item.id));
    }
    this.persist();
    const { recipientId: _recipient, seq: _seq, eventId: _event, ...item } = record;
    return item;
  }

  private visibleTo(userId: string): NotificationRecord[] {
    return this.items
      .filter((item) => item.recipientId === userId)
      .sort((a, b) => b.seq - a.seq);
  }

  list(auth: string | null, limit?: number): NotificationItem[] {
    this.rehydrate();
    const { userId } = this.validateAuth(auth);
    const effective = limit ?? RETENTION_LIMIT;
    if (!Number.isInteger(effective) || effective < 1 || effective > RETENTION_LIMIT) {
      throw new MockHttpError(422, 'validation_error', { field: 'limit' });
    }
    // 服务端内部送达序列倒序；更小 limit 只收窄本次响应（§5.1）
    return this.visibleTo(userId).slice(0, effective).map(stripInternal);
  }

  unreadCount(auth: string | null): number {
    this.rehydrate();
    const { userId } = this.validateAuth(auth);
    const watermark = this.watermarks.get(userId) ?? 0;
    const acked = this.ackedEvents.get(userId) ?? new Set<string>();
    return this.visibleTo(userId).filter(
      (item) =>
        !item.read && item.seq > watermark && (item.eventId === null || !acked.has(item.eventId)),
    ).length;
  }

  /** 单条 read：幂等 204。 */
  markRead(auth: string | null, id: string): void {
    this.rehydrate();
    const { userId } = this.validateAuth(auth);
    const record = this.items.find((item) => item.recipientId === userId && item.id === id);
    if (record !== undefined && !record.read) {
      // 修改需写回数组元素（record 为引用），直接置位
      (record as { read: boolean }).read = true;
      this.persist();
    }
  }

  /** read-all：不提交请求体；水位 = 事务时最大送达序号；成功与重复均 204。 */
  markAllRead(auth: string | null): void {
    this.rehydrate();
    const { userId } = this.validateAuth(auth);
    const maxSeq = this.visibleTo(userId).reduce((max, item) => Math.max(max, item.seq), 0);
    this.watermarks.set(userId, maxSeq);
    for (const item of this.items) {
      if (item.recipientId === userId && item.seq <= maxSeq) {
        (item as { read: boolean }).read = true;
      }
    }
    this.persist();
  }

  /**
   * ack：当前用户成功/重复确认均 204；事件不存在或非接收者 404；
   * 非 ingestion_completed / ocr_low_confidence 类型 409；ack 早于物化时后到通知直接为已读。
   * 事件存在性以事件注册表（outbox 事实）为准，与通知是否物化无关（§5.4）。
   */
  ack(auth: string | null, eventId: string): void {
    this.rehydrate();
    const { userId } = this.validateAuth(auth);
    const materialized = this.items.filter((item) => item.eventId === eventId);
    const pending = this.pendingEvents.get(eventId);
    if (materialized.length === 0 && pending === undefined) {
      throw new MockHttpError(404, 'not_found');
    }
    const isRecipient =
      materialized.some((item) => item.recipientId === userId) ||
      pending?.recipientId === userId;
    if (!isRecipient) {
      throw new MockHttpError(404, 'not_found');
    }
    const type = materialized[0]?.type ?? pending?.type;
    if (type === undefined || !ACKABLE_TYPES.has(type)) {
      throw new MockHttpError(409, 'notification_event_not_acknowledgeable');
    }
    const set = this.ackedEvents.get(userId) ?? new Set<string>();
    set.add(eventId);
    this.ackedEvents.set(userId, set);
    for (const item of materialized) {
      if (item.recipientId === userId) {
        (item as { read: boolean }).read = true;
      }
    }
    this.persist();
  }

  /** 种子：九类样例 + 未知类型 + 脱敏文案样例 + ack 事件样例（§13 / 规格 §8）。 */
  private seedFixtures(): void {
    const seed = this.addNotification.bind(this);
    // 普通用户 zhangsan：个人侧全类型
    seed('u_user', { type: 'ingestion_completed', title: NOTIFICATION_SEED_TITLES.ingestionDone, payload: { job_id: 'job_ing_1', document_id: 'doc_1' }, minutesAgo: 3, eventId: 'ev_ingestion_done_1' });
    seed('u_user', { type: 'ocr_low_confidence', title: '《扫描版采购合同》文字识别置信度较低', payload: { job_id: 'job_ocr_1', document_id: 'doc_2' }, minutesAgo: 18, eventId: 'ev_ocr_low_1' });
    seed('u_user', { type: 'quota_approved', title: '你的配额增加申请已通过', payload: { request_id: 'qr_1' }, minutesAgo: 65 });
    seed('u_user', { type: 'quota_rejected', title: '你的配额增加申请已被驳回', payload: { request_id: 'qr_2' }, minutesAgo: 130, read: true });
    seed('u_user', { type: 'submission_approved', title: '你的投稿《财务 FAQ》已通过审核', payload: { submission_id: 'sub_1', document_id: 'doc_3', job_id: 'job_sub_1' }, minutesAgo: 240 });
    seed('u_user', { type: 'submission_rejected', title: '你的投稿《旧版流程》已被驳回', payload: { submission_id: 'sub_2', reason: '内容过期' }, minutesAgo: 1500, read: true });
    seed('u_user', { type: 'submission_invalidated', title: '你的投稿《部门制度汇编》已失效', payload: { submission_id: 'sub_3', reason: '目标空间状态变化' }, minutesAgo: 2900 });
    // 已删除文档：后端固定脱敏文案（前端原样展示，不恢复文件名）
    seed('u_user', { type: 'ingestion_completed', title: NOTIFICATION_SEED_TITLES.redacted, payload: { job_id: 'job_del_1', document_id: 'doc_deleted' }, minutesAgo: 4300, read: true });
    // 未知类型（契约已定但前端未知）：保留条目，后端 title + 通用图标兜底
    seed('u_user', { type: 'deep_research_completed', title: NOTIFICATION_SEED_TITLES.unknownType, payload: { research_id: 'rs_1' }, minutesAgo: 8 });
    // 部长：投稿与配额
    seed('u_minister', { type: 'submission_approved', title: '你的投稿《部门周报模板》已通过审核', payload: { submission_id: 'sub_m1', document_id: 'doc_m1', job_id: 'job_m1' }, minutesAgo: 30 });
    seed('u_minister', { type: 'quota_approved', title: '你的配额增加申请已通过', payload: { request_id: 'qr_m1' }, minutesAgo: 200 });
    // 运维：校准开窗 + 图谱构建（succeeded / failed）+ 解析完成
    seed('u_ops', { type: 'calibration_window_suggested', title: '系统建议开启新一轮 A/B 校准窗口', payload: {}, minutesAgo: 45 });
    seed('u_ops', { type: 'graph_build_completed', title: '公共库图谱构建已完成', payload: { graph_build_id: 'gb_1', status: 'succeeded', source_revision: 'r_11' }, minutesAgo: 90 });
    seed('u_ops', { type: 'graph_build_completed', title: '公共库图谱构建失败', payload: { graph_build_id: 'gb_2', status: 'failed', source_revision: 'r_12', failure_class: 'embedding_error' }, minutesAgo: 2000 });
    seed('u_ops', { type: 'ingestion_completed', title: '《运维手册》已解析完成并入库', payload: { job_id: 'job_op_1', document_id: 'doc_op_1' }, minutesAgo: 10, eventId: 'ev_ingestion_ops_1' });
    // 超管：少量
    seed('u_admin', { type: 'graph_build_completed', title: '公共库图谱构建已取消', payload: { graph_build_id: 'gb_3', status: 'cancelled', source_revision: 'r_13' }, minutesAgo: 500, read: true });
  }
}

function stripInternal(record: NotificationRecord): NotificationItem {
  const { recipientId: _recipient, seq: _seq, eventId: _event, ...item } = record;
  return item;
}
