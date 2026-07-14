import { defineConfig, devices } from '@playwright/test';

/**
 * e2e 前置条件（硬性）:
 * 1. 后端以 environment=local（DEPLOYMENT_ENVIRONMENT=local）启动，
 *    否则会话 cookie 带 Secure，Playwright 的 http:// 上下文不会存储 cookie。
 * 2. AUTH_ENABLED=true + AUTH_PROVIDER=local_credentials
 * 3. AUTH_LOCAL_ADMIN_SEED=user:pass（或 E2E_ADMIN_USER/E2E_ADMIN_PASS）
 * 4. 后端监听 :9900（vite 代理目标）
 *
 * 运行: cd frontend && npm run test:e2e
 * 可传: AUTH_LOCAL_ADMIN_SEED=admin:secret npm run test:e2e
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  expect: { timeout: 10000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npx vite --port 5173',
    port: 5173,
    reuseExistingServer: !process.env.CI,
    // 仅启动前端；后端须由外部以 environment=local 启动
    env: {
      ...process.env,
    },
  },
});
