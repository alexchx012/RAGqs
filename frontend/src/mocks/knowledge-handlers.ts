/*
 * 知识库契约 mock 传输层（MSW 接线；契约 §6.2–6.11、§8.1/8.4–8.5）。
 * 与控制器分离：本文件只做 URL/参数/请求体解析与错误归一化。
 */

import { http, HttpResponse } from 'msw';
import { MockHttpError } from './auth-contract';
import type { MockKnowledgeController } from './knowledge-contract';

let requestSeq = 0;

function errorResponse(error: unknown) {
  const normalized = error instanceof MockHttpError ? error : new MockHttpError(500, 'internal_error');
  requestSeq += 1;
  return HttpResponse.json(
    {
      error: {
        code: normalized.code,
        message: normalized.code,
        details: normalized.details,
        request_id: `req_mock_knowledge_${requestSeq}`,
      },
    },
    { status: normalized.status },
  );
}

async function jsonObject(request: Request): Promise<Record<string, unknown>> {
  const body = await request.json().catch(() => null);
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new MockHttpError(422, 'validation_error');
  }
  return body as Record<string, unknown>;
}

function parseIntParam(value: string | null, fallback: number, field: string): number {
  if (value === null) {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) {
    throw new MockHttpError(422, 'validation_error', { field });
  }
  return parsed;
}

function requireIdempotencyKey(request: Request): string {
  const key = request.headers.get('Idempotency-Key');
  if (key === null || key.trim() === '') {
    throw new MockHttpError(422, 'validation_error', { field: 'idempotency_key' });
  }
  return key;
}

function requireExpectedVersion(body: Record<string, unknown>): number {
  const value = body['expected_version'];
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
    throw new MockHttpError(422, 'validation_error', { field: 'expected_version' });
  }
  return value;
}

interface ParsedUploadFile {
  readonly name: string;
  readonly size: number;
  readonly type: string;
  /** 内容 hash（FNV-1a over 原始字节；dedupe 依据，review C12）。 */
  readonly contentHash: string;
}

interface ParsedNewVersionParts {
  readonly file: ParsedUploadFile;
  readonly expectedVersion: number;
}

/**
 * 解析上传新版本的 multipart：同时包含 `file`（单文件）与 `expected_version` 表单字段。
 * 字节级解析与 parseUploadFiles 一致（undici 会抹掉 jsdom File 名）。
 */
async function parseNewVersionParts(request: Request): Promise<ParsedNewVersionParts> {
  const contentType = request.headers.get('Content-Type') ?? '';
  const boundaryMatch = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType);
  if (boundaryMatch === null) {
    throw new MockHttpError(422, 'validation_error', { field: 'file' });
  }
  const boundary = (boundaryMatch[1] ?? boundaryMatch[2] ?? '').trim();
  if (boundary === '') {
    throw new MockHttpError(422, 'validation_error', { field: 'file' });
  }
  const bytes = new Uint8Array(await request.arrayBuffer());
  const boundaryBytes = new TextEncoder().encode(`--${boundary}`);
  const headerEndMark = new TextEncoder().encode('\r\n\r\n');
  const crlf = new TextEncoder().encode('\r\n');
  const parts = parseMultipartParts(bytes, boundaryBytes, headerEndMark, crlf);
  const files = parts.filter(
    (part) => part.field === 'file' && part.filename !== null && part.filename !== '',
  );
  if (files.length !== 1) {
    throw new MockHttpError(422, 'validation_error', { field: 'file' });
  }
  const expectedVersionPart = parts.find((part) => part.field === 'expected_version');
  if (expectedVersionPart === undefined) {
    throw new MockHttpError(422, 'validation_error', { field: 'expected_version' });
  }
  const expectedVersion = Number(expectedVersionPart.text);
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) {
    throw new MockHttpError(422, 'validation_error', { field: 'expected_version' });
  }
  const file = files[0]!;
  return {
    file: {
      name: file.filename as string,
      size: file.size,
      type: file.type,
      contentHash: file.contentHash,
    },
    expectedVersion,
  };
}

interface MultipartPart {
  readonly field: string;
  readonly filename: string | null;
  readonly type: string;
  readonly size: number;
  readonly text: string;
  /** 原始字节内容 hash（FNV-1a 32-bit hex；二进制安全）。 */
  readonly contentHash: string;
}

