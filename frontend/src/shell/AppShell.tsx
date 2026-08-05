import { Outlet } from 'react-router';
import { copy } from '../copy';

/*
 * 应用壳（规格 §5）：全局基线的承载层。
 * 业务导航、铃铛、抽屉触发器等在 fe-shared-shell / 各业务 change 落地；
 * 本层只提供跳转主内容的可达性基线与 Outlet。
 */
export function AppShell() {
  return (
    <div className="min-h-screen bg-paper-white text-ink-black">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-50"
      >
        {copy.shell.skipToContent}
      </a>
      <main id="main" className="app-fade-in">
        <Outlet />
      </main>
    </div>
  );
}
