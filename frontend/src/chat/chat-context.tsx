/*
 * 聊天域的 React 绑定（fe-chat-home 规格；薄薄一层，逻辑全在纯 TS 层）。
 * ChatStore 与框架无关，本模块经 useSyncExternalStore 订阅；Batch C 的 UI 组件经
 * useChatStore / useChatState 消费状态与动作，不再触碰 store 内部。
 */

import { createContext, useContext, useSyncExternalStore, type ReactNode } from 'react';
import type { ChatStore, ChatStoreState } from './store';

const ChatContext = createContext<ChatStore | null>(null);

export function ChatProvider({ store, children }: { store: ChatStore; children: ReactNode }) {
  return <ChatContext.Provider value={store}>{children}</ChatContext.Provider>;
}

export function useChatStore(): ChatStore {
  const store = useContext(ChatContext);
  if (store === null) {
    throw new Error('useChatStore must be used within ChatProvider');
  }
  return store;
}

export function useChatState(): ChatStoreState {
  const store = useChatStore();
  return useSyncExternalStore(
    (listener) => store.subscribe(listener),
    () => store.getState(),
  );
}
