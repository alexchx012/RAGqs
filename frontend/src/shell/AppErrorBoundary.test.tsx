import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { AppErrorBoundary, ErrorBoundaryFallback } from './AppErrorBoundary';

/*
 * 顶层 ErrorBoundary 测试（A18）：子树渲染异常时呈现可恢复提示（非白屏）、刷新按钮可用；
 * 降级 UI 单独验证文案与交互。
 */

function Boom(): never {
  throw new Error('render boom');
}

describe('AppErrorBoundary 顶层渲染异常兜底（A18）', () => {
  it('子组件渲染抛错：显示可恢复提示与刷新按钮，而非向上传播白屏', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      render(
        <AppErrorBoundary>
          <Boom />
        </AppErrorBoundary>,
      );
      expect(screen.getByRole('alert')).toHaveTextContent(copy.shell.errorBoundary.title);
      expect(screen.getByText(copy.shell.errorBoundary.description)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: copy.shell.errorBoundary.reload })).toBeInTheDocument();
      expect(screen.queryByText('render boom')).not.toBeInTheDocument(); // 不回显原始错误
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('正常子树原样渲染，boundary 不介入', () => {
    render(
      <AppErrorBoundary>
        <p>正常内容</p>
      </AppErrorBoundary>,
    );
    expect(screen.getByText('正常内容')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('降级 UI：刷新按钮触发 onReload', async () => {
    const onReload = vi.fn();
    const user = userEvent.setup();
    render(<ErrorBoundaryFallback onReload={onReload} />);
    await user.click(screen.getByRole('button', { name: copy.shell.errorBoundary.reload }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
