/*
 * 页头下轻提示条：radius-images、15px；出现后停留一段时间开始 --duration-fast 淡出，
 * 淡出结束回调 onDismiss（由父级移除）。neutral/success 停留 3s；
 * danger（失败轻提示，聊天优化输入 §3.2）驻留 8s 并渲染关闭按钮可手动关闭——
 * 失败信息需要足够的阅读时间，不能 3s 就自动消失且无从关闭（审查 A3）。
 */

import { X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { copy } from '../copy';

export interface HeaderNoticeProps {
  message: string;
  onDismiss?: () => void;
  /** success：成功绿 15px 文字（写操作成功轻提示，运维端 §7.3）；danger：失败红（§3.2，驻留 8s + 手动关闭）；默认 neutral slate-strong。 */
  intent?: 'neutral' | 'success' | 'danger';
}

/** 出现后开始淡出的停留时长（ms）：danger 驻留 8s（审查 A3），其余 3s。 */
const HOLD_MS: Record<NonNullable<HeaderNoticeProps['intent']>, number> = {
  neutral: 3000,
  success: 3000,
  danger: 8000,
};

/** 淡出时长（--duration-fast = 150ms）。 */
const FADE_MS = 150;

const INTENT_CLASS: Record<NonNullable<HeaderNoticeProps['intent']>, string> = {
  neutral: 'bg-mist-gray text-slate-strong',
  success: 'text-success',
  danger:
    'border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] ' +
    'bg-[color-mix(in_srgb,var(--color-danger)_10%,transparent)] pl-4 pr-9 ' +
    'text-[color-mix(in_srgb,var(--color-danger)_75%,white)]',
};

export function HeaderNotice({ message, onDismiss, intent = 'neutral' }: HeaderNoticeProps) {
  const [fading, setFading] = useState(false);
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;
  const holdMs = HOLD_MS[intent];

  useEffect(() => {
    const fadeTimer = setTimeout(() => setFading(true), holdMs);
    const dismissTimer = setTimeout(() => onDismissRef.current?.(), holdMs + FADE_MS);
    return () => {
      clearTimeout(fadeTimer);
      clearTimeout(dismissTimer);
    };
  }, [holdMs]);

  return (
    <div
      role="status"
      className={
        'relative rounded-[var(--radius-images)] px-3 py-2 text-[15px] ' +
        `${INTENT_CLASS[intent]} ` +
        `transition-opacity duration-[var(--duration-fast)] ${fading ? 'opacity-0' : 'opacity-100'}`
      }
    >
      {message}
      {intent === 'danger' && (
        <button
          type="button"
          aria-label={copy.a11y.dialogClose}
          onClick={() => onDismissRef.current?.()}
          className="absolute top-1/2 right-1.5 -translate-y-1/2 p-1.5 text-current"
        >
          <X aria-hidden="true" className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}
