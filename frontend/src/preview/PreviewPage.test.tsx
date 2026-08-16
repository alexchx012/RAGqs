import { fireEvent, screen, waitFor } from '@testing-library/react';
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
  document_version_id: 'v_md_selected',
  name: '年假政策.md',
  media_kind: 'md',
  size_bytes: 1024,
  content_available: true,
  has_text_layer: false,
  tree_indexed: false,
  page_count: null,
  sheets: null,
  content_url: '/v1/documents/doc_md/content?document_version_id=v_md_selected',
  hits: [
    { index: 1, summary: '年假天数规则', snippet: '满 1 年不满 10 年为 5 天', locator: {} },
    { index: 2, summary: '申请流程', snippet: '申请流程', locator: {} },
  ],
};

const MD_TEXT = '# 年假政策\n\n员工年假天数按工龄分段：满 1 年不满 10 年为 5 天。\n\n## 申请流程\n\n提前 3 个工作日提交申请。\n';

const EXCEL_PREVIEW: DocumentPreviewResponse = {
  document_id: 'doc_xlsx',
  document_version_id: 'v_xlsx_selected',
  name: '报销明细.xlsx',
  media_kind: 'excel',
  size_bytes: 2048,
  content_available: true,
  has_text_layer: false,
  tree_indexed: false,
  page_count: null,
  sheets: [
    { name: 'Q1 报销', row_count: 5 },
    { name: 'Q2 报销', row_count: 3 },
  ],
  content_url: '/v1/documents/doc_xlsx/content?document_version_id=v_xlsx_selected',
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

  it('内容 renderer 使用 preview 返回的选定版本，而不是地址栏中的过期版本', async () => {
    const getTextContent = vi.fn(async () => MD_TEXT);
    const api = fakeApi({ getTextContent });

    await renderPage(api, '/preview/doc_md?document_version_id=v_stale');

    await waitFor(() =>
      expect(getTextContent).toHaveBeenCalledWith('doc_md', { documentVersionId: 'v_md_selected' }),
    );
  });

  it('默认处理元数据仍以保守策略打开内容', async () => {
    const previewWithDefaultRendererMetadata: DocumentPreviewResponse = {
      document_id: 'doc_word_basic',
      document_version_id: 'vwb_1',
      name: '会议纪要.docx',
      media_kind: 'word' as const,
      size_bytes: 512,
      content_available: true,
      has_text_layer: false,
      tree_indexed: false,
      page_count: null,
      sheets: null,
      content_url: '/v1/documents/doc_word_basic/content?document_version_id=vwb_1',
      hits: [],
    };
    const getTextContent = vi.fn(async () => '会议纪要');
    const api = fakeApi({
      getPreview: vi.fn(async () => previewWithDefaultRendererMetadata),
      getTextContent,
    });

    await renderPage(api, '/preview/doc_word_basic');

    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('会议纪要.docx'));
    await waitFor(() =>
      expect(getTextContent).toHaveBeenCalledWith('doc_word_basic', { documentVersionId: 'vwb_1' }),
    );
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


describe('PreviewPage 窄屏下滑关闭（fe-preview-swipe-close）', () => {
  function stubMedia(narrow: boolean, reducedMotion = false) {
    vi.stubGlobal(
      'matchMedia',
      (query: string): MediaQueryList =>
        ({
          matches:
            query === '(max-width: 767px)' ? narrow : query === '(prefers-reduced-motion: reduce)' ? reducedMotion : false,
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

  function pointer(panel: Element, type: 'pointerdown' | 'pointermove' | 'pointerup', clientY: number) {
    // fireEvent 包 act：jsdom PointerEvent 构造器可用，clientY 正常下发
    const init = { clientY };
    if (type === 'pointerdown') {
      fireEvent.pointerDown(panel, init);
    } else if (type === 'pointermove') {
      fireEvent.pointerMove(panel, init);
    } else {
      fireEvent.pointerUp(panel, init);
    }
  }

  async function openPanel(api: PreviewApi): Promise<HTMLElement> {
    const user = userEvent.setup();
    await renderPage(api, '/preview/doc_md?message_id=m_1');
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('年假政策.md'));
    await user.click(screen.getByRole('button', { name: copy.preview.navTitle(2) }));
    return screen.findByRole('dialog');
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('下滑超过阈值松手：拖动跟手，松手后滑出关闭', async () => {
    stubMedia(true);
    const panel = await openPanel(fakeApi());
    pointer(panel, 'pointerdown', 100);
    pointer(panel, 'pointermove', 140);
    expect(panel.style.transform).toBe('translateY(40px)');
    pointer(panel, 'pointermove', 200);
    pointer(panel, 'pointerup', 200); // dy=100 ≥ 阈值（jsdom 高度 0 → 回退 80px）
    expect(panel.style.transform).toBe('translateY(100%)');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('未达阈值且非快滑：回弹展开位置，面板保持打开', async () => {
    stubMedia(true);
    const panel = await openPanel(fakeApi());
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1000);
    pointer(panel, 'pointerdown', 100);
    pointer(panel, 'pointermove', 130);
    expect(panel.style.transform).toBe('translateY(30px)');
    nowSpy.mockReturnValue(1400); // dt=400ms → v=0.075 < 0.3px/ms
    pointer(panel, 'pointerup', 130); // dy=30 < 阈值 80
    expect(panel.style.transform).toBe('translateY(0px)');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('快速下滑（flick）未达阈值距离也关闭', async () => {
    stubMedia(true);
    const panel = await openPanel(fakeApi());
    pointer(panel, 'pointerdown', 100);
    pointer(panel, 'pointermove', 120);
    pointer(panel, 'pointerup', 120); // dt≈0 → 平均速度 ≥ 0.3px/ms
    expect(panel.style.transform).toBe('translateY(100%)');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('列表滚动容器不在顶部时向下拖：不进入关闭手势（保持列表滚动）', async () => {
    stubMedia(true);
    const panel = await openPanel(fakeApi());
    const scroller = panel.querySelector('[data-swipe-scroll]') as HTMLElement;
    Object.defineProperty(scroller, 'scrollTop', { value: 120, configurable: true });
    pointer(panel, 'pointerdown', 100);
    pointer(panel, 'pointermove', 200);
    expect(panel.style.transform).toBe('');
    pointer(panel, 'pointerup', 200);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('prefers-reduced-motion：关闭直出，无滑出动画', async () => {
    stubMedia(true, true);
    const panel = await openPanel(fakeApi());
    pointer(panel, 'pointerdown', 100);
    pointer(panel, 'pointermove', 220);
    pointer(panel, 'pointerup', 220);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('下滑关闭后再次打开：面板复位可见，手势仍可关闭', async () => {
    stubMedia(true);
    const user = userEvent.setup();
    const panel = await openPanel(fakeApi());
    pointer(panel, 'pointerdown', 100);
    pointer(panel, 'pointermove', 200);
    pointer(panel, 'pointerup', 200);
    expect(panel.style.transform).toBe('translateY(100%)');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // 再次打开：无残留 transform/animation，面板以入场动画正常出现
    await user.click(screen.getByRole('button', { name: copy.preview.navTitle(2) }));
    const panel2 = await screen.findByRole('dialog');
    expect(panel2.style.transform).toBe('');
    expect(panel2.style.animation).toBe('');

    // 再次下滑仍可关闭
    pointer(panel2, 'pointerdown', 100);
    pointer(panel2, 'pointermove', 200);
    pointer(panel2, 'pointerup', 200);
    expect(panel2.style.transform).toBe('translateY(100%)');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});
