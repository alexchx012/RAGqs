/*
 * 原文预览契约 mock 核心（fe-doc-preview；契约《前端接口需求.md》§4）。
 * 与传输层无关（preview-handlers.ts 负责 MSW 接线与 Range 分段），真实模拟：
 * - GET /documents/{id}/preview：message_id 携带时返回该次回答引用本文档的全部 hits，
 *   不携带则 hits 为空（管理侧只读形态）；document_version_id 历史引用必须透传，否则读当前 active 版本；
 * - GET /documents/{id}/content：PDF/图片为文件字节（仅 PDF 支持 Range；HEAD 仅原始 PDF/图片）；
 *   Word 建树为结构化 JSON、basic 为纯文本；md/txt/code/data 为纯文本；Excel/CSV 为按 Sheet 的 JSON（?sheet=）；
 * - 不可用态：文档删除 → 410 document_unavailable；版本 purging/purged → 410 document_version_unavailable；
 *   统一错误对象经 handler 归一化。
 * 夹具覆盖 A29：有文本层 PDF（hits+span）、扫描件 PDF、Excel 多 Sheet、CSV 虚拟 Sheet、图片、
 * 文本流（md/txt/code/data）、建树 Word、basic Word、不可用态文档；PDF 为真实可解析字节
 * （最小 PDF 生成器内嵌，文本层 ASCII，不嵌入中文字体；扫描件=无文本操作的 PDF）。
 */

import type { DocumentPreviewResponse, PreviewHit } from '../preview/types';
import { MockHttpError } from './auth-contract';

export { MockHttpError };

/** 鉴权注入：装配处用 MockAuthController.me 实现；无有效 Bearer 时抛 MockHttpError(401)。 */
export interface ValidatePreviewAuth {
  (header: string | null): { userId: string };
}

export interface PreviewMessageCitation {
  readonly document_id: string;
  readonly document_version_id: string;
  readonly locator: PreviewHit['locator'];
  readonly snippet?: string;
}

export interface GetPreviewMessageCitations {
  /** `null` means the message does not belong to the authenticated user. */
  (header: string | null, messageId: string): readonly PreviewMessageCitation[] | null;
}

/* ---------- 种子常量（e2e 经同一数据源引用，避免硬编码） ---------- */

export const PREVIEW_SEED = {
  scanDocId: 'doc_scan',
  scanDocName: '扫描合同.pdf',
  excelDocId: 'doc_xlsx',
  excelDocName: '报销明细.xlsx',
  excelSheetQ1: 'Q1 报销',
  excelSheetQ2: 'Q2 报销',
  excelQ2FirstCell: '住宿费',
  goneDocId: 'doc_gone',
  goneDocName: '已删除文档.pdf',
} as const;

/* ---------- 最小 PDF 生成器（真实可解析字节；文本层 ASCII） ---------- */

function pdfEscape(text: string): string {
  return text.replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
}

/**
 * 生成最小合法 PDF：对象 1=Catalog、2=Pages、3=Helvetica 字体，之后每页 Page+Content 两对象。
 * 每页 lines 逐行 Tj（T* 换行）；lines 为空数组 = 无文本操作的扫描件页（画一个灰底矩形）。
 * 全部 ASCII，string.length 即字节偏移（xref 精确计算）。
 */
