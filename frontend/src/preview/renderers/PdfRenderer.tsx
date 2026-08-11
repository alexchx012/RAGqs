/*
 * PDF 渲染器（fe-doc-preview；共用基座 §6）。
 * - 有文本层：react-pdf 逐页渲染（页卡白底 shadow-subtle），打开跳到命中页并用 snippet 在
 *   pdfjs 文本层匹配高亮（span 仅同页多处重复时消歧，highlight.ts 纯函数）；
 * - 扫描件（无文本层）：只跳页，不做片段高亮，不出现任何替代锚点 UI。
 * 内容经 pdfjs 直取（file.url + Authorization httpHeaders + withCredentials；Range 分段由 pdfjs 自管）。
 * jsdom 无 canvas：组件测以 vi.mock 替身 react-pdf 验证跳页与文本匹配接线（PdfRenderer.test.tsx）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Document, Page } from 'react-pdf';
import { copy } from '../../copy';
import { TextLink } from '../../ui/TextLink';
import { escapeHtml, resolveSnippetRange, type TextRange } from '../highlight';
import { scrollToCenter } from '../scroll';
import type { PreviewHit } from '../types';
import '../pdf-worker';

/** 预览区内容限宽（共用基座 §6：880px 居中）。 */
const PDF_PAGE_WIDTH = 880;

/**
 * pdfjs 文本项（只消费 str；坐标由组件按拼接序推导）。
 * pdfjs TextContent.items 是 TextItem | TextMarkedContent 联合（后者无 str）；
 * 索引签名让两个成员都能落入本类型（规避全可选属性的 weak-type 判定）。
 */
interface PdfTextItem {
  readonly str?: string;
  readonly [key: string]: unknown;
}

interface PdfTextContent {
  readonly items: readonly PdfTextItem[];
}

interface PageHitRange extends TextRange {
  readonly hitIndex: number;
  readonly current: boolean;
}

export interface PdfRendererProps {
  /** api.buildContentUrl 产物（含 document_version_id 透传）。 */
  readonly fileUrl: string;
  /** 当前 access token（httpHeaders 注入；null 时不携带）。 */
  readonly token: string | null;
  readonly hasTextLayer: boolean;
  readonly hits: readonly PreviewHit[];
  readonly currentHit: number | null;
}

function PdfSkeleton() {
  return (
    <div aria-hidden="true" className="ui-skeleton h-[640px] rounded-[var(--radius-images)] bg-mist-gray" />
  );
}

