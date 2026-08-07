/*
 * 测试装配：vitest 经 setupServer 在同一进程内拦截 fetch；
 * handlers 与开发环境共用，mock 状态在每个用例前复位。
 */

import { setupServer } from 'msw/node';
import { MockAuthController } from './auth-contract';
import { clearAuthCookies } from './dev-cookies';
import { createAuthHandlers, createNotificationHandlers } from './handlers';
import { createChatHandlers } from './chat-handlers';
import { MockChatController } from './chat-contract';
import { MockNotificationsController } from './notifications-contract';

export const mockAuth = new MockAuthController();
export const mockNotifications = new MockNotificationsController((header) => ({
  userId: mockAuth.me(header).id,
}));
// 会话与问答域：鉴权 + 角色 + 部门经 mockAuth.me 装配（§6.1 空间权限按角色推导）
export const mockChat = new MockChatController((header) => {
  const user = mockAuth.me(header);
  return { userId: user.id, role: user.role, departmentId: user.department?.id ?? null };
});

export const mockServer = setupServer(
  ...createAuthHandlers(mockAuth),
  ...createNotificationHandlers(mockNotifications, mockAuth),
  ...createChatHandlers(mockChat),
);

export function resetMockAuth(): void {
  mockAuth.reset();
  clearAuthCookies();
}

export function resetMockNotifications(): void {
  mockNotifications.reset();
}

export function resetMockChat(): void {
  mockChat.reset();
}
