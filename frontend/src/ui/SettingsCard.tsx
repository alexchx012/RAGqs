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
 * - 标题渲染为卡内 h2。有标题时区域名由该标题命名（section 的 aria-labelledby 指向它的 id）：
 *   区域名的权威来源就是用户看得见的标题，不再取可能与标题漂移的另一个文案键（D4 已把可见标题
 *   改成「常规设置 / 账号设置 / 安全设置」，旧 sectionLabel「外观 / 个人资料 / 安全」不再用于区域名）。
 *   读屏进区域时播报区域名、走到标题时播报标题，是正常的层次化播报，不是重复朗读。
 * - 无标题时才回退到 aria-label={ariaLabel}（知识库六张卡片即此形态，行为不变）；两者都缺省时卡片
 *   只是一个无名 section，与加该能力前一致。两个命名属性不同时挂，标题元素自身也不挂 aria-label。
 *   调用方的外层 <section> 不得再具名，否则与卡片形成嵌套同名 landmark。
 * - 副标题走 text-caption（15px 字级）+ text-slate-strong（次级灰 token，不写死 hex），
 *   紧随标题之后；标题块位于卡片 32px 内边距之内、第一个表单行之前，不另加上外边距——
 *   顶部 32px 留白只由卡片自身的 p-8 提供，叠加会超出版式。
 */
import { useId, type ReactNode } from 'react';

export interface SettingsCardProps {
  ariaLabel?: string;
  /** 卡片标题（20px 字级，渲染为 h2）。省略时不渲染标题。 */
  title?: string;
  /** 卡片灰色副标题（15px 字级）。省略时不渲染副标题。 */
  description?: string;
  children: ReactNode;
}

export function SettingsCard({ ariaLabel, title, description, children }: SettingsCardProps) {
  // 区域名：有标题时取自标题，无标题时才用 ariaLabel。
  const headingId = useId();
  const labelledBy = title === undefined ? undefined : headingId;
  const hasHeader = title !== undefined || description !== undefined;

  return (
    <section
      data-testid="settings-card"
      aria-label={labelledBy === undefined ? ariaLabel : undefined}
      aria-labelledby={labelledBy}
      className="mx-auto w-full max-w-[880px] overflow-hidden rounded-[var(--radius-cards)] bg-paper-white p-8 shadow-[var(--shadow-subtle-2)]"
    >
      {hasHeader && (
        <div>
          {title !== undefined && (
            <h2 id={headingId} className="text-body-lg leading-body-lg font-medium text-ink-black">
              {title}
            </h2>
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