export function PdfRenderer({ fileUrl, token, hasTextLayer, hits, currentHit }: PdfRendererProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [numPages, setNumPages] = useState<number | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  /** pageNumber → 文本层 items（onGetTextSuccess 捕获）。 */
  const [pageItems, setPageItems] = useState<ReadonlyMap<number, readonly PdfTextItem[]>>(new Map());

  const file = useMemo(
    () => ({
      url: fileUrl,
      ...(token === null ? {} : { httpHeaders: { Authorization: `Bearer ${token}` } }),
      withCredentials: true,
    }),
    [fileUrl, token],
  );

  const onDocumentLoadSuccess = useCallback((document: { numPages: number }) => {
    setNumPages(document.numPages);
    setLoadFailed(false);
  }, []);

  const onDocumentLoadError = useCallback(() => {
    setLoadFailed(true);
  }, []);

  /** 页文本（items 拼接序坐标；与 customTextRenderer 的 itemIndex 偏移一致）。 */
  const pageText = useCallback(
    (pageNumber: number): string => (pageItems.get(pageNumber) ?? []).map((item) => item.str ?? '').join(''),
    [pageItems],
  );

  /** 页 → 命中区间（仅文本层形态计算；扫描件恒空 → 无任何片段高亮）。 */
  const rangesByPage = useMemo(() => {
    const map = new Map<number, PageHitRange[]>();
    if (!hasTextLayer) {
      return map;
    }
    hits.forEach((hit, hitIndex) => {
      if (!('page' in hit.locator)) {
        return;
      }
      const pageNumber = hit.locator.page;
      const range = resolveSnippetRange(pageText(pageNumber), hit.snippet, hit.locator.span);
      if (range === null) {
        return;
      }
      const list = map.get(pageNumber) ?? [];
      list.push({ ...range, hitIndex, current: hitIndex === currentHit });
      map.set(pageNumber, list);
    });
    return map;
  }, [hasTextLayer, hits, currentHit, pageText]);

  /** react-pdf customTextRenderer：返回 HTML 字符串；非命中段必须转义（PDF 文本不可信）。 */
  const customTextRenderer = useCallback(
    ({ pageNumber, itemIndex, str }: { pageNumber: number; itemIndex: number; str: string }): string => {
      const items = pageItems.get(pageNumber);
      const ranges = rangesByPage.get(pageNumber) ?? [];
      if (items === undefined || ranges.length === 0) {
        return escapeHtml(str);
      }
      let itemStart = 0;
      for (let index = 0; index < itemIndex; index += 1) {
        itemStart += (items[index]?.str ?? '').length;
      }
      const itemEnd = itemStart + str.length;
      const overlapping = ranges
        .filter((range) => range.start < itemEnd && range.end > itemStart)
        .sort((a, b) => a.start - b.start);
      if (overlapping.length === 0) {
        return escapeHtml(str);
      }
      let html = '';
      let cursor = itemStart;
      for (const range of overlapping) {
        const from = Math.max(range.start, itemStart);
        const to = Math.min(range.end, itemEnd);
        if (from > cursor) {
          html += escapeHtml(str.slice(cursor - itemStart, from - itemStart));
        }
        const cls = range.current ? 'preview-hit preview-hit--current' : 'preview-hit';
        html += `<mark class="${cls}" data-hit-anchor="${range.hitIndex}">${escapeHtml(str.slice(from - itemStart, to - itemStart))}</mark>`;
        cursor = to;
      }
      if (cursor < itemEnd) {
        html += escapeHtml(str.slice(cursor - itemStart));
      }
      return html;
    },
    [pageItems, rangesByPage],
  );

  const onGetTextSuccess = useCallback((pageNumber: number, textContent: PdfTextContent) => {
    setPageItems((previous) => {
      const next = new Map(previous);
      next.set(pageNumber, textContent.items);
      return next;
    });
  }, []);

  // 打开 / 切换命中滚动：优先片段锚点（文本层），缺省回退命中页容器（扫描件唯一形态）
  const scrollReady = numPages !== null && (!hasTextLayer || pageItems.size > 0);
  useEffect(() => {
    if (!scrollReady || currentHit === null) {
      return;
    }
    const root = rootRef.current;
    const hit = hits[currentHit];
    if (root === null || hit === undefined || !('page' in hit.locator)) {
      return;
    }
    const anchor =
      root.querySelector(`[data-hit-anchor="${currentHit}"]`) ??
      root.querySelector(`[data-page-number="${hit.locator.page}"]`);
    scrollToCenter(anchor);
  }, [scrollReady, currentHit, hits, pageItems]);

  if (loadFailed) {
    return (
      <div className="flex flex-col items-center gap-2 py-20">
        <p className="text-[15px] text-slate-gray">{copy.preview.error}</p>
        <TextLink onClick={() => { setLoadFailed(false); setNumPages(null); setPageItems(new Map()); setRetryNonce((nonce) => nonce + 1); }}>
          {copy.preview.retry}
        </TextLink>
      </div>
    );
  }

  const pages = numPages === null ? [] : Array.from({ length: numPages }, (_unused, index) => index + 1);

  return (
    <div ref={rootRef} className="preview-pdf">
      <Document
        key={retryNonce}
        file={file}
        onLoadSuccess={onDocumentLoadSuccess}
        onLoadError={onDocumentLoadError}
        loading={<PdfSkeleton />}
        error={<PdfSkeleton />}
      >
        {pages.map((pageNumber) => (
          <div
            key={pageNumber}
            data-page-number={pageNumber}
            className="preview-pdf-page mb-6 overflow-hidden rounded-[var(--radius-images)] bg-paper-white shadow-[var(--shadow-subtle)]"
          >
            <Page
              pageNumber={pageNumber}
              width={PDF_PAGE_WIDTH}
              renderAnnotationLayer={false}
              onGetTextSuccess={(textContent: PdfTextContent) => onGetTextSuccess(pageNumber, textContent)}
              customTextRenderer={customTextRenderer}
              loading={<PdfSkeleton />}
              error={<PdfSkeleton />}
            />
          </div>
        ))}
      </Document>
    </div>
  );
}
