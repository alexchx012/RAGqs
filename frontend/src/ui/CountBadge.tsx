/*
 * 计数徽标（共用基座 §5.6 顶行数量徽标）：mist-gray 底、radius-buttons pill、Sohne 12px 480。
 * 计数为 0 时不渲染。
 */

export interface CountBadgeProps {
  count: number;
  className?: string;
}

export function CountBadge({ count, className = '' }: CountBadgeProps) {
  if (count <= 0) {
    return null;
  }
  return (
    <span
      className={
        'inline-flex h-[18px] min-w-[18px] items-center justify-center ' +
        `rounded-[var(--radius-buttons)] bg-mist-gray px-1.5 text-[12px] font-w480 ` +
        `text-ink-black ${className}`
      }
    >
      {count}
    </span>
  );
}
