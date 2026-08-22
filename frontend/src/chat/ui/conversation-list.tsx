/*
 * 会话列表（共用基座 §3.2；spec §2）。
 * 分组顺序：置顶 → 自定义分组（可折叠）→ 今天/本周/更早；条目 40px、hover/当前态/focus；
 * ⋯ 菜单（重命名/置顶|取消置顶/移入分组/移出分组（仅分组内会话）/删除），重命名就地输入、移入分组子菜单列已有分组+新建分组、
 * 删除二次确认（danger）；自定义分组头可折叠 + 分组重命名/删除（会话分组 CRUD）。
 * ⋯ 入口桌面 hover/focus 淡入、触屏（hover: none）常显（chat.css）；触屏长按条目唤起同一菜单（use-long-press）。
 */

import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { ChevronDown, Ellipsis } from 'lucide-react';
import { useState } from 'react';
import { copy } from '../../copy';
import { useEscShield } from '../../lib/esc-stack-provider';
import { formatRelativeTime } from '../../notifications/relative-time';
import { ConfirmDialog } from '../../ui/ConfirmDialog';
import { LoadingRows } from '../../ui/states';
import { TextLink } from '../../ui/TextLink';
import type { ConversationGroup, ConversationSummary } from '../types';
import { groupConversationList, type ConversationSection } from '../store';
import { useLongPress } from './use-long-press';

export interface ConversationListProps {
  readonly items: readonly ConversationSummary[];
  readonly groups: readonly ConversationGroup[];
  readonly listStatus: 'idle' | 'loading' | 'ready' | 'error';
  readonly currentId: string | null;
  readonly searchQuery: string;
  readonly onOpen: (id: string) => void;
  readonly onRename: (id: string, title: string) => void;
  readonly onTogglePin: (id: string, pinned: boolean) => void;
  readonly onMoveToGroup: (id: string, groupId: string | null) => void;
  readonly onDelete: (id: string) => void;
  readonly onRenameGroup: (groupId: string, name: string) => void;
  readonly onDeleteGroup: (groupId: string) => void;
  /** 创建分组；返回新分组 id（失败 null）。m5：新建分组后直接移入当前会话。 */
  readonly onCreateGroup: (name: string) => Promise<string | null>;
  readonly onRetryLoad: () => void;
}

export function ConversationList({
  items,
  groups,
  listStatus,
  currentId,
  searchQuery,
  onOpen,
  onRename,
  onTogglePin,
  onMoveToGroup,
  onDelete,
  onRenameGroup,
  onDeleteGroup,
  onCreateGroup,
  onRetryLoad,
}: ConversationListProps) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {listStatus === 'loading' ? (
          <LoadingRows count={5} />
        ) : listStatus === 'error' ? (
          <div className="flex items-center gap-2 px-1 py-4">
            <p className="text-[15px] text-slate-gray">{copy.chat.sidebar.listError}</p>
            <TextLink onClick={onRetryLoad}>{copy.states.retry}</TextLink>
          </div>
        ) : items.length === 0 && searchQuery.trim() === '' ? (
          <p className="py-10 text-center text-[15px] text-smoke-gray">{copy.chat.sidebar.emptyList}</p>
        ) : items.length === 0 ? (
          // m11：过滤无结果与无任何会话是两条独立措辞（§3.2）
          <p className="py-10 text-center text-[15px] text-smoke-gray">{copy.chat.sidebar.emptySearch}</p>
        ) : (
          <SidebarSections
            items={items}
            groups={groups}
            currentId={currentId}
            onOpen={onOpen}
            onRename={onRename}
            onTogglePin={onTogglePin}
            onMoveToGroup={onMoveToGroup}
            onDelete={onDelete}
            onRenameGroup={onRenameGroup}
            onDeleteGroup={onDeleteGroup}
            onCreateGroup={onCreateGroup}
          />
        )}
      </div>
    </div>
  );
}

