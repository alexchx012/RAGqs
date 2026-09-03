import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EscStackProvider } from '../../lib/esc-stack-provider';
import { copy } from '../../copy';
import type { ConversationGroup, ConversationSummary } from '../types';
import { ConversationList } from './conversation-list';

/*
 * 会话条目触屏操作测试（共用基座 §3.2；R8/A1）：
 * 触屏（hover: none）⋯ 入口常显（class 钩子 + chat.css 媒体查询承载）；
 * 触屏长按条目唤起与 ⋯ 完全相同的菜单（重命名/置顶/移入分组/移出分组（仅分组内会话）/删除），松手 click 不误开会话；
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

function renderList(
  item: ConversationSummary = conversation('c1', '项目周报'),
  groupList: readonly ConversationGroup[] = [],
) {
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
        items={[item]}
        groups={groupList}
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

describe('移出分组', () => {
  it('分组内会话的 ⋯ 菜单显示「移出分组」，点击移出（onMoveToGroup(id, null)）', async () => {
    const user = userEvent.setup();
    const grouped: ConversationSummary = { ...conversation('c1', '项目周报'), group_id: 'g1' };
    const { onMoveToGroup } = renderList(grouped, [{ id: 'g1', name: '工作' }]);

    await user.click(menuTrigger());
    await user.click(screen.getByRole('menuitem', { name: copy.chat.sidebar.menuMoveOutOfGroup }));

    expect(onMoveToGroup).toHaveBeenCalledWith('c1', null);
  });

  it('未分组会话的 ⋯ 菜单不显示「移出分组」', async () => {
    const user = userEvent.setup();
    renderList();

    await user.click(menuTrigger());

    expect(
      screen.queryByRole('menuitem', { name: copy.chat.sidebar.menuMoveOutOfGroup }),
    ).not.toBeInTheDocument();
  });
});

describe('A25 加载更多', () => {
  function renderListWith(overrides: { hasMore?: boolean; loadingMore?: boolean; onLoadMore?: () => void }) {
    const onOpen = vi.fn();
    render(
      <EscStackProvider>
        <ConversationList
          items={[conversation('c1', '项目周报')]}
          groups={[]}
          listStatus="ready"
          currentId={null}
          searchQuery=""
          onOpen={onOpen}
          onRename={vi.fn()}
          onTogglePin={vi.fn()}
          onMoveToGroup={vi.fn()}
          onDelete={vi.fn()}
          onRenameGroup={vi.fn()}
          onDeleteGroup={vi.fn()}
          onCreateGroup={vi.fn<(name: string) => Promise<string | null>>().mockResolvedValue(null)}
          onRetryLoad={vi.fn()}
          {...overrides}
        />
      </EscStackProvider>,
    );
    return { onOpen };
  }

  it('hasMore 时列表底部展示「加载更多」，点击触发 onLoadMore', async () => {
    const user = userEvent.setup();
    const onLoadMore = vi.fn();
    renderListWith({ hasMore: true, loadingMore: false, onLoadMore });

    await user.click(screen.getByRole('button', { name: copy.chat.sidebar.loadMore }));

    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it('loadingMore 时按钮禁用并切换为加载中文案', () => {
    renderListWith({ hasMore: true, loadingMore: true, onLoadMore: vi.fn() });

    expect(screen.getByRole('button', { name: copy.chat.sidebar.loadingMore })).toBeDisabled();
    expect(
      screen.queryByRole('button', { name: copy.chat.sidebar.loadMore }),
    ).not.toBeInTheDocument();
  });

  it('hasMore=false 不展示加载更多（不足一页 / 已达上限）', () => {
    renderListWith({ hasMore: false, loadingMore: false, onLoadMore: vi.fn() });

    expect(
      screen.queryByRole('button', { name: copy.chat.sidebar.loadMore }),
    ).not.toBeInTheDocument();
  });
});
