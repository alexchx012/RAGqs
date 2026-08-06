/*
 * 站内提醒契约 mock 行为验证（契约 §5、§13）：送达倒序与 limit 校验、50 条保留窗口、
 * 未读数权威（read-all 水位 / ack）、单条 read 幂等、ack 全语义与种子覆盖。
 * 直接驱动 MockNotificationsController 全局单例（用例间经 vitest-setup 自动 reset）；
 * 鉴权头由 mockAuth.login 对种子账号签发（zhangsan→u_user、ops-wang→u_ops、admin→u_admin）。
 */

import { describe, expect, it } from 'vitest';
import { MockHttpError } from './notifications-contract';
import { mockAuth, mockNotifications } from './testing';

// 契约 §13：u_user（普通用户）可达七类；calibration_window_suggested 与
// graph_build_completed 接收者为运维，由下方 u_ops 用例覆盖，九类样例由此凑齐。
const USER_KNOWN_TYPES = [
  'ingestion_completed',
  'ocr_low_confidence',
  'quota_approved',
  'quota_rejected',
  'submission_approved',
  'submission_rejected',
  'submission_invalidated',
] as const;

const KNOWN_TYPES = [
  ...USER_KNOWN_TYPES,
  'calibration_window_suggested',
  'graph_build_completed',
] as const;

function bearerOf(username: string): string {
  const { accessToken } = mockAuth.login(username, 'password123', 'vitest');
  return `Bearer ${accessToken}`;
}

function expectHttpError(fn: () => unknown, status: number, code: string): void {
  try {
    fn();
  } catch (error) {
    expect(error).toBeInstanceOf(MockHttpError);
    const httpError = error as MockHttpError;
    expect(httpError.status).toBe(status);
    expect(httpError.code).toBe(code);
    return;
  }
  throw new Error(`expected MockHttpError ${status} ${code}`);
}

