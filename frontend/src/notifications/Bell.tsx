/*
 * 铃铛按钮与组合组件（共用基座 §4；契约 §5）。
 * - Bell：20px 图标 ink-black，40px 触控区，hover 圆形 mist-gray（--duration-fast）；
 *   未读徽标 18px bg-danger 白字 Sohne 12px 500，>99 显示 99+；出现 / 脉冲 / 清零淡出
 *   动效见 styles/notifications.css；aria 经 copy.notifications。
 * - NotificationBell 组合导出（Popover.Root + Bell 触发器 + NotificationPanel），
 *   父组件只挂一个组件；open 期间 useEscShield 挂空盾，防止 Esc 穿透关闭下层抽屉（规格 §7）。
 * - 未读数 30s 轮询由 store.start()/stop() 承担，仅已认证时由调用方启动，组件不判角色。
 */

import * as Popover from '@radix-ui/react-popover';
import { Bell as BellIcon } from 'lucide-react';
import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ComponentPropsWithoutRef,
  type RefObject,
} from 'react';
import { copy } from '../copy';
import { useEscShield } from '../lib/esc-stack-provider';
import { NotificationPanel } from './NotificationPanel';
import type { NotificationsStore } from './store';

export interface NotificationBellProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly onNavigate: (path: string) => void;
  readonly store: NotificationsStore;
  /** Popover 自定义锚点；缺省锚定 Bell 按钮自身。 */
  readonly anchorRef?: RefObject<HTMLElement | null>;
}

/** 清零淡出时长，与 notification-badge-out（--duration-fast = 150ms）对齐。 */
const BADGE_EXIT_MS = 150;

function UnreadBadge({ count }: { readonly count: number | null }) {
  const effective = count ?? 0;
  const [displayed, setDisplayed] = useState(effective);
  const [variant, setVariant] = useState<'enter' | 'pulse'>('enter');
  const [exiting, setExiting] = useState(false);
  const previousRef = useRef(0);

  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = effective;
    if (effective > 0) {
      // 0→n 播出现动画；n→m 播数字变化脉冲（key 变化重挂载触发）
      setVariant(previous > 0 ? 'pulse' : 'enter');
      setDisplayed(effective);
      setExiting(false);
      return undefined;
    }
    if (previous > 0) {
      // 清零：150ms 缩放淡出后移除
      setExiting(true);
      const timer = setTimeout(() => {
        setDisplayed(0);
        setExiting(false);
      }, BADGE_EXIT_MS);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [effective]);

  if (displayed <= 0) {
    return null;
  }
  const phaseClass = exiting
    ? 'notification-badge-exit'
    : variant === 'pulse'
      ? 'notification-badge-pulse'
      : 'notification-badge-enter';
  return (
    <span
      key={displayed}
      aria-label={copy.notifications.unreadBadgeAria(displayed)}
      className={`pointer-events-none absolute right-[4px] top-[4px] flex h-[18px] min-w-[18px]
        items-center justify-center rounded-full bg-danger px-[4px] text-[12px] font-medium
        leading-none text-paper-white ${phaseClass}`}
    >
      {displayed > 99 ? '99+' : displayed}
    </span>
  );
}

export interface BellProps extends ComponentPropsWithoutRef<'button'> {
  readonly store: NotificationsStore;
}

export const Bell = forwardRef<HTMLButtonElement, BellProps>(function Bell(
  { store, ...buttonProps },
  ref,
) {
  const subscribe = useCallback((listener: () => void) => store.subscribe(listener), [store]);
  const unreadCount = useSyncExternalStore(subscribe, () => store.getState().unreadCount);
  return (
    <button
      // Popover.Trigger asChild 约定：子组件必须转发注入的事件 / aria props，否则触发器失效
      {...buttonProps}
      ref={ref}
      type="button"
      aria-label={copy.notifications.bellAria}
      className="relative flex h-10 w-10 items-center justify-center rounded-full text-ink-black
        transition-colors duration-(--duration-fast) hover:bg-mist-gray"
    >
      <BellIcon size={20} aria-hidden />
      <UnreadBadge count={unreadCount} />
    </button>
  );
});

/** 铃铛 + 下拉面板组合：父组件只挂这一个组件。 */
export function NotificationBell({
  open,
  onOpenChange,
  onNavigate,
  store,
  anchorRef,
}: NotificationBellProps) {
  useEscShield(open);
  return (
    <Popover.Root open={open} onOpenChange={onOpenChange}>
      {anchorRef !== undefined ? <Popover.Anchor virtualRef={anchorRef} /> : null}
      <Popover.Trigger asChild>
        <Bell store={store} />
      </Popover.Trigger>
      <NotificationPanel store={store} onNavigate={onNavigate} />
    </Popover.Root>
  );
}
