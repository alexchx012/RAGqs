/*
 * 应用壳（规格 §5）：全局基线的承载层 + 共享壳层（fe-shared-shell）接线。
 * - DrawerHost：URL 驱动的全屏抽屉宿主（/settings、/admin 路径段），聊天主页在其下保持挂载；
 *   页头右侧挂铃铛（共用基座 §5.1；主页右上角铃铛由 HomePage 挂）。
 * - 通知轮询仅已认证时运行：本层位于 RequireAuth 之下，mount 启动、unmount 停止（规格 §4）。
 */
import { useEffect } from 'react';
import { Outlet } from 'react-router';
import { copy } from '../copy';
import { useNotifications } from '../notifications/NotificationsProvider';
import { DrawerHost } from './drawer/DrawerHost';
import { ShellBell } from './ShellBell';

export function AppShell() {
  const notifications = useNotifications();
  useEffect(() => {
    notifications.start();
    return () => notifications.stop();
  }, [notifications]);
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
      <DrawerHost headerRight={<ShellBell store={notifications} />} />
    </div>
  );
}
