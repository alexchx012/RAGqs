import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EscStackProvider } from '../../lib/esc-stack-provider';
import { copy } from '../../copy';
import type { ConversationSummary } from '../types';
import { ConversationList } from './conversation-list';

/*
 * 会话条目触屏操作测试（共用基座 §3.2；R8/A1）：
 * 触屏（hover: none）⋯ 入口常显（class 钩子 + chat.css 媒体查询承载）；
 * 触屏长按条目唤起与 ⋯ 完全相同的菜单（重命名/置顶/移入分组/删除），松手 click 不误开会话；
 * 位移超阈值/短按/mouse 指针不触发长按（滚动、侧滑与桌面行为不变）。
 */

const MENU_ITEMS = [
  copy.chat.sidebar.menuRename,
  copy.chat.sidebar.menuPin,
  copy.chat.sidebar.menuMoveToGroup,
  copy.chat.sidebar.menuDelete,
];

function conversation(id: string, title: string): ConversationSummary {
  return { id, title, pinned: false, group_id: null, last_active_at: '2026-08-16T00:00:00Z' };
}

function renderList() {
  const handlers = {
    onOpen: vi.fn(),
    onRename: vi.fn(),
    onTogglePin: vi.fn(),
    onMoveToGroup: vi.fn(),
    onDelete: vi.fn(),
    onRenameGroup: vi.fn(),
    onDeleteGroup: vi.fn(),
    onCreateGroup: vi.fn<(name: string) => Promise<string | null>>().mockResolvedValue(null),
    onRetryLoad: vi.fn(),
  };
  render(
    <EscStackProvider>
      <ConversationList
        items={[conversation('c1', '项目周报')]}
        groups={[]}
        listStatus="ready"
        currentId={null}
        searchQuery=""
        {...handlers}
      />
    </EscStackProvider>,
  );
  return handlers;
}

function itemButton(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.chat-conversation-item');
  if (node === null) throw new Error('conversation item is missing');
  return node;
}

function menuTrigger(): HTMLElement {
  return screen.getByRole('button', { name: copy.chat.sidebar.itemMenuAria('项目周报') });
}

function touchDown(target: HTMLElement) {
  fireEvent.pointerDown(target, { pointerId: 1, pointerType: 'touch', clientX: 60, clientY: 8 });
}

function expectItemMenuOpen() {
  for (const label of MENU_ITEMS) {
    expect(screen.getByRole('menuitem', { name: label })).toBeInTheDocument();
  }
  expect(menuTrigger()).toHaveAttribute('data-state', 'open');
}

describe('ConversationList 触屏操作（R8/A1）', () => {
  describe('长按手势', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('触屏长按 500ms 唤起与 ⋯ 完全相同的菜单；松手后的 click 被吞掉（不误开会话）', async () => {
      const { onOpen } = renderList();

      touchDown(itemButton());
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });

      expectItemMenuOpen();

      fireEvent.pointerUp(itemButton(), { pointerId: 1, pointerType: 'touch' });
      fireEvent.click(itemButton());
      expect(onOpen).not.toHaveBeenCalled();
    });

    it('按压中位移超 10px 取消长按（滚动/侧滑手势不触发菜单），点按仍打开会话', async () => {
      const { onOpen } = renderList();

      touchDown(itemButton());
      fireEvent.pointerMove(itemButton(), { pointerId: 1, pointerType: 'touch', clientX: 60, clientY: 30 });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });

      expect(screen.queryByRole('menuitem')).not.toBeInTheDocument();

      fireEvent.pointerUp(itemButton(), { pointerId: 1, pointerType: 'touch' });
      fireEvent.click(itemButton());
      expect(onOpen).toHaveBeenCalledWith('c1');
    });

    it('短按（不足 500ms）不触发菜单，click 正常打开会话', async () => {
      const { onOpen } = renderList();

      touchDown(itemButton());
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300);
      });
      fireEvent.pointerUp(itemButton(), { pointerId: 1, pointerType: 'touch' });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });

      expect(screen.queryByRole('menuitem')).not.toBeInTheDocument();

      fireEvent.click(itemButton());
      expect(onOpen).toHaveBeenCalledWith('c1');
    });

    it('mouse 指针按住不触发长按（桌面 hover/focus 行为不变）', async () => {
      const { onOpen } = renderList();

      fireEvent.pointerDown(itemButton(), { pointerId: 2, pointerType: 'mouse', clientX: 60, clientY: 8 });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });
      fireEvent.pointerUp(itemButton(), { pointerId: 2, pointerType: 'mouse' });

      expect(screen.queryByRole('menuitem')).not.toBeInTheDocument();

      fireEvent.click(itemButton());
      expect(onOpen).toHaveBeenCalledWith('c1');
    });
  });

  it('⋯ 点击仍打开同一菜单（既有入口不变）', async () => {
    const user = userEvent.setup();
    renderList();

    await user.click(menuTrigger());

    expectItemMenuOpen();
  });

  it('⋯ 入口带触屏常显钩子 chat-item-menu-trigger（hover: none 常显由 chat.css 承载）', () => {
    renderList();
    expect(menuTrigger()).toHaveClass('chat-item-menu-trigger');
  });
});
