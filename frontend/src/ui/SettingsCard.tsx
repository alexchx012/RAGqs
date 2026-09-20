/*
 * 设置表单卡片容器（drawer-visual-system：880px / 16px 圆角 / 柔和双层阴影 / 32px 内边距）。
 * 宽度不作为参数：880px 是本变更的固定规格，参数化会立刻产生第二个尺寸。
 * 圆角与阴影一律走 token，使抽屉作用域（--radius-cards: 16px）与非抽屉页面各自取到正确值。
 * overflow-hidden：卡片半径是唯一权威，满宽子元素（如 FormFooter 的 fog 底为方角）一律裁到圆角内。
 * 卡片内没有 portal 浮层，裁切不会误伤。
 */
import type { ReactNode } from 'react';

export interface SettingsCardProps {
  ariaLabel?: string;
  children: ReactNode;
}

export function SettingsCard({ ariaLabel, children }: SettingsCardProps) {
  return (
    <section
      data-testid="settings-card"
      aria-label={ariaLabel}
      className="mx-auto w-full max-w-[880px] overflow-hidden rounded-[var(--radius-cards)] bg-paper-white p-8 shadow-[var(--shadow-subtle-2)]"
    >
      {children}
    </section>
  );
}
