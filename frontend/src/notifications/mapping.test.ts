/*
 * 提醒类型映射测试（契约 §13）：九类已知类型的图标 / 着色 / 跳转目标，
 * graph_build_completed 按 payload.status 着色，未知类型通用兜底不崩溃，
 * NOTIFICATION_INTENT_CLASS 五键齐全。
 */

import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  FileCheck2,
  FlaskConical,
  MailCheck,
  MailWarning,
  MailX,
  Network,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import { describe, expect, it } from 'vitest';
import {
  NOTIFICATION_INTENT_CLASS,
  resolveNotificationMapping,
  type NotificationIntent,
} from './mapping';

const KNOWN_CASES: ReadonlyArray<{
  type: string;
  icon: LucideIcon;
  intent: NotificationIntent;
  target: string;
}> = [
  { type: 'ingestion_completed', icon: FileCheck2, intent: 'success', target: '/settings/knowledge/uploads' },
  { type: 'ocr_low_confidence', icon: AlertTriangle, intent: 'warning', target: '/settings/knowledge/uploads' },
  { type: 'quota_approved', icon: CheckCircle2, intent: 'success', target: '/settings/knowledge' },
  { type: 'quota_rejected', icon: XCircle, intent: 'danger', target: '/settings/knowledge' },
  { type: 'submission_approved', icon: MailCheck, intent: 'success', target: '/settings/knowledge/submissions' },
  { type: 'submission_rejected', icon: MailX, intent: 'danger', target: '/settings/knowledge/submissions' },
  { type: 'submission_invalidated', icon: MailWarning, intent: 'warning', target: '/settings/knowledge/submissions' },
  { type: 'calibration_window_suggested', icon: FlaskConical, intent: 'ink', target: '/admin/evaluation' },
  { type: 'graph_build_completed', icon: Network, intent: 'success', target: '/admin/spaces/public' },
];

describe('提醒类型映射', () => {
  for (const { type, icon, intent, target } of KNOWN_CASES) {
    it(`已知类型 ${type}：图标 / 着色 / 跳转目标`, () => {
      const payload = type === 'graph_build_completed' ? { status: 'succeeded' } : {};
      const mapping = resolveNotificationMapping({ type, payload });
      expect(mapping.icon).toBe(icon);
      expect(mapping.intent).toBe(intent);
      expect(mapping.target).toBe(target);
    });
  }

  it('graph_build_completed 按 payload.status 着色：succeeded 成功绿 / failed 危险红 / cancelled slate', () => {
    const succeeded = resolveNotificationMapping({
      type: 'graph_build_completed',
      payload: { graph_build_id: 'gb_1', status: 'succeeded' },
    });
    expect(succeeded.icon).toBe(Network);
    expect(succeeded.intent).toBe('success');
    expect(succeeded.target).toBe('/admin/spaces/public');

    const failed = resolveNotificationMapping({
      type: 'graph_build_completed',
      payload: { graph_build_id: 'gb_2', status: 'failed' },
    });
    expect(failed.intent).toBe('danger');

    const cancelled = resolveNotificationMapping({
      type: 'graph_build_completed',
      payload: { graph_build_id: 'gb_3', status: 'cancelled' },
    });
    expect(cancelled.intent).toBe('slate');
  });

  it('未知类型走通用兜底：Bell 图标 / slate / 无跳转目标，不抛错', () => {
    const mapping = resolveNotificationMapping({
      type: 'deep_research_completed',
      payload: { research_id: 'rs_1' },
    });
    expect(mapping.icon).toBe(Bell);
    expect(mapping.intent).toBe('slate');
    expect(mapping.target).toBeNull();
  });

  it('NOTIFICATION_INTENT_CLASS 五键齐全', () => {
    expect(NOTIFICATION_INTENT_CLASS).toEqual({
      success: 'text-success',
      warning: 'text-warning',
      danger: 'text-danger',
      ink: 'text-ink-black',
      slate: 'text-slate-gray',
    });
  });
});
