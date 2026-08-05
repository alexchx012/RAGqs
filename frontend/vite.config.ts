import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// 生产构建资源挂在 /static/（FastAPI 静态挂载，单端口托管）；开发/e2e 用根路径。
// 开发代理（API 请求转发）在 fe-auth-login 落地，本 change 不配置。
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
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
