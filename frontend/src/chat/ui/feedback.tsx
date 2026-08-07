/*
 * 常设 👍👎 反馈（共用基座 §3.4；契约 §3.8）。
 * 16px 图标、间距 8px、slate-gray；hover 变 ink；👍 直接计票变 ink 实心无 toast；
 * 👎 弹 200px 轻量浮层两行选项（「这个答案没依据」/「引用错了」），点选即提交并收起；
 * 已投后实心态固化、无改票入口。提交失败（网络）由调用方经 store 复用 Idempotency-Key 重试。
 */

import * as Popover from '@radix-ui/react-popover';
import { ThumbsDown, ThumbsUp } from 'lucide-react';
import { useState } from 'react';
import { copy } from '../../copy';
import { useEscShield } from '../../lib/esc-stack-provider';
import type { FeedbackDownReason, FeedbackState } from '../types';

export interface FeedbackProps {
  readonly messageId: string;
  readonly feedback: FeedbackState;
  readonly disabled?: boolean;
  readonly onVote: (messageId: string, vote: { vote: 'up' } | { vote: 'down'; reason: FeedbackDownReason }) => void;
}

export function Feedback({ messageId, feedback, disabled = false, onVote }: FeedbackProps) {
  const [downOpen, setDownOpen] = useState(false);
  useEscShield(downOpen);
  const upActive = feedback?.vote === 'up';
  const downActive = feedback?.vote === 'down';
  const locked = feedback !== null;

  const pickReason = (reason: FeedbackDownReason) => {
    setDownOpen(false);
    onVote(messageId, { vote: 'down', reason });
  };

  return (
    <div className="mt-2 flex items-center gap-2 text-slate-gray">
      <button
        type="button"
        aria-label={copy.chat.feedbackUpAria}
        disabled={disabled || locked}
        onClick={() => onVote(messageId, { vote: 'up' })}
        className={
          'inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-images)] ' +
          'transition-colors duration-[var(--duration-fast)] ' +
          (upActive ? 'text-ink-black' : 'hover:text-ink-black disabled:text-smoke-gray')
        }
      >
        <ThumbsUp aria-hidden="true" className={upActive ? 'h-4 w-4 fill-current' : 'h-4 w-4'} />
      </button>
      <Popover.Root open={downOpen} onOpenChange={setDownOpen}>
        <Popover.Trigger asChild>
          <button
            type="button"
            aria-label={copy.chat.feedbackDownAria}
            disabled={disabled || locked}
            className={
              'inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-images)] ' +
              'transition-colors duration-[var(--duration-fast)] ' +
              (downActive ? 'text-ink-black' : 'hover:text-ink-black disabled:text-smoke-gray')
            }
          >
            <ThumbsDown aria-hidden="true" className={downActive ? 'h-4 w-4 fill-current' : 'h-4 w-4'} />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            side="top"
            sideOffset={4}
            align="start"
            className="ui-menu-content w-[200px] rounded-[var(--radius-elevatedcards)] bg-paper-white p-1 shadow-[var(--shadow-subtle)]"
          >
            <p className="px-3 pt-2 text-[15px] font-w480 text-ink-black" aria-label={copy.chat.feedbackDownMenuAria}>
              {copy.chat.feedbackDownMenuAria}
            </p>
            <Popover.Close asChild>
              <button
                type="button"
                onClick={() => pickReason('no_grounding')}
                className="flex h-9 w-full cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none hover:bg-mist-gray"
              >
                {copy.chat.feedbackNoGrounding}
              </button>
            </Popover.Close>
            <Popover.Close asChild>
              <button
                type="button"
                onClick={() => pickReason('wrong_citation')}
                className="flex h-9 w-full cursor-pointer items-center rounded-[var(--radius-images)] px-3 text-[15px] text-ink-black outline-none select-none hover:bg-mist-gray"
              >
                {copy.chat.feedbackWrongCitation}
              </button>
            </Popover.Close>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}
