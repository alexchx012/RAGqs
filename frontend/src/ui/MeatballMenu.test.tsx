/*
 * MeatballMenu 测试（共用基座 §3.2）：键盘展开与导航选择、danger 项红字、Esc 关闭。
 * 组件内 useEscShield 需要 EscStackProvider。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { MeatballMenu, type MeatballMenuItem } from './MeatballMenu';

function renderMenu(items: MeatballMenuItem[]) {
  return render(
    <EscStackProvider>
      <MeatballMenu items={items} ariaLabel="row actions" alwaysVisible />
    </EscStackProvider>,
  );
}

function createItems(onSelect = vi.fn()): MeatballMenuItem[] {
  return [
    { key: 'rename', label: 'rename', onSelect },
    { key: 'pin', label: 'pin', onSelect },
    { key: 'delete', label: 'delete', onSelect, danger: true },
  ];
}

describe('MeatballMenu', () => {
  it('键盘 Enter 展开菜单，方向键导航，Enter 选择对应项', async () => {
    const onSelect = vi.fn();
    renderMenu(createItems(onSelect));
    const user = userEvent.setup();

    screen.getByRole('button', { name: 'row actions' }).focus();
    await user.keyboard('{Enter}');
    const menu = await screen.findByRole('menu');
    expect(menu).toBeInTheDocument();

    await user.keyboard('{ArrowDown}');
    await user.keyboard('{Enter}');
    expect(onSelect).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });
  });

  it('danger 项 text-danger', async () => {
    renderMenu(createItems());
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'row actions' }));
    expect(await screen.findByRole('menuitem', { name: 'delete' })).toHaveClass('text-danger');
    expect(screen.getByRole('menuitem', { name: 'rename' })).toHaveClass('text-ink-black');
  });

  it('Esc 关闭菜单且不选择', async () => {
    const onSelect = vi.fn();
    renderMenu(createItems(onSelect));
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'row actions' }));
    expect(await screen.findByRole('menu')).toBeInTheDocument();

    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('点击外部关闭', async () => {
    renderMenu(createItems());
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'row actions' }));
    expect(await screen.findByRole('menu')).toBeInTheDocument();

    await user.click(document.body);
    await waitFor(() => {
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });
  });
});
