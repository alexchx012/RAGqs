import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { createAuthedStore, renderWithAuth } from '../test/auth-fixtures';
import type { PreviewApi } from './api';
import { PreviewPage } from './PreviewPage';
import type { DocumentPreviewResponse, SheetContentResponse } from './types';

/*
 * 原文预览页（fe-doc-preview）页面级状态：
 * 加载骨架 / 错误重试 / 不可用不泄露元数据 / 无 message_id 只读空态 / 命中切换 / 窄屏面板 / ?sheet= 深链。
 */

const MD_PREVIEW: DocumentPreviewResponse = {
  document_id: 'doc_md',
  name: '年假政策.md',
  media_kind: 'md',
  has_text_layer: false,
  tree_indexed: false,
  page_count: null,
  sheets: null,
  content_url: '/documents/doc_md/content',
  hits: [
    { index: 1, summary: '年假天数规则', snippet: '满 1 年不满 10 年为 5 天', locator: {} },
    { index: 2, summary: '申请流程', snippet: '申请流程', locator: {} },
  ],
};

const MD_TEXT = '# 年假政策\n\n员工年假天数按工龄分段：满 1 年不满 10 年为 5 天。\n\n## 申请流程\n\n提前 3 个工作日提交申请。\n';

const EXCEL_PREVIEW: DocumentPreviewResponse = {
  document_id: 'doc_xlsx',
  name: '报销明细.xlsx',
  media_kind: 'excel',
  has_text_layer: false,
  tree_indexed: false,
  page_count: null,
  sheets: [
    { name: 'Q1 报销', row_count: 5 },
    { name: 'Q2 报销', row_count: 3 },
  ],
  content_url: '/documents/doc_xlsx/content',
  hits: [
    { index: 1, summary: 'Q1 交通费记录', locator: { sheet: 'Q1 报销', a1_range: 'A2:C2' } },
    { index: 2, summary: 'Q2 住宿费记录', locator: { sheet: 'Q2 报销', a1_range: 'A2' } },
  ],
};

const Q1_SHEET: SheetContentResponse = {
  sheet: 'Q1 报销',
  row_count: 5,
  rows: [
    ['项目', '金额', '状态'],
    ['交通费', 320, '已批'],
    ['餐饮费', 158, '待批'],
    ['办公用品', 89, '已批'],
    ['快递费', 24, '已批'],
  ],
};

function fakeApi(overrides: Partial<PreviewApi> = {}): PreviewApi {
  return {
    getPreview: vi.fn(async () => MD_PREVIEW),
    getTextContent: vi.fn(async () => MD_TEXT),
    getWordContent: vi.fn(async () => ({ sections: [] })),
    getSheetContent: vi.fn(async () => Q1_SHEET),
    getImageContent: vi.fn(async () => new Blob()),
    buildContentUrl: vi.fn(() => 'http://localhost/v1/documents/doc_x/content'),
    ...overrides,
  };
}

function apiError(status: number, code = 'internal_error'): ApiError {
  return new ApiError({ status, code, message: code, details: {}, requestId: null });
}

async function renderPage(api: PreviewApi, entry: string) {
  const store = await createAuthedStore();
  return renderWithAuth(
    <Routes>
      <Route path="/preview/:document_id" element={<PreviewPage api={api} />} />
    </Routes>,
    store,
    [entry],
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('PreviewPage 加载与就绪', () => {
  it('加载态：预览区骨架块 + 导航骨架条目 3 条', async () => {
    const api = fakeApi({ getPreview: vi.fn(() => new Promise<DocumentPreviewResponse>(() => {})) });
    const { container } = await renderPage(api, '/preview/doc_md?message_id=m_1');
    expect(screen.getByLabelText(copy.preview.loadingAria)).toBeInTheDocument();
    expect(container.querySelectorAll('aside .ui-skeleton')).toHaveLength(3);
  });

  it('就绪：页头文档名 + 载体标签 + 命中导航 + 第一处命中自动当前高亮', async () => {
    const api = fakeApi();
    const { container } = await renderPage(api, '/preview/doc_md?message_id=m_1');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('年假政策.md'));
    expect(screen.getByText(copy.preview.mediaKind.md)).toBeInTheDocument();
    const aside = screen.getByLabelText(copy.preview.navAria);
    expect(aside.querySelectorAll('button')).toHaveLength(2);
    await waitFor(() => expect(container.querySelector('[data-hit-anchor="0"]')).toHaveClass('preview-hit--current'));
    // 关闭按钮（新窗口页）
    expect(screen.getByRole('button', { name: copy.preview.closeAria })).toBeInTheDocument();
  });

  it('点击命中条目：切换当前高亮', async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    const { container } = await renderPage(api, '/preview/doc_md?message_id=m_1');
    await waitFor(() => expect(container.querySelector('[data-hit-anchor="0"]')).toHaveClass('preview-hit--current'));
    const aside = screen.getByLabelText(copy.preview.navAria);
    await user.click(aside.querySelectorAll('button')[1] as HTMLElement);
    await waitFor(() => {
      expect(container.querySelector('[data-hit-anchor="1"]')).toHaveClass('preview-hit--current');
      expect(container.querySelector('[data-hit-anchor="0"]')).not.toHaveClass('preview-hit--current');
    });
    expect(aside.querySelectorAll('button')[1]).toHaveAttribute('aria-current', 'true');
  });

  it('无 message_id：管理侧只读形态（导航空态、无高亮）', async () => {
    const api = fakeApi({ getPreview: vi.fn(async () => ({ ...MD_PREVIEW, hits: [] })) });
    const { container } = await renderPage(api, '/preview/doc_md');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('年假政策.md'));
    expect(screen.getByText(copy.preview.navEmpty)).toBeInTheDocument();
    await waitFor(() => expect(container.textContent).toContain('年假政策'));
    expect(container.querySelector('mark')).toBeNull();
  });
});

