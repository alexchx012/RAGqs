/*
 * 两栏表单行（drawer-visual-system）：左列固定 260px（标签 + 灰色说明），列间距 24px，
 * 右列弹性承载控件；行内容上下各 20px（py-5）。
 * 行间 1px 分隔线由本组件始终渲染；组件不做「是否最后一行」的判断（它无法可靠知道自己的位置），
 * 由「直接包裹这组行的父容器」用 [&>*:last-child]:border-b-0 关闭末行分隔线。该父容器只要求是这些
 * 行的直接父级，不要求是卡片本身：卡片内若还有 FormFooter 之类的兄弟节点，请给整组行单独一层容器，
 * 否则加在卡片上的 :last-child 命中的是页脚，被去掉的会是页脚的下边框而不是末行的分隔线。
 * 分隔线颜色走 border-hairline（全局 #ececec，抽屉作用域 #e4e3e7），不写死 hex。
 * 只读行（readOnlyValue）右列渲染纯文本：禁用输入框会传达「本可编辑但当前不可用」的错误语义。
 */
import type { ReactNode } from 'react';

export interface FormRowProps {
  label: string;
  description?: string;
  htmlFor?: string;
  /** 只读行的文本值；提供时右列渲染纯文本，不渲染输入控件，也不参与保存。 */
  readOnlyValue?: string;
  children?: ReactNode;
}

export function FormRow({ label, description, htmlFor, readOnlyValue, children }: FormRowProps) {
  return (
    <div
      data-testid="form-row"
      className="flex flex-col gap-2 border-b border-hairline py-5 md:flex-row md:gap-6"
    >
      <div data-testid="form-row-label" className="shrink-0 md:w-[260px]">
        <label htmlFor={htmlFor} className="block text-caption font-w480 text-ink-black">
          {label}
        </label>
        {description !== undefined && (
          <p className="mt-1 text-caption text-slate-strong">{description}</p>
        )}
      </div>
      <div className="min-w-0 flex-1">
        {readOnlyValue !== undefined ? (
          <p className="text-body text-ink-black">{readOnlyValue}</p>
        ) : (
          children
        )}
      </div>
    </div>
  );
}
