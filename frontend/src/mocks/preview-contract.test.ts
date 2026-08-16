import { describe, expect, it } from 'vitest';
import { resolveUrl } from '../api/client';
import type { DocumentPreviewResponse, SheetContentResponse } from '../preview/types';
import { MockHttpError, PREVIEW_SEED } from './preview-contract';
import { mockAuth, mockPreview } from './testing';

/*
 * 原文预览契约直测（fe-doc-preview；§4）：
 * - message_id 有无决定 hits 行为（携带=该次回答全部 hits；不携带=管理侧只读空 hits）；
 * - document_version_id 透传（历史版本内容/hits、purged 410、未知版本 404）；
 * - Excel/CSV 按 Sheet 的 JSON 行列数据（默认首签、?sheet=、未知 Sheet 404）；
 * - 不可用态：文档删除 410 document_unavailable（preview 与 content 一致）；
 * - Range 分段（经 MSW handler 实测 HTTP 层）：206 + Content-Range + Accept-Ranges / 416 / HEAD；
 * - 鉴权：无 Bearer 401。
 */

function bearerOf(username = 'zhangsan', password = 'password123'): string {
  const { accessToken } = mockAuth.login(username, password, 'vitest');
  return `Bearer ${accessToken}`;
}

function expectHttpError(fn: () => unknown, status: number, code: string): void {
  try {
    fn();
  } catch (error) {
    expect(error).toBeInstanceOf(MockHttpError);
    const httpError = error as MockHttpError;
    expect(httpError.status).toBe(status);
    expect(httpError.code).toBe(code);
    return;
  }
  throw new Error(`expected MockHttpError ${status} ${code}`);
}

describe('GET /documents/{id}/preview：message_id 行为', () => {
  it('不携带 message_id：hits 为空（管理侧只读形态），元数据照常返回', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_1', {});
    expect(preview.hits).toEqual([]);
    expect(preview.name).toBe('员工手册.pdf');
    expect(preview.media_kind).toBe('pdf');
    expect(preview.document_version_id).toBe('v_1');
    expect(preview.size_bytes).toBeGreaterThan(0);
    expect(preview.content_available).toBe(true);
    expect(preview.has_text_layer).toBe(true);
    expect(preview.page_count).toBe(2);
    expect(preview.content_url).toBe('/v1/documents/doc_1/content?document_version_id=v_1');
  });

  it('携带 message_id：返回该次回答引用本文档的全部 hits（含 span 消歧数据）', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_1', { messageId: 'm_1' });
    expect(preview.hits.length).toBe(2);
    const [first, second] = preview.hits;
    expect(first).toMatchObject({ index: 1, snippet: '5 days per year', locator: { page: 1, span: { start: 30, end: 45 } } });
    expect(second).toMatchObject({ index: 2, snippet: 'medical certificate', locator: { page: 2 } });
  });

  it('空或不可读的 message_id 按服务端契约拒绝', () => {
    expectHttpError(() => mockPreview.getPreview(bearerOf(), 'doc_1', { messageId: '' }), 422, 'validation_error');
    expectHttpError(
      () => mockPreview.getPreview(bearerOf(), 'doc_1', { messageId: 'm_missing' }),
      404,
      'message_not_found',
    );
  });

  it('其他用户不能读取另一用户的消息上下文', () => {
    expectHttpError(
      () => mockPreview.getPreview(bearerOf('minister-li'), 'doc_1', { messageId: 'm_1' }),
      404,
      'message_not_found',
    );
  });

  it('没有 processing summary 时仍返回完整 preview wire shape 的默认元数据', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_word_basic', {});

    expect(preview).toMatchObject({
      document_id: 'doc_word_basic',
      document_version_id: 'vwb_1',
      size_bytes: 1024,
      content_available: true,
      content_url: '/v1/documents/doc_word_basic/content?document_version_id=vwb_1',
      has_text_layer: false,
      tree_indexed: false,
      page_count: null,
      sheets: null,
    });
  });

  it('已知消息不会为未引用的文档虚构 hits', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_word', { messageId: 'm_1' });

    expect(preview.hits).toEqual([]);
  });

  it('建树 Word 返回原始文件大小，而不是渲染 JSON 的长度', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_word', { messageId: 'm_1' });

    expect(preview.size_bytes).toBe(1536);
  });

  it('未知文档：404 document_not_found', () => {
    expectHttpError(() => mockPreview.getPreview(bearerOf(), 'doc_nope', {}), 404, 'document_not_found');
  });
});

