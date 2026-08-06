/*
 * 三态组件（共用基座 §3.2/§4/§5.6/§5.7 的空态、错误态、加载态统一形态）。
 * 禁整页 spinner（骨架屏优先）、禁 toast；默认文案来自 copy.states，可按模块覆盖。
 */

import { Inbox, type LucideIcon } from 'lucide-react';
import { copy } from '../copy';
import { SkeletonCard, SkeletonRow } from './Skeleton';
import { TextLink } from './TextLink';

export interface EmptyStateProps {
  /** 24px lucide 图标（默认 Inbox）。 */
  icon?: LucideIcon;
  text?: string;
}

export function EmptyState({ icon: Icon = Inbox, text = copy.states.empty }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-2 py-10">
      <Icon aria-hidden="true" className="h-6 w-6 text-smoke-gray" />
      <p className="text-[15px] text-smoke-gray">{text}</p>
    </div>
  );
}

export interface ErrorStateProps {
  text?: string;
  retryLabel?: string;
  onRetry?: () => void;
}

export function ErrorState({
  text = copy.states.error,
  retryLabel = copy.states.retry,
  onRetry,
}: ErrorStateProps) {
  return (
    <div className="flex items-center gap-2 py-4">
      <p className="text-[15px] text-slate-gray">{text}</p>
      {onRetry !== undefined && <TextLink onClick={onRetry}>{retryLabel}</TextLink>}
    </div>
  );
}

export interface LoadingRowsProps {
  count?: number;
}

/** 列表加载态：n 条骨架行（呼吸动画）。 */
export function LoadingRows({ count = 3 }: LoadingRowsProps) {
  return (
    <div aria-busy="true" className="flex flex-col gap-2">
      {Array.from({ length: count }, (_unused, index) => (
        <SkeletonRow key={index} />
      ))}
    </div>
  );
}

export interface LoadingCardsProps {
  count?: number;
}

/** 卡片列表加载态：n 张骨架卡（呼吸动画）。 */
export function LoadingCards({ count = 2 }: LoadingCardsProps) {
  return (
    <div aria-busy="true" className="flex flex-col gap-4">
      {Array.from({ length: count }, (_unused, index) => (
        <SkeletonCard key={index} />
      ))}
    </div>
  );
}
