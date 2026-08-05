/*
 * 全页加载占位：认证状态未知（静默 refresh 进行中）时渲染。
 * 无文案文案纪律豁免说明：仅加载点动画，role="status" 提供可达性名称。
 */

import { copy } from '../copy';

export function FullPageLoading() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-paper-white">
      <span className="loading-dots" role="status" aria-label={copy.shell.loading}>
        <span />
        <span />
        <span />
      </span>
    </div>
  );
}
