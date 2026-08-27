/*
 * 单条 assistant 回答渲染（共用基座 §3.4；spec §4–§6；动效 Streaming Text / orbs / web search）。
 * 组合：系统提示条 → 正文（Markdown + 引用角标 + 块状打字光标）→ 分档占位区（Orb 状态行 /
 * 深度研究步骤三态子弹头）→ 停止态小字 / 失败错误行+重试 → 常设 👍👎（A/B voted:false 期间隐藏）
 * → A/B 对比视图 → hover 淡入相对时间（与用户气泡一致，§3.4）。
 * 模拟流式正文来自 store 合并视图的 generation.content（实时进度）；终态后由读模型收敛。
 */

import { Info } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { copy } from '../../copy';
import { formatRelativeTime } from '../../notifications/relative-time';
import { Orb } from '../../ui/Orb';
import { TextLink } from '../../ui/TextLink';
import type { AssistantMessageView } from '../store';
import type {
  AbChoice,
  FeedbackVoteRequest,
  Notice,
  SseStagePhase,
  StopReason,
} from '../types';
import { AbCompare } from './ab-compare';
import { CitationBadges } from './citation-badge';
import { Feedback } from './feedback';
import { Markdown } from './markdown';

export interface AssistantMessageProps {
  readonly message: AssistantMessageView;
  readonly onRetry: (messageId: string) => void;
  readonly onFeedback: (messageId: string, vote: FeedbackVoteRequest) => void;
  readonly onAbVote: (messageId: string, choice: AbChoice) => void;
  /** m2：反馈 / A/B 投票提交中，锁定控件（禁连击同键不同请求体）。 */
  readonly pendingSubmits?: readonly { readonly kind: 'feedback' | 'ab-vote'; readonly messageId: string }[];
}

const NOTICE_TEXT: Record<string, (copyModule: typeof copy.chat) => string> = {
  effort_upgraded: (module) => module.notice.effortUpgraded,
  retrieval_degraded: (module) => module.notice.retrievalDegraded,
  rerank_degraded: (module) => module.notice.rerankDegraded,
};

function noticeText(notice: Notice): string {
  const mapper = NOTICE_TEXT[notice.kind];
  return mapper !== undefined ? mapper(copy.chat) : copy.chat.notice.generic;
}

const STAGE_TEXT: Record<SseStagePhase, string> = {
  retrieving: copy.chat.stage.retrieving,
  retrieving_again: copy.chat.stage.retrievingAgain,
  generating: copy.chat.stage.generating,
};

