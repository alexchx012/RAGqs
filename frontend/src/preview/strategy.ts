/*
 * 预览策略矩阵（fe-doc-preview；契约 §4）：media_kind + has_text_layer + tree_indexed → 渲染与定位行为。
 * 纯函数，不看扩展名；未知 media_kind 兜底为只读文本流（打开不崩溃，§1 未知枚举容忍）。
 * 定位纪律：只做渲染层定位，无自然定位单位的载体不硬造锚点（locate 'none'）。
 */

import type { PreviewMediaKind } from './types';

export type PreviewRendererKind = 'pdf' | 'word' | 'text' | 'sheet' | 'image';

/** 命中定位方式：渲染层如何消费 locator + snippet。 */
export type PreviewLocateMode =
  /** 文本层 PDF：跳页 + snippet 在 pdfjs 文本层匹配高亮（span 仅同页多处重复时消歧）。 */
  | 'page-and-snippet'
  /** 扫描件 PDF：只跳页，不做片段高亮，不出现任何替代锚点 UI。 */
  | 'page-only'
  /** 建树 Word：按 section_path（+paragraph）锚点定位并高亮命中段落。 */
  | 'section'
  /** md / txt / code / data：文本流 snippet 匹配高亮；匹配不到仅打开文档。 */
  | 'snippet'
  /** Excel / CSV：a1_range 行列区域滚动定位并高亮。 */
  | 'a1'
  /** 无自然定位单位（basic Word / 图片 / 未知载体）：仅打开。 */
  | 'none';

export interface PreviewStrategy {
  readonly renderer: PreviewRendererKind;
  readonly locate: PreviewLocateMode;
  /** 文本流渲染的细分形态：code 走语法高亮，data 按 JSON 文本流。 */
  readonly textKind: 'plain' | 'code' | 'data' | null;
}

export interface PreviewStrategyInput {
  readonly media_kind: PreviewMediaKind;
  readonly has_text_layer: boolean;
  readonly tree_indexed: boolean;
}

const READONLY_TEXT: PreviewStrategy = { renderer: 'text', locate: 'none', textKind: 'plain' };

export function previewStrategy(meta: PreviewStrategyInput): PreviewStrategy {
  switch (meta.media_kind) {
    case 'pdf':
      return meta.has_text_layer
        ? { renderer: 'pdf', locate: 'page-and-snippet', textKind: null }
        : { renderer: 'pdf', locate: 'page-only', textKind: null };
    case 'word':
      return meta.tree_indexed
        ? { renderer: 'word', locate: 'section', textKind: null }
        : { renderer: 'word', locate: 'none', textKind: null };
    case 'md':
    case 'txt':
      return { renderer: 'text', locate: 'snippet', textKind: 'plain' };
    case 'code':
      return { renderer: 'text', locate: 'snippet', textKind: 'code' };
    case 'data':
      return { renderer: 'text', locate: 'snippet', textKind: 'data' };
    case 'excel':
    case 'csv':
      return { renderer: 'sheet', locate: 'a1', textKind: null };
    case 'image':
      return { renderer: 'image', locate: 'none', textKind: null };
    default:
      // 未知载体：只读文本流兜底，不做位置推断
      return READONLY_TEXT;
  }
}
