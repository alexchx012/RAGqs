/*
 * 侧边栏（共用基座 §3.1–§3.2；spec §2）。
 * 桌面 ≥768px 常驻（宽 280px fog-white 底 + 右侧 1px hairline）；窄屏 <768px 收为滑出抽屉
 * （280px 自左滑入 + 24% 墨色遮罩，Esc/点遮罩/选中会话后关闭）。
 * 自上而下：顶部工具行（搜索 + 新建会话 filled pill）、会话列表、底部头像区（内嵌 12px 圆角卡片，hover 选中态，按角色开抽屉）。
 */

import { Menu, Plus } from 'lucide-react';
import { useEffect } from 'react';
import { useLocation } from 'react-router';
import { copy } from '../../copy';
import type { User } from '../../auth/types';
import { ConversationList } from './conversation-list';
import type { ConversationGroup, ConversationSummary } from '../types';

export interface ChatSidebarProps {
  readonly user: User | null;
  readonly conversations: readonly ConversationSummary[];
  readonly groups: readonly ConversationGroup[];
  readonly listStatus: 'idle' | 'loading' | 'ready' | 'error';
  readonly currentId: string | null;
  readonly searchQuery: string;
  readonly drawerOpen: boolean;
  readonly onDrawerOpenChange: (open: boolean) => void;
  readonly onSearchChange: (query: string) => void;
  readonly onNewConversation: () => void;
  readonly onOpen: (id: string) => void;
  readonly onRename: (id: string, title: string) => void;
  readonly onTogglePin: (id: string, pinned: boolean) => void;
  readonly onMoveToGroup: (id: string, groupId: string | null) => void;
  readonly onDelete: (id: string) => void;
  readonly onRenameGroup: (groupId: string, name: string) => void;
  readonly onDeleteGroup: (groupId: string) => void;
  readonly onCreateGroup: (name: string) => Promise<string | null>;
  readonly onRetryLoad: () => void;
  readonly onOpenDrawer: () => void;
}

export function ChatSidebar(props: ChatSidebarProps) {
  return (
    <>
      {/* 桌面常驻侧边栏 */}
      <aside className="hidden w-[280px] shrink-0 flex-col border-r border-hairline bg-fog-white md:flex">
        <SidebarContent {...props} />
      </aside>
      {/* 窄屏抽屉 */}
      <NarrowDrawer {...props} />
    </>
  );
}

type SidebarContentProps = Omit<ChatSidebarProps, 'drawerOpen' | 'onDrawerOpenChange'>;

function SidebarContent({
  user,
  conversations,
  groups,
  listStatus,
  currentId,
  searchQuery,
  onSearchChange,
  onNewConversation,
  onOpen,
  onRename,
  onTogglePin,
  onMoveToGroup,
  onDelete,
  onRenameGroup,
  onDeleteGroup,
  onCreateGroup,
  onRetryLoad,
  onOpenDrawer,
}: SidebarContentProps) {
  return (
    <>
      <div className="flex items-center gap-2 p-3">
        <input
          type="search"
          value={searchQuery}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder={copy.chat.sidebar.searchPlaceholder}
          aria-label={copy.chat.sidebar.searchPlaceholder}
          className="h-9 min-w-0 flex-1 rounded-[var(--radius-inputs)] border border-hairline bg-paper-white px-3 text-[15px] text-ink-black outline-none placeholder:text-smoke-gray focus:border-ink-black"
        />
        <button
          type="button"
          onClick={onNewConversation}
          aria-label={copy.chat.sidebar.newConversation}
          className="inline-flex h-9 shrink-0 items-center justify-center gap-1 rounded-[var(--radius-buttons)] bg-ink-black px-3 text-[16px] text-paper-white transition-opacity duration-[var(--duration-fast)] hover:opacity-[0.88]"
        >
          <Plus aria-hidden="true" className="h-4 w-4" />
          <span className="hidden lg:inline">{copy.chat.sidebar.newConversation}</span>
        </button>
      </div>
      <ConversationList
        items={conversations}
        groups={groups}
        listStatus={listStatus}
        currentId={currentId}
        searchQuery={searchQuery}
        onOpen={onOpen}
        onRename={onRename}
        onTogglePin={onTogglePin}
        onMoveToGroup={onMoveToGroup}
        onDelete={onDelete}
        onRenameGroup={onRenameGroup}
        onDeleteGroup={onDeleteGroup}
        onCreateGroup={onCreateGroup}
        onRetryLoad={onRetryLoad}
      />
      <button
        type="button"
        onClick={onOpenDrawer}
        aria-label={copy.shell.home.openDrawerAria}
        className="chat-sidebar-footer-shadow shrink-0 border-t border-hairline bg-fog-white p-2 text-left"
      >
        <span className="flex h-14 w-full items-center gap-3 rounded-[var(--radius-images)] px-2 transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-mist-gray text-body font-w480 text-ink-black">
            {user?.display_name.slice(0, 2) ?? ''}
          </span>
          <span className="truncate text-body font-w480 text-ink-black">{user?.display_name}</span>
        </span>
      </button>
    </>
  );
}