describe('document_version_id 透传', () => {
  it('历史版本：preview hits 与 content 均为该版本', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_1', { messageId: 'm_1', documentVersionId: 'v_0' });
    expect(preview.document_version_id).toBe('v_0');
    expect(preview.content_url).toBe('/v1/documents/doc_1/content?document_version_id=v_0');
    expect(preview.hits.length).toBe(1);
    expect(preview.hits[0]?.summary).toBe('4 days per year');
    const content = mockPreview.getContent(bearerOf(), 'doc_1', { documentVersionId: 'v_0' });
    const text = new TextDecoder().decode(content.body);
    expect(text).toContain('Legacy handbook text.');
    expect(text).not.toContain('5 days per year');
  });

  it('省略 document_version_id：读当前 active 版本', () => {
    const content = mockPreview.getContent(bearerOf(), 'doc_1', {});
    expect(new TextDecoder().decode(content.body)).toContain('5 days per year');
  });

  it('未知版本：404 document_version_not_found', () => {
    expectHttpError(
      () => mockPreview.getPreview(bearerOf(), 'doc_1', { documentVersionId: 'v_nope' }),
      404,
      'document_version_not_found',
    );
  });

  it('purged 版本：410 document_version_unavailable（preview 与 content 一致）', () => {
    expectHttpError(
      () => mockPreview.getPreview(bearerOf(), 'doc_1', { documentVersionId: 'v_purged' }),
      410,
      'document_version_unavailable',
    );
    expectHttpError(
      () => mockPreview.getContent(bearerOf(), 'doc_1', { documentVersionId: 'v_purged' }),
      410,
      'document_version_unavailable',
    );
  });
});

describe('Excel/CSV Sheet 数据', () => {
  it('省略 sheet：默认第一页签（源 Sheet 名原样返回）', () => {
    const content = mockPreview.getContent(bearerOf(), PREVIEW_SEED.excelDocId, {});
    const payload = JSON.parse(new TextDecoder().decode(content.body)) as SheetContentResponse;
    expect(payload.sheet).toBe(PREVIEW_SEED.excelSheetQ1);
    expect(payload.row_count).toBe(5);
    expect(payload.rows[0]).toEqual(['项目', '金额', '状态']);
  });

  it('?sheet= 切换：返回目标 Sheet 行列', () => {
    const content = mockPreview.getContent(bearerOf(), PREVIEW_SEED.excelDocId, { sheet: PREVIEW_SEED.excelSheetQ2 });
    const payload = JSON.parse(new TextDecoder().decode(content.body)) as SheetContentResponse;
    expect(payload.sheet).toBe(PREVIEW_SEED.excelSheetQ2);
    expect(payload.rows[1]?.[0]).toBe(PREVIEW_SEED.excelQ2FirstCell);
  });

  it('未知 Sheet：404 sheet_not_found', () => {
    expectHttpError(
      () => mockPreview.getContent(bearerOf(), PREVIEW_SEED.excelDocId, { sheet: 'nope' }),
      404,
      'sheet_not_found',
    );
  });

  it('空 Query 参数在解析文档前按契约拒绝', () => {
    expectHttpError(
      () => mockPreview.getPreview(bearerOf(), 'doc_nope', { messageId: '' }),
      422,
      'validation_error',
    );
    expectHttpError(
      () => mockPreview.getPreview(bearerOf(), 'doc_nope', { documentVersionId: '' }),
      422,
      'validation_error',
    );
    expectHttpError(
      () => mockPreview.getContent(bearerOf(), 'doc_nope', { documentVersionId: '' }),
      422,
      'validation_error',
    );
  });

  it('空 Sheet 参数按服务端 Query 校验返回 422', () => {
    expectHttpError(
      () => mockPreview.getContent(bearerOf(), PREVIEW_SEED.excelDocId, { sheet: '' }),
      422,
      'validation_error',
    );
  });

  it('空 Sheet 参数在解析文档前按 Query 契约拒绝', () => {
    expectHttpError(
      () => mockPreview.getContent(bearerOf(), 'doc_nope', { sheet: '' }),
      422,
      'validation_error',
    );
  });

  it('CSV：固定唯一虚拟 Sheet CSV', () => {
    const preview = mockPreview.getPreview(bearerOf(), 'doc_csv', { messageId: 'm_1' });
    expect(preview.sheets).toEqual([{ name: 'CSV', row_count: 3 }]);
    expect(preview.size_bytes).toBe(128);
    const content = mockPreview.getContent(bearerOf(), 'doc_csv', { sheet: 'CSV' });
    const payload = JSON.parse(new TextDecoder().decode(content.body)) as SheetContentResponse;
    expect(payload.rows.length).toBe(3);
  });
});

describe('不可用态', () => {
  it('文档已删除：preview 与 content 均 410 document_unavailable（不下发任何元数据）', () => {
    expectHttpError(() => mockPreview.getPreview(bearerOf(), PREVIEW_SEED.goneDocId, {}), 410, 'document_unavailable');
    expectHttpError(() => mockPreview.getContent(bearerOf(), PREVIEW_SEED.goneDocId, {}), 410, 'document_unavailable');
  });

  it('无 Bearer：401 invalid_token', () => {
    expectHttpError(() => mockPreview.getPreview(null, 'doc_1', {}), 401, 'invalid_token');
  });
});

