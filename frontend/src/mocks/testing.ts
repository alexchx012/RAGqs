/*
 * 测试装配：vitest 经 setupServer 在同一进程内拦截 fetch；
 * handlers 与开发环境共用，mock 状态在每个用例前复位。
 */

import { setupServer } from 'msw/node';
import { MockAuthController } from './auth-contract';
import { clearAuthCookies } from './dev-cookies';
import { createAuthHandlers, createNotificationHandlers } from './handlers';
import { MockNotificationsController } from './notifications-contract';

export const mockAuth = new MockAuthController();
export const mockNotifications = new MockNotificationsController((header) => ({
  userId: mockAuth.me(header).id,
}));

export const mockServer = setupServer(
  ...createAuthHandlers(mockAuth),
  ...createNotificationHandlers(mockNotifications, mockAuth),
);

export function resetMockAuth(): void {
  mockAuth.reset();
  clearAuthCookies();
}

export function resetMockNotifications(): void {
  mockNotifications.reset();
}
