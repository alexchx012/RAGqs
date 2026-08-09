/*
 * 消息流（共用基座 §3.4；spec §4）：用户右对齐气泡 + AI 全宽；相邻 24px；新消息进入动效；
 * hover 淡入相对时间；生成中可滚动不强制吸底，用户上翻后浮出「回到底部」40px 圆钮。
 * 空态（新会话）：对话列垂直居中 Signifier 44px 问候语 + 输入区；首条消息后问候语淡出、输入区落底 400ms。
 * N4：Composer 单一宿主位，空态/消息态切换不跨分支重挂载（草稿/焦点由父级 HomePage 保持）。
 */

import { ArrowDown } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode, type UIEvent } from 'react';
import { copy } from '../../copy';
import { formatRelativeTime } from '../../notifications/relative-time';
import type { AbChoice, FeedbackVoteRequest } from '../types';
import { AssistantMessage } from './assistant-message';

export interface MessageListProps {
  readonly messages: readonly import('../store').ChatMessageView[];
  readonly onRetry: (messageId: string) => void;
  readonly onFeedback: (messageId: string, vote: FeedbackVoteRequest) => void;
  readonly onAbVote: (messageId: string, choice: AbChoice) => void;
  /** m2：反馈 / A/B 投票提交中锁定控件。 */
  readonly pendingSubmits?: readonly { readonly kind: 'feedback' | 'ab-vote'; readonly messageId: string }[];
  /** M11：输入区节点——空态时随问候语居中下方，首条消息后落底（400ms 过渡）。 */
  readonly composer?: ReactNode;
}

export function MessageList({
  messages,
  onRetry,
  onFeedback,
  onAbVote,
  pendingSubmits = [],
  composer,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [showScrollBottom, setShowScrollBottom] = useState(false);
  const isEmpty = messages.length === 0;
  // 问候语离场：消息到达后仍保持挂载，下一帧再 data-hidden 触发淡出（不可条件卸载）
  const [greetingHidden, setGreetingHidden] = useState(!isEmpty);

  useEffect(() => {
    if (!isEmpty) {
      const frame = requestAnimationFrame(() => setGreetingHidden(true));
      return () => cancelAnimationFrame(frame);
    }
    setGreetingHidden(false);
    return undefined;
  }, [isEmpty]);

  const onScroll = (event: UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    const distance = target.scrollHeight - target.scrollTop - target.clientHeight;
    setShowScrollBottom(distance > 120);
  };

  const scrollToBottom = () => {
    const target = scrollRef.current;
    if (target !== null) {
      target.scrollTo({ top: target.scrollHeight, behavior: 'smooth' });
    }
  };

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* 空态顶 spacer：与底 spacer 一起把问候语+输入区垂直居中（M11） */}
      {isEmpty && <div className="min-h-0 flex-1" aria-hidden="true" />}

      {/* 问候语始终挂载：首条消息后 data-hidden 淡出，避免条件卸载导致动画不可达 */}
      <div
        className="chat-empty-greeting flex shrink-0 flex-col items-center justify-center px-6"
        data-hidden={greetingHidden}
        aria-hidden={greetingHidden}
      >
        <h1 className="font-signifier text-[44px] font-normal leading-[1.3] tracking-[-0.66px] text-ink-black">
          {copy.chat.sidebar.emptyGreeting}
        </h1>
      </div>

      {!isEmpty && (
        <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-col gap-6 px-6">
            {messages.map((message) =>
              message.role === 'user' ? (
                <UserBubble key={message.id} content={message.content} createdAt={message.created_at} />
              ) : (
                <AssistantMessage
                  key={message.id}
                  message={message}
                  onRetry={onRetry}
                  onFeedback={onFeedback}
                  onAbVote={onAbVote}
                  pendingSubmits={pendingSubmits}
                />
              ),
            )}
          </div>
        </div>
      )}

      {/* N4：Composer 单一宿主——空态/消息态切换不换位重挂载 */}
      {composer !== undefined && (
        <div
          className={`chat-composer-settle shrink-0 ${isEmpty ? 'pb-2 pt-6' : 'pb-6 pt-2'}`}
          data-empty={isEmpty}
        >
          {composer}
        </div>
      )}

      {isEmpty && <div className="min-h-0 flex-1" aria-hidden="true" />}

      {showScrollBottom && (
        <button
          type="button"
          aria-label={copy.chat.message.scrollToBottom}
          onClick={scrollToBottom}
          className="chat-scroll-bottom absolute bottom-4 left-1/2 flex h-10 w-10 -translate-x-1/2 items-center justify-center rounded-full bg-paper-white transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray"
        >
          <ArrowDown aria-hidden="true" className="h-5 w-5 text-ink-black" />
        </button>
      )}
    </div>
  );
}

function UserBubble({ content, createdAt }: { content: string; createdAt: string }) {
  return (
    <div className="chat-message-enter flex justify-end">
      <div className="group flex max-w-[70%] flex-col items-end">
        <div className="chat-body-text rounded-[var(--radius-smallcards)] bg-mist-gray px-4 py-3 text-ink-black">
          {content}
        </div>
        <span className="mt-1 text-[15px] text-slate-gray opacity-0 transition-opacity duration-[var(--duration-fast)] group-hover:opacity-100">
          {formatRelativeTime(createdAt)}
        </span>
      </div>
    </div>
  );
}
