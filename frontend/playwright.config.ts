import { defineConfig, devices } from '@playwright/test';

/**
 * e2e 骨架：自包含，不依赖后端（API 代理在 fe-auth-login 落地）。
 * 只启动 vite dev server，验证应用壳、主题机制与动效降级。
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
