/*
 * 顶层 ErrorBoundary（A18）：React 渲染异常兜底，显示可恢复提示与刷新按钮而非白屏。
 * React 官方类组件模式（getDerivedStateFromError 渲染降级 UI，componentDidCatch 记录错误）；
 * 文案经 src/copy；包裹 App 全树（含路由与 provider），boundary 之上不再有会抛错的 React 子树。
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';
import { copy } from '../copy';
import { Pill } from '../ui';

export interface AppErrorBoundaryProps {
  readonly children: ReactNode;
}

interface AppErrorBoundaryState {
  readonly failed: boolean;
}

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // 渲染异常不静默：落控制台（生产可观测性入口），不中断降级 UI 呈现
    console.error('[app-error-boundary]', error, info.componentStack);
  }

  render() {
    return this.state.failed ? <ErrorBoundaryFallback onReload={() => window.location.reload()} /> : this.props.children;
  }
}

export interface ErrorBoundaryFallbackProps {
  readonly onReload: () => void;
}

/** 降级 UI 独立导出：boundary 行为与提示渲染可分别测试。 */
export function ErrorBoundaryFallback({ onReload }: ErrorBoundaryFallbackProps) {
  return (
    <div role="alert" className="flex min-h-screen flex-col items-center justify-center gap-3 bg-paper-white px-6 text-center">
      <h1 className="text-[20px] font-medium text-ink-black">{copy.shell.errorBoundary.title}</h1>
      <p className="max-w-[480px] text-[15px] text-slate-gray">{copy.shell.errorBoundary.description}</p>
      <Pill size="sm" className="mt-2" onClick={onReload}>
        {copy.shell.errorBoundary.reload}
      </Pill>
    </div>
  );
}