export function buildMinimalPdf(pages: readonly (readonly string[])[]): Uint8Array {
  const objects: string[] = [];
  objects[0] = '<< /Type /Catalog /Pages 2 0 R >>';
  const kids = pages.map((_unused, index) => `${4 + index * 2} 0 R`).join(' ');
  objects[1] = `<< /Type /Pages /Kids [${kids}] /Count ${pages.length} >>`;
  objects[2] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>';
  pages.forEach((lines, index) => {
    const pageId = 4 + index * 2;
    const contentId = pageId + 1;
    const stream =
      lines.length === 0
        ? '0.9 g\n40 40 532 762 re f\n'
        : ['BT', '/F1 12 Tf', '14 TL', '50 780 Td', ...lines.map((line, lineIndex) => `${lineIndex === 0 ? '' : 'T*\n'}(${pdfEscape(line)}) Tj`), 'ET'].join('\n');
    objects[pageId - 1] =
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] ` +
      `/Resources << /Font << /F1 3 0 R >> >> /Contents ${contentId} 0 R >>`;
    objects[contentId - 1] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
  });

  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((body, index) => {
    offsets.push(out.length);
    out += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xrefStart = out.length;
  out += `xref\n0 ${objects.length + 1}\n`;
  out += '0000000000 65535 f \n';
  for (const offset of offsets) {
    out += `${String(offset).padStart(10, '0')} 00000 n \n`;
  }
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefStart}\n%%EOF\n`;
  return new TextEncoder().encode(out);
}

/** 1x1 透明 PNG。 */
const TINY_PNG = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='), (ch) => ch.charCodeAt(0));

/* ---------- 夹具模型 ---------- */

interface MockPreviewVersion {
  readonly versionId: string;
  readonly status: 'active' | 'superseded' | 'purged';
  readonly contentType: string;
  /** 原始文件字节数；结构化内容的响应体并不代表它。 */
  readonly sizeBytes?: number;
  /** 已序列化的内容字节（JSON 在种子处 stringify）。 */
  readonly body: Uint8Array;
}

interface MockPreviewDocument {
  readonly id: string;
  readonly name: string;
  readonly mediaKind: string;
  readonly hasTextLayer: boolean;
  readonly treeIndexed: boolean;
  readonly pageCount: number | null;
  readonly sheets: readonly { name: string; row_count: number }[] | null;
  readonly available: boolean;
  readonly versions: readonly MockPreviewVersion[];
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function json(value: unknown): Uint8Array {
  return utf8(JSON.stringify(value));
}

function previewHitFromCitation(citation: PreviewMessageCitation, index: number): PreviewHit {
  const structuredLocator = 'section_path' in citation.locator || 'sheet' in citation.locator;
  const snippet =
    !structuredLocator && typeof citation.snippet === 'string' && citation.snippet.trim() !== ''
      ? citation.snippet.trim()
      : undefined;
  return {
    index,
    summary: snippet ?? summaryFromLocator(citation.locator),
    locator: citation.locator,
    ...(snippet === undefined ? {} : { snippet }),
  };
}

function summaryFromLocator(locator: PreviewHit['locator']): string {
  if ('page' in locator) {
    return `Page ${locator.page}`;
  }
  if ('section_path' in locator) {
    return locator.section_path.join(' / ');
  }
  if ('sheet' in locator) {
    return `Sheet ${locator.sheet}, range ${locator.a1_range}`;
  }
  return 'Document citation';
}

const PDF_TEXT = 'application/pdf';
const PNG = 'image/png';
const JSON_TYPE = 'application/json';
const TEXT_PLAIN = 'text/plain; charset=utf-8';
const TEXT_MARKDOWN = 'text/markdown; charset=utf-8';

function seedDocuments(): MockPreviewDocument[] {
  /* doc_1：有文本层 PDF（与 chat 域 doc_1 员工手册同名同版本链；引用角标深链目标）。 */
  const handbookV1Pages: readonly (readonly string[])[] = [
    ['Employee Handbook', 'Annual leave: 5 days per year for service under 10 years.', 'Leave balance carries over to next year.'],
    ['Sick leave requires a medical certificate.', 'Overtime must be approved in advance.'],
  ];
  const handbookV0Pages: readonly (readonly string[])[] = [
    ['Legacy handbook text.', 'Annual leave: 4 days per year.'],
  ];
  const handbook: MockPreviewDocument = {
    id: 'doc_1',
    name: '员工手册.pdf',
    mediaKind: 'pdf',
    hasTextLayer: true,
    treeIndexed: false,
    pageCount: 2,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'v_1',
        status: 'active',
        contentType: PDF_TEXT,
        body: buildMinimalPdf(handbookV1Pages),
      },
      {
        versionId: 'v_0',
        status: 'superseded',
        contentType: PDF_TEXT,
        body: buildMinimalPdf(handbookV0Pages),
      },
      { versionId: 'v_purged', status: 'purged', contentType: PDF_TEXT, body: new Uint8Array(0) },
    ],
  };

  /* doc_scan：扫描件 PDF（无文本层；只跳页无片段高亮）。 */
  const scan: MockPreviewDocument = {
    id: PREVIEW_SEED.scanDocId,
    name: PREVIEW_SEED.scanDocName,
    mediaKind: 'pdf',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: 1,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vs_1',
        status: 'active',
        contentType: PDF_TEXT,
        body: buildMinimalPdf([[]]),
      },
    ],
  };

  /* doc_xlsx：Excel 多 Sheet（源 Sheet 名原样下发；A1 定位）。 */
  const excel: MockPreviewDocument = {
    id: PREVIEW_SEED.excelDocId,
    name: PREVIEW_SEED.excelDocName,
    mediaKind: 'excel',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: [
      { name: PREVIEW_SEED.excelSheetQ1, row_count: 5 },
      { name: PREVIEW_SEED.excelSheetQ2, row_count: 3 },
    ],
    available: true,
    versions: [
      {
        versionId: 'vx_1',
        status: 'active',
        contentType: JSON_TYPE,
        sizeBytes: 256,
        body: new Uint8Array(0), // 表格内容按 ?sheet= 动态组装（见 sheetRows）
      },
    ],
  };

  /* doc_csv：CSV 固定唯一虚拟 Sheet「CSV」。 */
  const csv: MockPreviewDocument = {
    id: 'doc_csv',
    name: '联系人.csv',
    mediaKind: 'csv',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: [{ name: 'CSV', row_count: 3 }],
    available: true,
    versions: [
      {
        versionId: 'vc_1',
        status: 'active',
        contentType: JSON_TYPE,
        sizeBytes: 128,
        body: new Uint8Array(0),
      },
    ],
  };

  /* doc_img：图片（空 locator，只有文档名，无锚点）。 */
  const image: MockPreviewDocument = {
    id: 'doc_img',
    name: '架构图.png',
    mediaKind: 'image',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vi_1',
        status: 'active',
        contentType: PNG,
        body: TINY_PNG,
      },
    ],
  };

  /* doc_md / doc_txt：文本流（snippet 匹配高亮；空 locator）。 */
  const md: MockPreviewDocument = {
    id: 'doc_md',
    name: '年假政策.md',
    mediaKind: 'md',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vm_1',
        status: 'active',
        contentType: TEXT_MARKDOWN,
        body: utf8('# 年假政策\n\n员工年假天数按工龄分段：满 1 年不满 10 年为 5 天。\n\n## 申请流程\n\n提前 3 个工作日提交申请。\n'),
      },
    ],
  };
  const txt: MockPreviewDocument = {
    id: 'doc_txt',
    name: '值班安排.txt',
    mediaKind: 'txt',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vt_1',
        status: 'active',
        contentType: TEXT_PLAIN,
        body: utf8('值班安排\n\n周一至周五由各部门轮流值班。\n节假日值班另行通知。\n'),
      },
    ],
  };

  /* doc_code / doc_data：代码语法高亮与 JSON 文本流。 */
  const code: MockPreviewDocument = {
    id: 'doc_code',
    name: '巡检脚本.py',
    mediaKind: 'code',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vk_1',
        status: 'active',
        contentType: TEXT_PLAIN,
        body: utf8('def main():\n    print("scan")\n\nif __name__ == "__main__":\n    main()\n'),
      },
    ],
  };
  const data: MockPreviewDocument = {
    id: 'doc_data',
    name: '指标数据.json',
    mediaKind: 'data',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vd_1',
        status: 'active',
        contentType: TEXT_PLAIN,
        body: utf8('{"annual_leave": 5, "sick_leave": 10}\n'),
      },
    ],
  };

  /* doc_word：建树 Word（section_path + paragraph 锚点）；doc_word_basic：未建树仅打开。 */
  const word: MockPreviewDocument = {
    id: 'doc_word',
    name: '员工制度.docx',
    mediaKind: 'word',
    hasTextLayer: false,
    treeIndexed: true,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vw_1',
        status: 'active',
        contentType: JSON_TYPE,
        sizeBytes: 1536,
        body: json({
          sections: [
            { path: ['第 1 章', '总则'], paragraphs: ['本制度适用于全体员工。', '制度自发布之日起生效。'] },
            { path: ['第 2 章', '考勤管理'], paragraphs: ['标准工时为每日 8 小时。', '迟到 30 分钟以上记为缺勤。', '加班需提前审批。'] },
          ],
        }),
      },
    ],
  };
  const wordBasic: MockPreviewDocument = {
    id: 'doc_word_basic',
    name: '会议纪要.docx',
    mediaKind: 'word',
    hasTextLayer: false,
    treeIndexed: false,
    pageCount: null,
    sheets: null,
    available: true,
    versions: [
      {
        versionId: 'vwb_1',
        status: 'active',
        contentType: TEXT_PLAIN,
        sizeBytes: 1024,
        body: utf8('会议纪要\n\n本次会议确认了上线节奏。\n\n后续行动项由各部门跟进。\n'),
      },
    ],
  };

  /* doc_gone：不可用态（文档已删除 → 410 document_unavailable）。 */
  const gone: MockPreviewDocument = {
    id: PREVIEW_SEED.goneDocId,
    name: PREVIEW_SEED.goneDocName,
    mediaKind: 'pdf',
    hasTextLayer: true,
    treeIndexed: false,
    pageCount: 3,
    sheets: null,
    available: false,
    versions: [],
  };

  return [handbook, scan, excel, csv, image, md, txt, code, data, word, wordBasic, gone];
}

