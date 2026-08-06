/*
 * 聊天主页占位（fe-shared-shell）：会话工具行、会话列表、消息流与发送链路均在 fe-chat-home。
 * 本页只承载共享壳层验证所需的最小结构：
 * - 侧边栏底部头像区：点击进入全屏抽屉（共用基座 §3.2）——普通用户 / 部长打开个人段
 *   「设置」，运维与超管同一入口打开管理段「总览」dashboard（各端文档 §7）；
 * - 主区域右上角铃铛（距顶 20px、距右 24px，共用基座 §3.1）；
 * - 输入区占位：抽屉为 URL 驱动的覆盖层，本页在其下保持挂载不卸载，输入草稿原样保留（§5.1）；
 * - 消费运维登录落地标记（router/landing.ts）：自动展开管理段首层（shared-shell 规格 §1）。
 */

import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useAuthState } from '../auth/AuthProvider';
import { copy } from '../copy';
import { useNotifications } from '../notifications/NotificationsProvider';
import { AUTO_OPEN_ADMIN_DRAWER_STATE_KEY } from '../router/landing';
import { ShellBell } from './ShellBell';

export function HomePage() {
  const { user } = useAuthState();
  const navigate = useNavigate();
  const location = useLocation();
  const notifications = useNotifications();

  // 运维登录落地标记：自动展开管理段首层；replace 导航消费标记、不留历史条目
  useEffect(() => {
    const state = location.state as Record<string, unknown> | null;
    if (state?.[AUTO_OPEN_ADMIN_DRAWER_STATE_KEY] === true) {
      navigate('/admin/dashboard', { replace: true });
    }
  }, [location.state, navigate]);

  const openDrawer = () => {
    navigate(user !== null && (user.role === 'ops' || user.role === 'admin') ? '/admin/dashboard' : '/settings');
  };

  return (
    <div className="flex min-h-screen">
      {/* 侧边栏占位：搜索 / 新建会话 / 会话列表在 fe-chat-home 落地（共用基座 §3.2） */}
      <aside className="hidden w-[280px] shrink-0 flex-col justify-end border-r border-hairline bg-fog-white md:flex">
        <button
          type="button"
          onClick={openDrawer}
          aria-label={copy.shell.home.openDrawerAria}
          className="flex h-16 items-center gap-3 border-t border-hairline px-3 text-left transition-colors duration-(--duration-fast) hover:bg-mist-gray"
        >
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-mist-gray text-body font-w480 text-ink-black">
            {user?.display_name.slice(0, 1) ?? ''}
          </span>
          <span className="truncate text-body font-w480 text-ink-black">{user?.display_name}</span>
        </button>
      </aside>
      <div className="relative flex min-w-0 flex-1 flex-col">
        <div className="absolute right-6 top-5">
          <ShellBell store={notifications} />
        </div>
        {/* 窄屏抽屉入口：侧边栏收起时（fe-chat-home 的汉堡归其所有），本占位经头像区同一按钮 */}
        <div className="absolute left-6 top-5 md:hidden">
          <button
            type="button"
            onClick={openDrawer}
            aria-label={copy.shell.home.openDrawerAria}
            className="flex h-10 w-10 items-center justify-center rounded-full bg-mist-gray text-body font-w480 text-ink-black transition-colors duration-(--duration-fast) hover:bg-hairline"
          >
            {user?.display_name.slice(0, 1) ?? ''}
          </button>
        </div>
        <div className="flex flex-1 flex-col items-center justify-center px-6">
          <h1 className="font-signifier text-heading font-normal leading-heading tracking-heading">
            {copy.shell.placeholderTitle}
          </h1>
          <p className="mt-4 text-body text-slate-gray">{copy.shell.placeholderBody}</p>
        </div>
        {/* 输入区占位（共用基座 §3.3 的最小形态）：证明抽屉开关心路中草稿原样保留 */}
        <div className="mx-auto w-full max-w-[760px] px-6 pb-6">
          <textarea
            aria-label={copy.shell.home.composerPlaceholder}
            placeholder={copy.shell.home.composerPlaceholder}
            rows={2}
            className="w-full resize-none rounded-[var(--radius-inputs)] border border-hairline bg-paper-white p-4 text-body text-ink-black placeholder:text-smoke-gray focus:border-ink-black focus:outline-none"
          />
        </div>
      </div>
    </div>
  );
}
