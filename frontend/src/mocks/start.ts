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
import { MockKnowledgeController } from './knowledge-contract';
import { createKnowledgeHandlers } from './knowledge-handlers';
import { MockNotificationsController } from './notifications-contract';
import { MockPreviewController } from './preview-contract';
import { createPreviewHandlers } from './preview-handlers';
import { MockQuotaStore } from './quota-contract';
import { MockSettingsController } from './settings-contract';
import { createSettingsHandlers } from './settings-handlers';

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
  const quota = new MockQuotaStore([
    { userId: 'u_user', unlimited: false, used: 120, baseLimit: 500 },
    { userId: 'u_minister', unlimited: false, used: 120, baseLimit: 500 },
    { userId: 'u_ops', unlimited: true, used: 0, baseLimit: 500 },
    { userId: 'u_admin', unlimited: true, used: 0, baseLimit: 500 },
  ]);
  const settingsController = new MockSettingsController(authController, quota);
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
  const knowledgeController = new MockKnowledgeController(
    (header) => authController.me(header),
    quota,
    notificationsController,
  );
  // 原文预览域（fe-doc-preview）：内存态即可，页面刷新后预览经接口重新拉取
  const previewController = new MockPreviewController((header) => ({ userId: authController.me(header).id }));
  const worker = setupWorker(
    ...createAuthHandlers(authController),
    ...createSettingsHandlers(settingsController),
    ...createNotificationHandlers(notificationsController, authController),
    // 知识库 handler 先于 chat handler 注册：两者都匹配 GET /v1/spaces/:id/documents，
    // 按注册顺序首匹配命中；chat 检索范围 chip 只消费 items。
    ...createKnowledgeHandlers(knowledgeController),
    ...createChatHandlers(chatController),
    ...createPreviewHandlers(previewController),
  );
  await worker.start({ onUnhandledRequest: 'bypass' });
}
