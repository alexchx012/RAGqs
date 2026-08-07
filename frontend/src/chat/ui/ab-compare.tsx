/*
 * 盲测 A/B 对比视图（共用基座 §3.4；契约 §3.9）。
 * 两候选随机化左右顺序、完全盲测；每列底部 ghost pill「选这条」按该列候选的 candidate 序号投票
 * （与界面左右解耦）；区块底部「两个都不选，继续」= choice 'neither'。
 * 左右随机化由父组件（assistant-message）按挂载随机一次传入（刷新后重新随机，不揭晓配置）。
 */

import { useCallback } from 'react';
import { copy } from '../../copy';
import type { AbChoice, Citation } from '../types';
import { CitationBadges } from './citation-badge';
import { Markdown } from './markdown';

export interface AbCompareCandidate {
  readonly candidate: 0 | 1;
  readonly content: string;
  readonly citations: readonly Citation[];
}

export interface AbCompareProps {
  readonly messageId: string;
  readonly candidates: readonly AbCompareCandidate[];
  readonly leftCandidate: 0 | 1;
  readonly onVote: (messageId: string, choice: AbChoice) => void;
  /** m2：投票提交中锁定（禁连击同键不同请求体）。 */
  readonly disabled?: boolean;
}

export function AbCompare({ messageId, candidates, leftCandidate, onVote, disabled = false }: AbCompareProps) {
  const left = candidates.find((candidate) => candidate.candidate === leftCandidate);
  const right = candidates.find((candidate) => candidate.candidate === (leftCandidate === 0 ? 1 : 0));
  const vote = useCallback(
    (candidate: 0 | 1) => {
      onVote(messageId, String(candidate) as '0' | '1');
    },
    [messageId, onVote],
  );

  return (
    <div
      role="region"
      aria-label={copy.chat.abCompareAria}
      className="mt-4 grid gap-6 md:grid-cols-2"
    >
      {[left, right].map((candidate, index) =>
        candidate === undefined ? null : (
          <div
            key={candidate.candidate}
            data-side={candidate.candidate === leftCandidate ? 'left' : 'right'}
            className={
              'chat-ab-column rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 ' +
              'shadow-[var(--shadow-subtle)] ' +
              (index === 1 ? 'chat-ab-second-enter' : '')
            }
          >
            <div className="text-[17px] leading-[var(--leading-body)] text-ink-black">
              <Markdown markdown={candidate.content} />
              <CitationBadges citations={candidate.citations} />
            </div>
            <div className="mt-4 flex justify-center">
              <button
                type="button"
                disabled={disabled}
                onClick={() => vote(candidate.candidate)}
                aria-label={copy.chat.abVoteOptionAria(String(candidate.candidate))}
                className="inline-flex h-9 items-center justify-center rounded-[var(--radius-buttons)] border border-ink-black px-4 text-[15px] text-ink-black transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray disabled:border-hairline disabled:text-smoke-gray disabled:hover:bg-transparent"
              >
                {copy.chat.abPickThis}
              </button>
            </div>
          </div>
        ),
      )}
      <div className="col-span-full text-center">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onVote(messageId, 'neither')}
          className="text-[15px] text-slate-gray underline-offset-2 transition-colors duration-[var(--duration-fast)] hover:underline hover:text-ink-black disabled:text-smoke-gray disabled:hover:no-underline"
        >
          {copy.chat.abChoiceNeither}
        </button>
      </div>
    </div>
  );
}
