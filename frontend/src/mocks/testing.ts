/*
 * 测试装配：vitest 经 setupServer 在同一进程内拦截 fetch；
 * handlers 与开发环境共用，mock 状态在每个用例前复位。
 */

import { setupServer } from 'msw/node';
import { MockAdminController } from './admin-contract';
import { createAdminHandlers } from './admin-handlers';
import { MockAuthController } from './auth-contract';
import { clearAuthCookies } from './dev-cookies';
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

export const mockAuth = new MockAuthController();
export const mockNotifications = new MockNotificationsController((header) => ({
  userId: mockAuth.me(header).id,
}));
/** 配额单一权威：settings 计数器、配额申请、知识库上传 quota_exceeded 共用。 */
export const mockQuota = new MockQuotaStore([
  { userId: 'u_user', unlimited: false, used: 120, baseLimit: 500 },
  { userId: 'u_minister', unlimited: false, used: 120, baseLimit: 500 },
  { userId: 'u_ops', unlimited: true, used: 0, baseLimit: 500 },
  { userId: 'u_admin', unlimited: true, used: 0, baseLimit: 500 },
]);
export const mockSettings = new MockSettingsController(mockAuth, mockQuota);
// 会话与问答域：鉴权 + 角色 + 部门经 mockAuth.me 装配（§6.1 空间权限按角色推导）
export const mockChat = new MockChatController((header) => {
  const user = mockAuth.me(header);
  return { userId: user.id, role: user.role, departmentId: user.department?.id ?? null };
});
export const mockKnowledge = new MockKnowledgeController(
  (header) => mockAuth.me(header),
  mockQuota,
  mockNotifications,
);
// 原文预览域：鉴权经 mockAuth.me 装配，并与 chat mock 共用可读消息集合（fe-doc-preview）
export const mockPreview = new MockPreviewController(
  (header) => ({ userId: mockAuth.me(header).id }),
  (header, messageId) => mockChat.hasMessage(header, messageId),
);
// 管理面板域：鉴权经 mockAuth.me 装配；配额审批联动 mockQuota，投稿计数/任务队列联动 mockKnowledge
export const mockAdmin = new MockAdminController(
  (header) => mockAuth.me(header),
  mockKnowledge,
  mockNotifications,
  mockQuota,
);

export const mockServer = setupServer(
  ...createAuthHandlers(mockAuth),
  ...createSettingsHandlers(mockSettings),
  ...createNotificationHandlers(mockNotifications, mockAuth),
  // 管理面板 handler 先于 knowledge handler 注册：GET /v1/approvals/summary 同名路由在 admin
  // 域遮蔽（ops 的 quota_pending 由 admin 计数），MSW 按注册顺序首匹配命中。
  ...createAdminHandlers(mockAdmin, mockKnowledge),
  // 知识库 handler 先于 chat handler 注册：两者都匹配 GET /v1/spaces/:id/documents，
  // MSW 按注册顺序首匹配命中；知识库实现带分页与 page_size，chat 的检索范围 chip 只消费 items。
  ...createKnowledgeHandlers(mockKnowledge),
  ...createChatHandlers(mockChat),
  // 预览 handler 路径（/documents/:id/preview|content）与既有 /documents/:id/... 均不冲突，殿后注册
  ...createPreviewHandlers(mockPreview),
);

export function resetMockAuth(): void {
  mockAuth.reset();
  mockSettings.reset();
  mockQuota.reset();
  clearAuthCookies();
}

export function resetMockNotifications(): void {
  mockNotifications.reset();
}

export function resetMockChat(): void {
  mockChat.reset();
}

export function resetMockKnowledge(): void {
  mockKnowledge.reset();
}

export function resetMockAdmin(): void {
  mockAdmin.reset();
}

export function resetMockPreview(): void {
  mockPreview.reset();
}
