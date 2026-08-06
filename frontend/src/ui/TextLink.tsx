/*
 * 文字链（共用基座 §3.2 重试/清除搜索、§4 全部已读、§5.6 预览/恢复）。
 * 15px text-slate-gray，hover 下划线（--duration-fast）；danger 变体 text-danger。
 */

import type { ButtonHTMLAttributes } from 'react';

export interface TextLinkProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 危险变体：text-danger（退出、删除等文字链）。 */
  danger?: boolean;
}

export function TextLink({
  danger = false,
  disabled,
  className = '',
  children,
  type = 'button',
  ...rest
}: TextLinkProps) {
  return (
    <button
      type={type}
      disabled={disabled}
      className={
        `text-[15px] underline-offset-2 transition-colors duration-[var(--duration-fast)] ` +
        `${danger ? 'text-danger' : 'text-slate-gray'} ` +
        `enabled:hover:underline disabled:text-smoke-gray ${className}`
      }
      {...rest}
    >
      {children}
    </button>
  );
}
