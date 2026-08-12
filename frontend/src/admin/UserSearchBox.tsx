/*
 * 聚合用户搜索框（spec §4 用户个人库顶部工具行；§7 用户管理工具行复用同一组件）。
 * 高 36px、rounded-inputs、paper-white 底、1px hairline 边、placeholder smoke 15px；
 * 受控纯展示组件：实时过滤的防抖与 q 参数提交由父层负责（前端只防抖传 q，
 * 匹配姓名 / 显示名 / 用户名 / 部门名 / 角色名的聚合在服务端完成）。
 */

import { Search } from 'lucide-react';
import { copy } from '../copy';

export interface UserSearchBoxProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  /** 缺省为用户管理工具行占位文案；个人库层传入 copy.admin.spaces.userSearchPlaceholder。 */
  readonly placeholder?: string;
  readonly ariaLabel?: string;
}

export function UserSearchBox({ value, onChange, placeholder, ariaLabel }: UserSearchBoxProps) {
  return (
    <div className="relative w-full max-w-[320px]">
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-smoke-gray"
      />
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder ?? copy.admin.users.searchPlaceholder}
        aria-label={ariaLabel ?? placeholder ?? copy.admin.users.searchPlaceholder}
        className={
          'h-9 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] ' +
          'bg-paper-white pr-3 pl-9 text-[15px] text-ink-black placeholder:text-smoke-gray ' +
          'focus:border-ink-black'
        }
      />
    </div>
  );
}
