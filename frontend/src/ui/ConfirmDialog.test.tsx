/*
 * ConfirmDialog 测试（共用基座 §5.6）：标题/说明渲染、取消与确认、Esc 关闭、
 * 焦点圈定与关闭后焦点返回触发元素（Radix 自带）。组件内 useEscShield 需要 EscStackProvider。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { ConfirmDialog } from './ConfirmDialog';

function Harness({
  onConfirm = vi.fn(),
  danger = false,
}: {
  onConfirm?: () => void;
  danger?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <EscStackProvider>
      <button type="button" onClick={() => setOpen(true)}>
        open dialog
      </button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete document?"
        description="This action cannot be undone."
        confirmLabel="Delete"
        danger={danger}
        onConfirm={onConfirm}
      />
    </EscStackProvider>
  );
}

function renderDialog(props: { onConfirm?: () => void; danger?: boolean } = {}) {
  return render(<Harness {...props} />);
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'open dialog' }));
  return screen.findByRole('dialog');
}

describe('ConfirmDialog', () => {
  it('展开：标题 20px 500、说明行、取消与确认键（danger 红底）', async () => {
    renderDialog({ danger: true });
    const user = userEvent.setup();
    const dialog = await openDialog(user);

    expect(dialog).toBeInTheDocument();
    expect(screen.getByText('Delete document?').className).toContain('text-[20px]');
    expect(screen.getByText('This action cannot be undone.').className).toContain(
      'text-slate-gray',
    );
    expect(screen.getByRole('button', { name: 'Delete' }).className).toContain('bg-danger');
  });

  it('取消键关闭，不触发 onConfirm', async () => {
    const onConfirm = vi.fn();
    renderDialog({ onConfirm });
    const user = userEvent.setup();
    await openDialog(user);

    await user.click(screen.getByRole('button', { name: copy.controls.cancel }));
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('确认键触发 onConfirm', async () => {
    const onConfirm = vi.fn();
    renderDialog({ onConfirm });
    const user = userEvent.setup();
    await openDialog(user);

    await user.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('Esc 关闭且焦点返回触发元素', async () => {
    renderDialog();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: 'open dialog' });
    await openDialog(user);
    expect(trigger).not.toHaveFocus();

    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();
  });

  it('焦点圈定：Tab 在框内按钮间循环', async () => {
    renderDialog();
    const user = userEvent.setup();
    await openDialog(user);

    const cancel = screen.getByRole('button', { name: copy.controls.cancel });
    const confirm = screen.getByRole('button', { name: 'Delete' });
    // Radix 打开后焦点移入框内；末位再 Tab 循环回首位
    confirm.focus();
    await user.tab();
    expect(cancel).toHaveFocus();
    await user.tab();
    expect(confirm).toHaveFocus();
    await user.tab({ shift: true });
    expect(cancel).toHaveFocus();
  });
});
