/*
 * 原文预览域 API 封装（fe-doc-preview；契约《前端接口需求.md》§4）。
 * 复用 src/api/client.ts 的 ApiClient（/v1 前缀 + Bearer + 401 自动 refresh + 错误归一化）。
 * - GET /documents/{id}/preview：message_id 携带时返回该次回答引用本文档的全部 hits，否则只读形态；
 *   document_version_id 历史引用必须透传（否则读当前 active 版本）。
 * - GET /documents/{id}/content：PDF/图片二进制（pdfjs 经 buildContentUrl 自管 Range，不经本封装）；
 *   Word/md/txt/code/data 文本或结构化文档流；Excel/CSV 按 Sheet 的 JSON（?sheet=）。
 * 前端不解析源文件：表格内容/A1 定位数据由后端结构化 loader 供给。
 */

import { resolveUrl, type ApiClient } from '../api/client';
import type {
  DocumentPreviewResponse,
  SheetContentResponse,
  WordContentResponse,
} from './types';

export interface PreviewRequestOptions {
  readonly documentVersionId?: string | null;
}

export interface PreviewApi {
  getPreview(
    documentId: string,
    options: { messageId?: string | null; documentVersionId?: string | null },
  ): Promise<DocumentPreviewResponse>;
  /** md / txt / code / data / basic Word：纯文本内容。 */
  getTextContent(documentId: string, options?: PreviewRequestOptions): Promise<string>;
  /** 建树 Word：结构化文档流。 */
  getWordContent(documentId: string, options?: PreviewRequestOptions): Promise<WordContentResponse>;
  /** Excel / CSV：按 Sheet 的 JSON 行列数据。 */
  getSheetContent(
    documentId: string,
    sheet: string,
    options?: PreviewRequestOptions,
  ): Promise<SheetContentResponse>;
  /** 图片：文件流（img 无法携带 Bearer，经客户端取 Blob 后转 objectURL）。 */
  getImageContent(documentId: string, options?: PreviewRequestOptions): Promise<Blob>;
  /** pdfjs 直取内容的绝对 URL（Range 由 pdfjs 自管；Authorization 经 httpHeaders 注入）。 */
  buildContentUrl(contentUrl: string, documentVersionId?: string | null): string;
}

function previewQuery(options: { messageId?: string | null; documentVersionId?: string | null }): string {
  const params = new URLSearchParams();
  if (typeof options.messageId === 'string' && options.messageId !== '') {
    params.set('message_id', options.messageId);
  }
  if (typeof options.documentVersionId === 'string' && options.documentVersionId !== '') {
    params.set('document_version_id', options.documentVersionId);
  }
  const query = params.toString();
  return query === '' ? '' : `?${query}`;
}

function contentPath(documentId: string, options?: PreviewRequestOptions & { sheet?: string }): string {
  const params = new URLSearchParams();
  if (typeof options?.sheet === 'string' && options.sheet !== '') {
    params.set('sheet', options.sheet);
  }
  if (typeof options?.documentVersionId === 'string' && options.documentVersionId !== '') {
    params.set('document_version_id', options.documentVersionId);
  }
  const query = params.toString();
  return `/documents/${encodeURIComponent(documentId)}/content${query === '' ? '' : `?${query}`}`;
}

export function createPreviewApi(client: ApiClient): PreviewApi {
  return {
    getPreview(documentId, options) {
      return client.request<DocumentPreviewResponse>(
        `/documents/${encodeURIComponent(documentId)}/preview${previewQuery(options)}`,
      );
    },
    async getTextContent(documentId, options) {
      const blob = await client.request(contentPath(documentId, options), { responseType: 'blob' });
      return blob.text();
    },
    getWordContent(documentId, options) {
      return client.request<WordContentResponse>(contentPath(documentId, options));
    },
    getSheetContent(documentId, sheet, options) {
      return client.request<SheetContentResponse>(contentPath(documentId, { ...options, sheet }));
    },
    getImageContent(documentId, options) {
      return client.request(contentPath(documentId, options), { responseType: 'blob' });
    },
    buildContentUrl(contentUrl, documentVersionId) {
      const params = new URLSearchParams();
      if (typeof documentVersionId === 'string' && documentVersionId !== '') {
        params.set('document_version_id', documentVersionId);
      }
      const query = params.toString();
      // content_url 为 /v1 相对路径（契约 §4 示例 "/documents/doc_9/content"）
      return resolveUrl(`/v1${contentUrl}${query === '' ? '' : `?${query}`}`);
    },
  };
}
