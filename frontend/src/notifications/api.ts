/*
 * 站内提醒 API 封装（shared-shell 规格 §8；契约 §5）。
 * 经 ApiClient 携带 /v1 前缀与 Bearer；ack 仅供上传结果层调用，本 change 仅封装。
 */

import type { ApiClient } from '../api/client';
import type { NotificationItem } from './types';

export interface NotificationsApi {
  /** GET /notifications；limit 1–50，默认 50。 */
  list(limit?: number): Promise<{ items: NotificationItem[] }>;
  /** GET /notifications/unread-count → { count }（服务端权威计数）。 */
  unreadCount(): Promise<{ count: number }>;
  /** POST /notifications/{id}/read（幂等 204）。 */
  markRead(id: string): Promise<void>;
  /** POST /notifications/read-all：不提交请求体（§5.3）。 */
  markAllRead(): Promise<void>;
  /** POST /notifications/events/{event_id}/ack（仅上传结果层使用）。 */
  ack(eventId: string): Promise<void>;
}

export function createNotificationsApi(client: ApiClient): NotificationsApi {
  return {
    list(limit) {
      const query = limit === undefined ? '' : `?limit=${limit}`;
      return client.request<{ items: NotificationItem[] }>(`/notifications${query}`);
    },
    unreadCount() {
      return client.request<{ count: number }>('/notifications/unread-count');
    },
    async markRead(id) {
      await client.request<void>(`/notifications/${encodeURIComponent(id)}/read`, {
        method: 'POST',
      });
    },
    async markAllRead() {
      // 不提交请求体、客户端时间戳、ID 列表或游标（§5.3）
      await client.request<void>('/notifications/read-all', { method: 'POST' });
    },
    async ack(eventId) {
      await client.request<void>(`/notifications/events/${encodeURIComponent(eventId)}/ack`, {
        method: 'POST',
      });
    },
  };
}