/** FNV-1a 32-bit：对原始字节计算稳定内容指纹（dedupe 依据）。 */
function fnv1a(bytes: Uint8Array): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < bytes.length; index += 1) {
    hash ^= bytes[index]!;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

function parseMultipartParts(
  bytes: Uint8Array,
  boundaryBytes: Uint8Array,
  headerEndMark: Uint8Array,
  crlf: Uint8Array,
): MultipartPart[] {
  const parts: MultipartPart[] = [];
  let searchFrom = 0;
  while (true) {
    const partStart = indexOfSequence(bytes, boundaryBytes, searchFrom);
    if (partStart === -1) {
      break;
    }
    const partBodyStart = partStart + boundaryBytes.length;
    if (bytes[partBodyStart] === 0x2d && bytes[partBodyStart + 1] === 0x2d) {
      break;
    }
    const headerEnd = indexOfSequence(bytes, headerEndMark, partBodyStart + 2);
    if (headerEnd === -1) {
      break;
    }
    const headerText = new TextDecoder('utf-8').decode(bytes.slice(partBodyStart + 2, headerEnd));
    const field = /name="([^"]*)"/i.exec(headerText)?.[1] ?? '';
    const filename = /filename="([^"]*)"/i.exec(headerText)?.[1] ?? null;
    const type = /Content-Type:\s*([^\r\n]+)/i.exec(headerText)?.[1]?.trim() ?? '';
    // 部件体：headerEnd+4 起至下一个 `\r\n--boundary` 前；长度按字节计算
    let bodyEnd = headerEnd + 4;
    const nextBoundary = indexOfSequence(bytes, boundaryBytes, bodyEnd);
    if (nextBoundary !== -1) {
      bodyEnd = nextBoundary;
    }
    let size = bodyEnd - (headerEnd + 4);
    if (size >= crlf.length && sequenceEquals(bytes, bodyEnd - crlf.length, crlf)) {
      size -= crlf.length;
    }
    const text = new TextDecoder('utf-8').decode(bytes.slice(headerEnd + 4, headerEnd + 4 + size));
    const contentHash = fnv1a(bytes.slice(headerEnd + 4, headerEnd + 4 + size));
    parts.push({ field, filename, type, size: Math.max(0, size), text, contentHash });
    searchFrom = nextBoundary === -1 ? bytes.length : nextBoundary;
  }
  return parts;
}

/**
 * 手工解析 multipart 上传体。
 * MSW node（undici）对 jsdom FormData 请求体解析时会把 File 的 name 抹成 'blob'，
 * 因此不能依赖 request.formData() 的条目名；直接按字节读取 multipart：
 * - header 区按 UTF-8 解码（filename 可能含中文）；
 * - 部件体只计字节长度（不消费内容），避免二进制体解码。
 */
async function parseUploadFiles(request: Request): Promise<ParsedUploadFile[]> {
  const contentType = request.headers.get('Content-Type') ?? '';
  const boundaryMatch = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType);
  if (boundaryMatch === null) {
    throw new MockHttpError(422, 'validation_error', { field: 'files' });
  }
  const boundary = (boundaryMatch[1] ?? boundaryMatch[2] ?? '').trim();
  if (boundary === '') {
    throw new MockHttpError(422, 'validation_error', { field: 'files' });
  }
  const bytes = new Uint8Array(await request.arrayBuffer());
  const parts = parseMultipartParts(
    bytes,
    new TextEncoder().encode(`--${boundary}`),
    new TextEncoder().encode('\r\n\r\n'),
    new TextEncoder().encode('\r\n'),
  );
  const files = parts
    .filter((part) => part.field === 'files' && part.filename !== null && part.filename !== '')
    .map((part) => ({
      name: part.filename as string,
      size: part.size,
      type: part.type,
      contentHash: part.contentHash,
    }));
  if (files.length === 0) {
    throw new MockHttpError(422, 'validation_error', { field: 'files' });
  }
  return files;
}

function indexOfSequence(haystack: Uint8Array, needle: Uint8Array, from: number): number {
  outer: for (let index = from; index <= haystack.length - needle.length; index += 1) {
    for (let offset = 0; offset < needle.length; offset += 1) {
      if (haystack[index + offset] !== needle[offset]) {
        continue outer;
      }
    }
    return index;
  }
  return -1;
}

function sequenceEquals(haystack: Uint8Array, at: number, needle: Uint8Array): boolean {
  if (at < 0 || at + needle.length > haystack.length) {
    return false;
  }
  for (let offset = 0; offset < needle.length; offset += 1) {
    if (haystack[at + offset] !== needle[offset]) {
      return false;
    }
  }
  return true;
}

