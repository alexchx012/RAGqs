/*
 * 「⋯」菜单（共用基座 §3.2 会话条目 ⋯ 菜单、§5.6 文档行操作菜单）。
 * 触发钮：16px Ellipsis 图标，默认随父级 group hover/focus 淡入（opacity 0→1 --duration-fast），
 * 触屏常显由父级控制（alwaysVisible）；浮层宽 160px paper-white + radius-elevatedcards +
 * shadow-subtle；菜单项高 36px 15px，hover/highlighted 底 mist-gray；danger 项危险红文字。
 * 进出动效 keyframes 在 ui.css；Esc 与点外部关闭由 Radix 处理，打开期间 useEscShield 挂空盾。
 */

import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Ellipsis } from 'lucide-react';
import { useState } from 'react';
import { useEscShield } from '../lib/esc-stack-provider';

export interface MeatballMenuItem {
  key: string;
  label: string;
  onSelect: () => void;
  /** 危险项（删除等）：危险红文字（共用基座 §3.2）。 */
  danger?: boolean;
}

export interface MeatballMenuProps {
  items: MeatballMenuItem[];
  ariaLabel: string;
  /** 触屏等无 hover 场景常显；默认随父级 group hover/focus 淡入。 */
  alwaysVisible?: boolean;
}

export function MeatballMenu({ items, ariaLabel, alwaysVisible = false }: MeatballMenuProps) {
  const [open, setOpen] = useState(false);
  useEscShield(open);
  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen} modal={false}>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={ariaLabel}
          className={
            'inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-images)] ' +
            'text-ink-black transition-opacity duration-[var(--duration-fast)] ' +
            'data-[state=open]:opacity-100 ' +
            (alwaysVisible
              ? 'opacity-100'
              : 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100')
          }
        >
          <Ellipsis aria-hidden="true" className="h-4 w-4" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          sideOffset={4}
          align="end"
          className={
            'ui-menu-content w-[160px] rounded-[var(--radius-elevatedcards)] bg-paper-white ' +
            'p-1 shadow-[var(--shadow-subtle)]'
          }
        >
          {items.map((item) => (
            <DropdownMenu.Item
              key={item.key}
              onSelect={item.onSelect}
              className={
                'flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 ' +
                `text-[15px] outline-none select-none data-[highlighted]:bg-mist-gray ${
                  item.danger === true ? 'text-danger' : 'text-ink-black'
                }`
              }
            >
              {item.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
