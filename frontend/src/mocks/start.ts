/*
 * 开发环境契约 mock 入口（规格 §1）：仅经 main.tsx 在 VITE_ENABLE_MSW=true 时动态引入，
 * 不进生产构建。
 * mock 服务端状态经 localStorage 持久化：页面刷新后 refresh Cookie 对应的会话仍在，
 * 静默 refresh 才能恢复会话（与真实后端行为一致）。
 */

import { setupWorker } from 'msw/browser';
import { MockAuthController } from './auth-contract';
import { createAuthHandlers } from './handlers';

const PERSISTENCE_KEY = 'ragqs.mock-auth.v1';

export async function startMockWorker(): Promise<void> {
  const controller = new MockAuthController(
    {},
    {
      load: () => localStorage.getItem(PERSISTENCE_KEY),
      save: (snapshot) => localStorage.setItem(PERSISTENCE_KEY, snapshot),
    },
  );
  const worker = setupWorker(...createAuthHandlers(controller));
  await worker.start({ onUnhandledRequest: 'bypass' });
}