/** Excel/CSV 的按 Sheet 行列数据（结构化 loader 供给；首行为表头）。 */
const SHEET_ROWS: Record<string, Record<string, readonly (readonly (string | number)[])[]>> = {
  [PREVIEW_SEED.excelDocId]: {
    [PREVIEW_SEED.excelSheetQ1]: [
      ['项目', '金额', '状态'],
      ['交通费', 320, '已批'],
      ['餐饮费', 158, '待批'],
      ['办公用品', 89, '已批'],
      ['快递费', 24, '已批'],
    ],
    [PREVIEW_SEED.excelSheetQ2]: [
      ['项目', '金额', '状态'],
      [PREVIEW_SEED.excelQ2FirstCell, 1200, '待批'],
      ['机票', 2300, '待批'],
    ],
  },
  doc_csv: {
    CSV: [
      ['姓名', '部门'],
      ['张三', '财务部'],
      ['李四', '人事部'],
    ],
  },
};

/* ---------- 控制器 ---------- */

export interface MockContentResult {
  readonly contentType: string;
  readonly body: Uint8Array;
}

export class MockPreviewController {
  private documents = new Map<string, MockPreviewDocument>();

  constructor(
    private readonly validateAuth: ValidatePreviewAuth,
    private readonly getMessageCitations: GetPreviewMessageCitations,
  ) {
    this.reset();
  }

