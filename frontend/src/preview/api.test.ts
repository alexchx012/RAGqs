import { describe, expect, it, vi } from 'vitest';
import type { ApiClient } from '../api/client';
import { createPreviewApi } from './api';

function clientStub(): ApiClient {
  return {
    captureAuthSessionGuard: vi.fn(),
    request: vi.fn(),
  } as unknown as ApiClient;
}

describe('PreviewApi content URL', () => {
  it('keeps the server-selected version in a qualified content URL', () => {
    const api = createPreviewApi(clientStub());

    expect(
      api.buildContentUrl('/documents/doc_1/content?document_version_id=v_0', 'v_stale'),
    ).toBe(new URL('/v1/documents/doc_1/content?document_version_id=v_0', globalThis.location.origin).toString());
  });
});
