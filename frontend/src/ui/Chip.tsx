/*
 * chip 触发钮（共用基座 §3.3 检索范围 chip）：ghost pill 高 32px padding 0 12px，
 * 透明底 + 1px 发丝边，Sohne 14px，右侧 16px 下拉箭头；展开箭头旋转 180°（--duration-base
 * --ease-in-out），hover 底 mist-gray，展开态底 mist-gray 常驻；非默认时左侧 6px 墨色实心点。
 * 仅触发钮；浮层内容（空间多选列表）由业务侧经 Popover 承载。
 */

import { ChevronDown } from 'lucide-react';
import type { ButtonHTMLAttributes } from 'react';

export interface ChipProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 展开态：箭头旋转 180° + mist-gray 底常驻。 */
  open?: boolean;
  /** 非默认选择：左侧 6px 墨色实心点。 */
  nonDefault?: boolean;
}

export function Chip({
  open = false,
  nonDefault = false,
  className = '',
  children,
  type = 'button',
  ...rest
}: ChipProps) {
  return (
    <button
      type={type}
      aria-expanded={open}
      className={
        'inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-buttons)] border ' +
        'border-[var(--color-hairline)] px-3 text-[14px] text-ink-black transition-colors ' +
        `duration-[var(--duration-fast)] hover:bg-mist-gray ${
          open ? 'bg-mist-gray' : 'bg-transparent'
        } ${className}`
      }
      {...rest}
    >
      {nonDefault && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-ink-black" />}
      <span className="max-w-[200px] truncate">{children}</span>
      <ChevronDown
        aria-hidden="true"
        className={
          'h-4 w-4 transition-transform duration-[var(--duration-base)] ' +
          `ease-[var(--ease-in-out)] ${open ? 'rotate-180' : ''}`
        }
      />
    </button>
  );
}
