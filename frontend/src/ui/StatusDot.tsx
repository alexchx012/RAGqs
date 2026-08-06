/*
 * 状态点（共用基座 §5.6 状态列、§3.4 分档状态、§5.7 结果卡状态标签）。
 * 6px 圆点；intent 映射工程 token 类；可选脉冲（opacity 0.4↔1 1000ms，keyframes 在 ui.css）。
 */

export type StatusDotIntent = 'ink' | 'slate' | 'success' | 'warning' | 'danger';

const INTENT_CLASS: Record<StatusDotIntent, string> = {
  ink: 'bg-ink-black',
  slate: 'bg-slate-gray',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
};

export interface StatusDotProps {
  intent: StatusDotIntent;
  /** 处理中等进行中状态：脉冲动画（共用基座 §2.5 循环动画表）。 */
  pulse?: boolean;
  className?: string;
}

export function StatusDot({ intent, pulse = false, className = '' }: StatusDotProps) {
  return (
    <span
      aria-hidden="true"
      className={
        `inline-block h-1.5 w-1.5 rounded-full ${INTENT_CLASS[intent]} ` +
        `${pulse ? 'ui-status-pulse' : ''} ${className}`
      }
    />
  );
}
