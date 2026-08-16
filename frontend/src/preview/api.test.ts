import { describe, expect, it, vi } from 'vitest';
import type { ApiClient } from '../api/client';
import { createPreviewApi } from './api';

function clientStub(): ApiClient {
  return {
    captureAuthSessionGuard: vi.fn(),
    request: vi.fn(),
  } as unknown as ApiClient;
}

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
