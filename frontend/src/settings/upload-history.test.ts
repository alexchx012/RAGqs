import { describe, expect, it, vi } from 'vitest';
import { clearUploadHistory, readUploadHistory, recordUploadHistory, subscribeUploadHistory } from './upload-history';

function entry(name: string) {
  return {
    response: {
      upload_batch_id: 'ub_1',
      items: [
        {
          accepted: true as const,
          name,
          space_id: 'personal:u_user',
          document_id: 'doc_1',
          document_version_id: 'ver_1',
          job_id: 'job_1',
          publication_id: 'pub_1',
          deduplicated: false,
          status: 'pending',
        },
      ],
    },
    target: null,
    at: '2026-08-09T00:00:00Z',
  };
}

describe('upload-history（review A2：按 auth session 隔离）', () => {
  it('不同 sessionKey 互不覆盖；null session 拒绝落库', () => {
    clearUploadHistory();
    expect(recordUploadHistory(entry('A'), 'sess1:u_user')).toBe(true);
    expect(recordUploadHistory(entry('B'), 'sess2:u_user')).toBe(true);
    expect(readUploadHistory('sess1:u_user')?.response.items[0]).toMatchObject({ name: 'A' });
    expect(readUploadHistory('sess2:u_user')?.response.items[0]).toMatchObject({ name: 'B' });
    // 未认证（null）写入被拒
    expect(recordUploadHistory(entry('C'), null)).toBe(false);
    expect(readUploadHistory(null)).toBeNull();
  });

  it('旧会话异步回调不覆盖新会话槽位；订阅在写入后通知重读', () => {
    clearUploadHistory();
    const listener = vi.fn();
    const unsubscribe = subscribeUploadHistory(listener);
    // 旧会话响应落地时用当前 sessionKey 写入：先写 sessA，再写 sessB
    expect(recordUploadHistory(entry('old'), 'sessA:u_user')).toBe(true);
    expect(recordUploadHistory(entry('new'), 'sessB:u_user')).toBe(true);
    expect(listener).toHaveBeenCalledTimes(2);
    // 会话 A 的槽位不被 B 覆盖
    expect(readUploadHistory('sessA:u_user')?.response.items[0]).toMatchObject({ name: 'old' });
    unsubscribe();
  });
});