function SidebarSections({
  items,
  groups,
  currentId,
  onOpen,
  onRename,
  onTogglePin,
  onMoveToGroup,
  onDelete,
  onRenameGroup,
  onDeleteGroup,
  onCreateGroup,
}: Omit<ConversationListProps, 'listStatus' | 'searchQuery' | 'onSearchChange' | 'onRetryLoad'>) {
  const sections = groupConversationList(items, groups);
  return (
    <div className="flex flex-col gap-3">
      {sections.map((section) => (
        <Section
          key={sectionKey(section)}
          section={section}
          groups={groups}
          currentId={currentId}
          onOpen={onOpen}
          onRename={onRename}
          onTogglePin={onTogglePin}
          onMoveToGroup={onMoveToGroup}
          onDelete={onDelete}
          onRenameGroup={onRenameGroup}
          onDeleteGroup={onDeleteGroup}
          onCreateGroup={onCreateGroup}
        />
      ))}
    </div>
  );
}

function sectionKey(section: ConversationSection): string {
  if (section.kind === 'group' && section.group !== null) {
    return `group:${section.group.id}`;
  }
  return section.kind;
}

/** 未命名会话展示标题：首条消息生成标题前 title 为 ''，条目/菜单 aria/删除确认统一显示「新会话」。 */
function displayTitle(title: string): string {
  return title.trim() === '' ? copy.chat.sidebar.untitledConversation : title;
}

