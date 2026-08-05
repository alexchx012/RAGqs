import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// 生产构建资源挂在 /static/（FastAPI 静态挂载，单端口托管）；开发/e2e 用根路径。
// API 联调基准为契约 mock（MSW，规格 §1）：public/ 只放 mockServiceWorker.js，
// 生产构建关闭 publicDir，mock 资产不进生产构建；真实后端联调代理待后端就绪后另行配置。
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  publicDir: command === 'build' ? false : 'public',
  server: {
    port: 5173,
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  base: command === 'build' ? '/static/' : '/',
}));
