/*
 * Word 渲染器（fe-doc-preview；共用基座 §6）。
 * - 建树（tree_indexed）：结构化文档流渲染（正文 17px --leading-body），
 *   按 section_path（+paragraph，1 基）锚点定位并高亮命中段落；paragraph 缺省高亮整个章节块。
 * - basic（未建树）：纯文本流，仅打开（无自然定位单位，不硬造锚点）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { copy } from '../../copy';
import { ErrorState } from '../../ui/states';
import type { PreviewApi } from '../api';
import type { PreviewHit } from '../types';
import type { WordContentResponse } from '../types';
import { useHitScroll } from '../use-hit-scroll';

export interface WordRendererProps {
  readonly api: PreviewApi;
  readonly documentId: string;
  readonly documentVersionId: string | null;
  readonly treeIndexed: boolean;
  readonly hits: readonly PreviewHit[];
  readonly currentHit: number | null;
}

function WordSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-3">
      {[70, 100, 95, 55].map((width) => (
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

function pathEquals(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((segment, index) => segment === b[index]);
}

/** 命中点 → 渲染目标：section 下标 + paragraph（1 基；undefined 表示整章）。 */
interface WordHitTarget {
  readonly hitIndex: number;
  readonly sectionIndex: number;
  readonly paragraph: number | null;
  readonly current: boolean;
}

export function WordRenderer({ api, documentId, documentVersionId, treeIndexed, hits, currentHit }: WordRendererProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [tree, setTree] = useState<WordContentResponse | null>(null);
  const [plainText, setPlainText] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setTree(null);
    setPlainText(null);
    setLoadError(false);
    const request = treeIndexed
      ? api.getWordContent(documentId, { documentVersionId }).then((content) => content.sections)
      : api.getTextContent(documentId, { documentVersionId });
    void Promise.resolve(request).then(
      (content) => {
        if (cancelled) {
          return;
        }
        if (Array.isArray(content)) {
          setTree({ sections: content as WordContentResponse['sections'] });
        } else {
          setPlainText(content as string);
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
  }, [api, documentId, documentVersionId, treeIndexed, retryNonce]);

  const targets = useMemo((): WordHitTarget[] => {
    if (tree === null) {
      return [];
    }
    const result: WordHitTarget[] = [];
    hits.forEach((hit, hitIndex) => {
      if (!('section_path' in hit.locator)) {
        return;
      }
      const locator = hit.locator;
      const sectionIndex = tree.sections.findIndex((section) => pathEquals(section.path, locator.section_path));
      if (sectionIndex === -1) {
        return;
      }
      const section = tree.sections[sectionIndex];
      const paragraph = locator.paragraph ?? null;
      if (paragraph !== null && (section === undefined || paragraph < 1 || paragraph > section.paragraphs.length)) {
        return;
      }
      result.push({ hitIndex, sectionIndex, paragraph, current: hitIndex === currentHit });
    });
    return result;
  }, [tree, hits, currentHit]);

  useHitScroll({ rootRef, hits, currentHit, ready: tree !== null || plainText !== null });

  const retry = useCallback(() => setRetryNonce((nonce) => nonce + 1), []);

  if (loadError) {
    return <ErrorState text={copy.preview.error} retryLabel={copy.preview.retry} onRetry={retry} />;
  }
  if (treeIndexed && tree === null) {
    return <WordSkeleton />;
  }
  if (!treeIndexed && plainText === null) {
    return <WordSkeleton />;
  }

  if (!treeIndexed) {
    // basic Word：纯文本流，仅打开
    const paragraphs = (plainText as string).split(/\n{2,}/).filter((paragraph) => paragraph.trim() !== '');
    return (
      <div ref={rootRef} className="preview-word flex flex-col gap-4">
        {paragraphs.map((paragraph, index) => (
          <p key={index} className="whitespace-pre-wrap text-body leading-[var(--leading-body)] text-ink-black">
            {paragraph}
          </p>
        ))}
      </div>
    );
  }

  const content = tree as WordContentResponse;
  return (
    <div ref={rootRef} className="preview-word flex flex-col gap-6">
      {content.sections.map((section, sectionIndex) => {
        const sectionTargets = targets.filter((target) => target.sectionIndex === sectionIndex);
        const wholeSection = sectionTargets.find((target) => target.paragraph === null);
        return (
          <section
            key={sectionIndex}
            data-hit-anchor={wholeSection !== undefined ? wholeSection.hitIndex : undefined}
            className={
              wholeSection !== undefined
                ? wholeSection.current
                  ? 'preview-hit-block preview-hit-block--current rounded-[var(--radius-images)] px-3 py-2'
                  : 'preview-hit-block rounded-[var(--radius-images)] px-3 py-2'
                : undefined
            }
          >
            <h2 className="mb-2 text-body font-w480 leading-[var(--leading-body)] text-ink-black">
              {section.path.join(' / ')}
            </h2>
            <div className="flex flex-col gap-3">
              {section.paragraphs.map((paragraph, paragraphIndex) => {
                const target = sectionTargets.find((candidate) => candidate.paragraph === paragraphIndex + 1);
                return (
                  <p
                    key={paragraphIndex}
                    data-hit-anchor={target !== undefined ? target.hitIndex : undefined}
                    className={
                      'whitespace-pre-wrap text-body leading-[var(--leading-body)] text-ink-black' +
                      (target !== undefined
                        ? target.current
                          ? ' preview-hit-block preview-hit-block--current rounded-[var(--radius-images)] px-3 py-2'
                          : ' preview-hit-block rounded-[var(--radius-images)] px-3 py-2'
                        : '')
                    }
                  >
                    {paragraph}
                  </p>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}