describe('站内提醒契约 mock', () => {
  describe('列表与 limit', () => {
    it('按服务端送达序号倒序返回（最新在前）', () => {
      const auth = bearerOf('admin');
      mockNotifications.addNotification('u_admin', {
        type: 'ingestion_completed',
        title: 'added-first',
      });
      mockNotifications.addNotification('u_admin', {
        type: 'ingestion_completed',
        title: 'added-second',
      });
      const items = mockNotifications.list(auth);
      expect(items[0]?.title).toBe('added-second');
      expect(items[1]?.title).toBe('added-first');
      expect(items[2]?.title).toBe('公共库图谱构建已取消');
    });

    it('limit 1–50 合法；0 / 51 / 非整数 → 422 validation_error', () => {
      const auth = bearerOf('zhangsan');
      expect(mockNotifications.list(auth, 1)).toHaveLength(1);
      expect(mockNotifications.list(auth, 50)).toHaveLength(9);
      expectHttpError(() => mockNotifications.list(auth, 0), 422, 'validation_error');
      expectHttpError(() => mockNotifications.list(auth, 51), 422, 'validation_error');
      expectHttpError(() => mockNotifications.list(auth, 1.5), 422, 'validation_error');
    });

    it('limit 收窄只影响本次响应，不影响后续默认拉取', () => {
      const auth = bearerOf('zhangsan');
      expect(mockNotifications.list(auth, 2)).toHaveLength(2);
      expect(mockNotifications.list(auth)).toHaveLength(9);
    });

    it('无有效 Bearer → 401', () => {
      expectHttpError(() => mockNotifications.list(null), 401, 'invalid_token');
      expectHttpError(() => mockNotifications.list('Bearer mat_bogus'), 401, 'invalid_token');
      expectHttpError(() => mockNotifications.unreadCount(null), 401, 'invalid_token');
    });
  });

  describe('保留窗口', () => {
    it('每账号在线保留最新 50 条，最旧条目直接消失', () => {
      const auth = bearerOf('admin');
      for (let index = 1; index <= 51; index += 1) {
        mockNotifications.addNotification('u_admin', {
          type: 'ingestion_completed',
          title: `backfill-${String(index).padStart(2, '0')}`,
        });
      }
      const items = mockNotifications.list(auth);
      expect(items).toHaveLength(50);
      expect(items[0]?.title).toBe('backfill-51');
      expect(items.at(-1)?.title).toBe('backfill-02');
      // 最旧两条（种子 + backfill-01）挤出保留窗口
      expect(items.some((item) => item.title === 'backfill-01')).toBe(false);
      expect(items.some((item) => item.title === '公共库图谱构建已取消')).toBe(false);
    });
  });

  describe('未读数权威', () => {
    it('read-all 水位之后新物化的未读仍计数', () => {
      const auth = bearerOf('zhangsan');
      mockNotifications.markAllRead(auth);
      expect(mockNotifications.unreadCount(auth)).toBe(0);
      mockNotifications.addNotification('u_user', {
        type: 'ingestion_completed',
        title: 'materialized-after-watermark',
      });
      expect(mockNotifications.unreadCount(auth)).toBe(1);
    });

    it('ack 过的事件不计入未读数', () => {
      const auth = bearerOf('zhangsan');
      // 种子未读：ingestion / ocr / quota_approved / submission_approved /
      // submission_invalidated / deep_research 共 6 条
      expect(mockNotifications.unreadCount(auth)).toBe(6);
      mockNotifications.ack(auth, 'ev_ingestion_done_1');
      expect(mockNotifications.unreadCount(auth)).toBe(5);
    });
  });

  describe('单条 read', () => {
    it('幂等：重复调用不报错，已读条目 read=true', () => {
      const auth = bearerOf('zhangsan');
      const target = mockNotifications.list(auth).find((item) => !item.read);
      expect(target).toBeDefined();
      const id = target?.id as string;
      mockNotifications.markRead(auth, id);
      mockNotifications.markRead(auth, id);
      const after = mockNotifications.list(auth).find((item) => item.id === id);
      expect(after?.read).toBe(true);
    });
  });

  describe('read-all', () => {
    it('水位取事务时最大送达序号：已渲染条目全 read=true，重复调用幂等', () => {
      const auth = bearerOf('zhangsan');
      mockNotifications.markAllRead(auth);
      mockNotifications.markAllRead(auth);
      expect(mockNotifications.unreadCount(auth)).toBe(0);
      expect(mockNotifications.list(auth).every((item) => item.read)).toBe(true);
    });
  });

  describe('ack', () => {
    it('已物化事件：成功与重复确认均不抛错', () => {
      const auth = bearerOf('zhangsan');
      expect(() => mockNotifications.ack(auth, 'ev_ingestion_done_1')).not.toThrow();
      expect(() => mockNotifications.ack(auth, 'ev_ingestion_done_1')).not.toThrow();
    });

    it('不存在的 eventId → 404', () => {
      const auth = bearerOf('zhangsan');
      expectHttpError(() => mockNotifications.ack(auth, 'ev_not_exists'), 404, 'not_found');
    });

    it('非接收者 → 404', () => {
      const auth = bearerOf('ops-wang');
      // ev_ingestion_done_1 属于 u_user
      expectHttpError(() => mockNotifications.ack(auth, 'ev_ingestion_done_1'), 404, 'not_found');
    });

    it('非可 ack 类型（quota_approved）→ 409 notification_event_not_acknowledgeable', () => {
      const auth = bearerOf('zhangsan');
      mockNotifications.addNotification('u_user', {
        type: 'quota_approved',
        title: 'quota-event',
        eventId: 'ev_quota_409',
      });
      expectHttpError(
        () => mockNotifications.ack(auth, 'ev_quota_409'),
        409,
        'notification_event_not_acknowledgeable',
      );
    });

    it('ack 早于物化：后到通知直接 read=true 且不进未读数', () => {
      const auth = bearerOf('zhangsan');
      mockNotifications.registerPendingEvent('u_user', 'ev_early_ack', 'ingestion_completed');
      expect(() => mockNotifications.ack(auth, 'ev_early_ack')).not.toThrow();
      const before = mockNotifications.unreadCount(auth);
      const item = mockNotifications.addNotification('u_user', {
        type: 'ingestion_completed',
        title: 'late-materialized',
        eventId: 'ev_early_ack',
      });
      expect(item.read).toBe(true);
      expect(mockNotifications.unreadCount(auth)).toBe(before);
    });
  });

  describe('种子覆盖', () => {
    it('u_user：个人侧七类已知类型 + 未知类型 + 脱敏文案样例均存在', () => {
      const auth = bearerOf('zhangsan');
      const items = mockNotifications.list(auth);
      const types = new Set(items.map((item) => item.type));
      for (const type of USER_KNOWN_TYPES) {
        expect(types.has(type)).toBe(true);
      }
      expect(types.has('deep_research_completed')).toBe(true);
      expect(items.some((item) => item.title === '相关文档已删除')).toBe(true);
    });

    it('u_ops：calibration_window_suggested 与 graph_build_completed（succeeded / failed 各一），跨账号种子并集覆盖九类', () => {
      const auth = bearerOf('ops-wang');
      const items = mockNotifications.list(auth);
      expect(items.some((item) => item.type === 'calibration_window_suggested')).toBe(true);
      const statuses = new Set(
        items
          .filter((item) => item.type === 'graph_build_completed')
          .map((item) => (item.payload as { status?: string }).status),
      );
      expect(statuses.has('succeeded')).toBe(true);
      expect(statuses.has('failed')).toBe(true);

      const userTypes = new Set(
        mockNotifications.list(bearerOf('zhangsan')).map((item) => item.type),
      );
      const union = new Set([...userTypes, ...items.map((item) => item.type)]);
      for (const type of KNOWN_TYPES) {
        expect(union.has(type)).toBe(true);
      }
    });
  });
});
