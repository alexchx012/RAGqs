/*
 * Steep 通用按钮 Pill（共用基座 §3.2 新建会话按钮、§5.3 保存/取消、§5.6 删除确认按钮）。
 * - filled：bg-ink-black 白字、hover 不透明度 0.88（--duration-fast）、无阴影、Sohne 16px；
 * - ghost：透明底 + 1px 发丝边（或墨边变体），hover 底 mist-gray；
 * - 尺寸 md 36px / sm 32px / xs 28px；loading 复用 base.css .loading-dots 内联加载点。
 */

import { forwardRef, type ButtonHTMLAttributes } from 'react';

export type PillVariant = 'filled' | 'ghost';
export type PillSize = 'md' | 'sm' | 'xs';

export interface PillProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: PillVariant;
  /** ghost 描边变体：hairline 发丝边（默认）或 ink 墨边（共用基座 §5.6 申请增加页数）。 */
  ghostBorder?: 'hairline' | 'ink';
  /** filled 危险变体：bg-danger 白字（删除确认等，共用基座 §5.6）。 */
  danger?: boolean;
  size?: PillSize;
  /** 提交中：禁用并显示内联加载点（spinner 仅限按钮内，共用基座 §2.5）。 */
  loading?: boolean;
}

const SIZE_CLASS: Record<PillSize, string> = {
  md: 'h-9 px-4 text-[16px]',
  sm: 'h-8 px-3 text-[15px]',
  xs: 'h-7 px-3 text-[14px]',
};

export const Pill = forwardRef<HTMLButtonElement, PillProps>(function Pill(
  {
    variant = 'filled',
    ghostBorder = 'hairline',
    danger = false,
    size = 'md',
    loading = false,
    disabled,
    className = '',
    children,
    type = 'button',
    ...rest
  },
  ref,
) {
  const filledClass = danger
    ? 'bg-danger text-paper-white transition-opacity duration-[var(--duration-fast)] ' +
      'enabled:hover:opacity-[0.88]'
    : 'bg-ink-black text-paper-white transition-opacity duration-[var(--duration-fast)] ' +
      'enabled:hover:opacity-[0.88]';
  const ghostClass = `border text-ink-black transition-colors duration-[var(--duration-fast)] ${
    ghostBorder === 'ink' ? 'border-ink-black' : 'border-[var(--color-hairline)]'
  } enabled:hover:bg-mist-gray`;
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={
        `inline-flex items-center justify-center gap-2 rounded-[var(--radius-buttons)] ` +
        `disabled:bg-mist-gray disabled:text-smoke-gray disabled:opacity-100 ` +
        `${SIZE_CLASS[size]} ${variant === 'filled' ? filledClass : ghostClass} ${className}`
      }
      {...rest}
    >
      {loading ? (
        <span className="loading-dots" role="status">
          <span />
          <span />
          <span />
        </span>
      ) : (
        children
      )}
    </button>
  );
});
