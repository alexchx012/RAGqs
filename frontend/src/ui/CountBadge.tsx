/*
 * 计数徽标（共用基座 §5.6 顶行数量徽标）：mist-gray 底、radius-buttons pill、Sohne 12px 480。
 * 计数为 0 时不渲染。
 */

export interface CountBadgeProps {
  count: number;
  /** warning：警告琥珀徽标（系统运维超时任务计数）；默认 mist 灰。 */
  intent?: 'neutral' | 'warning';
  className?: string;
}

const INTENT_CLASS: Record<NonNullable<CountBadgeProps['intent']>, string> = {
  neutral: 'bg-mist-gray text-ink-black',
  warning: 'bg-warning/15 text-warning',
};

export function CountBadge({ count, intent = 'neutral', className = '' }: CountBadgeProps) {
  if (count <= 0) {
    return null;
  }
  return (
    <span
      className={
        'inline-flex h-[18px] min-w-[18px] items-center justify-center ' +
        `rounded-[var(--radius-buttons)] px-1.5 text-[12px] font-w480 ` +
        `${INTENT_CLASS[intent]} ${className}`
      }
    >
      {count}
    </span>
  );
}