function Section({
  section,
  groups,
  currentId,
  onOpen,
  onRename,
  onTogglePin,
  onMoveToGroup,
  onDelete,
  onRenameGroup,
  onDeleteGroup,
  onCreateGroup,
}: {
  section: ConversationSection;
  groups: readonly ConversationGroup[];
  currentId: string | null;
  onOpen: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onTogglePin: (id: string, pinned: boolean) => void;
  onMoveToGroup: (id: string, groupId: string | null) => void;
  onDelete: (id: string) => void;
  onRenameGroup: (groupId: string, name: string) => void;
  onDeleteGroup: (groupId: string) => void;
  onCreateGroup: (name: string) => Promise<string | null>;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const isGroup = section.kind === 'group';
  const title =
    section.kind === 'pinned'
      ? copy.chat.sidebar.sectionPinned
      : section.kind === 'group'
        ? section.group?.name ?? ''
        : section.kind === 'today'
          ? copy.chat.sidebar.sectionToday
          : section.kind === 'week'
            ? copy.chat.sidebar.sectionWeek
            : copy.chat.sidebar.sectionEarlier;

  return (
    <div>
      <div className="flex items-center justify-between px-1 pb-1">
        {isGroup ? (
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-expanded={!collapsed}
            aria-label={copy.chat.sidebar.groupSectionAria(title)}
            className="flex min-w-0 flex-1 items-center gap-1 text-left"
          >
            <ChevronDown aria-hidden="true" className="chat-group-chevron h-3.5 w-3.5 text-ash-gray" data-open={!collapsed} />
            <span className="truncate text-[14px] font-normal text-ash-gray">{title}</span>
          </button>
        ) : (
          <p className="flex-1 truncate px-2 text-[14px] font-normal text-ash-gray">{title}</p>
        )}
        {isGroup && section.group !== null && (
          <GroupMenu
            group={section.group}
            onRenameGroup={onRenameGroup}
            onDeleteGroup={onDeleteGroup}
          />
        )}
      </div>
      <div className="chat-group-items" data-open={!collapsed}>
        <div className="flex flex-col">
          {section.items.map((item) => (
            <ConversationItem
              key={item.id}
              item={item}
              current={item.id === currentId}
              groups={groups}
              onOpen={onOpen}
              onRename={onRename}
              onTogglePin={onTogglePin}
              onMoveToGroup={onMoveToGroup}
              onDelete={onDelete}
              onCreateGroup={onCreateGroup}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function GroupMenu({
  group,
  onRenameGroup,
  onDeleteGroup,
}: {
  group: ConversationGroup;
  onRenameGroup: (groupId: string, name: string) => void;
  onDeleteGroup: (groupId: string) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [name, setName] = useState(group.name);
  useEscShield(menuOpen || confirmOpen);

  const commitRename = () => {
    if (name.trim() !== '' && name.trim() !== group.name) {
      onRenameGroup(group.id, name.trim());
    }
    setRenaming(false);
  };

  if (renaming) {
    return (
      <div className="flex-1" onClick={(event) => event.stopPropagation()}>
        <input
          value={name}
          autoFocus
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') commitRename();
            if (event.key === 'Escape') {
              setName(group.name);
              setRenaming(false);
            }
          }}
          onBlur={commitRename}
          placeholder={copy.chat.sidebar.newGroupPlaceholder}
          className="h-9 w-full rounded-[var(--radius-inputs)] border border-hairline bg-paper-white px-3 text-[15px] outline-none placeholder:text-smoke-gray focus:border-ink-black"
        />
      </div>
    );
  }

  return (
    <>
      <DropdownMenu.Root open={menuOpen} onOpenChange={setMenuOpen} modal={false}>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            aria-label={copy.chat.sidebar.groupSectionAria(group.name)}
            className="inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-images)] text-ink-black transition-opacity duration-[var(--duration-fast)] data-[state=open]:opacity-100 hover:bg-mist-gray"
          >
            <Ellipsis aria-hidden="true" className="h-4 w-4" />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            sideOffset={4}
            align="end"
            className="ui-menu-content w-[160px] rounded-[var(--radius-elevatedcards)] bg-paper-white p-1 shadow-[var(--shadow-subtle)]"
          >
            <DropdownMenu.Item
              onSelect={() => setRenaming(true)}
              className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none data-[highlighted]:bg-mist-gray"
            >
              {copy.chat.sidebar.menuRename}
            </DropdownMenu.Item>
            <DropdownMenu.Item
              onSelect={() => setConfirmOpen(true)}
              className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-danger outline-none select-none data-[highlighted]:bg-mist-gray"
            >
              {copy.chat.sidebar.menuDelete}
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={copy.chat.sidebar.deleteDialogTitle}
        description={copy.chat.sidebar.deleteDialogDesc(group.name)}
        confirmLabel={copy.chat.sidebar.deleteConfirm}
        danger
        onConfirm={() => {
          onDeleteGroup(group.id);
          setConfirmOpen(false);
        }}
      />
    </>
  );
}

function ConversationItem({
  item,
  current,
  groups,
  onOpen,
  onRename,
  onTogglePin,
  onMoveToGroup,
  onDelete,
  onCreateGroup,
}: {
  item: ConversationSummary;
  current: boolean;
  groups: readonly ConversationGroup[];
  onOpen: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onTogglePin: (id: string, pinned: boolean) => void;
  onMoveToGroup: (id: string, groupId: string | null) => void;
  onDelete: (id: string) => void;
  onCreateGroup: (name: string) => Promise<string | null>;
}) {
  const [renaming, setRenaming] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [title, setTitle] = useState(item.title);
  const [newGroupName, setNewGroupName] = useState('');
  useEscShield(menuOpen || confirmOpen || renaming);
  // 触屏长按唤起与 ⋯ 相同的菜单（基座 §3.2）；长按已触发时吞掉松手后的 click，避免误开会话
  const longPress = useLongPress(() => setMenuOpen(true));

  const commitRename = () => {
    if (title.trim() !== '' && title.trim() !== item.title) {
      onRename(item.id, title.trim());
    }
    setRenaming(false);
  };

  const commitNewGroup = () => {
    const name = newGroupName.trim();
    if (name === '') return;
    setNewGroupName('');
    // m5：新建分组后直接移入当前会话（基座 §3.2「新建分组在列表中就地出现命名输入框」）
    void onCreateGroup(name).then((groupId) => {
      if (groupId !== null) {
        onMoveToGroup(item.id, groupId);
      }
    });
  };

  if (renaming) {
    return (
      <div className="px-1 py-0.5">
        <input
          value={title}
          autoFocus
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') commitRename();
            if (event.key === 'Escape') {
              setTitle(item.title);
              setRenaming(false);
            }
          }}
          onBlur={commitRename}
          placeholder={copy.chat.sidebar.renamePlaceholder}
          className="h-9 w-full rounded-[var(--radius-inputs)] border border-hairline bg-paper-white px-3 text-[15px] outline-none placeholder:text-smoke-gray focus:border-ink-black"
        />
      </div>
    );
  }

  return (
    <div className="group relative">
      <button
        type="button"
        onClick={() => {
          if (longPress.consumeFired()) return;
          onOpen(item.id);
        }}
        {...longPress.itemProps}
        className={
          'chat-conversation-item flex h-10 w-full cursor-pointer items-center rounded-[var(--radius-images)] px-3 pr-10 text-left ' +
          'transition-colors duration-[var(--duration-fast)] ' +
          (current ? 'bg-mist-gray font-w480' : 'hover:bg-mist-gray')
        }
      >
        <span className="min-w-0 flex-1 truncate text-[15px] font-normal text-ink-black">{displayTitle(item.title)}</span>
        {/* M4：会话条目相对时间（§3.2 标题+相对时间） */}
        <span className="ml-2 shrink-0 text-[13px] text-ash-gray">{formatRelativeTime(item.last_active_at)}</span>
      </button>
      {/* ⋯ 入口垂直居中对齐条目（40px）hover/选中态背景高度 */}
      <div className="absolute top-1/2 right-1 -translate-y-1/2">
        <DropdownMenu.Root open={menuOpen} onOpenChange={setMenuOpen} modal={false}>
          <DropdownMenu.Trigger asChild>
            <button
              type="button"
              aria-label={copy.chat.sidebar.itemMenuAria(displayTitle(item.title))}
              className="chat-item-menu-trigger inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-images)] text-ink-black transition-opacity duration-[var(--duration-fast)] opacity-0 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
            >
              <Ellipsis aria-hidden="true" className="h-4 w-4" />
            </button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              sideOffset={4}
              align="end"
              className="ui-menu-content w-[160px] rounded-[var(--radius-elevatedcards)] bg-paper-white p-1 shadow-[var(--shadow-subtle)]"
            >
              <DropdownMenu.Item
                onSelect={() => setRenaming(true)}
                className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none data-[highlighted]:bg-mist-gray"
              >
                {copy.chat.sidebar.menuRename}
              </DropdownMenu.Item>
              <DropdownMenu.Item
                onSelect={() => onTogglePin(item.id, !item.pinned)}
                className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none data-[highlighted]:bg-mist-gray"
              >
                {item.pinned ? copy.chat.sidebar.menuUnpin : copy.chat.sidebar.menuPin}
              </DropdownMenu.Item>
              <DropdownMenu.Sub>
                <DropdownMenu.SubTrigger className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none data-[highlighted]:bg-mist-gray">
                  {copy.chat.sidebar.menuMoveToGroup}
                </DropdownMenu.SubTrigger>
                <DropdownMenu.Portal>
                  <DropdownMenu.SubContent
                    sideOffset={8}
                    className="ui-menu-content w-[200px] rounded-[var(--radius-elevatedcards)] bg-paper-white p-1 shadow-[var(--shadow-subtle)]"
                  >
                    {groups.map((group) => (
                      <DropdownMenu.Item
                        key={group.id}
                        onSelect={() => onMoveToGroup(item.id, group.id)}
                        className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none data-[highlighted]:bg-mist-gray"
                      >
                        <span className="truncate">{group.name}</span>
                      </DropdownMenu.Item>
                    ))}
                    <DropdownMenu.Item
                      onSelect={(event) => event.preventDefault()}
                      className="flex h-9 cursor-default items-center rounded-[var(--radius-images)] px-3 text-[15px] text-slate-gray outline-none select-none"
                    >
                      {copy.chat.sidebar.newGroupPlaceholder}
                    </DropdownMenu.Item>
                    <div className="px-2 pb-1">
                      <input
                        value={newGroupName}
                        autoFocus
                        onChange={(event) => setNewGroupName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') commitNewGroup();
                          if (event.key === 'Escape') setNewGroupName('');
                        }}
                        placeholder={copy.chat.sidebar.newGroupPlaceholder}
                        className="h-8 w-full rounded-[var(--radius-inputs)] border border-hairline bg-paper-white px-2 text-[14px] outline-none placeholder:text-smoke-gray focus:border-ink-black"
                      />
                    </div>
                  </DropdownMenu.SubContent>
                </DropdownMenu.Portal>
              </DropdownMenu.Sub>
              {item.group_id !== null && (
                <DropdownMenu.Item
                  onSelect={() => onMoveToGroup(item.id, null)}
                  className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none data-[highlighted]:bg-mist-gray"
                >
                  {copy.chat.sidebar.menuMoveOutOfGroup}
                </DropdownMenu.Item>
              )}
              <DropdownMenu.Item
                onSelect={() => setConfirmOpen(true)}
                className="flex h-9 cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-danger outline-none select-none data-[highlighted]:bg-mist-gray"
              >
                {copy.chat.sidebar.menuDelete}
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={copy.chat.sidebar.deleteDialogTitle}
        description={copy.chat.sidebar.deleteDialogDesc(displayTitle(item.title))}
        confirmLabel={copy.chat.sidebar.deleteConfirm}
        danger
        onConfirm={() => {
          onDelete(item.id);
          setConfirmOpen(false);
        }}
      />
    </div>
  );
}
