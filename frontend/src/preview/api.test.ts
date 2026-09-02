import { describe, expect, it, vi } from 'vitest';
import type { ApiClient, BlobApiRequestOptions, JsonApiRequestOptions } from '../api/client';
import { createPreviewApi } from './api';

/** request double 签名：显式带 options 形参，便于对调用参数断言（对齐 settings/api.test.ts 先例）。 */
type ApiRequestDouble = (
  path: string,
  options?: JsonApiRequestOptions | BlobApiRequestOptions,
) => Promise<unknown>;

function clientStub(): ApiClient {
  return {
    captureAuthSessionGuard: vi.fn(),
    request: vi.fn(),
  } as unknown as ApiClient;
}

describe('原件 blob 下载显式大超时（A17）', () => {
  it('getTextContent / getImageContent：timeoutMs 显式 120s（不走基座默认 10s）', async () => {
    const request = vi.fn<ApiRequestDouble>(async () => new Blob(['x']));
    const api = createPreviewApi({
      captureAuthSessionGuard: vi.fn(),
      request,
    } as unknown as ApiClient);
    await api.getTextContent('doc_1');
    await api.getImageContent('doc_2');
    expect(request.mock.calls[0]?.[1]).toMatchObject({ responseType: 'blob', timeoutMs: 120_000 });
    expect(request.mock.calls[1]?.[1]).toMatchObject({ responseType: 'blob', timeoutMs: 120_000 });
  });
});

describe('预览内容 URL', () => {
  const api = createPreviewApi(clientStub());

  it('保留受支持相对内容端点的既有查询参数并追加版本', () => {
    const url = new URL(api.buildContentUrl('/v1/documents/doc_1/content?sheet=Summary', 'ver_2'));

    expect(url.pathname).toBe('/v1/documents/doc_1/content');
    expect(url.searchParams.get('sheet')).toBe('Summary');
    expect(url.searchParams.get('document_version_id')).toBe('ver_2');
  });

  it('保留服务端已选择的版本', () => {
    expect(
      api.buildContentUrl('/v1/documents/doc_1/content?document_version_id=v_0', 'v_stale'),
    ).toBe(
      new URL(
        '/v1/documents/doc_1/content?document_version_id=v_0',
        globalThis.location.origin,
      ).toString(),
    );
  });

  it('拒绝外部内容 URL，而不是拼接为畸形的应用内路径', () => {
    expect(() => api.buildContentUrl('https://files.example.test/document.pdf', 'ver_2')).toThrow(
      'application-relative',
    );
  });
});