/** 窄屏汉堡按钮（20px 图标 40px 触控区）。 */
export function ChatMenuButton({ onOpen, hidden }: { onOpen: () => void; hidden?: boolean }) {
  return (
    <button
      type="button"
      aria-label={copy.chat.sidebar.openSidebarAria}
      onClick={onOpen}
      className={
        'absolute left-6 top-5 z-10 flex h-10 w-10 items-center justify-center rounded-full ' +
        'text-ink-black transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray ' +
        (hidden === true ? 'md:hidden' : 'md:hidden')
      }
    >
      <Menu aria-hidden="true" className="h-5 w-5" />
    </button>
  );
}

/** 窄屏滑出抽屉：280px 自左滑入 + 遮罩；Esc / 点遮罩 / 选中会话后关闭。 */
function NarrowDrawer({
  user,
  conversations,
  groups,
  listStatus,
  currentId,
  searchQuery,
  drawerOpen,
  onDrawerOpenChange,
  onSearchChange,
  onNewConversation,
  onOpen,
  onRename,
  onTogglePin,
  onMoveToGroup,
  onDelete,
  onRenameGroup,
  onDeleteGroup,
  onCreateGroup,
  onRetryLoad,
  onOpenDrawer,
}: ChatSidebarProps) {
  const location = useLocation();
  // 路径变化（选中会话 / 抽屉跳转）后关闭抽屉
  useEffect(() => {
    if (drawerOpen) {
      onDrawerOpenChange(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onDrawerOpenChange(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [drawerOpen, onDrawerOpenChange]);

  return (
    <div className="md:hidden">
      {drawerOpen && (
        <div
          role="presentation"
          className="chat-drawer-overlay fixed inset-0 z-40"
          onClick={() => onDrawerOpenChange(false)}
        />
      )}
      <div
        data-open={drawerOpen}
        aria-hidden={!drawerOpen}
        className="chat-drawer-panel fixed inset-y-0 left-0 z-50 flex w-[280px] flex-col border-r border-hairline bg-fog-white"
      >
        <div className="flex h-16 shrink-0 items-center justify-end border-b border-hairline px-3">
          <button
            type="button"
            aria-label={copy.chat.sidebar.closeSidebarAria}
            onClick={() => onDrawerOpenChange(false)}
            className="flex h-10 w-10 items-center justify-center rounded-full text-ink-black transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray"
          >
            <CloseIcon aria-hidden="true" className="h-5 w-5" />
          </button>
        </div>
        <SidebarContent
          user={user}
          conversations={conversations}
          groups={groups}
          listStatus={listStatus}
          currentId={currentId}
          searchQuery={searchQuery}
          onSearchChange={onSearchChange}
          onNewConversation={onNewConversation}
          onOpen={onOpen}
          onRename={onRename}
          onTogglePin={onTogglePin}
          onMoveToGroup={onMoveToGroup}
          onDelete={onDelete}
          onRenameGroup={onRenameGroup}
          onDeleteGroup={onDeleteGroup}
          onCreateGroup={onCreateGroup}
          onRetryLoad={onRetryLoad}
          onOpenDrawer={onOpenDrawer}
        />
      </div>
    </div>
  );
}

function CloseIcon({ className = '' }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className={className}>
      <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
