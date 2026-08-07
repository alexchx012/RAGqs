/*
 * 开发环境契约 mock 入口（规格 §1）：仅经 main.tsx 在 VITE_ENABLE_MSW=true 时动态引入，
 * 不进生产构建。
 * mock 服务端状态经 localStorage 持久化：页面刷新后 refresh Cookie 对应的会话仍在，
 * 静默 refresh 才能恢复会话（与真实后端行为一致）；通知的已读 / ack 状态同理保留。
 */

import { setupWorker } from 'msw/browser';
import { MockAuthController } from './auth-contract';
import { createAuthHandlers, createNotificationHandlers } from './handlers';
import { createChatHandlers } from './chat-handlers';
import { MockChatController } from './chat-contract';
import { MockNotificationsController } from './notifications-contract';

const PERSISTENCE_KEY = 'ragqs.mock-auth.v1';
const NOTIFICATIONS_PERSISTENCE_KEY = 'ragqs.mock-notifications.v1';

export async function startMockWorker(): Promise<void> {
  const authController = new MockAuthController(
    {},
    {
      load: () => localStorage.getItem(PERSISTENCE_KEY),
      save: (snapshot) => localStorage.setItem(PERSISTENCE_KEY, snapshot),
    },
  );
  const notificationsController = new MockNotificationsController(
    (header) => ({ userId: authController.me(header).id }),
    {
      load: () => localStorage.getItem(NOTIFICATIONS_PERSISTENCE_KEY),
      save: (snapshot) => localStorage.setItem(NOTIFICATIONS_PERSISTENCE_KEY, snapshot),
    },
  );
  // 会话与问答域：mock 状态随会话共享（内存态，页面刷新后按读模型恢复是前端职责，非 mock 持久化）
  const chatController = new MockChatController((header) => {
    const user = authController.me(header);
    return { userId: user.id, role: user.role, departmentId: user.department?.id ?? null };
  });
  const worker = setupWorker(
    ...createAuthHandlers(authController),
    ...createNotificationHandlers(notificationsController, authController),
    ...createChatHandlers(chatController),
  );
  await worker.start({ onUnhandledRequest: 'bypass' });
}
