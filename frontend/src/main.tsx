import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { copy } from './copy';
import { initTheme } from './theme/theme';
import './index.css';

async function main() {
  // 契约 mock（MSW）仅用于开发，不进生产构建（规格 §1）：
  // VITE_ENABLE_MSW 只在 .env.development 置 true，生产构建时该分支被静态消除
  if (import.meta.env.VITE_ENABLE_MSW === 'true') {
    const { startMockWorker } = await import('./mocks/start');
    await startMockWorker();
  }

  initTheme();
  document.title = copy.appName;

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void main();
