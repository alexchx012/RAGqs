/*
 * 相对时间展示（契约 §1：时间一律 ISO 8601 UTC，前端自行转相对时间）。
 * 文案走单一文案常量文件（copy.notifications.relative）。
 */

import { copy } from '../copy';

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

export function formatRelativeTime(isoUtc: string, now: Date = new Date()): string {
  const occurred = new Date(isoUtc).getTime();
  const delta = Math.max(0, now.getTime() - occurred);
  if (delta < MINUTE_MS) {
    return copy.notifications.relative.justNow;
  }
  if (delta < HOUR_MS) {
    return copy.notifications.relative.minutes(Math.floor(delta / MINUTE_MS));
  }
  if (delta < DAY_MS) {
    return copy.notifications.relative.hours(Math.floor(delta / HOUR_MS));
  }
  return copy.notifications.relative.days(Math.floor(delta / DAY_MS));
}
