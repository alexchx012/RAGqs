/*
 * 聊天主页（fe-chat-home）：登录落地页。替换 fe-shared-shell 占位，保留既有行为：
 * - 主区域右上角铃铛（距顶 20px、距右 24px，共用基座 §3.1）；
 * - 运维登录落地标记自动展开管理段（router/landing.ts AUTO_OPEN_ADMIN_DRAWER_STATE_KEY，replace 消费）；
 * - 侧边栏底部头像区按角色打开抽屉（/settings | /admin/dashboard，共用基座 §3.2）；
 * - 抽屉为 URL 驱动的覆盖层，主页在其下保持挂载不卸载：输入草稿原样保留（共用基座 §5.1）。
 * 布局（§3.1）：左侧边栏 280px fog-white + 1px hairline；主区域对话列限宽 760px 居中 padding 24px；
 * 输入区粘底同宽、底距 24px；窄屏 <768px 侧边栏收为滑出抽屉（chat/ui/sidebar）。
 * ChatStore 在此以惰性 useState 装配（复用 auth 会话层 single-flight refresh 与 token）。
 */

import { useEffect, useRef, useState, type MutableRefObject } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { createApiClient } from '../api/client';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import { createChatApi } from '../chat/api';
import { ChatProvider, useChatStore, useChatState } from '../chat/chat-context';
import { ChatStore } from '../chat/store';
import { ChatMenuButton, ChatSidebar } from '../chat/ui/sidebar';
import { Composer } from '../chat/ui/composer';
import { MessageList } from '../chat/ui/message-list';
import { copy } from '../copy';
import { useNotifications } from '../notifications/NotificationsProvider';
import { AUTO_OPEN_ADMIN_DRAWER_STATE_KEY } from '../router/landing';
import { ShellBell } from './ShellBell';
import type { ConversationScope, EffortLevel } from '../chat/types';
import type { ScopeSelection } from '../chat/ui/scope-chip';

function createChatStore(authStore: ReturnType<typeof useAuthStore>): ChatStore {
  const client = createApiClient({
    getAccessToken: () => authStore.getState().token,
    refresh: () => authStore.refresh(),
  });
  const api = createChatApi(client);
  return new ChatStore({
    api,
    getToken: () => authStore.getState().token,
    refresh: () => authStore.refresh(),
    getReducedMotion: () =>
      typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
        : false,
  });
}

/** 会话内记住档位与范围、新会话重置（共用基座 §3.3）：以 conversationId 为键的记忆表。 */
interface ComposerMemory {
  readonly effortLevel: EffortLevel;
  readonly selection: ScopeSelection;
}

const DEFAULT_MEMORY: ComposerMemory = { effortLevel: 'quick', selection: { space_ids: [], document_ids: [] } };

export function HomePage() {
  const { user } = useAuthState();
  const authStore = useAuthStore();
  const navigate = useNavigate();
  const location = useLocation();
  const notifications = useNotifications();
  const [chat] = useState(() => createChatStore(authStore));
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [composerMemory, setComposerMemory] = useState<ComposerMemory>(DEFAULT_MEMORY);
  const memoryByConversation = useRef(new Map<string, ComposerMemory>());

  // m8：卸载时 dispose ChatStore（中止 SSE / 退避计时器 / 模拟器计时器）
  useEffect(() => {
    const store = chat;
    return () => {
      store.dispose();
    };
  }, [chat]);

  // 运维登录落地标记：自动展开管理段首层；replace 导航消费标记、不留历史条目（沿用占位行为）
  useEffect(() => {
    const state = location.state as Record<string, unknown> | null;
    if (state?.[AUTO_OPEN_ADMIN_DRAWER_STATE_KEY] === true) {
      navigate('/admin/dashboard', { replace: true });
    }
  }, [location.state, navigate]);

  const openDrawer = () => {
    navigate(user !== null && (user.role === 'ops' || user.role === 'admin') ? '/admin/dashboard' : '/settings');
  };

  return (
    <ChatProvider store={chat}>
      <ChatHomeInner
        user={user}
        notifications={notifications}
        drawerOpen={drawerOpen}
        onDrawerOpenChange={setDrawerOpen}
        onOpenDrawer={openDrawer}
        composerMemory={composerMemory}
        onComposerMemoryChange={setComposerMemory}
        memoryByConversation={memoryByConversation}
      />
    </ChatProvider>
  );
}

