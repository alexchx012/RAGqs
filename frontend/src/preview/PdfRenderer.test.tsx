import { render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { scrollToCenter } from './scroll';
import { PdfRenderer } from './renderers/PdfRenderer';
import type { PreviewHit } from './types';

/*
 * PDF 渲染器接线测（fe-doc-preview）：jsdom 无 canvas，以 vi.mock 替身 react-pdf 验证
 * - 逐页渲染（页卡容器 data-page-number）；
 * - 文本层 snippet 匹配 → customTextRenderer 产出 <mark>（span 消歧同页多处）；
 * - 打开 / 切换命中的滚动定位（scrollToCenter 目标：片段锚点优先，扫描件回退页容器）；
 * - 扫描件（无文本层）：不做片段高亮、无替代锚点 UI。
 */

const fixtures = vi.hoisted(() => ({
  numPages: 2,
  pageItems: new Map<number, string[]>([
    [1, ['Employee Handbook', 'Annual leave: 5 days per year for service under 10 years.']],
    [2, ['Sick leave requires a medical certificate.']],
  ]),
}));

vi.mock('./scroll', () => ({ scrollToCenter: vi.fn() }));

vi.mock('react-pdf', async () => {
  const React = await import('react');
  interface DocumentProps {
    readonly children?: React.ReactNode;
    readonly onLoadSuccess?: (document: { numPages: number }) => void;
  }
  function Document({ children, onLoadSuccess }: DocumentProps) {
    React.useEffect(() => {
      onLoadSuccess?.({ numPages: fixtures.numPages });
    }, []);
    return <div data-testid="pdf-document">{children}</div>;
  }
  interface PageProps {
    readonly pageNumber: number;
    readonly onGetTextSuccess?: (textContent: { items: readonly { str: string }[] }) => void;
    readonly customTextRenderer?: (layer: { pageNumber: number; itemIndex: number; str: string }) => string;
  }
  function Page({ pageNumber, onGetTextSuccess, customTextRenderer }: PageProps) {
    const items = (fixtures.pageItems.get(pageNumber) ?? []).map((str) => ({ str }));
    React.useEffect(() => {
      onGetTextSuccess?.({ items });
    }, [pageNumber]);
    return (
      <div data-testid={`pdf-page-${pageNumber}`}>
        {items.map((item, index) => (
          <span
            key={index}
            // 与真实 react-pdf 一致：customTextRenderer 返回 HTML 字符串
            dangerouslySetInnerHTML={{
              __html: customTextRenderer?.({ pageNumber, itemIndex: index, str: item.str }) ?? item.str,
            }}
          />
        ))}
      </div>
    );
  }
  return { pdfjs: { GlobalWorkerOptions: { workerSrc: '' } }, Document, Page };
});

const HITS: readonly PreviewHit[] = [
  { index: 1, summary: '年假', snippet: '5 days per year', locator: { page: 1, span: { start: 30, end: 45 } } },
  { index: 2, summary: '病假', snippet: 'medical certificate', locator: { page: 2 } },
];

function renderPdf(props: Partial<Parameters<typeof PdfRenderer>[0]> = {}) {
  return render(
    <PdfRenderer
      fileUrl="http://localhost/v1/documents/doc_1/content"
      token="tok"
      hasTextLayer
      hits={HITS}
      currentHit={0}
      {...props}
    />,
  );
}

beforeEach(() => {
  fixtures.numPages = 2;
  vi.mocked(scrollToCenter).mockClear();
});

describe('PdfRenderer 有文本层', () => {
  it('逐页渲染 + snippet 文本匹配高亮（当前 100%、其余浅标）', async () => {
    const { container } = renderPdf();
    await waitFor(() => expect(container.querySelectorAll('[data-page-number]')).toHaveLength(2));
    await waitFor(() => expect(container.querySelectorAll('mark.preview-hit').length).toBe(2));
    const first = container.querySelector('[data-hit-anchor="0"]') as HTMLElement;
    expect(first).toHaveClass('preview-hit--current');
    expect(first.textContent).toBe('5 days per year');
    const second = container.querySelector('[data-hit-anchor="1"]') as HTMLElement;
    expect(second).not.toHaveClass('preview-hit--current');
    expect(second.textContent).toBe('medical certificate');
    // 非命中文本经转义原样保留
    expect(container.textContent).toContain('Employee Handbook');
  });

  it('打开跳页：滚动目标为第一处命中的片段锚点；切换命中改目标', async () => {
    const { container, rerender } = renderPdf();
    await waitFor(() => expect(container.querySelector('[data-hit-anchor="0"]')).not.toBeNull());
    await waitFor(() => expect(vi.mocked(scrollToCenter)).toHaveBeenCalled());
    const firstTarget = vi.mocked(scrollToCenter).mock.calls[0]?.[0] as HTMLElement;
    expect(firstTarget.getAttribute('data-hit-anchor')).toBe('0');

    vi.mocked(scrollToCenter).mockClear();
    rerender(
      <PdfRenderer
        fileUrl="http://localhost/v1/documents/doc_1/content"
        token="tok"
        hasTextLayer
        hits={HITS}
        currentHit={1}
      />,
    );
    await waitFor(() => expect(container.querySelector('[data-hit-anchor="1"]')).toHaveClass('preview-hit--current'));
    await waitFor(() => expect(vi.mocked(scrollToCenter)).toHaveBeenCalled());
    const secondTarget = vi.mocked(scrollToCenter).mock.calls[0]?.[0] as HTMLElement;
    expect(secondTarget.getAttribute('data-hit-anchor')).toBe('1');
  });

  it('长文档只挂载有界页窗，切换到离屏命中时渲染并定位目标页', async () => {
    fixtures.numPages = 50;
    const longHits: readonly PreviewHit[] = [
      { index: 1, summary: '开头', locator: { page: 1 } },
      { index: 2, summary: '结尾', locator: { page: 50 } },
    ];
    const { container, rerender } = render(
      <PdfRenderer
        fileUrl="http://localhost/v1/documents/doc_long/content"
        token="tok"
        hasTextLayer={false}
        hits={longHits}
        currentHit={0}
      />,
    );

    await waitFor(() => expect(container.querySelector('[data-testid="pdf-page-1"]')).not.toBeNull());
    const initialPageCards = container.querySelectorAll('[data-page-number]');
    expect(initialPageCards.length).toBeGreaterThan(0);
    expect(initialPageCards.length).toBeLessThan(50);
    expect(container.querySelectorAll('.preview-pdf-page')).toHaveLength(initialPageCards.length);
    expect(container.querySelector('[data-testid="pdf-page-50"]')).toBeNull();

    vi.mocked(scrollToCenter).mockClear();
    rerender(
      <PdfRenderer
        fileUrl="http://localhost/v1/documents/doc_long/content"
        token="tok"
        hasTextLayer={false}
        hits={longHits}
        currentHit={1}
      />,
    );

    await waitFor(() => expect(container.querySelector('[data-testid="pdf-page-50"]')).not.toBeNull());
    expect(container.querySelectorAll('[data-page-number]').length).toBeLessThan(50);
    expect(container.querySelectorAll('.preview-pdf-page')).toHaveLength(
      container.querySelectorAll('[data-page-number]').length,
    );
    await waitFor(() => expect(vi.mocked(scrollToCenter)).toHaveBeenCalled());
    const target = vi.mocked(scrollToCenter).mock.calls.at(-1)?.[0] as HTMLElement;
    expect(target.getAttribute('data-page-number')).toBe('50');
  });

  it('span 消歧：同页多处重复时高亮 span 所指的一处', async () => {
    fixtures.numPages = 1;
    fixtures.pageItems.set(1, ['repeat alpha repeat alpha repeat']);
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '重复', snippet: 'alpha', locator: { page: 1, span: { start: 22, end: 27 } } },
    ];
    const { container } = render(
      <PdfRenderer fileUrl="http://localhost/v1/documents/doc_1/content" token={null} hasTextLayer hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.querySelectorAll('mark.preview-hit').length).toBe(1));
    const mark = container.querySelector('mark.preview-hit') as HTMLElement;
    expect(mark.textContent).toBe('alpha');
    // 第二处 'alpha'（起始 20）被选中：mark 前的文本为 'repeat alpha repeat '
    expect(mark.previousSibling?.textContent).toBe('repeat alpha repeat ');
    fixtures.pageItems.set(1, ['Employee Handbook', 'Annual leave: 5 days per year for service under 10 years.']);
  });
});

describe('PdfRenderer 扫描件（无文本层）', () => {
  it('只跳页：无片段高亮、滚动目标为命中页容器', async () => {
    const scanHits: readonly PreviewHit[] = [{ index: 1, summary: '扫描页', locator: { page: 1 } }];
    const { container } = render(
      <PdfRenderer
        fileUrl="http://localhost/v1/documents/doc_scan/content"
        token="tok"
        hasTextLayer={false}
        hits={scanHits}
        currentHit={0}
      />,
    );
    await waitFor(() => expect(container.querySelectorAll('[data-page-number]')).toHaveLength(2));
    await waitFor(() => expect(vi.mocked(scrollToCenter)).toHaveBeenCalled());
    // 无任何 mark / 替代锚点 UI
    expect(container.querySelector('mark')).toBeNull();
    const target = vi.mocked(scrollToCenter).mock.calls[0]?.[0] as HTMLElement;
    expect(target.getAttribute('data-page-number')).toBe('1');
  });
});
