/*
 * 相对时间展示测试（契约 §1）：固定 now，断言分钟 / 小时 / 天分档与「刚刚」，
 * 未来时间按 0 处理；文案一律引用 copy.notifications.relative。
 */

import { describe, expect, it } from 'vitest';
import { copy } from '../copy';
import { formatRelativeTime } from './relative-time';

const NOW = new Date('2026-06-20T12:00:00.000Z');

function isoBefore(ms: number): string {
  return new Date(NOW.getTime() - ms).toISOString();
}

describe('相对时间', () => {
  it('不足 1 分钟显示「刚刚」', () => {
    expect(formatRelativeTime(isoBefore(30_000), NOW)).toBe(copy.notifications.relative.justNow);
  });

  it('分钟级', () => {
    expect(formatRelativeTime(isoBefore(60_000), NOW)).toBe(copy.notifications.relative.minutes(1));
    expect(formatRelativeTime(isoBefore(5 * 60_000), NOW)).toBe(
      copy.notifications.relative.minutes(5),
    );
    expect(formatRelativeTime(isoBefore(59 * 60_000), NOW)).toBe(
      copy.notifications.relative.minutes(59),
    );
  });

  it('小时级', () => {
    expect(formatRelativeTime(isoBefore(3_600_000), NOW)).toBe(copy.notifications.relative.hours(1));
    expect(formatRelativeTime(isoBefore(3 * 3_600_000), NOW)).toBe(
      copy.notifications.relative.hours(3),
    );
  });

  it('天级', () => {
    expect(formatRelativeTime(isoBefore(86_400_000), NOW)).toBe(copy.notifications.relative.days(1));
    expect(formatRelativeTime(isoBefore(2 * 86_400_000), NOW)).toBe(
      copy.notifications.relative.days(2),
    );
  });

  it('未来时间按 0 处理为「刚刚」', () => {
    const future = new Date(NOW.getTime() + 60_000).toISOString();
    expect(formatRelativeTime(future, NOW)).toBe(copy.notifications.relative.justNow);
  });
});
