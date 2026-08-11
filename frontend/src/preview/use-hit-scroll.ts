/*
 * 命中滚动定位 Hook（fe-doc-preview）：各渲染器共用。
 * currentHit 变化（含打开时首个命中）且内容已渲染后，把 [data-hit-anchor="<i>"] 平滑滚动至视口中央；
 * 锚点缺省（扫描件命中页无片段高亮等）时用 fallback 选择器（如命中页容器），仍无则不动（不硬造锚点）。
 */

import { useEffect, type RefObject } from 'react';
import { scrollToCenter } from './scroll';
import type { PreviewHit } from './types';

export interface UseHitScrollOptions {
  readonly rootRef: RefObject<HTMLElement | null>;
  readonly hits: readonly PreviewHit[];
  readonly currentHit: number | null;
  /** 内容渲染就绪信号（文本已取回 / 表格已加载 / PDF 文本层已出）。 */
  readonly ready: boolean;
  /** 无片段锚点时的兜底选择器（按命中 locator 推导，如 PDF 页容器）。 */
  readonly fallbackSelector?: (hit: PreviewHit) => string | null;
}

export function useHitScroll({ rootRef, hits, currentHit, ready, fallbackSelector }: UseHitScrollOptions): void {
  useEffect(() => {
    if (!ready || currentHit === null) {
      return;
    }
    const root = rootRef.current;
    const hit = hits[currentHit];
    if (root === null || hit === undefined) {
      return;
    }
    const anchor = root.querySelector(`[data-hit-anchor="${currentHit}"]`);
    if (anchor !== null) {
      scrollToCenter(anchor);
      return;
    }
    const fallback = fallbackSelector?.(hit);
    if (fallback != null) {
      scrollToCenter(root.querySelector(fallback));
    }
  }, [rootRef, hits, currentHit, ready, fallbackSelector]);
}
