/*
 * 提醒下拉面板（共用基座 §4；契约 §5）。
 * - Radix Popover 承载：铃铛正下方右对齐（side=bottom align=end）；桌面宽 360px、
 *   max-height 480px 内部滚动；窄屏宽 calc(100vw - 32px)；
 *   paper-white + --radius-elevatedcards + --shadow-subtle；进出动效见 styles/notifications.css。
 * - 打开面板即拉列表（Content 仅在 open 时挂载，挂载即 store.openPanel()）；打开不清除未读；
 *   Esc 与点外部关闭由 Radix 承担，useEscShield 由组合组件 NotificationBell 挂接（规格 §7）。
 * - 顶部行：标题 +「全部已读」文字链；点击后已渲染条目未读标识经 token 过渡淡出，
 *   随后刷新未读数与列表，新未读按服务端结果重新显示红点（契约 §5.3）。
 * - 列表保持服务端送达顺序不重排；未读：6px bg-danger 圆点 + bg-fog-white 底；
 *   hover bg-mist-gray；脱敏 title 原样展示、不做任何恢复。
 * - 状态：加载 3 条骨架（bg-mist-gray 呼吸）；空态 24px 图标 + 一行说明；
 *   错误态说明 + 重试文字链。禁整页 spinner、禁 toast。
 */

import * as Popover from '@radix-ui/react-popover';
import { BellOff } from 'lucide-react';
import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { copy } from '../copy';
import { NOTIFICATION_INTENT_CLASS, resolveNotificationMapping } from './mapping';
import { formatRelativeTime } from './relative-time';
import type { NotificationsStore } from './store';
import type { NotificationItem } from './types';

export interface NotificationPanelProps {
  readonly store: NotificationsStore;
  readonly onNavigate: (path: string) => void;
}

function SkeletonRows() {
  return (
    <ul data-testid="notification-skeleton-list" aria-label={copy.shell.loading}>
      {[0, 1, 2].map((index) => (
        <li key={index} className="flex h-16 items-center gap-3 px-4 py-3">
          <span className="notification-skeleton h-5 w-5 shrink-0 rounded-full bg-mist-gray" />
          <span className="min-w-0 flex-1">
            <span className="notification-skeleton block h-4 w-3/4 rounded-[var(--radius-images)] bg-mist-gray" />
            <span className="notification-skeleton mt-2 block h-3 w-1/3 rounded-[var(--radius-images)] bg-mist-gray" />
          </span>
        </li>
      ))}
    </ul>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-10">
      <BellOff size={24} aria-hidden className="text-smoke-gray" />
      <p className="text-caption text-smoke-gray">{copy.notifications.empty}</p>
    </div>
  );
}

function ErrorState({ onRetry }: { readonly onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-8">
      <p className="text-caption text-slate-gray">{copy.notifications.error}</p>
      <button
        type="button"
        onClick={onRetry}
        className="text-caption text-slate-gray transition-colors duration-(--duration-fast) hover:underline"
      >
        {copy.notifications.retry}
      </button>
    </div>
  );
}

interface NotificationRowProps {
  readonly item: NotificationItem;
  readonly store: NotificationsStore;
  readonly onNavigate: (path: string) => void;
}

function NotificationRow({ item, store, onNavigate }: NotificationRowProps) {
  const mapping = resolveNotificationMapping(item);
  const Icon = mapping.icon;
  return (
    <li>
      <button
        type="button"
        onClick={() => {
          // 标已读（红点 / 底色经 token 过渡淡出）并跳转目标；target 为 null（未知 type）不导航
          if (!item.read) {
            void store.markRead(item.id).catch(() => undefined);
          }
          if (mapping.target !== null) {
            onNavigate(mapping.target);
          }
        }}
        className={`notification-item flex h-16 w-full items-center gap-3
          rounded-[var(--radius-images)] px-4 py-3 text-left hover:bg-mist-gray
          ${item.read ? 'bg-transparent' : 'bg-fog-white'}`}
      >
        <Icon
          size={20}
          aria-hidden
          className={`shrink-0 ${NOTIFICATION_INTENT_CLASS[mapping.intent]}`}
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span
              aria-hidden
              className={`notification-unread-dot h-[6px] w-[6px] shrink-0 rounded-full bg-danger
                ${item.read ? 'opacity-0' : 'opacity-100'}`}
            />
            <span className="truncate text-caption text-ink-black">{item.title}</span>
          </span>
          <span className="mt-0.5 block truncate text-caption text-slate-gray">
            {formatRelativeTime(item.event_occurred_at, new Date())}
          </span>
        </span>
      </button>
    </li>
  );
}

export function NotificationPanel({ store, onNavigate }: NotificationPanelProps) {
  const subscribe = useCallback((listener: () => void) => store.subscribe(listener), [store]);
  const state = useSyncExternalStore(subscribe, () => store.getState());

  // 打开即拉列表：Content 仅在 open 时挂载
  useEffect(() => {
    void store.openPanel();
  }, [store]);

  let body;
  if (state.listStatus === 'error') {
    body = <ErrorState onRetry={() => void store.openPanel()} />;
  } else if (state.listStatus !== 'ready' || state.items === null) {
    body = <SkeletonRows />;
  } else if (state.items.length === 0) {
    body = <EmptyState />;
  } else {
    body = (
      <ul>
        {state.items.map((item) => (
          <NotificationRow key={item.id} item={item} store={store} onNavigate={onNavigate} />
        ))}
      </ul>
    );
  }

  return (
    <Popover.Portal>
      <Popover.Content
        side="bottom"
        align="end"
        sideOffset={8}
        className="notification-panel z-50 flex max-h-[480px] w-[calc(100vw-32px)] flex-col
          rounded-[var(--radius-elevatedcards)] bg-paper-white shadow-[var(--shadow-subtle)]
          md:w-[360px]"
      >
        <div className="flex items-center justify-between px-4 pb-2 pt-3">
          <h2 className="text-[16px] font-w480 text-ink-black">{copy.notifications.title}</h2>
          <button
            type="button"
            onClick={() => void store.markAllRead().catch(() => undefined)}
            className="text-caption text-slate-gray transition-colors duration-(--duration-fast) hover:underline"
          >
            {copy.notifications.readAll}
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">{body}</div>
      </Popover.Content>
    </Popover.Portal>
  );
}
