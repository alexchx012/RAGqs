/*
 * 原文预览 MSW handlers（fe-doc-preview；契约《前端接口需求.md》§4）。
 * 把 GET /documents/{id}/preview 与 GET /documents/{id}/content 接到 MockPreviewController。
 * content 支持 Range 分段加载（pdfjs）：bytes 区间 → 206 + Content-Range + Accept-Ranges，
 * 非法/越界区间 → 416 + Content-Range bytes *​/size；HEAD 返回同头空体（预检分段能力）。
 * 非 2xx 一律按 §1 HTTP 请求级错误对象返回。
 */

import { http, HttpResponse } from 'msw';
import type { MockContentResult, MockPreviewController } from './preview-contract';
import { MockHttpError } from './auth-contract';

export { MockHttpError };

let requestSeq = 0;

function errorResponse(error: unknown) {
  const normalized =
    error instanceof MockHttpError ? error : new MockHttpError(500, 'internal_error');
  requestSeq += 1;
  return HttpResponse.json(
    {
      error: {
        code: normalized.code,
        message: normalized.code,
        details: normalized.details,
        request_id: `req_mock_${requestSeq}`,
      },
    },
    { status: normalized.status },
  );
}

/** Range 分段（契约 §4：PDF/图片文件流支持 Range；文本/JSON 全量 200）。 */
function contentResponse(request: Request, result: MockContentResult): Response {
  const bytes = result.body;
  const baseHeaders: Record<string, string> = {
    'Content-Type': result.contentType,
    'Accept-Ranges': 'bytes',
  };
  if (request.method === 'HEAD') {
    return new HttpResponse(null, {
      status: 200,
      headers: { ...baseHeaders, 'Content-Length': String(bytes.length) },
    });
  }
  const rangeHeader = request.headers.get('Range');
  if (rangeHeader !== null) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
    if (match === null || ((match[1] ?? '') === '' && (match[2] ?? '') === '')) {
      return new HttpResponse(null, { status: 416, headers: { 'Content-Range': `bytes */${bytes.length}` } });
    }
    const first = match[1] ?? '';
    const last = match[2] ?? '';
    let start: number;
    let end: number;
    if (first === '') {
      // 后缀区间：最后 N 字节
      const suffix = Number(last);
      start = Math.max(0, bytes.length - suffix);
      end = bytes.length - 1;
    } else {
      start = Number(first);
      end = last === '' ? bytes.length - 1 : Math.min(Number(last), bytes.length - 1);
    }
    if (!Number.isSafeInteger(start) || start > end || start >= bytes.length) {
      return new HttpResponse(null, { status: 416, headers: { 'Content-Range': `bytes */${bytes.length}` } });
    }
    return new HttpResponse(bytes.slice(start, end + 1), {
      status: 206,
      headers: {
        ...baseHeaders,
        'Content-Range': `bytes ${start}-${end}/${bytes.length}`,
        'Content-Length': String(end - start + 1),
      },
    });
  }
  return new HttpResponse(bytes, {
    status: 200,
    headers: { ...baseHeaders, 'Content-Length': String(bytes.length) },
  });
}

function previewQuery(request: Request) {
  const url = new URL(request.url);
  return {
    messageId: url.searchParams.get('message_id'),
    documentVersionId: url.searchParams.get('document_version_id'),
  };
}

function contentQuery(request: Request) {
  const url = new URL(request.url);
  return {
    documentVersionId: url.searchParams.get('document_version_id'),
    sheet: url.searchParams.get('sheet'),
  };
}

export function createPreviewHandlers(controller: MockPreviewController) {
  return [
    http.get('/v1/documents/:id/preview', ({ request, params }) => {
      try {
        return HttpResponse.json(
          controller.getPreview(request.headers.get('Authorization'), String(params['id']), previewQuery(request)),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/documents/:id/content', ({ request, params }) => {
      try {
        const result = controller.getContent(
          request.headers.get('Authorization'),
          String(params['id']),
          contentQuery(request),
        );
        return contentResponse(request, result);
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.head('/v1/documents/:id/content', ({ request, params }) => {
      try {
        const result = controller.getContent(
          request.headers.get('Authorization'),
          String(params['id']),
          contentQuery(request),
        );
        return contentResponse(request, result);
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}
