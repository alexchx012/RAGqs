/*
 * 渲染层定位纯函数（fe-doc-preview；契约 §4「定位纪律」）。
 * 只做匹配与解析：snippet 文本匹配（空白容错 + span 消歧）、a1_range 行列解析、
 * DOM 文本节点高亮包裹；位置数据一律来自接口 locator + snippet，不做位置推断。
 */

export interface TextRange {
  /** 起始（含），目标文本坐标。 */
  readonly start: number;
  /** 结束（不含），目标文本坐标。 */
  readonly end: number;
}

/** HTML 转义（react-pdf customTextRenderer 返回 HTML 字符串；非命中段必须转义防注入）。 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 空白折叠归一化：连续空白折为单个空格，保留归一化坐标 → 原坐标的映射。 */
function normalizeWhitespace(text: string): { normalized: string; map: number[] } {
  const out: string[] = [];
  const map: number[] = [];
  let pendingWhitespace = -1;
  for (let index = 0; index < text.length; index += 1) {
    const ch = text[index] as string;
    if (/\s/.test(ch)) {
      if (out.length > 0) {
        pendingWhitespace = index;
      }
      continue;
    }
    if (pendingWhitespace >= 0) {
      out.push(' ');
      map.push(pendingWhitespace);
      pendingWhitespace = -1;
    }
    out.push(ch);
    map.push(index);
  }
  return { normalized: out.join(''), map };
}

function exactRanges(text: string, snippet: string): TextRange[] {
  const ranges: TextRange[] = [];
  let from = 0;
  while (from <= text.length - snippet.length) {
    const index = text.indexOf(snippet, from);
    if (index === -1) {
      break;
    }
    ranges.push({ start: index, end: index + snippet.length });
    from = index + 1;
  }
  return ranges;
}

/**
 * snippet 在文本中的全部出现位置（原坐标，允许重叠）。
 * 先精确匹配；匹配不到再按空白折叠归一化匹配（pdfjs 文本层换行/多空格与 snippet 不一致时兜底）。
 * snippet 为空串时返回空（无匹配物不高亮）。
 */
export function findSnippetRanges(text: string, snippet: string): TextRange[] {
  if (snippet === '') {
    return [];
  }
  const exact = exactRanges(text, snippet);
  if (exact.length > 0) {
    return exact;
  }
  const { normalized, map } = normalizeWhitespace(text);
  const normalizedSnippet = normalizeWhitespace(snippet).normalized;
  if (normalizedSnippet === '') {
    return [];
  }
  return exactRanges(normalized, normalizedSnippet).map((range) => {
    const start = map[range.start];
    const last = map[range.end - 1];
    return start === undefined || last === undefined
      ? range
      : { start, end: last + 1 };
  });
}

/**
 * 单个命中点的目标区间：多处重复时用 span（页内字符偏移）取最近一处消歧，其余取第一处；
 * 匹配不到返回 null（调用方降级为仅打开，不硬造锚点）。
 */
export function resolveSnippetRange(
  text: string,
  snippet: string | undefined,
  span?: { readonly start: number; readonly end: number },
): TextRange | null {
  if (snippet === undefined || snippet === '') {
    return null;
  }
  const ranges = findSnippetRanges(text, snippet);
  if (ranges.length === 0) {
    return null;
  }
  if (span === undefined || ranges.length === 1) {
    return ranges[0] as TextRange;
  }
  let best = ranges[0] as TextRange;
  for (const range of ranges) {
    if (Math.abs(range.start - span.start) < Math.abs(best.start - span.start)) {
      best = range;
    }
  }
  return best;
}

/** a1_range 解析结果（0 基、含端点）：B3:D30 → rows 2..29、cols 1..3。 */
export interface A1Range {
  readonly startRow: number;
  readonly startCol: number;
  readonly endRow: number;
  readonly endCol: number;
}

const A1_CELL = /^([A-Za-z]+)([0-9]+)$/;

