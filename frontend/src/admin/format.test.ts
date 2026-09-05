/*
 * 管理面板展示格式化测试：formatBytes 分档（审查 A12a 补 GB 档）。
 */

import { describe, expect, it } from 'vitest';
import { formatBytes } from './format';

describe('formatBytes', () => {
  it('B / KB / MB 分档', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(1536 * 1024)).toBe('1.5 MB');
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB');
  });

  it('GB 档（审查 A12a）：≥1GB 显示 x.x GB', () => {
    expect(formatBytes(1024 ** 3)).toBe('1.0 GB');
    expect(formatBytes(2.5 * 1024 ** 3)).toBe('2.5 GB');
  });
});
