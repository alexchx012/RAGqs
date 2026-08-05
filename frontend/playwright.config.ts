import { defineConfig, devices } from '@playwright/test';

/**
 * e2e 骨架：自包含（契约 mock MSW 接管 API，不依赖后端）。
 * 只启动 vite dev server，验证应用壳、主题机制、动效降级与登录链路。
 *
 * 运行: cd frontend && npm run test:e2e
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  expect: { timeout: 10000 },
  fullyParallel: true,
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
  },
});