function colToIndex(letters: string): number | null {
  let value = 0;
  for (const ch of letters.toUpperCase()) {
    const code = ch.charCodeAt(0);
    if (code < 65 || code > 90) {
      return null;
    }
    value = value * 26 + (code - 64);
  }
  return value - 1;
}

function parseA1Cell(part: string): { row: number; col: number } | null {
  const match = A1_CELL.exec(part.trim());
  if (match === null) {
    return null;
  }
  const col = colToIndex(match[1] as string);
  const row = Number(match[2]);
  if (col === null || !Number.isSafeInteger(row) || row < 1) {
    return null;
  }
  return { row: row - 1, col };
}

/** 解析 a1_range（`B3` / `B3:D30`，大小写不敏感、允许倒置）；非法输入返回 null。 */
export function parseA1Range(a1: string): A1Range | null {
  const parts = a1.split(':');
  if (parts.length < 1 || parts.length > 2) {
    return null;
  }
  const first = parseA1Cell(parts[0] as string);
  const second = parts.length === 2 ? parseA1Cell(parts[1] as string) : first;
  if (first === null || second === null) {
    return null;
  }
  return {
    startRow: Math.min(first.row, second.row),
    startCol: Math.min(first.col, second.col),
    endRow: Math.max(first.row, second.row),
    endCol: Math.max(first.col, second.col),
  };
}

/** DOM 高亮入参：concat 文本坐标区间 + 命中点序号 + 是否当前命中。 */
export interface DomHighlightRange extends TextRange {
  readonly hitIndex: number;
  readonly current: boolean;
}

/**
 * 在容器文本节点上包裹 <mark>（代码高亮等 HTML 已渲染、无法用声明式切片的场景）。
 * 坐标基于容器内全部文本节点按文档序拼接的文本；不改写文本内容，只插入包裹节点。
 */
export function highlightDomRanges(container: HTMLElement, ranges: readonly DomHighlightRange[]): void {
  if (ranges.length === 0) {
    return;
  }
  const walker = container.ownerDocument.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes: Array<{ node: Text; start: number; end: number }> = [];
  let offset = 0;
  let current = walker.nextNode();
  while (current !== null) {
    const text = current as Text;
    nodes.push({ node: text, start: offset, end: offset + text.data.length });
    offset += text.data.length;
    current = walker.nextNode();
  }
  const sorted = [...ranges].sort((a, b) => a.start - b.start || a.end - b.end);
  for (const entry of nodes) {
    const hits = sorted.filter((range) => range.start < entry.end && range.end > entry.start);
    if (hits.length === 0) {
      continue;
    }
    const boundaries = new Set<number>([entry.start, entry.end]);
    for (const range of hits) {
      boundaries.add(Math.max(range.start, entry.start));
      boundaries.add(Math.min(range.end, entry.end));
    }
    const ordered = [...boundaries].sort((a, b) => a - b);
    const fragment = container.ownerDocument.createDocumentFragment();
    for (let index = 0; index < ordered.length - 1; index += 1) {
      const from = ordered[index] as number;
      const to = ordered[index + 1] as number;
      const piece = entry.node.data.slice(from - entry.start, to - entry.start);
      const covering = hits.filter((range) => range.start <= from && range.end >= to);
      if (covering.length === 0) {
        fragment.appendChild(container.ownerDocument.createTextNode(piece));
        continue;
      }
      // 重叠区间取首个命中点（同一片段不叠标）
      const markFor = covering[0] as DomHighlightRange;
      const mark = container.ownerDocument.createElement('mark');
      mark.className = markFor.current ? 'preview-hit preview-hit--current' : 'preview-hit';
      mark.setAttribute('data-hit-anchor', String(markFor.hitIndex));
      mark.textContent = piece;
      fragment.appendChild(mark);
    }
    entry.node.replaceWith(fragment);
  }
}