export function AssistantMessage({
  message,
  onRetry,
  onFeedback,
  onAbVote,
  pendingSubmits = [],
}: AssistantMessageProps) {
  const { generation, status, ab, stop_reason } = message;
  const [stepsOpen, setStepsOpen] = useState(false);
  // A/B 左右随机化：按挂载随机一次（刷新后重新随机，不揭晓配置）；hook 无条件调用
  const leftCandidate = useMountSide();

  const notices = generation.notices;
  const isAb = ab !== null;
  const abOpen = ab?.status === 'open';
  const generating = status === 'generating';

  // A/B 候选正文：实时流（generation.abContents 含每候选引用，M7）优先，读模型重建（ab.candidates）兜底
  const abCandidates =
    generation.abContents !== null
      ? generation.abContents.map((entry) => ({
          candidate: entry.candidate,
          content: entry.content,
          citations: entry.citations,
        }))
      : ab?.candidates?.map((candidate) => ({ candidate: candidate.candidate, content: candidate.content, citations: candidate.citations })) ?? [];

  // 已投票 0/1：message.content 为所选候选正文（store 已收敛），走普通回答
  const showFeedback =
    !isAb ||
    (ab?.status === 'voted' && ab.choice !== 'neither' && ab.choice !== null);

  let body: ReactNode;
  const isAbVotePending = pendingSubmits.some(
    (item) => item.messageId === message.id && item.kind === 'ab-vote',
  );

  if (abOpen) {
    // 对比视图：两候选均发布、可投票；m2：提交中锁定禁连击不同 choice
    body = (
      <AbCompare
        messageId={message.id}
        candidates={abCandidates}
        leftCandidate={leftCandidate}
        onVote={onAbVote}
        disabled={isAbVotePending}
      />
    );
  } else if (isAb && ab?.status === 'pending') {
    // m13：ab_start 后候选尚未到达——分档占位区（打字光标 + 阶段状态行），正文到达后由候选接管
    const single = abCandidates[0];
    body = (
      <div className="chat-body-text leading-[var(--leading-body)] text-ink-black">
        {single !== undefined && (
          <>
            <Markdown markdown={single.content} />
            <CitationBadges citations={single.citations} messageId={message.id} />
          </>
        )}
        {generating && (
          <>
            <span className="chat-caret" aria-hidden="true" />
            {generation.stage !== null && (
              <div className="chat-stage-swap mt-1 flex items-center gap-2 text-[15px] text-slate-gray">
                <Orb size={16} className="text-ink-black" />
                {STAGE_TEXT[generation.stage]}
              </div>
            )}
          </>
        )}
      </div>
    );
  } else if (isAb && ab?.status === 'voted' && ab.choice === 'neither') {
    // neither：不保留任何候选正文，不渲染常设 👍👎（spec §6）
    body = null;
  } else if (generation.requestError !== null) {
    // M2：请求级错误（409 idempotency_key_conflict / 406 / pre-start 网络耗尽）就地呈现，
    // 输入区已解锁可再次提问
    body = (
      <div className="mt-1 flex items-center gap-2">
        <p className="text-[15px] text-danger">{copy.chat.requestError}</p>
      </div>
    );
  } else {
    body = (
      <div className="chat-body-text leading-[var(--leading-body)] text-ink-black">
        <Markdown markdown={message.content} />
        <CitationBadges citations={message.citations} messageId={message.id} />
        {generating && <span className="chat-caret" aria-hidden="true" />}
        {generating && message.content === '' && generation.stage !== null && (
          <div className="chat-stage-swap mt-1 flex items-center gap-2 text-[15px] text-slate-gray">
            <Orb size={16} className="text-ink-black" />
            {STAGE_TEXT[generation.stage]}
          </div>
        )}
    {generation.steps.length > 0 && (
          <DeepSteps
            steps={generation.steps}
            open={stepsOpen}
            onToggle={() => setStepsOpen((value) => !value)}
            collapsed={!generating}
          />
        )}
        {status === 'stopped' && stop_reason !== null && (
          <p className="mt-1 text-[15px] text-smoke-gray">{stopReasonText(stop_reason)}</p>
        )}
        {status === 'failed' && (
          <div className="mt-2 flex items-center gap-2">
            <p className="text-[15px] text-danger">{copy.chat.message.errorLine}</p>
            <TextLink onClick={() => onRetry(message.id)}>{copy.chat.message.retry}</TextLink>
          </div>
        )}
      </div>
    );
  }

  const isPendingSubmit = pendingSubmits.some(
    (item) => item.messageId === message.id && (item.kind === 'feedback' || item.kind === 'ab-vote'),
  );

  return (
    <div className="chat-message-enter group">
      {notices.length > 0 && (
        <div className="mb-2 flex flex-col gap-2">
          {notices.map((notice, index) => (
            <div
              key={index}
              className="chat-notice-enter flex w-fit items-center gap-3 rounded-[var(--radius-images)] bg-mist-gray px-3 py-2"
            >
              <Info aria-hidden="true" className="h-4 w-4 shrink-0 text-slate-gray" />
              <p className="text-[15px] text-slate-gray">{noticeText(notice)}</p>
            </div>
          ))}
        </div>
      )}
      {body}
      {showFeedback && !generating && status !== 'failed' && (
        <Feedback
          messageId={message.id}
          feedback={message.feedback}
          disabled={isPendingSubmit}
          onVote={onFeedback}
        />
      )}
      {/* hover 回答下方淡入相对时间（基座 §3.4，与用户气泡一致；reduced-motion 由 base.css 全局直出） */}
      <span className="mt-1 block text-[15px] text-slate-gray opacity-0 transition-opacity duration-[var(--duration-fast)] group-hover:opacity-100">
        {formatRelativeTime(message.created_at)}
      </span>
    </div>
  );
}

function stopReasonText(reason: StopReason): string {
  switch (reason) {
    case 'manual_request':
      return copy.chat.stopReason.manualRequest;
    case 'client_disconnected':
      return copy.chat.stopReason.clientDisconnected;
    case 'authorization_revoked':
      return copy.chat.stopReason.authorizationRevoked;
  }
}

type DeepStep = {
  readonly index: number;
  readonly label: string;
  readonly state: 'active' | 'done';
};