export function createKnowledgeHandlers(controller: MockKnowledgeController) {
  return [
    /* ---------- §6.2 文档列表 ---------- */

    http.get('/v1/spaces/:id/documents', ({ request, params }) => {
      try {
        const url = new URL(request.url);
        const q = url.searchParams.get('q') ?? undefined;
        const page = parseIntParam(url.searchParams.get('page'), 1, 'page');
        const pageSize = parseIntParam(url.searchParams.get('page_size'), 10, 'page_size');
        return HttpResponse.json(
          controller.listDocuments(
            request.headers.get('Authorization'),
            String(params['id']),
            q,
            page,
            pageSize,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.3 上传 ---------- */

    http.post('/v1/spaces/:id/documents', async ({ request, params }) => {
      try {
        const files = await parseUploadFiles(request);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.uploadDocuments(
            request.headers.get('Authorization'),
            String(params['id']),
            files,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.4 上传新版本 ---------- */

    http.post('/v1/documents/:id/versions', async ({ request, params }) => {
      try {
        const parts = await parseNewVersionParts(request);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.uploadNewVersion(
            request.headers.get('Authorization'),
            String(params['id']),
            parts.file,
            parts.expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.6 入库任务 ---------- */

    http.get('/v1/ingestion-jobs', ({ request }) => {
      try {
        const limit = parseIntParam(new URL(request.url).searchParams.get('limit'), 50, 'limit');
        return HttpResponse.json(controller.listJobs(request.headers.get('Authorization'), limit));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ingestion-jobs/:id/cancel', async ({ request, params }) => {
      try {
        // §6.7：cancel 不带请求 body 或 Idempotency-Key；携带时按幂等回放处理（兼容旧调用方）。
        const idempotencyKey = request.headers.get('Idempotency-Key') ?? '';
        controller.cancelJob(
          request.headers.get('Authorization'),
          String(params['id']),
          idempotencyKey,
        );
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ingestion-jobs/:id/replay', async ({ request, params }) => {
      try {
        const idempotencyKey = requireIdempotencyKey(request);
        const result = controller.replayJob(
          request.headers.get('Authorization'),
          String(params['id']),
          idempotencyKey,
        );
        return HttpResponse.json(result);
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.3.1 批次汇总 ---------- */

    http.get('/v1/upload-batches/:id', ({ request, params }) => {
      try {
        return HttpResponse.json(
          controller.getUploadBatch(request.headers.get('Authorization'), String(params['id'])),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.9 版本记录 ---------- */

    http.get('/v1/documents/:id/versions', ({ request, params }) => {
      try {
        return HttpResponse.json(
          controller.listVersions(request.headers.get('Authorization'), String(params['id'])),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/documents/:id/versions/:versionId/restore', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.restoreVersion(
            request.headers.get('Authorization'),
            String(params['id']),
            String(params['versionId']),
            expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/documents/:id/reindex', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.rebuildDocument(
            request.headers.get('Authorization'),
            String(params['id']),
            expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/documents/:id', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        controller.deleteDocument(
          request.headers.get('Authorization'),
          String(params['id']),
          expectedVersion,
          idempotencyKey,
        );
        return new HttpResponse(null, { status: 202 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.10 投稿 ---------- */

    http.get('/v1/submissions', ({ request }) => {
      try {
        const status = new URL(request.url).searchParams.get('status') ?? 'all';
        return HttpResponse.json(controller.listSubmissions(request.headers.get('Authorization'), status as never));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/submissions/:id/content', ({ request, params }) => {
      try {
        const content = controller.getSubmissionContent(
          request.headers.get('Authorization'),
          String(params['id']),
        );
        return new HttpResponse(content.bytes, {
          status: 200,
          headers: { 'Content-Type': content.type },
        });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/submissions/:id/withdraw', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.withdrawSubmission(
            request.headers.get('Authorization'),
            String(params['id']),
            expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/submissions/:id', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        controller.deleteSubmission(
          request.headers.get('Authorization'),
          String(params['id']),
          expectedVersion,
          idempotencyKey,
        );
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §8.1 / §8.4–8.5 部长部门库审核 ---------- */

    http.get('/v1/approvals/summary', ({ request }) => {
      try {
        return HttpResponse.json(controller.getApprovalSummary(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/approvals/submissions', ({ request }) => {
      try {
        return HttpResponse.json(controller.listApprovals(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/approvals/submissions/:id/approve', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.approveSubmission(
            request.headers.get('Authorization'),
            String(params['id']),
            expectedVersion,
            idempotencyKey,
          ),
          // §8.5：通过返回 202（已创建入库任务，文档初始版本成功后才进入目标空间）。
          { status: 202 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/approvals/submissions/:id/reject', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        const reason = typeof body['reason'] === 'string' ? body['reason'] : null;
        return HttpResponse.json(
          controller.rejectSubmission(
            request.headers.get('Authorization'),
            String(params['id']),
            expectedVersion,
            reason,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}
