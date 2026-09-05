/*
 * 命中导航（fe-doc-preview；共用基座 §6）。
 * 条目：序号（15px 480）+ 摘要一行（15px 截断）+ 定位小字（15px slate-strong：第 X 页 / 章节路径 / Sheet + A1）；
 * padding 12px 16px；当前条目底 mist-gray（圆角 radius-images）；hover 同底 duration-fast。
 * 顶部提供「当前位置/总数」指示（aria-live），↑/↓ 方向键在命中间循环移动并选中（审查 P3）。
 * 桌面右栏与窄屏底部面板共用；空态（无 message_id 只读形态 / 错误态）由 empty 分支呈现。
 */

import { useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { copy } from '../copy';
import { EmptyState } from '../ui/states';
import type { PreviewHit, PreviewLocator } from './types';

export interface HitNavProps {
  readonly hits: readonly PreviewHit[];
  /** 当前命中（hits 数组下标）；无命中或无当前项为 null。 */
  readonly current: number | null;
  /** 点击条目：预览区滚动至对应命中并切换当前高亮。 */
  readonly onSelect: (index: number) => void;
}

/** 定位小字（与 citation-badge 悬停卡同规则；空 locator 无定位行）。 */
export function hitLocatorLine(locator: PreviewLocator): string | null {
  if ('page' in locator) {
    return copy.preview.hitLocatorPage(locator.page);
  }
  if ('section_path' in locator) {
    return copy.preview.hitLocatorSection(locator.section_path, locator.paragraph);
  }
  if ('sheet' in locator) {
    return copy.preview.hitLocatorSheet(locator.sheet, locator.a1_range);
  }
  return null;
}

export function HitNav({ hits, current, onSelect }: HitNavProps) {
  const listRef = useRef<HTMLUListElement | null>(null);
  if (hits.length === 0) {
    return <EmptyState text={copy.preview.navEmpty} />;
  }

  // ↑/↓ 方向键在命中间循环移动：移动即选中（与点击同路径），焦点同步落到对应条目
  const onKeyDown = (event: ReactKeyboardEvent<HTMLUListElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') {
      return;
    }
    event.preventDefault();
    const total = hits.length;
    const delta = event.key === 'ArrowDown' ? 1 : -1;
    const base = current ?? (delta === 1 ? -1 : 0);
    const next = (((base + delta) % total) + total) % total;
    onSelect(next);
    listRef.current?.querySelectorAll<HTMLButtonElement>('button')[next]?.focus();
  };

  return (
    <div>
      {current !== null && (
        <p aria-live="polite" className="px-4 pt-2 pb-1 text-caption text-slate-strong">
          {copy.preview.hitPosition(current + 1, hits.length)}
        </p>
      )}
      <ul ref={listRef} onKeyDown={onKeyDown} className="flex flex-col gap-1 p-2 pt-1">
        {hits.map((hit, index) => {
          const active = current === index;
          const locator = hitLocatorLine(hit.locator);
          return (
            <li key={`${hit.index}:${index}`}>
              <button
                type="button"
                aria-current={active || undefined}
                data-current={active || undefined}
                onClick={() => onSelect(index)}
                className="w-full rounded-[var(--radius-images)] px-4 py-3 text-left transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray data-[current]:bg-mist-gray"
              >
                <span className="flex items-baseline gap-2">
                  <span className="shrink-0 text-[15px] font-w480 text-ink-black">{hit.index}</span>
                  <span className="truncate text-[15px] text-ink-black">{hit.summary}</span>
                </span>
                {locator !== null && <p className="mt-0.5 text-[15px] text-slate-strong">{locator}</p>}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
