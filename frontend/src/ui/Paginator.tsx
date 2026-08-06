/*
 * 页码器（共用基座 §5.6 文档列表分页）：上一页 / 页码 / 下一页，
 * 15px slate-gray，当前页 ink-black 字重 480；边界禁用态 smoke-gray。
 */

import { copy } from '../copy';
import { TextLink } from './TextLink';

export interface PaginatorProps {
  /** 当前页（1 起）。 */
  page: number;
  totalPages: number;
  onChange: (page: number) => void;
}

export function Paginator({ page, totalPages, onChange }: PaginatorProps) {
  const hasPrev = page > 1;
  const hasNext = page < totalPages;
  return (
    <nav className="flex items-center justify-center gap-4">
      <TextLink disabled={!hasPrev} onClick={() => onChange(page - 1)}>
        {copy.controls.paginatorPrev}
      </TextLink>
      <span className="text-[15px] font-w480 text-ink-black">
        {copy.controls.pageIndicator(page, totalPages)}
      </span>
      <TextLink disabled={!hasNext} onClick={() => onChange(page + 1)}>
        {copy.controls.paginatorNext}
      </TextLink>
    </nav>
  );
}