describe('Range 分段（HTTP 层，经 MSW handler）', () => {
  async function fetchContent(headers: Record<string, string>, method = 'GET'): Promise<Response> {
    return fetch(resolveUrl('/v1/documents/doc_1/content'), { method, headers });
  }

  it('bytes 区间：206 + Content-Range + Accept-Ranges + 精确字节', async () => {
    const full = await fetchContent({ Authorization: bearerOf() });
    expect(full.status).toBe(200);
    expect(full.headers.get('Accept-Ranges')).toBe('bytes');
    const fullBytes = new Uint8Array(await full.arrayBuffer());
    expect(fullBytes.length).toBeGreaterThan(200);
    // 种子 PDF 为真实可解析字节（%PDF 头）
    expect(new TextDecoder().decode(fullBytes.slice(0, 8))).toBe('%PDF-1.4');

    const ranged = await fetchContent({ Authorization: bearerOf(), Range: 'bytes=0-99' });
    expect(ranged.status).toBe(206);
    expect(ranged.headers.get('Content-Range')).toBe(`bytes 0-99/${fullBytes.length}`);
    const slice = new Uint8Array(await ranged.arrayBuffer());
    expect(slice.length).toBe(100);
    expect(slice).toEqual(fullBytes.slice(0, 100));
  });

  it('后缀区间与开放区间', async () => {
    const suffix = await fetchContent({ Authorization: bearerOf(), Range: 'bytes=-10' });
    expect(suffix.status).toBe(206);
    const suffixBytes = new Uint8Array(await suffix.arrayBuffer());
    expect(suffixBytes.length).toBe(10);

    const open = await fetchContent({ Authorization: bearerOf(), Range: 'bytes=5-' });
    expect(open.status).toBe(206);
    expect(open.headers.get('Content-Range')).toMatch(/^bytes 5-\d+\/\d+$/);
  });

  it('越界 / 非法区间：416 + Content-Range bytes */size', async () => {
    const beyond = await fetchContent({ Authorization: bearerOf(), Range: 'bytes=99999999-' });
    expect(beyond.status).toBe(416);
    expect(beyond.headers.get('Content-Range')).toMatch(/^bytes \*\/\d+$/);
    const malformed = await fetchContent({ Authorization: bearerOf(), Range: 'bytes=-' });
    expect(malformed.status).toBe(416);
  });

  it('HEAD：200 同头空体（pdfjs 预检分段能力）', async () => {
    const head = await fetchContent({ Authorization: bearerOf() }, 'HEAD');
    expect(head.status).toBe(200);
    expect(head.headers.get('Accept-Ranges')).toBe('bytes');
    expect(Number(head.headers.get('Content-Length'))).toBeGreaterThan(200);
    expect(await head.arrayBuffer()).toEqual(new ArrayBuffer(0));

    const rangedHead = await fetchContent({ Authorization: bearerOf(), Range: 'bytes=0-3' }, 'HEAD');
    expect(rangedHead.status).toBe(206);
    expect(rangedHead.headers.get('Content-Range')).toMatch(/^bytes 0-3\/\d+$/);
    expect(rangedHead.headers.get('Content-Length')).toBe('4');
    expect(await rangedHead.arrayBuffer()).toEqual(new ArrayBuffer(0));
  });

  it('Range 与 HEAD 只按服务端支持的媒体类型开放', async () => {
    const textRange = await fetch(resolveUrl('/v1/documents/doc_md/content'), {
      headers: { Authorization: bearerOf(), Range: 'bytes=0-3' },
    });
    expect(textRange.status).toBe(200);
    expect(textRange.headers.get('Accept-Ranges')).toBeNull();

    const textHead = await fetch(resolveUrl('/v1/documents/doc_md/content'), {
      method: 'HEAD',
      headers: { Authorization: bearerOf() },
    });
    expect(textHead.status).toBe(405);
    expect(textHead.headers.get('Allow')).toBe('GET');

    const imageHead = await fetch(resolveUrl('/v1/documents/doc_img/content'), {
      method: 'HEAD',
      headers: { Authorization: bearerOf() },
    });
    expect(imageHead.status).toBe(200);
    expect(imageHead.headers.get('Accept-Ranges')).toBeNull();
  });

  it('错误对象形态统一（HTTP 层）', async () => {
    const response = await fetch(resolveUrl(`/v1/documents/${PREVIEW_SEED.goneDocId}/preview`), {
      headers: { Authorization: bearerOf() },
    });
    expect(response.status).toBe(410);
    const body = (await response.json()) as { error: { code: string; details: Record<string, unknown>; request_id: string } };
    expect(body.error.code).toBe('document_unavailable');
    expect(typeof body.error.request_id).toBe('string');
    expect(body.error.details).toEqual({});
  });

  it('preview 响应经 HTTP 层与契约形状一致', async () => {
    const response = await fetch(resolveUrl('/v1/documents/doc_1/preview?message_id=m_1&document_version_id=v_1'), {
      headers: { Authorization: bearerOf() },
    });
    expect(response.status).toBe(200);
    const body = (await response.json()) as DocumentPreviewResponse;
    expect(body.document_id).toBe('doc_1');
    expect(body.hits.length).toBe(2);
  });
});
