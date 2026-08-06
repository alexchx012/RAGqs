/*
 * 悬停卡（共用基座 §3.4 引用悬停卡规格：浮层卡 paper-white + radius-elevatedcards +
 * shadow-subtle，宽 280px padding 16px；openDelay 120ms；进入 opacity 0→1 + 上移 4px
 * --duration-fast --ease-out，移出后 150ms 淡出——keyframes 在 ui.css）。
 * 打开期间 useEscShield 挂空盾，防止 Esc 穿透关闭下层抽屉（esc-stack-provider 约定）。
 */

import * as RadixHoverCard from '@radix-ui/react-hover-card';
import { useState, type ReactNode } from 'react';
import { useEscShield } from '../lib/esc-stack-provider';

export interface HoverCardProps {
  /** 触发元素（引用角标等），以 asChild 透传。 */
  trigger: ReactNode;
  children: ReactNode;
  /** 默认 120ms（共用基座 §3.4）。 */
  openDelay?: number;
}

export function HoverCard({ trigger, children, openDelay = 120 }: HoverCardProps) {
  const [open, setOpen] = useState(false);
  useEscShield(open);
  return (
    <RadixHoverCard.Root open={open} onOpenChange={setOpen} openDelay={openDelay}>
      <RadixHoverCard.Trigger asChild>{trigger}</RadixHoverCard.Trigger>
      <RadixHoverCard.Portal>
        <RadixHoverCard.Content
          sideOffset={4}
          className={
            'ui-hovercard-content w-[280px] rounded-[var(--radius-elevatedcards)] ' +
            'bg-paper-white p-4 shadow-[var(--shadow-subtle)]'
          }
        >
          {children}
        </RadixHoverCard.Content>
      </RadixHoverCard.Portal>
    </RadixHoverCard.Root>
  );
}
