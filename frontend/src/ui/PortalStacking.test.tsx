/*
 * 浮层层级测试（审查 A1）：设置抽屉根是 fixed inset-0 z-40 的不透明面板，Radix portal 到 body
 * 的浮层若不显式抬层会被抽屉盖住（表现为「点了没反应」）。要求 menu/hovercard > 40、
 * dialog 盖住已打开的 menu，且浮层确实挂在 document.body 下（不在 z-40 抽屉内）。
 * jsdom 的 getComputedStyle 不做样式表级联（类上的 z-index 读不到），数值断言从
 * styles/ui.css 解析规则块；组件侧断言 portal 归属与类名。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { ConfirmDialog } from './ConfirmDialog';
import { MeatballMenu, type MeatballMenuItem } from './MeatballMenu';

// 路径先落变量，避免 vite 对 `new URL('<literal>', import.meta.url)` 的静态资源改写
const UI_CSS_PATH = '../styles/ui.css';
const uiCss = readFileSync(fileURLToPath(new URL(UI_CSS_PATH, import.meta.url)), 'utf8');

/** 从 ui.css 解析包含指定类名的规则块的 z-index；找不到返回 NaN（断言必然失败）。 */
function zIndexFor(className: string): number {
  for (const block of uiCss.split('}')) {
    const [selector, body] = block.split('{');
    if (selector !== undefined && body !== undefined && selector.includes(className)) {
      const match = body.match(/z-index:\s*(\d+)/);
      if (match) {
        return Number(match[1]);
      }
    }
  }
  return Number.NaN;
}

/** 模拟设置抽屉：z-40 不透明面板盖住其下内容。 */
function Drawer40Shell({ children }: { children: ReactNode }) {
  return (
    <EscStackProvider>
      <div data-testid="drawer" className="fixed inset-0 z-40 bg-paper-white">
        {children}
      </div>
    </EscStackProvider>
  );
}

const MENU_ITEMS: MeatballMenuItem[] = [{ key: 'rename', label: 'rename', onSelect: () => {} }];

describe('浮层层级（审查 A1）', () => {
  it('ui.css 数值：menu/hovercard > 40，dialog 遮罩与内容盖住 menu', () => {
    const menu = zIndexFor('ui-menu-content');
    const hovercard = zIndexFor('ui-hovercard-content');
    const overlay = zIndexFor('ui-dialog-overlay');
    const content = zIndexFor('ui-dialog-content');
    expect(menu).toBeGreaterThan(40);
    expect(hovercard).toBeGreaterThan(40);
    expect(overlay).toBeGreaterThan(menu);
    expect(content).toBeGreaterThan(menu);
  });

  it('z-40 抽屉内打开菜单：浮层挂在 body 下且不在抽屉内', async () => {
    render(
      <Drawer40Shell>
        <MeatballMenu items={MENU_ITEMS} ariaLabel="row actions" alwaysVisible />
      </Drawer40Shell>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'row actions' }));

    const menuContent = document.querySelector('.ui-menu-content');
    expect(menuContent).not.toBeNull();
    expect(screen.getByRole('menu')).toBeInTheDocument();
    const drawer = screen.getByTestId('drawer');
    expect(drawer.contains(menuContent)).toBe(false);
    expect(document.body.contains(menuContent)).toBe(true);
  });

  it('z-40 抽屉内打开 ConfirmDialog：遮罩与内容挂在 body 下且不在抽屉内', async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <Drawer40Shell>
          <button type="button" onClick={() => setOpen(true)}>
            open dialog
          </button>
          <ConfirmDialog
            open={open}
            onOpenChange={setOpen}
            title="Delete document?"
            description="This action cannot be undone."
            onConfirm={() => {}}
          />
        </Drawer40Shell>
      );
    }
    render(<Harness />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'open dialog' }));

    const overlay = document.querySelector('.ui-dialog-overlay');
    const content = document.querySelector('.ui-dialog-content');
    expect(overlay).not.toBeNull();
    expect(content).not.toBeNull();
    const drawer = screen.getByTestId('drawer');
    expect(drawer.contains(overlay)).toBe(false);
    expect(drawer.contains(content)).toBe(false);
    expect(document.body.contains(content)).toBe(true);
  });
});
