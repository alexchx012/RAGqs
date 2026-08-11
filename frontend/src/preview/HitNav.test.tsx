import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { HitNav } from './HitNav';
import type { PreviewHit } from './types';

/*
 * 命中导航（fe-doc-preview）：条目三行（序号 / 摘要 / 定位小字）、当前态、点击切换、空态。
 */

const HITS: readonly PreviewHit[] = [
  { index: 1, summary: '年假天数按工龄分段', snippet: '5 days per year', locator: { page: 12, span: { start: 345, end: 412 } } },
  { index: 2, summary: '考勤缺勤判定', locator: { section_path: ['第 2 章', '考勤管理'], paragraph: 2 } },
  { index: 3, summary: 'Q1 交通费记录', locator: { sheet: 'Q1 报销', a1_range: 'A2:C2' } },
  { index: 4, summary: '架构总览图', locator: {} },
];

describe('HitNav', () => {
  it('渲染条目：序号 + 摘要 + 定位小字（四种 locator 形态）', () => {
    render(<HitNav hits={HITS} current={null} onSelect={() => {}} />);
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(4);
    expect(buttons[0]).toHaveTextContent('1');
    expect(buttons[0]).toHaveTextContent('年假天数按工龄分段');
    expect(buttons[0]).toHaveTextContent(copy.preview.hitLocatorPage(12));
    expect(buttons[1]).toHaveTextContent(copy.preview.hitLocatorSection(['第 2 章', '考勤管理'], 2));
    expect(buttons[2]).toHaveTextContent(copy.preview.hitLocatorSheet('Q1 报销', 'A2:C2'));
    // 空 locator：无定位行，只有序号 + 摘要
    expect(buttons[3]).toHaveTextContent('架构总览图');
    expect(buttons[3]?.querySelectorAll('p')).toHaveLength(0);
  });

  it('当前条目：aria-current 与 data-current 标记', () => {
    render(<HitNav hits={HITS} current={1} onSelect={() => {}} />);
    const buttons = screen.getAllByRole('button');
    expect(buttons[1]).toHaveAttribute('aria-current', 'true');
    expect(buttons[0]).not.toHaveAttribute('aria-current');
  });

  it('点击条目：onSelect 携带 hits 数组下标', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<HitNav hits={HITS} current={0} onSelect={onSelect} />);
    await user.click(screen.getAllByRole('button')[2] as HTMLElement);
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it('空态（无 message_id 只读形态）', () => {
    render(<HitNav hits={[]} current={null} onSelect={() => {}} />);
    expect(screen.getByText(copy.preview.navEmpty)).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