  reset(): void {
    this.documents.clear();
    for (const document of seedDocuments()) {
      this.documents.set(document.id, document);
    }
  }

  /* ---------- GET /documents/{id}/preview ---------- */

  getPreview(
    auth: string | null,
    documentId: string,
    query: { messageId?: string | null; documentVersionId?: string | null },
  ): DocumentPreviewResponse {
    this.requireAuth(auth);
    if (query.messageId === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'message_id' });
    }
    if (query.documentVersionId === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'document_version_id' });
    }
    const document = this.document(documentId);
    const version = this.version(document, query.documentVersionId);
    const citations =
      query.messageId === null || query.messageId === undefined
        ? null
        : this.getMessageCitations(auth, query.messageId);
    if (query.messageId !== undefined && query.messageId !== null && citations === null) {
      throw new MockHttpError(404, 'message_not_found');
    }
    // message_id 携带 → 该次回答引用本文档的全部 hits；不携带 → 管理侧只读形态（hits 为空）
    const hits = (citations ?? [])
      .filter(
        (citation) =>
          citation.document_id === document.id && citation.document_version_id === version.versionId,
      )
      .map((citation, index) => previewHitFromCitation(citation, index + 1));
    return {
      document_id: document.id,
      document_version_id: version.versionId,
      name: document.name,
      media_kind: document.mediaKind,
      size_bytes: version.sizeBytes ?? version.body.byteLength,
      content_available: true,
      has_text_layer: document.hasTextLayer,
      tree_indexed: document.treeIndexed,
      page_count: document.pageCount,
      sheets: document.sheets,
      content_url: `/v1/documents/${document.id}/content?document_version_id=${encodeURIComponent(version.versionId)}`,
      hits,
    };
  }

  /* ---------- GET /documents/{id}/content ---------- */

  getContent(
    auth: string | null,
    documentId: string,
    query: { documentVersionId?: string | null; sheet?: string | null },
  ): MockContentResult {
    this.requireAuth(auth);
    if (query.documentVersionId === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'document_version_id' });
    }
    if (query.sheet === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'sheet' });
    }
    const document = this.document(documentId);
    const version = this.version(document, query.documentVersionId);
    if (document.sheets !== null) {
      // Excel/CSV：按 Sheet 的 JSON 行列数据；省略 sheet 时默认第一页签
      const sheetName = query.sheet ?? document.sheets[0]?.name;
      const rows = SHEET_ROWS[document.id]?.[sheetName ?? ''];
      if (sheetName == null || rows === undefined) {
        throw new MockHttpError(404, 'sheet_not_found');
      }
      return {
        contentType: JSON_TYPE,
        body: json({ sheet: sheetName, row_count: rows.length, rows }),
      };
    }
    return { contentType: version.contentType, body: version.body };
  }

  /* ---------- 内部 ---------- */

  private requireAuth(auth: string | null): void {
    this.validateAuth(auth);
  }

  private document(id: string): MockPreviewDocument {
    const document = this.documents.get(id);
    if (document === undefined) {
      throw new MockHttpError(404, 'document_not_found');
    }
    if (!document.available) {
      // 不可用态：不泄露任何元数据（仅统一错误码）
      throw new MockHttpError(410, 'document_unavailable');
    }
    return document;
  }

  private version(document: MockPreviewDocument, versionId: string | null | undefined): MockPreviewVersion {
    const target =
      versionId == null
        ? document.versions.find((version) => version.status === 'active')
        : document.versions.find((version) => version.versionId === versionId);
    if (target === undefined) {
      throw new MockHttpError(404, 'document_version_not_found');
    }
    if (target.status === 'purged') {
      throw new MockHttpError(410, 'document_version_unavailable');
    }
    return target;
  }
}
