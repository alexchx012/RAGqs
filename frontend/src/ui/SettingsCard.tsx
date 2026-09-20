/*
 * 设置表单卡片容器（drawer-visual-system：880px / 16px 圆角 / 柔和双层阴影 / 32px 内边距）。
 * 宽度不作为参数：880px 是本变更的固定规格，参数化会立刻产生第二个尺寸。
 * 圆角与阴影一律走 token，使抽屉作用域（--radius-cards: 16px）与非抽屉页面各自取到正确值。
 * overflow-hidden：卡片半径是唯一权威，满宽子元素（如 FormFooter 的 fog 底为方角）一律裁到圆角内。
 * 卡片内没有 portal 浮层，裁切不会误伤。
 *
 * 标题与副标题（3.8，设计图：每张卡片都在第一行之前有「20px 标题 + 15px 灰色副标题」）：
 * - 两个 prop 都可选，且只看「是否有值」决定渲染。都缺省时不插入任何节点——知识库等既有调用方
 *   （六张卡片只传 ariaLabel + children）的输出与加该能力前逐字节相同，由测试的 innerHTML 守卫。
 * - 标题渲染为卡内 h2，不把标题文字挂到 section 的 aria-label 上：区域名只在 section 出现一次，
 *   标题元素自身不挂 aria-label，同一元素上不会既当标题又当可访问名（避免重复朗读）。
 *   调用方的外层 <section> 不得再具名，否则与卡片形成嵌套同名 landmark。
 * - 副标题走 text-caption（15px 字级）+ text-slate-strong（次级灰 token，不写死 hex），
 *   紧随标题之后；标题块位于卡片 32px 内边距之内、第一个表单行之前，不另加上外边距——
 *   顶部 32px 留白只由卡片自身的 p-8 提供，叠加会超出版式。
 */
import type { ReactNode } from 'react';

export interface SettingsCardProps {
  ariaLabel?: string;
  /** 卡片标题（20px 字级，渲染为 h2）。省略时不渲染标题。 */
  title?: string;
  /** 卡片灰色副标题（15px 字级）。省略时不渲染副标题。 */
  description?: string;
  children: ReactNode;
}

export function SettingsCard({ ariaLabel, title, description, children }: SettingsCardProps) {
  const hasHeader = title !== undefined || description !== undefined;

  return (
    <section
      data-testid="settings-card"
      aria-label={ariaLabel}
      className="mx-auto w-full max-w-[880px] overflow-hidden rounded-[var(--radius-cards)] bg-paper-white p-8 shadow-[var(--shadow-subtle-2)]"
    >
      {hasHeader && (
        <div>
          {title !== undefined && (
            <h2 className="text-body-lg leading-body-lg font-medium text-ink-black">{title}</h2>
          )}
          {description !== undefined && (
            <p className="mt-1.5 text-caption text-slate-strong">{description}</p>
          )}
        </div>
      )}
      {children}
    </section>
  );
}
