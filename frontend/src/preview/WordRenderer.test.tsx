import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { PreviewApi } from './api';
import { WordRenderer } from './renderers/WordRenderer';
import type { PreviewHit, WordContentResponse } from './types';

/*
 * Word 渲染器（fe-doc-preview）：建树 Word 按 section_path（+paragraph）锚点定位并高亮命中段落；
 * basic（未建树）仅打开（无任何锚点）。
 */

const TREE: WordContentResponse = {
  sections: [
    { path: ['第 1 章', '总则'], paragraphs: ['本制度适用于全体员工。', '制度自发布之日起生效。'] },
    { path: ['第 2 章', '考勤管理'], paragraphs: ['标准工时为每日 8 小时。', '迟到 30 分钟以上记为缺勤。', '加班需提前审批。'] },
  ],
};

function fakeApi(overrides: Partial<PreviewApi> = {}): PreviewApi {
  return {
    getPreview: vi.fn(),
    getTextContent: vi.fn(async () => '会议纪要\n\n本次会议确认了上线节奏。\n\n后续行动项由各部门跟进。\n'),
    getWordContent: vi.fn(async () => TREE),
    getSheetContent: vi.fn(),
    getImageContent: vi.fn(),
    buildContentUrl: vi.fn(() => 'http://localhost/v1/documents/doc_word/content'),
    ...overrides,
  };
}

describe('WordRenderer 建树（tree_indexed）', () => {
  it('section_path + paragraph 锚点定位并高亮命中段落', async () => {
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '考勤缺勤判定', locator: { section_path: ['第 2 章', '考勤管理'], paragraph: 2 } },
    ];
    const { container } = render(
      <WordRenderer api={fakeApi()} documentId="doc_word" documentVersionId={null} treeIndexed hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.textContent).toContain('迟到 30 分钟以上记为缺勤'));
    const anchor = container.querySelector('[data-hit-anchor="0"]') as HTMLElement;
    expect(anchor).not.toBeNull();
    expect(anchor.textContent).toBe('迟到 30 分钟以上记为缺勤。');
    expect(anchor).toHaveClass('preview-hit-block--current');
    // 章节标题渲染；其他段落不带锚点
    expect(container.textContent).toContain('第 2 章 / 考勤管理');
    expect(container.querySelectorAll('[data-hit-anchor]')).toHaveLength(1);
  });

  it('paragraph 缺省：高亮整个章节块', async () => {
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '总则', locator: { section_path: ['第 1 章', '总则'] } },
    ];
    const { container } = render(
      <WordRenderer api={fakeApi()} documentId="doc_word" documentVersionId={null} treeIndexed hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.textContent).toContain('本制度适用于全体员工'));
    const anchor = container.querySelector('[data-hit-anchor="0"]') as HTMLElement;
    expect(anchor.tagName).toBe('SECTION');
    expect(anchor).toHaveClass('preview-hit-block--current');
    expect(anchor.textContent).toContain('制度自发布之日起生效。');
  });

  it('section_path 匹配不到：降级不高亮', async () => {
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '未知章节', locator: { section_path: ['第 9 章', '不存在'] } },
    ];
    const { container } = render(
      <WordRenderer api={fakeApi()} documentId="doc_word" documentVersionId={null} treeIndexed hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.textContent).toContain('本制度适用于全体员工'));
    expect(container.querySelector('[data-hit-anchor]')).toBeNull();
  });

  it('切换当前命中：current 类移动', async () => {
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '工时', locator: { section_path: ['第 2 章', '考勤管理'], paragraph: 1 } },
      { index: 2, summary: '缺勤', locator: { section_path: ['第 2 章', '考勤管理'], paragraph: 2 } },
    ];
    const { container, rerender } = render(
      <WordRenderer api={fakeApi()} documentId="doc_word" documentVersionId={null} treeIndexed hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.querySelector('[data-hit-anchor="0"]')).toHaveClass('preview-hit-block--current'));
    rerender(
      <WordRenderer api={fakeApi()} documentId="doc_word" documentVersionId={null} treeIndexed hits={hits} currentHit={1} />,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-hit-anchor="0"]')).not.toHaveClass('preview-hit-block--current');
      expect(container.querySelector('[data-hit-anchor="1"]')).toHaveClass('preview-hit-block--current');
    });
  });
});

describe('WordRenderer basic（未建树）', () => {
  it('纯文本流仅打开：无锚点无高亮', async () => {
    const hits: readonly PreviewHit[] = [{ index: 1, summary: '会议结论', locator: {} }];
    const { container } = render(
      <WordRenderer api={fakeApi()} documentId="doc_word_basic" documentVersionId={null} treeIndexed={false} hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.textContent).toContain('本次会议确认了上线节奏。'));
    expect(container.querySelector('[data-hit-anchor]')).toBeNull();
    expect(container.querySelector('.preview-hit-block')).toBeNull();
    expect(container.textContent).toContain('后续行动项由各部门跟进。');
  });
});