/** 深度研究步骤列表：进行中底部追加；完成后折叠为「已完成 N 步」可展开。 */
function DeepSteps({
  steps,
  open,
  onToggle,
  collapsed,
}: {
  steps: readonly DeepStep[];
  open: boolean;
  onToggle: () => void;
  collapsed: boolean;
}) {
  const doneCount = steps.filter((step) => step.state === 'done').length;
  if (collapsed) {
    return (
      <div className="mt-2">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-label={open ? copy.chat.stepsCollapseAria : copy.chat.stepsExpandAria}
          className="flex items-center gap-2 text-[15px] text-slate-gray transition-colors duration-[var(--duration-fast)] hover:text-ink-black"
        >
          <span className="inline-flex h-3 w-3 items-center justify-center">
            <ChevronDown className="chat-steps-collapse-chevron" data-open={open} />
          </span>
          {copy.chat.stepsDone(doneCount)}
        </button>
        <div className="chat-steps-collapse" data-open={open}>
          <StepsRail steps={steps} className="mt-2" />
        </div>
      </div>
    );
  }
  return <StepsRail steps={steps} className="mt-2" />;
}

/** 步骤列表（动效 web search）：左侧 1px 轨道线贯穿全部行，行内子弹头三态切换。 */
function StepsRail({ steps, className = '' }: { steps: readonly DeepStep[]; className?: string }) {
  return (
    <div className={`chat-steps-list ${className}`}>
      <span className="chat-steps-rail" aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {steps.map((step, index) => (
          <StepRow key={`${step.index}:${step.label}:${index}`} step={step} />
        ))}
      </div>
    </div>
  );
}

/* 步骤行（动效 web search）：12px 子弹头两态切换——active 经线地球淡入、done 成功勾弹入；
 * active 文案 shimmer 扫光。 */
function StepRow({ step }: { step: DeepStep }) {
  return (
    <div
      className="chat-step-enter chat-step-row flex items-center gap-2 text-[15px]"
      data-state={step.state}
    >
      <span className="chat-step-bullet" aria-hidden="true">
        <span className="chat-step-globe">
          <Globe />
        </span>
        <Check className="chat-step-check h-3 w-3" />
      </span>
      {step.state === 'active' ? (
        <span className="chat-step-label-active">{copy.chat.stepLabel(step.label)}</span>
      ) : (
        <span className="text-slate-gray">{copy.chat.stepLabel(step.label)}</span>
      )}
    </div>
  );
}

/* 转场地球（动效 web search）：六条经线相位偏移 1/6 周期，SMIL 形变读作一颗旋转球体。 */
const MERIDIAN = {
  L: 'M6.057 11.565 C2.081 11.565 0.371 8.159 0.371 5.964 C0.371 3.642 2.152 0.329 6.05 0.329',
  ML: 'M6.012 11.55 C4.575 10.496 3.333 8.116 3.321 5.964 C3.307 3.399 4.974 0.977 6.012 0.329',
  MR: 'M6.012 11.55 C7.211 10.781 8.715 8.287 8.715 5.964 C8.715 3.399 7.24 1.233 6.012 0.329',
  R: 'M6.012 11.55 C9.677 11.55 11.65 8.487 11.65 5.964 C11.65 3.499 9.748 0.329 6.012 0.329',
};

function Globe() {
  const values = [MERIDIAN.L, MERIDIAN.ML, MERIDIAN.MR, MERIDIAN.R, MERIDIAN.L].join(';');
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="0.85"
      strokeLinecap="round"
      style={{ overflow: 'visible' }}
      aria-hidden="true"
    >
      <circle cx="6" cy="6" r="5.7" opacity="0.9" />
      <line x1="0.3" y1="6" x2="11.7" y2="6" opacity="0.9" />
      {['0s', '-1.2s', '-2.4s', '-3.6s', '-4.8s', '-6s'].map((begin) => (
        <path key={begin} d={MERIDIAN.L} opacity="0">
          <animate
            attributeName="d"
            dur="7.2s"
            begin={begin}
            repeatCount="indefinite"
            calcMode="spline"
            keyTimes="0;0.25;0.5;0.75;1"
            keySplines="0.42 0 0.58 1;0.42 0 0.58 1;0.42 0 0.58 1;0.42 0 0.58 1"
            values={values}
          />
          <animate
            attributeName="opacity"
            dur="7.2s"
            begin={begin}
            repeatCount="indefinite"
            calcMode="linear"
            keyTimes="0;0.05;0.7;0.75;1"
            values="0;0.9;0.9;0;0"
          />
        </path>
      ))}
    </svg>
  );
}

function useMountSide(): 0 | 1 {
  const [side] = useState<0 | 1>(() => (Math.random() < 0.5 ? 0 : 1));
  return side;
}

function ChevronDown({ className = '', ...rest }: { className?: string; [key: string]: unknown }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" className={`h-3 w-3 ${className}`} {...rest}>
      <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Check({ className = '', ...rest }: { className?: string; [key: string]: unknown }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" className={`h-3 w-3 ${className}`} {...rest}>
      <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
