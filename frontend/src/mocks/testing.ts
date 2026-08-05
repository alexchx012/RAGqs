/*
 * 测试装配：vitest 经 setupServer 在同一进程内拦截 fetch；
 * handlers 与开发环境共用，mock 状态在每个用例前复位。
 */

import { setupServer } from 'msw/node';
import { MockAuthController } from './auth-contract';
import { clearAuthCookies } from './dev-cookies';
import { createAuthHandlers } from './handlers';

export const mockAuth = new MockAuthController();
export const mockServer = setupServer(...createAuthHandlers(mockAuth));

export function resetMockAuth(): void {
  mockAuth.reset();
  clearAuthCookies();
}
