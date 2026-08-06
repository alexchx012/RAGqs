/*
 * 骨架（共用基座 §2.5 循环动画、§3.2/§5.6/§5.7 加载态：骨架屏优先于 spinner）。
 * 呼吸动画类 ui-skeleton 在 styles/ui.css（opacity 0.6↔1 1200ms --ease-in-out 无限交替）。
 */

export interface SkeletonRowProps {
  className?: string;
}

/** 列表骨架行：高 40px mist-gray 底，圆角 radius-images（共用基座 §3.2/§5.6）。 */
export function SkeletonRow({ className = '' }: SkeletonRowProps) {
  return (
    <div
      aria-hidden="true"
      className={`ui-skeleton h-10 rounded-[var(--radius-images)] bg-mist-gray ${className}`}
    />
  );
}

export interface SkeletonTextProps {
  /** 行条宽（CSS 长度或百分比），默认 '100%'。 */
  width?: string;
  className?: string;
}

/** 文本行条骨架。 */
export function SkeletonText({ width = '100%', className = '' }: SkeletonTextProps) {
  return (
    <div
      aria-hidden="true"
      className={`ui-skeleton h-4 rounded-[var(--radius-images)] bg-mist-gray ${className}`}
      style={{ width }}
    />
  );
}

export interface SkeletonCardProps {
  /** 卡内骨架条数，默认 3。 */
  lines?: number;
  className?: string;
}

/** 卡片骨架：mist-gray 底 radius-cards padding 20px，内含递减宽度骨架条。 */
export function SkeletonCard({ lines = 3, className = '' }: SkeletonCardProps) {
  return (
    <div
      aria-hidden="true"
      className={`rounded-[var(--radius-cards)] bg-mist-gray p-5 ${className}`}
    >
      <div className="flex flex-col gap-3">
        {Array.from({ length: lines }, (_unused, index) => (
          <div
            key={index}
            className="ui-skeleton h-4 rounded-[var(--radius-images)] bg-paper-white"
            style={{ width: `${100 - index * 20}%` }}
          />
        ))}
      </div>
    </div>
  );
}
