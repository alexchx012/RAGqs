import { describe, expect, it } from 'vitest';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import { ApiError } from '../api/errors';

describe('idempotency scope（review A3）', () => {
  it('同 op+target+payload 复用同键；target 或 payload 变化换新键（不跨文档/投稿复用）', () => {
    const scope = createIdempotencyScope();
    const first = scope.keyFor('upload-new-version', 'doc_A', 'a.pdf:100:1');
    const second = scope.keyFor('upload-new-version', 'doc_A', 'a.pdf:100:1');
    expect(second).toBe(first);

    // 换 target（A 文档 → B 文档）：换键
    const otherTarget = scope.keyFor('upload-new-version', 'doc_B', 'a.pdf:100:1');
    expect(otherTarget).not.toBe(first);

    // 同 target 换 payload（文件内容变化）：换键
    const otherPayload = scope.keyFor('upload-new-version', 'doc_B', 'b.pdf:200:2');
    expect(otherPayload).not.toBe(otherTarget);

    // 换 op：换键
    const otherOp = scope.keyFor('delete-document', 'doc_B', 'b.pdf:200:2');
    expect(otherOp).not.toBe(otherPayload);
  });

  it('业务响应清键后下次操作拿新键；clear 后同样换键', () => {
    const scope = createIdempotencyScope();
    const first = scope.keyFor('quota-request', 'me', '100');
    scope.businessResponse();
    const afterBusiness = scope.keyFor('quota-request', 'me', '100');
    expect(afterBusiness).not.toBe(first);

    const again = scope.keyFor('quota-request', 'me', '100');
    scope.clear();
    const afterClear = scope.keyFor('quota-request', 'me', '100');
    expect(afterClear).not.toBe(again);
  });

  it('isBusinessResponse：status null（网络未知/超时）为 false，有 status 为 true', () => {
    expect(isBusinessResponse(null)).toBe(false);
    expect(isBusinessResponse(new Error('network offline'))).toBe(false);
    expect(isBusinessResponse(new ApiError({ status: null, code: 'timeout', message: '', details: {}, requestId: null }))).toBe(false);
    expect(isBusinessResponse(new ApiError({ status: 409, code: 'idempotency_key_conflict', message: '', details: {}, requestId: null }))).toBe(true);
    expect(isBusinessResponse(new ApiError({ status: 422, code: 'validation_error', message: '', details: {}, requestId: null }))).toBe(true);
  });
});
