/*
 * 文字链（共用基座 §3.2 重试/清除搜索、§4 全部已读、§5.6 预览/恢复）。
 * 15px text-slate-gray，hover 下划线（--duration-fast）；danger 变体 text-danger；
 * ink 变体 text-ink-black（用户管理「编辑」、部门管理「刷新 / 改名」文字链，运维端 §7.7 / 超管端 §7.6）。
 */

import type { ButtonHTMLAttributes } from 'react';

export interface TextLinkProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 危险变体：text-danger（退出、删除等文字链）。 */
  danger?: boolean;
  /** 墨色变体：text-ink-black（普通操作文字链；与 danger 互斥，danger 优先）。 */
  ink?: boolean;
}

export function TextLink({
  danger = false,
  ink = false,
  disabled,
  className = '',
  children,
  type = 'button',
  ...rest
}: TextLinkProps) {
  const toneClass = danger ? 'text-danger' : ink ? 'text-ink-black' : 'text-slate-gray';
  return (
    <button
      type={type}
      disabled={disabled}
      className={
        `text-[15px] underline-offset-2 transition-colors duration-[var(--duration-fast)] ` +
        `${toneClass} enabled:hover:underline disabled:text-smoke-gray ${className}`
      }
      {...rest}
    >
      {children}
    </button>
  );
}
