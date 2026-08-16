/*
 * 原文预览域契约类型（fe-doc-preview；契约《前端接口需求.md》§4）。
 * 仅描述 HTTP 契约形状；snake_case 只用于 JSON 契约字段，内部标识一律 camelCase。
 * 预览与高亮策略由 media_kind + has_text_layer + tree_indexed 决定（strategy.ts），不按扩展名。
 * 未知 media_kind 按 §1 兜底规则用 `string & {}` 收窄（不丢弃、不崩溃）。
 */

/** 载体类别（§4）；未知值保留并按只读文本形态兜底渲染。 */
export type PreviewMediaKind =
  | 'pdf'
  | 'word'
  | 'md'
  | 'txt'
  | 'excel'
  | 'csv'
  | 'image'
  | 'code'
  | 'data'
  | (string & {});

/**
 * 命中点定位（§4；与 Citation.locator 同四形态，只带载体固有单位，前端不做位置推断）：
 * - 文本层 PDF：page + 可选 span（页内字符偏移，仅同页多处重复时消歧）；
 * - 扫描件 PDF：仅 page；
 * - 建树文档：section_path + 可选 paragraph（1 基段落序号）；
 * - 表格：sheet + a1_range；
 * - basic 文档 / 图片 / 文本流：空对象。
 */
export type PreviewLocator =
  | { readonly page: number; readonly span?: { readonly start: number; readonly end: number } }
  | { readonly section_path: readonly string[]; readonly paragraph?: number }
  | { readonly sheet: string; readonly a1_range: string }
  | Record<string, never>;

/** 命中点（§4 hits[]）：summary 导航一行摘要；snippet 精确片段（文本层 PDF 与文本流必带）。 */
export interface PreviewHit {
  readonly index: number;
  readonly summary: string;
  readonly snippet?: string;
  readonly locator: PreviewLocator;
}

/** 表格类 Sheet 元数据（§4；Excel 用源 Sheet 名，CSV 固定唯一虚拟 Sheet「CSV」，前端不改名不合并）。 */
export interface PreviewSheetMeta {
  readonly name: string;
  readonly row_count: number;
}

/** GET /documents/{id}/preview 响应（§4）。 */
export interface DocumentPreviewResponse {
  readonly document_id: string;
  readonly document_version_id: string;
  readonly name: string;
  readonly media_kind: PreviewMediaKind;
  readonly size_bytes: number;
  readonly content_available: boolean;
  readonly has_text_layer: boolean;
  readonly tree_indexed: boolean;
  readonly page_count: number | null;
  readonly sheets: readonly PreviewSheetMeta[] | null;
  readonly content_url: string;
  readonly hits: readonly PreviewHit[];
}

/** GET /documents/{id}/content（建树 Word）：结构化文档流。 */
export interface WordContentSection {
  /** 与 locator.section_path 对齐的章节路径。 */
  readonly path: readonly string[];
  readonly paragraphs: readonly string[];
}

export interface WordContentResponse {
  readonly sections: readonly WordContentSection[];
}

/** GET /documents/{id}/content?sheet=（Excel/CSV）：按 Sheet 的 JSON 行列数据（首行为表头）。 */
export type SheetCell = string | number | boolean | null;

export interface SheetContentResponse {
  readonly sheet: string;
  readonly row_count: number;
  readonly rows: readonly (readonly SheetCell[])[];
}
