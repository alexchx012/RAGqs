/*
 * Steep 通用控件库统一出口（fe-shared-shell src/ui/）。
 * 供本 change 与全部后续业务模块复用；设计来源：共用基座设计.md §2–§5。
 */

export { Chip, type ChipProps } from './Chip';
export { ConfirmDialog, type ConfirmDialogProps } from './ConfirmDialog';
export { CountBadge, type CountBadgeProps } from './CountBadge';
export { HeaderNotice, type HeaderNoticeProps } from './HeaderNotice';
export { HoverCard, type HoverCardProps } from './HoverCard';
export { MeatballMenu, type MeatballMenuItem, type MeatballMenuProps } from './MeatballMenu';
export { Paginator, type PaginatorProps } from './Paginator';
export { Pill, type PillProps, type PillSize, type PillVariant } from './Pill';
export {
  SegmentedControl,
  type SegmentedControlProps,
  type SegmentedOption,
} from './SegmentedControl';
export { SkeletonCard, SkeletonRow, SkeletonText } from './Skeleton';
export {
  EmptyState,
  ErrorState,
  LoadingCards,
  LoadingRows,
  type EmptyStateProps,
  type ErrorStateProps,
} from './states';
export { StatCard, type DistributionItem, type StatCardProps } from './StatCard';
export { StatusDot, type StatusDotIntent, type StatusDotProps } from './StatusDot';
export { Switch, type SwitchProps } from './Switch';
export { TextLink, type TextLinkProps } from './TextLink';
