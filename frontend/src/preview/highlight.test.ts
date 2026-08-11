import { describe, expect, it } from 'vitest';
import {
  escapeHtml,
  findSnippetRanges,
  highlightDomRanges,
  parseA1Range,
  resolveSnippetRange,
} from './highlight';

/*
 * 定位纯函数（fe-doc-preview）：snippet 匹配（空白容错 + span 消歧）、a1_range 解析、
 * DOM 高亮包裹；匹配不到一律降级（null / 空），不做位置推断。
 */

describe('findSnippetRanges', () => {
  it('精确匹配：单处与多处（允许重叠）', () => {
    expect(findSnippetRanges('alpha beta alpha', 'alpha')).toEqual([
      { start: 0, end: 5 },
      { start: 11, end: 16 },
    ]);
    expect(findSnippetRanges('aaaa', 'aa')).toEqual([
      { start: 0, end: 2 },
      { start: 1, end: 3 },
      { start: 2, end: 4 },
    ]);
  });

  it('空 snippet / 无匹配：空数组', () => {
    expect(findSnippetRanges('text', '')).toEqual([]);
    expect(findSnippetRanges('text', 'missing')).toEqual([]);
  });

  it('空白容错：文本换行/多空格与 snippet 单空格不一致时归一化匹配', () => {
    const text = 'Annual leave:\n  5 days   per year.';
    // 归一化后 '5' 起始于原文 16，'r' 结束于原文 32（含 32 之后的 '.' 不含）
    expect(findSnippetRanges(text, '5 days per year')).toEqual([{ start: 16, end: 33 }]);
  });

  it('空白容错：连续空白长度差异折叠', () => {
    const text = '满 1 年  不满 10 年';
    // 归一化后全串匹配（双空格折为单空格，覆盖至末尾 '年'）
    expect(findSnippetRanges(text, '满 1 年 不满 10 年')).toEqual([{ start: 0, end: 14 }]);
  });
});

describe('resolveSnippetRange', () => {
    it('snippet 缺失或空串：null（调用方降级仅打开）', () => {
    expect(resolveSnippetRange('text', undefined)).toBeNull();
    expect(resolveSnippetRange('text', '')).toBeNull();
  });

  it('匹配不到：null', () => {
    expect(resolveSnippetRange('text', 'missing')).toBeNull();
  });

  it('单处：直接命中（span 不参与）', () => {
    expect(resolveSnippetRange('alpha beta', 'beta', { start: 99, end: 100 })).toEqual({ start: 6, end: 10 });
  });

  it('多处 + span：取页内偏移最近一处消歧', () => {
    const text = 'repeat alpha repeat alpha repeat';
    // 出现在 7 与 20；span 指向第二处附近
    expect(resolveSnippetRange(text, 'alpha', { start: 22, end: 27 })).toEqual({ start: 20, end: 25 });
    // span 指向第一处附近
    expect(resolveSnippetRange(text, 'alpha', { start: 8, end: 13 })).toEqual({ start: 7, end: 12 });
  });

  it('多处无 span：取第一处', () => {
    expect(resolveSnippetRange('alpha alpha', 'alpha')).toEqual({ start: 0, end: 5 });
  });
});

describe('parseA1Range', () => {
  it('单元格与区域（0 基含端点）', () => {
    expect(parseA1Range('B3')).toEqual({ startRow: 2, startCol: 1, endRow: 2, endCol: 1 });
    expect(parseA1Range('B3:D30')).toEqual({ startRow: 2, startCol: 1, endRow: 29, endCol: 3 });
  });

  it('大小写不敏感与多字母列', () => {
    expect(parseA1Range('b3:d30')).toEqual({ startRow: 2, startCol: 1, endRow: 29, endCol: 3 });
    expect(parseA1Range('AA1:AB2')).toEqual({ startRow: 0, startCol: 26, endRow: 1, endCol: 27 });
  });

  it('倒置区域归一化', () => {
    expect(parseA1Range('D30:B3')).toEqual({ startRow: 2, startCol: 1, endRow: 29, endCol: 3 });
  });

  it('非法输入：null', () => {
    for (const input of ['', 'B', '1A', 'A0', 'A1:B', 'A1:B2:C3', 'A-1', 'A 1']) {
      expect(parseA1Range(input), input).toBeNull();
    }
  });
});

describe('escapeHtml', () => {
  it('转义全部注入面', () => {
    expect(escapeHtml(`<b>&"x"'y</b>`)).toBe('&lt;b&gt;&amp;&quot;x&quot;&#39;y&lt;/b&gt;');
  });
});

describe('highlightDomRanges', () => {
  it('跨文本节点包裹 mark，保留原文与类名', () => {
    const container = document.createElement('div');
    container.innerHTML = '<span>alpha <em>be</em>ta</span> gamma';
    // 全文 'alpha beta gamma'；命中 'beta'（跨 em 边界）与 'gamma'
    highlightDomRanges(container, [
      { start: 6, end: 10, hitIndex: 0, current: true },
      { start: 11, end: 16, hitIndex: 1, current: false },
    ]);
    expect(container.textContent).toBe('alpha beta gamma');
    const marks = container.querySelectorAll('mark');
    expect(marks.length).toBeGreaterThanOrEqual(2);
    const current = container.querySelector('mark.preview-hit--current');
    expect(current?.getAttribute('data-hit-anchor')).toBe('0');
    expect(current?.textContent).toContain('be');
    const plain = container.querySelector('mark:not(.preview-hit--current)');
    expect(plain?.getAttribute('data-hit-anchor')).toBe('1');
    expect(plain?.textContent).toBe('gamma');
  });

  it('空 ranges：不改写 DOM', () => {
    const container = document.createElement('div');
    container.innerHTML = '<span>keep</span>';
    highlightDomRanges(container, []);
    expect(container.innerHTML).toBe('<span>keep</span>');
  });
});
