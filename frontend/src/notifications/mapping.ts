/*
 * 提醒类型映射（契约 §13；共用基座 §4 提醒类型表逐行一致，两处不得只改一处）。
 * - 图标着色为前端映射，不随接口下发：成功绿 / 警告琥珀 / 危险红 / 墨色（功能色仅小面积）。
 * - graph_build_completed 按 payload.status 映射：succeeded 成功绿 / failed 危险红 / cancelled slate。
 * - 未知 type（§1 未知枚举兜底）：保留条目，后端 title + 通用图标，不展示机读值，不崩溃。
 * - 跳转目标落到抽屉对应层（路径段式 URL）；目标模块未注册时由抽屉落首层占位（规格 §3）。
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
  Siren,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import type { GraphBuildPayload, NotificationItem } from './types';

export type NotificationIntent = 'success' | 'warning' | 'danger' | 'ink' | 'slate';

export interface NotificationMapping {
  readonly icon: LucideIcon;
  readonly intent: NotificationIntent;
  /** 点击跳转的抽屉路径；null 表示无跳转目标（未知 type 不导航）。 */
  readonly target: string | null;
}

export const NOTIFICATION_INTENT_CLASS: Record<NotificationIntent, string> = {
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  ink: 'text-ink-black',
  slate: 'text-slate-gray',
};

const KNOWN_MAPPINGS: Record<string, NotificationMapping> = {
  ingestion_completed: {
    icon: FileCheck2,
    intent: 'success',
    target: '/settings/knowledge/uploads',
  },
  ocr_low_confidence: {
    icon: AlertTriangle,
    intent: 'warning',
    target: '/settings/knowledge/uploads',
  },
  quota_approved: {
    icon: CheckCircle2,
    intent: 'success',
    target: '/settings/knowledge',
  },
  quota_rejected: {
    icon: XCircle,
    intent: 'danger',
    target: '/settings/knowledge',
  },
  submission_approved: {
    icon: MailCheck,
    intent: 'success',
    target: '/settings/knowledge/submissions',
  },
  submission_rejected: {
    icon: MailX,
    intent: 'danger',
    target: '/settings/knowledge/submissions',
  },
  submission_invalidated: {
    icon: MailWarning,
    intent: 'warning',
    target: '/settings/knowledge/submissions',
  },
  calibration_window_suggested: {
    icon: FlaskConical,
    intent: 'ink',
    target: '/admin/evaluation',
  },
  graph_build_completed: {
    icon: Network,
    intent: 'success', // 实际着色按 payload.status 在 resolveNotificationMapping 再判定
    target: '/admin/spaces/public',
  },
  evaluation_judge_configuration_missing: {
    icon: Siren,
    intent: 'danger',
    target: '/admin/evaluation',
  },
};

const UNKNOWN_MAPPING: NotificationMapping = { icon: Bell, intent: 'slate', target: null };

/** 单条提醒的呈现映射；未知 type 走通用兜底，不读机读值、不崩溃。 */
export function resolveNotificationMapping(
  item: Pick<NotificationItem, 'type' | 'payload'>,
): NotificationMapping {
  if (item.type === 'graph_build_completed') {
    const status = (item.payload as Partial<GraphBuildPayload>).status;
    const intent: NotificationIntent =
      status === 'succeeded' ? 'success' : status === 'failed' ? 'danger' : 'slate';
    return { ...KNOWN_MAPPINGS['graph_build_completed'], intent };
  }
  return KNOWN_MAPPINGS[item.type] ?? UNKNOWN_MAPPING;
}
