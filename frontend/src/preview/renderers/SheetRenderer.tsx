/*
 * 表格渲染器（fe-doc-preview；共用基座 §6）：Excel / CSV 只读表格。
 * - 单元格 1px hairline 发丝边、padding 8px 12px、表头（首行）mist-gray 底；
 * - Sheet 页签为分段开关样式（墨色系）置于预览顶部；?sheet= 切换由 PreviewPage 驱动；
 * - a1_range 滚动定位到行列区域并高亮：当前命中 blush-peach 100% 底 + 1px sienna-brown 内框，
 *   其余命中同底色 50% 浅标；Sheet 名原样透传（Excel 源 Sheet 名 / CSV 虚拟 Sheet，不改名不合并）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { copy } from '../../copy';
import { ErrorState } from '../../ui/states';
import { SegmentedControl } from '../../ui/SegmentedControl';
import type { PreviewApi } from '../api';
import { parseA1Range, type A1Range } from '../highlight';
import type { PreviewHit, PreviewSheetMeta, SheetContentResponse } from '../types';
import { useHitScroll } from '../use-hit-scroll';

export interface SheetRendererProps {
  readonly api: PreviewApi;
  readonly documentId: string;
  readonly documentVersionId: string | null;
  readonly sheets: readonly PreviewSheetMeta[];
  readonly activeSheet: string;
  readonly onSheetChange: (sheet: string) => void;
  readonly hits: readonly PreviewHit[];
  readonly currentHit: number | null;
}

interface SheetHitRange {
  readonly range: A1Range;
  readonly hitIndex: number;
  readonly current: boolean;
}

function SheetSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-2">
      {[100, 100, 100].map((width, index) => (
        <div
          key={index}
          aria-hidden="true"
          className="ui-skeleton h-10 rounded-[var(--radius-images)] bg-mist-gray"
          style={{ width: `${width}%` }}
        />
      ))}
    </div>
  );
}

function cellText(value: SheetContentResponse['rows'][number][number]): string {
  if (value === null) {
    return '';
  }
  return String(value);
}

export function SheetRenderer({
  api,
  documentId,
  documentVersionId,
  sheets,
  activeSheet,
  onSheetChange,
  hits,
  currentHit,
}: SheetRendererProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [content, setContent] = useState<SheetContentResponse | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setContent(null);
    setLoadError(false);
    api.getSheetContent(documentId, activeSheet, { documentVersionId }).then(
      (response) => {
        if (!cancelled) {
          setContent(response);
        }
      },
      () => {
        if (!cancelled) {
          setLoadError(true);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, documentId, activeSheet, documentVersionId, retryNonce]);

  /** 当前 Sheet 的命中区域（locator.sheet 与激活 Sheet 一致才定位；解析失败降级不高亮）。 */
  const hitRanges = useMemo((): SheetHitRange[] => {
    const result: SheetHitRange[] = [];
    hits.forEach((hit, hitIndex) => {
      if (!('sheet' in hit.locator) || hit.locator.sheet !== activeSheet) {
        return;
      }
      const range = parseA1Range(hit.locator.a1_range);
      if (range !== null) {
        result.push({ range, hitIndex, current: hitIndex === currentHit });
      }
    });
    return result;
  }, [hits, activeSheet, currentHit]);

  useHitScroll({ rootRef, hits, currentHit, ready: content !== null });

  const retry = useCallback(() => setRetryNonce((nonce) => nonce + 1), []);

  const header = content?.rows[0] ?? [];
  const bodyRows = content?.rows.slice(1) ?? [];

  return (
    <div ref={rootRef} className="preview-sheet-root flex flex-col gap-4">
      {sheets.length > 0 && (
        <div className="self-start">
          <SegmentedControl
            options={sheets.map((sheet) => ({ value: sheet.name, label: sheet.name }))}
            value={activeSheet}
            onChange={onSheetChange}
            ariaLabel={copy.preview.sheetTabsAria}
          />
        </div>
      )}
      {loadError ? (
        <ErrorState text={copy.preview.error} retryLabel={copy.preview.retry} onRetry={retry} />
      ) : content === null ? (
        <SheetSkeleton />
      ) : (
        <div className="overflow-x-auto">
          <table className="preview-sheet-table">
            <thead>
              <tr>
                {header.map((cell, columnIndex) => (
                  <th key={columnIndex} scope="col" className="text-left font-w480">
                    {cellText(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bodyRows.map((row, bodyRowIndex) => {
                const rowIndex = bodyRowIndex + 1;
                return (
                  <tr key={rowIndex}>
                    {row.map((cell, columnIndex) => {
                      const covering = hitRanges.find(
                        (hit) =>
                          rowIndex >= hit.range.startRow &&
                          rowIndex <= hit.range.endRow &&
                          columnIndex >= hit.range.startCol &&
                          columnIndex <= hit.range.endCol,
                      );
                      const isAnchor =
                        covering !== undefined &&
                        rowIndex === covering.range.startRow &&
                        columnIndex === covering.range.startCol;
                      return (
                        <td
                          key={columnIndex}
                          data-hit-anchor={isAnchor ? covering.hitIndex : undefined}
                          className={
                            covering !== undefined
                              ? covering.current
                                ? 'preview-hit-cell preview-hit-cell--current'
                                : 'preview-hit-cell'
                              : undefined
                          }
                        >
                          {cellText(cell)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