describe('PreviewPage 错误与不可用', () => {
  it('错误态：预览区居中错误说明 + 重试文字链；导航区同步空态；重试成功恢复', async () => {
    const user = userEvent.setup();
    const getPreview = vi.fn().mockRejectedValueOnce(apiError(500)).mockResolvedValue(MD_PREVIEW);
    const api = fakeApi({ getPreview });
    const { container } = await renderPage(api, '/preview/doc_md?message_id=m_1');
    await waitFor(() => expect(screen.getByText(copy.preview.error)).toBeInTheDocument());
    expect(screen.getByText(copy.preview.navEmpty)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.preview.retry }));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('年假政策.md'));
    expect(getPreview).toHaveBeenCalledTimes(2);
    expect(container.querySelector('mark')).not.toBeNull();
  });

  it('不可用态（410）：只显示「内容已不可用」，不泄露文档名 / 导航 / 原文', async () => {
    const api = fakeApi({ getPreview: vi.fn(async () => Promise.reject(apiError(410, 'document_unavailable'))) });
    await renderPage(api, '/preview/doc_gone?message_id=m_1');
    await waitFor(() => expect(screen.getByText(copy.preview.unavailable)).toBeInTheDocument());
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(copy.preview.navAria)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('已删除文档');
    expect(screen.getByRole('button', { name: copy.preview.closeAria })).toBeInTheDocument();
  });

  it('不可用态（403 无权限）同形态', async () => {
    const api = fakeApi({ getPreview: vi.fn(async () => Promise.reject(apiError(403, 'forbidden'))) });
    await renderPage(api, '/preview/doc_gone');
    await waitFor(() => expect(screen.getByText(copy.preview.unavailable)).toBeInTheDocument());
    expect(screen.queryByLabelText(copy.preview.navAria)).not.toBeInTheDocument();
  });
});

describe('PreviewPage Sheet 深链', () => {
  it('无 ?sheet=：激活首个命中所在 Sheet', async () => {
    const getSheetContent = vi.fn(async () => Q1_SHEET);
    const api = fakeApi({ getPreview: vi.fn(async () => EXCEL_PREVIEW), getSheetContent });
    const { container } = await renderPage(api, '/preview/doc_xlsx?message_id=m_1');
    await waitFor(() => expect(container.textContent).toContain('交通费'));
    expect(getSheetContent).toHaveBeenCalledWith('doc_xlsx', 'Q1 报销', expect.anything());
  });

  it('?sheet= 合法值优先；非法值回退首个命中 Sheet', async () => {
    const getSheetContent = vi.fn(async () => Q1_SHEET);
    const api = fakeApi({ getPreview: vi.fn(async () => EXCEL_PREVIEW), getSheetContent });
    await renderPage(api, `/preview/doc_xlsx?message_id=m_1&sheet=${encodeURIComponent('Q2 报销')}`);
    await waitFor(() => expect(getSheetContent).toHaveBeenCalledWith('doc_xlsx', 'Q2 报销', expect.anything()));

    const fallback = vi.fn(async () => Q1_SHEET);
    const api2 = fakeApi({ getPreview: vi.fn(async () => EXCEL_PREVIEW), getSheetContent: fallback });
    await renderPage(api2, '/preview/doc_xlsx?message_id=m_1&sheet=nope');
    await waitFor(() => expect(fallback).toHaveBeenCalledWith('doc_xlsx', 'Q1 报销', expect.anything()));
  });
});

describe('PreviewPage 窄屏（<768px）', () => {
  function stubNarrow(matches: boolean) {
    vi.stubGlobal(
      'matchMedia',
      (query: string): MediaQueryList =>
        ({
          matches: query === '(max-width: 767px)' ? matches : false,
          media: query,
          onchange: null,
          addEventListener: () => {},
          removeEventListener: () => {},
          addListener: () => {},
          removeListener: () => {},
          dispatchEvent: () => false,
        }) as MediaQueryList,
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('导航收起为「命中点 N」按钮；点击滑出面板；Esc 关闭；面板内点击条目切换并关闭', async () => {
    stubNarrow(true);
    const user = userEvent.setup();
    const api = fakeApi();
    const { container } = await renderPage(api, '/preview/doc_md?message_id=m_1');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('年假政策.md'));
    // 桌面右栏不渲染
    expect(screen.queryByLabelText(copy.preview.navAria)).not.toBeInTheDocument();
    const toggle = screen.getByRole('button', { name: copy.preview.navTitle(2) });
    expect(toggle).toBeInTheDocument();

    await user.click(toggle);
    const panel = await screen.findByRole('dialog');
    expect(panel.querySelectorAll('button')).toHaveLength(2);

    // 面板内点击第二条：切换当前并关闭面板
    await user.click(panel.querySelectorAll('button')[1] as HTMLElement);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(container.querySelector('[data-hit-anchor="1"]')).toHaveClass('preview-hit--current');

    // 再次打开，Esc 关闭
    await user.click(toggle);
    await screen.findByRole('dialog');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});
