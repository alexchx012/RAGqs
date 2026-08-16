import { describe, expect, it } from 'vitest';
import type { ApiClient } from '../api/client';
import { createPreviewApi } from './api';

describe('预览内容 URL', () => {
  const api = createPreviewApi({} as ApiClient);

  it('保留受支持相对内容端点的既有查询参数并追加版本', () => {
    const url = new URL(api.buildContentUrl('/documents/doc_1/content?sheet=Summary', 'ver_2'));

    expect(url.pathname).toBe('/v1/documents/doc_1/content');
    expect(url.searchParams.get('sheet')).toBe('Summary');
    expect(url.searchParams.get('document_version_id')).toBe('ver_2');
  });

  it('拒绝外部内容 URL，而不是拼接为畸形的应用内路径', () => {
    expect(() => api.buildContentUrl('https://files.example.test/document.pdf', 'ver_2')).toThrow(
      'application-relative',
    );
  });
});
