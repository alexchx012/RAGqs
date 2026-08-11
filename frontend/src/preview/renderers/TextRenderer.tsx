/*
 * 文本流渲染器（fe-doc-preview；共用基座 §6）：md / txt / code / data。
 * snippet 文本流匹配高亮（空白容错，highlight.ts）；匹配不到仅打开文档（ranges 为空，不高亮）。
 * - plain：md / txt 原文文本流（whitespace-pre-wrap，17px --leading-body）；
 * - code：highlight.js 语法高亮（HTML 渲染后 DOM 包裹 <mark>，不打断 hljs span）；
 * - data：JSON 文本流（可解析则美化缩进，失败回退原文，勿过度设计）。
 */

import hljs from 'highlight.js/lib/common';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { copy } from '../../copy';
import { ErrorState } from '../../ui/states';
import { escapeHtml, highlightDomRanges, resolveSnippetRange, type DomHighlightRange } from '../highlight';
import { useHitScroll } from '../use-hit-scroll';
import type { PreviewApi } from '../api';
import type { PreviewHit } from '../types';

export type TextKind = 'plain' | 'code' | 'data';

export interface TextRendererProps {
  readonly api: PreviewApi;
  readonly documentId: string;
  readonly documentVersionId: string | null;
  readonly textKind: TextKind;
  readonly hits: readonly PreviewHit[];
  readonly currentHit: number | null;
}

function TextSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-3">
      {[100, 85, 60].map((width) => (
        <div
          key={width}
          aria-hidden="true"
          className="ui-skeleton h-4 rounded-[var(--radius-images)] bg-mist-gray"
          style={{ width: `${width}%` }}
        />
      ))}
    </div>
  );
}

/** 命中点 → 文本区间（hits 数组下标序）；匹配不到的命中点降级为不高亮。 */
function useHitRanges(text: string | null, hits: readonly PreviewHit[], currentHit: number | null): DomHighlightRange[] {
  return useMemo(() => {
    if (text === null) {
      return [];
    }
    const ranges: DomHighlightRange[] = [];
    hits.forEach((hit, hitIndex) => {
      const range = resolveSnippetRange(text, hit.snippet);
      if (range !== null) {
        ranges.push({ ...range, hitIndex, current: hitIndex === currentHit });
      }
    });
    return ranges.sort((a, b) => a.start - b.start || a.end - b.end);
  }, [text, hits, currentHit]);
}

export function TextRenderer({ api, documentId, documentVersionId, textKind, hits, currentHit }: TextRendererProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const codeRef = useRef<HTMLPreElement | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setLoadError(false);
    api.getTextContent(documentId, { documentVersionId }).then(
      (content) => {
        if (!cancelled) {
          setText(content);
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
  }, [api, documentId, documentVersionId, retryNonce]);

  /** data 形态：JSON 可解析则美化，失败回退原文。 */
  const displayText = useMemo(() => {
    if (text === null || textKind !== 'data') {
      return text;
    }
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }, [text, textKind]);

  const ranges = useHitRanges(displayText, hits, currentHit);

  // code：hljs HTML 渲染后 DOM 包裹 <mark>（坐标基于 textContent，与原文一致）
  const codeHtml = useMemo(() => {
    if (displayText === null || textKind !== 'code') {
      return null;
    }
    try {
      return hljs.highlightAuto(displayText).value;
    } catch {
      return escapeHtml(displayText);
    }
  }, [displayText, textKind]);

  useEffect(() => {
    if (codeHtml === null) {
      return;
    }
    const element = codeRef.current;
    if (element === null) {
      return;
    }
    element.innerHTML = codeHtml;
    highlightDomRanges(element, ranges);
  }, [codeHtml, ranges]);

  useHitScroll({ rootRef, hits, currentHit, ready: displayText !== null });

  const retry = useCallback(() => setRetryNonce((nonce) => nonce + 1), []);

  if (loadError) {
    return <ErrorState text={copy.preview.error} retryLabel={copy.preview.retry} onRetry={retry} />;
  }
  if (displayText === null) {
    return <TextSkeleton />;
  }

  if (textKind === 'code') {
    return (
      <div ref={rootRef} className="preview-text">
        <pre className="preview-code overflow-x-auto rounded-[var(--radius-images)] bg-fog-white p-4 text-[15px] leading-[var(--leading-body)] text-ink-black">
          <code ref={codeRef} className="hljs" />
        </pre>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="preview-text">
      <div className="whitespace-pre-wrap text-body leading-[var(--leading-body)] text-ink-black">
        <PlainTextWithHits text={displayText} ranges={ranges} />
      </div>
    </div>
  );
}

/** 声明式切片渲染：非命中段纯文本，命中段 <mark>（重叠区间跳过后来者，不叠标）。 */
function PlainTextWithHits({ text, ranges }: { text: string; ranges: readonly DomHighlightRange[] }): ReactElement {
  const parts: ReactNode[] = [];
  let cursor = 0;
  ranges.forEach((range) => {
    if (range.start < cursor) {
      return;
    }
    if (range.start > cursor) {
      parts.push(text.slice(cursor, range.start));
    }
    parts.push(
      <mark
        key={`${range.hitIndex}:${range.start}`}
        data-hit-anchor={range.hitIndex}
        className={range.current ? 'preview-hit preview-hit--current' : 'preview-hit'}
      >
        {text.slice(range.start, range.end)}
      </mark>,
    );
    cursor = range.end;
  });
  if (cursor < text.length) {
    parts.push(text.slice(cursor));
  }
  return <>{parts}</>;
}
