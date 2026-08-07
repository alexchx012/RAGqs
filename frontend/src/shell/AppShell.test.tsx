import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as Dialog from '@radix-ui/react-dialog';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { AppRoutes } from '../router/AppRoutes';
import { createAuthedStore, renderWithShell } from '../test/auth-fixtures';

describe('应用壳与路由骨架', () => {
  it('聊天主页（fe-chat-home）渲染单一文案常量文件中的文案（输入区 placeholder）', async () => {
    renderWithShell(<AppRoutes />, await createAuthedStore());
    expect(
      await screen.findByLabelText(copy.chat.composer.inputPlaceholder),
    ).toBeInTheDocument();
    // 侧边栏搜索占位（同样来自 copy）
    expect(screen.getAllByLabelText(copy.chat.sidebar.searchPlaceholder).length).toBeGreaterThan(0);
  });

  it('未知路径渲染 404 占位，返回链接可用', async () => {
    renderWithShell(<AppRoutes />, await createAuthedStore(), ['/no-such-route']);
    expect(
      await screen.findByRole('heading', { name: copy.shell.notFoundTitle }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: copy.shell.notFoundBack })).toHaveAttribute('href', '/');
  });

  it('跳转主内容链接存在（可达性基线）', async () => {
    renderWithShell(<AppRoutes />, await createAuthedStore());
    expect(await screen.findByRole('link', { name: copy.shell.skipToContent })).toHaveAttribute(
      'href',
      '#main',
    );
  });
});

describe('Radix 无头原语集成（可访问性底层）', () => {
  it('对话框渲染、焦点圈定与 Esc 关闭由无头库承载', async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <Dialog.Root open onOpenChange={onOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay />
          <Dialog.Content aria-describedby={undefined}>
            <Dialog.Title>{copy.shell.placeholderTitle}</Dialog.Title>
            <Dialog.Close>{copy.a11y.dialogClose}</Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>,
    );
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // 焦点圈定：打开后焦点移入框内
    expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement);
    await user.keyboard('{Escape}');
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
