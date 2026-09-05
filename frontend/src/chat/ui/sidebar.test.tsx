import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { EscStackProvider, useEscShield } from '../../lib/esc-stack-provider';
import { copy } from '../../copy';
import { ChatSidebar, type ChatSidebarProps } from './sidebar';

/*
 * 侧边栏窄屏抽屉 a11y / Esc 分层（审查 A13/A14）：
 * - A13：关闭态面板 inert + aria-hidden——常驻 DOM 但不可被 Tab 聚焦 / 读屏遍历；
 * - A14：Esc 走全局栈（只关最上层）：抽屉层在栈内才收到 Esc；栈顶空盾（Radix 浮层
 *   打开期间登记）消费 Esc 时不穿透关闭抽屉。
 * 注：路径变化关闭抽屉的 effect 在挂载时也会执行一次，故「打开」一律先挂载关闭态再 rerender。
 */

function baseProps(overrides: Partial<ChatSidebarProps> = {}): ChatSidebarProps {
  return {
    user: null,
    conversations: [],
    groups: [],
    listStatus: 'ready',
    hasMore: false,
    loadingMore: false,
    onLoadMore: vi.fn(),
    currentId: null,
    searchQuery: '',
    drawerOpen: false,
    onDrawerOpenChange: vi.fn(),
    onSearchChange: vi.fn(),
    onNewConversation: vi.fn(),
    onOpen: vi.fn(),
    onRename: vi.fn(),
    onTogglePin: vi.fn(),
    onMoveToGroup: vi.fn(),
    onDelete: vi.fn(),
    onRenameGroup: vi.fn(),
    onDeleteGroup: vi.fn(),
    onCreateGroup: vi.fn(),
    onRetryLoad: vi.fn(),
    onOpenDrawer: vi.fn(),
    ...overrides,
  };
}

function Shield({ active }: { active: boolean }): null {
  useEscShield(active);
  return null;
}

interface HarnessProps {
  readonly drawerOpen: boolean;
  readonly onDrawerOpenChange: (open: boolean) => void;
  readonly shieldActive?: boolean;
}

function Harness({ drawerOpen, onDrawerOpenChange, shieldActive = false }: HarnessProps) {
  return (
    <MemoryRouter initialEntries={['/']}>
      <EscStackProvider>
        <ChatSidebar {...baseProps({ drawerOpen, onDrawerOpenChange })} />
        <Shield active={shieldActive} />
      </EscStackProvider>
    </MemoryRouter>
  );
}

function drawerPanel(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.chat-drawer-panel');
  if (node === null) throw new Error('drawer panel is missing');
  return node;
}

describe('窄屏抽屉（NarrowDrawer）', () => {
  it('A13：关闭态面板 inert + aria-hidden（不可 Tab 聚焦/读屏遍历），打开态恢复', () => {
    const onDrawerOpenChange = vi.fn();
    const { rerender } = render(<Harness drawerOpen={false} onDrawerOpenChange={onDrawerOpenChange} />);
    const panel = drawerPanel();
    expect(panel).toHaveAttribute('data-open', 'false');
    expect(panel).toHaveAttribute('inert');
    expect(panel).toHaveAttribute('aria-hidden', 'true');

    rerender(<Harness drawerOpen onDrawerOpenChange={onDrawerOpenChange} />);
    expect(panel).toHaveAttribute('data-open', 'true');
    expect(panel).not.toHaveAttribute('inert');
    expect(panel).toHaveAttribute('aria-hidden', 'false');
  });

  it('A13：关闭时焦点残留在面板内则移出（blur）', () => {
    const onDrawerOpenChange = vi.fn();
    const { rerender } = render(<Harness drawerOpen={false} onDrawerOpenChange={onDrawerOpenChange} />);
    rerender(<Harness drawerOpen onDrawerOpenChange={onDrawerOpenChange} />);
    const panel = drawerPanel();
    const close = screen.getByRole('button', { name: copy.chat.sidebar.closeSidebarAria });
    close.focus();
    expect(panel.contains(document.activeElement)).toBe(true);

    rerender(<Harness drawerOpen={false} onDrawerOpenChange={onDrawerOpenChange} />);
    expect(panel.contains(document.activeElement)).toBe(false);
  });

  it('A14：抽屉打开时 Esc 关闭（全局栈层响应）', () => {
    const onDrawerOpenChange = vi.fn();
    const { rerender } = render(<Harness drawerOpen={false} onDrawerOpenChange={onDrawerOpenChange} />);
    rerender(<Harness drawerOpen onDrawerOpenChange={onDrawerOpenChange} />);
    onDrawerOpenChange.mockClear();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onDrawerOpenChange).toHaveBeenCalledWith(false);
  });

  it('A14：抽屉关闭时 Esc 不触发关闭回调（层未注册）', () => {
    const onDrawerOpenChange = vi.fn();
    render(<Harness drawerOpen={false} onDrawerOpenChange={onDrawerOpenChange} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onDrawerOpenChange).not.toHaveBeenCalled();
  });

  it('A14：栈顶空盾（抽屉内浮层打开）时 Esc 不穿透关闭抽屉', () => {
    const onDrawerOpenChange = vi.fn();
    // 次序对齐真实场景：先开抽屉（登记抽屉层），再打开抽屉内浮层（空盾后入栈到栈顶）
    const { rerender } = render(
      <Harness drawerOpen={false} onDrawerOpenChange={onDrawerOpenChange} shieldActive={false} />,
    );
    rerender(<Harness drawerOpen onDrawerOpenChange={onDrawerOpenChange} shieldActive={false} />);
    rerender(<Harness drawerOpen onDrawerOpenChange={onDrawerOpenChange} shieldActive />);
    onDrawerOpenChange.mockClear();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onDrawerOpenChange).not.toHaveBeenCalled();
  });
});
