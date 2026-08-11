import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import type { PreviewApi } from './api';
import { TextRenderer } from './renderers/TextRenderer';
import type { PreviewHit } from './types';

/*
 * 文本流渲染器（fe-doc-preview）：md/txt/code/data 的 snippet 匹配高亮、
 * 当前命中切换、匹配不到降级、错误重试。
 */

function fakeApi(text: string | Error): PreviewApi {
  return {
    getPreview: vi.fn(),
    getTextContent: vi.fn(async () => {
      if (text instanceof Error) {
        throw text;
      }
      return text;
    }),
    getWordContent: vi.fn(),
    getSheetContent: vi.fn(),
    getImageContent: vi.fn(),
    buildContentUrl: vi.fn(() => 'http://localhost/v1/documents/doc_x/content'),
  };
}

const MD_TEXT = '# 年假政策\n\n员工年假天数按工龄分段：满 1 年不满 10 年为 5 天。\n\n## 申请流程\n\n提前 3 个工作日提交申请。\n';
const MD_HITS: readonly PreviewHit[] = [
  { index: 1, summary: '年假天数规则', snippet: '满 1 年不满 10 年为 5 天', locator: {} },
];

describe('TextRenderer plain（md / txt）', () => {
  it('snippet 匹配：命中段包裹 mark，当前命中带 current 类', async () => {
    const { container } = render(
      <TextRenderer api={fakeApi(MD_TEXT)} documentId="doc_md" documentVersionId={null} textKind="plain" hits={MD_HITS} currentHit={0} />,
    );
    await waitFor(() => expect(container.querySelector('mark')).not.toBeNull());
    const mark = container.querySelector('mark') as HTMLElement;
    expect(mark).toHaveClass('preview-hit', 'preview-hit--current');
    expect(mark.getAttribute('data-hit-anchor')).toBe('0');
    expect(mark.textContent).toBe('满 1 年不满 10 年为 5 天');
    // 全文保留
    expect(container.textContent).toContain('年假政策');
    expect(container.textContent).toContain('提前 3 个工作日提交申请');
  });

  it('切换当前命中：current 类在命中点间移动', async () => {
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '规则', snippet: '年假政策', locator: {} },
      { index: 2, summary: '流程', snippet: '申请流程', locator: {} },
    ];
    const { container, rerender } = render(
      <TextRenderer api={fakeApi(MD_TEXT)} documentId="doc_md" documentVersionId={null} textKind="plain" hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.querySelectorAll('mark').length).toBe(2));
    const marks = container.querySelectorAll('mark');
    expect(marks[0]).toHaveClass('preview-hit--current');
    expect(marks[1]).not.toHaveClass('preview-hit--current');
    rerender(
      <TextRenderer api={fakeApi(MD_TEXT)} documentId="doc_md" documentVersionId={null} textKind="plain" hits={hits} currentHit={1} />,
    );
    await waitFor(() => {
      const next = container.querySelectorAll('mark');
      expect(next[0]).not.toHaveClass('preview-hit--current');
      expect(next[1]).toHaveClass('preview-hit--current');
    });
  });

  it('匹配不到：仅打开文档（无 mark），空 hits 同样不高亮', async () => {
    const missing: readonly PreviewHit[] = [{ index: 1, summary: '缺失', snippet: '不存在的片段', locator: {} }];
    const { container, rerender } = render(
      <TextRenderer api={fakeApi(MD_TEXT)} documentId="doc_md" documentVersionId={null} textKind="plain" hits={missing} currentHit={0} />,
    );
    await waitFor(() => expect(container.textContent).toContain('年假政策'));
    expect(container.querySelector('mark')).toBeNull();
    rerender(
      <TextRenderer api={fakeApi(MD_TEXT)} documentId="doc_md" documentVersionId={null} textKind="plain" hits={[]} currentHit={null} />,
    );
    await waitFor(() => expect(container.textContent).toContain('申请流程'));
    expect(container.querySelector('mark')).toBeNull();
  });
});

describe('TextRenderer code / data', () => {
  it('code：hljs 语法高亮结构上叠加命中 mark（不打断 hljs span）', async () => {
    const codeText = 'def main():\n    print("scan")\n\nif __name__ == "__main__":\n    main()\n';
    const hits: readonly PreviewHit[] = [{ index: 1, summary: '入口', snippet: 'def main', locator: {} }];
    const { container } = render(
      <TextRenderer api={fakeApi(codeText)} documentId="doc_code" documentVersionId={null} textKind="code" hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.querySelector('mark')).not.toBeNull());
    // hljs 会把 'def main' 切成 keyword span + 普通文本：同一命中允许拆成多个相邻 mark
    const hitMarks = [...container.querySelectorAll('mark[data-hit-anchor="0"]')] as HTMLElement[];
    expect(hitMarks.map((mark) => mark.textContent).join('')).toBe('def main');
    expect(hitMarks[0]).toHaveClass('preview-hit--current');
    // hljs 已产出结构化 span
    expect(container.querySelector('pre code .hljs-keyword, pre code [class*="hljs"]')).not.toBeNull();
    expect(container.querySelector('pre')?.textContent).toContain('print("scan")');
  });

  it('data：JSON 美化后文本流渲染并高亮', async () => {
    const dataText = '{"annual_leave": 5, "sick_leave": 10}\n';
    const hits: readonly PreviewHit[] = [{ index: 1, summary: '年假指标', snippet: '"annual_leave"', locator: {} }];
    const { container } = render(
      <TextRenderer api={fakeApi(dataText)} documentId="doc_data" documentVersionId={null} textKind="data" hits={hits} currentHit={0} />,
    );
    await waitFor(() => expect(container.querySelector('mark')).not.toBeNull());
    expect(container.textContent).toContain('"sick_leave"');
    expect(container.querySelector('mark')?.textContent).toBe('"annual_leave"');
  });
});

describe('TextRenderer 错误态', () => {
  it('加载失败：错误说明 + 重试；重试成功恢复', async () => {
    const user = userEvent.setup();
    const api = fakeApi(new Error('boom'));
    const { container, rerender } = render(
      <TextRenderer api={api} documentId="doc_md" documentVersionId={null} textKind="plain" hits={[]} currentHit={null} />,
    );
    await waitFor(() => expect(screen.getByText(copy.preview.error)).toBeInTheDocument());
    (api.getTextContent as ReturnType<typeof vi.fn>).mockResolvedValue(MD_TEXT);
    await user.click(screen.getByRole('button', { name: copy.preview.retry }));
    await waitFor(() => expect(container.textContent).toContain('年假政策'));
    rerender(<></>);
  });
});
