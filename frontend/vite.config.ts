import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 生产构建资源挂在 /static/（FastAPI 静态挂载）；开发/e2e 用根路径，
// 否则 BrowserRouter 的 /login /chat 等路由在 dev server 上 404。
export default defineConfig(({ command }) => ({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:9900',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  base: command === 'build' ? '/static/' : '/',
}));
