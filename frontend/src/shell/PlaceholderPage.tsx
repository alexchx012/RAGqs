import { copy } from '../copy';

/*
 * 基座占位页：业务页面（登录页、聊天主页、设置页、管理面板）均在后续 change 实现。
 * 本页只验证 token、字体与主题机制在真实页面生效，文案全部来自单一文案常量文件。
 */
export function PlaceholderPage() {
  return (
    <div className="mx-auto max-w-[var(--page-max-width)] px-5 py-10">
      <h1 className="font-signifier text-heading leading-heading tracking-heading font-normal">
        {copy.shell.placeholderTitle}
      </h1>
      <p className="mt-4 text-body text-slate-gray">{copy.shell.placeholderBody}</p>
    </div>
  );
}
