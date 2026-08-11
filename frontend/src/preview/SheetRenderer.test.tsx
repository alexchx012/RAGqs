import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import type { PreviewApi } from './api';
import { SheetRenderer } from './renderers/SheetRenderer';
import type { PreviewHit, SheetContentResponse } from './types';

/*
 * 表格渲染器（fe-doc-preview）：源 Sheet 名页签（Excel）/ 虚拟 Sheet（CSV）、
 * ?sheet= 切换经 onSheetChange 上抛、a1_range 行列定位高亮（当前 100% + sienna 内框，其余 50%）。
 */

const SHEETS = [
  { name: 'Q1 报销', row_count: 5 },
  { name: 'Q2 报销', row_count: 3 },
] as const;

const Q1: SheetContentResponse = {
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

const Q2: SheetContentResponse = {
  sheet: 'Q2 报销',
  row_count: 3,
  rows: [
    ['项目', '金额', '状态'],
    ['住宿费', 1200, '待批'],
    ['机票', 2300, '待批'],
  ],
};

const HITS: readonly PreviewHit[] = [
  { index: 1, summary: 'Q1 交通费记录', locator: { sheet: 'Q1 报销', a1_range: 'A2:C2' } },
  { index: 2, summary: 'Q1 多行区域', locator: { sheet: 'Q1 报销', a1_range: 'B3:C4' } },
  { index: 3, summary: 'Q2 住宿费记录', locator: { sheet: 'Q2 报销', a1_range: 'A2' } },
];

function fakeApi(): PreviewApi {
  return {
    getPreview: vi.fn(),
    getTextContent: vi.fn(),
    getWordContent: vi.fn(),
    getSheetContent: vi.fn(async (_id: string, sheet: string) => (sheet === 'Q2 报销' ? Q2 : Q1)),
    getImageContent: vi.fn(),
    buildContentUrl: vi.fn(() => 'http://localhost/v1/documents/doc_xlsx/content'),
  };
}

function renderSheet(props: Partial<Parameters<typeof SheetRenderer>[0]> = {}) {
  const onSheetChange = props.onSheetChange ?? vi.fn();
  const utils = render(
    <SheetRenderer
      api={fakeApi()}
      documentId="doc_xlsx"
      documentVersionId={null}
      sheets={SHEETS}
      activeSheet="Q1 报销"
      onSheetChange={onSheetChange}
      hits={HITS}
      currentHit={0}
      {...props}
    />,
  );
  return { ...utils, onSheetChange };
}

describe('SheetRenderer 页签与数据', () => {
  it('源 Sheet 名页签（分段开关 radiogroup）+ 表头 mist-gray 行 + 单元格内容', async () => {
    const { container } = renderSheet();
    const tabs = screen.getByRole('radiogroup', { name: copy.preview.sheetTabsAria });
    expect(tabs).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Q1 报销' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('radio', { name: 'Q2 报销' })).toHaveAttribute('aria-checked', 'false');
    await waitFor(() => expect(container.textContent).toContain('交通费'));
    expect(container.querySelector('thead')).not.toBeNull();
    expect(container.textContent).toContain('快递费');
  });

  it('点击页签：onSheetChange 上抛目标 Sheet（?sheet= 由页面层写入）', async () => {
    const user = userEvent.setup();
    const { onSheetChange } = renderSheet();
    await user.click(screen.getByRole('radio', { name: 'Q2 报销' }));
    expect(onSheetChange).toHaveBeenCalledWith('Q2 报销');
  });

  it('切换激活 Sheet：拉取并渲染目标 Sheet 数据', async () => {
    const { container, rerender } = renderSheet();
    await waitFor(() => expect(container.textContent).toContain('交通费'));
    rerender(
      <SheetRenderer
        api={fakeApi()}
        documentId="doc_xlsx"
        documentVersionId={null}
        sheets={SHEETS}
        activeSheet="Q2 报销"
        onSheetChange={vi.fn()}
        hits={HITS}
        currentHit={2}
      />,
    );
    await waitFor(() => expect(container.textContent).toContain('住宿费'));
    expect(container.textContent).not.toContain('交通费');
  });
});

describe('SheetRenderer a1_range 定位高亮', () => {
  it('当前命中区域：blush 100% + sienna 内框类；其余命中 50% 浅标类；锚点在区域左上角', async () => {
    const { container } = renderSheet();
    await waitFor(() => expect(container.textContent).toContain('交通费'));
    // 当前命中 A2:C2（表头占第 1 行 → tbody 第 1 行 3 格全部当前）
    const currentCells = container.querySelectorAll('td.preview-hit-cell--current');
    expect(currentCells).toHaveLength(3);
    expect(currentCells[0]?.textContent).toBe('交通费');
    expect(currentCells[0]?.getAttribute('data-hit-anchor')).toBe('0');
    expect(currentCells[1]).not.toHaveAttribute('data-hit-anchor');
    // 其余命中 B3:C4（2 行 × 2 列）为浅标
    const dimCells = container.querySelectorAll('td.preview-hit-cell:not(.preview-hit-cell--current)');
    expect(dimCells).toHaveLength(4);
    // 非激活 Sheet 的命中（Q2）不在本表高亮
    expect(container.querySelectorAll('td.preview-hit-cell')).toHaveLength(7);
  });

  it('切换当前命中到非激活 Sheet 的命中：本表无当前高亮；切到该 Sheet 后出现', async () => {
    const { container, rerender } = renderSheet({ currentHit: 2 });
    await waitFor(() => expect(container.textContent).toContain('交通费'));
    expect(container.querySelectorAll('td.preview-hit-cell--current')).toHaveLength(0);
    rerender(
      <SheetRenderer
        api={fakeApi()}
        documentId="doc_xlsx"
        documentVersionId={null}
        sheets={SHEETS}
        activeSheet="Q2 报销"
        onSheetChange={vi.fn()}
        hits={HITS}
        currentHit={2}
      />,
    );
    await waitFor(() => expect(container.textContent).toContain('住宿费'));
    const current = container.querySelectorAll('td.preview-hit-cell--current');
    expect(current).toHaveLength(1);
    expect(current[0]?.textContent).toBe('住宿费');
    expect(current[0]?.getAttribute('data-hit-anchor')).toBe('2');
  });

  it('非法 a1_range：降级不高亮（不硬造锚点）', async () => {
    const hits: readonly PreviewHit[] = [
      { index: 1, summary: '坏区域', locator: { sheet: 'Q1 报销', a1_range: 'not-a-range' } },
    ];
    const { container } = renderSheet({ hits, currentHit: 0 });
    await waitFor(() => expect(container.textContent).toContain('交通费'));
    expect(container.querySelectorAll('td.preview-hit-cell')).toHaveLength(0);
  });
});

describe('SheetRenderer CSV 虚拟 Sheet', () => {
  it('固定唯一虚拟 Sheet CSV（页签原样展示，不改名）', async () => {
    const api = fakeApi();
    (api.getSheetContent as ReturnType<typeof vi.fn>).mockResolvedValue({
      sheet: 'CSV',
      row_count: 3,
      rows: [
        ['姓名', '部门'],
        ['张三', '财务部'],
        ['李四', '人事部'],
      ],
    } satisfies SheetContentResponse);
    const { container } = render(
      <SheetRenderer
        api={api}
        documentId="doc_csv"
        documentVersionId={null}
        sheets={[{ name: 'CSV', row_count: 3 }]}
        activeSheet="CSV"
        onSheetChange={vi.fn()}
        hits={[{ index: 1, summary: '张三行', locator: { sheet: 'CSV', a1_range: 'A2:B2' } }]}
        currentHit={0}
      />,
    );
    expect(screen.getByRole('radio', { name: 'CSV' })).toBeInTheDocument();
    await waitFor(() => expect(container.textContent).toContain('张三'));
    expect(container.querySelectorAll('td.preview-hit-cell--current')).toHaveLength(2);
  });
});