interface ChatHomeInnerProps {
  readonly user: ReturnType<typeof useAuthState>['user'];
  readonly notifications: ReturnType<typeof useNotifications>;
  readonly drawerOpen: boolean;
  readonly onDrawerOpenChange: (open: boolean) => void;
  readonly onOpenDrawer: () => void;
  readonly composerMemory: ComposerMemory;
  readonly onComposerMemoryChange: (memory: ComposerMemory) => void;
  readonly memoryByConversation: MutableRefObject<Map<string, ComposerMemory>>;
}

function ChatHomeInner({
  user,
  notifications,
  drawerOpen,
  onDrawerOpenChange,
  onOpenDrawer,
  composerMemory,
  onComposerMemoryChange,
  memoryByConversation,
}: ChatHomeInnerProps) {
  const store = useChatStore();
  const state = useChatState();
  const [spaces, setSpaces] = useState<readonly import('../chat/types').SpaceItem[]>([]);

  // 首次挂载：拉会话列表 + 检索空间；随后自动打开最近会话（登录落地恢复）
  useEffect(() => {
    void store.loadConversationList();
    void store.fetchSpaces().then((items) => {
      if (items !== null) setSpaces(items);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 列表就绪且未打开会话：自动打开最近一条（历史会话恢复；空列表则停留在空态问候语）
  useEffect(() => {
    if (state.listStatus !== 'ready' || state.conversationId !== null) return;
    const first = state.visibleConversations[0];
    if (first !== undefined) {
      void store.openConversation(first.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.listStatus, state.visibleConversations.length]);

  // 会话切换：加载该会话记忆（无则默认快速 + 全部范围）；新建会话重置
  const conversationId = state.conversationId ?? '';
  useEffect(() => {
    if (conversationId === '') return;
    const memory = memoryByConversation.current.get(conversationId) ?? DEFAULT_MEMORY;
    onComposerMemoryChange(memory);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  const openConversation = (id: string) => {
    onDrawerOpenChange(false); // 窄屏抽屉：选中会话后关闭
    void store.openConversation(id);
  };

  const newConversation = async () => {
    onDrawerOpenChange(false);
    const created = await store.createConversation();
    if (created !== null) {
      onComposerMemoryChange(DEFAULT_MEMORY);
    }
  };

  const rememberMemory = () => {
    if (conversationId !== '') {
      memoryByConversation.current.set(conversationId, composerMemory);
    }
  };

  const onEffortChange = (effortLevel: EffortLevel) => {
    const next = { ...composerMemory, effortLevel };
    onComposerMemoryChange(next);
    if (conversationId !== '') memoryByConversation.current.set(conversationId, next);
  };

  const onSelectionChange = (selection: ScopeSelection) => {
    const next = { ...composerMemory, selection };
    onComposerMemoryChange(next);
    if (conversationId !== '') memoryByConversation.current.set(conversationId, next);
  };

  const send = async (content: string): Promise<boolean> => {
    rememberMemory();
    if (state.conversationId === null) {
      const created = await store.createConversation();
      if (created === null) return false;
      onComposerMemoryChange(DEFAULT_MEMORY);
    }
    // scope 省略 = 全部范围；document_ids 仅个人库文档级收窄时携带（ScopeChip 已约束）
    const scope: ConversationScope | undefined =
      composerMemory.selection.space_ids.length === 0 && composerMemory.selection.document_ids.length === 0
        ? undefined
        : {
            space_ids: [...composerMemory.selection.space_ids],
            document_ids: [...composerMemory.selection.document_ids],
          };
    return store.ask(content, composerMemory.effortLevel, scope);
  };

  // m4：ScopeChip 文档名过滤 q 必须透传到 listDocuments（不得丢掉第二参）
  const fetchDocuments = async (spaceId: string, q?: string) => {
    const result = await store.fetchSpaceDocuments(spaceId, q);
    return result?.items.map((doc) => ({ id: doc.id, name: doc.name })) ?? null;
  };

  const session = state.session;
  // M2：requestError 视为可恢复终态——解锁输入区，允许再次发送（session 仍保留错误视图至下次 ask 清理）
  const generating =
    session !== null && session.terminal === null && session.requestError === null;
  const canStop =
    generating && session?.start !== null && !session.stopRequested && session.phase !== 'stopping';
  const stopping = session?.phase === 'stopping' || session?.stopRequested === true;

  return (
    <div className="flex min-h-screen">
      <ChatSidebar
        user={user}
        conversations={state.visibleConversations}
        groups={state.groups}
        listStatus={state.listStatus}
        currentId={state.conversationId}
        searchQuery={state.searchQuery}
        drawerOpen={drawerOpen}
        onDrawerOpenChange={onDrawerOpenChange}
        onSearchChange={(query) => store.setSearchQuery(query)}
        onNewConversation={() => void newConversation()}
        onOpen={openConversation}
        onRename={(id, title) => void store.patchConversation(id, { title })}
        onTogglePin={(id, pinned) => void store.patchConversation(id, { pinned })}
        onMoveToGroup={(id, groupId) => void store.patchConversation(id, { group_id: groupId })}
        onDelete={(id) => void store.deleteConversation(id)}
        onRenameGroup={(groupId, name) => void store.patchGroup(groupId, name)}
        onDeleteGroup={(groupId) => void store.deleteGroup(groupId)}
        onCreateGroup={(name) => store.createGroup(name)}
        onRetryLoad={() => void store.loadConversationList()}
        onOpenDrawer={onOpenDrawer}
      />
      <div className="relative flex min-w-0 flex-1 flex-col bg-paper-white">
        <div className="absolute right-6 top-5 z-10">
          <ShellBell store={notifications} />
        </div>
        <ChatMenuButton onOpen={() => onDrawerOpenChange(true)} />
        <main className="flex min-h-0 flex-1 flex-col">
          <div className="mx-auto flex w-full max-w-[760px] flex-1 flex-col px-6">
            {state.actionNotice !== null && (
              <div className="chat-notice-enter mt-4 flex items-center gap-2 rounded-[var(--radius-images)] bg-mist-gray px-3 py-2">
                <p className="text-[15px] text-slate-gray">
                  {state.actionNotice.type === 'feedback_conflict'
                    ? copy.chat.feedbackConflict
                    : copy.chat.abConflict}
                </p>
              </div>
            )}
            <MessageList
              conversationId={state.conversationId}
              messages={state.messages}
              onRetry={(messageId) => void store.retry(messageId)}
              onFeedback={(messageId, vote) => void store.submitFeedback(messageId, vote)}
              onAbVote={(messageId, choice) => void store.submitAbVote(messageId, choice)}
              pendingSubmits={state.pendingSubmits}
              composer={
                <Composer
                  effortLevel={composerMemory.effortLevel}
                  onEffortChange={onEffortChange}
                  spaces={spaces}
                  onFetchDocuments={fetchDocuments}
                  selection={composerMemory.selection}
                  onSelectionChange={onSelectionChange}
                  generating={generating}
                  canStop={canStop}
                  stopping={stopping}
                  onSend={send}
                  onStop={() => store.stop()}
                />
              }
            />
          </div>
        </main>
      </div>
    </div>
  );
}
