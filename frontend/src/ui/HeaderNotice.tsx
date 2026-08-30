/*
 * 页头下轻提示条：radius-images、15px；出现后 3s 开始 --duration-fast 淡出，
 * 淡出结束回调 onDismiss（由父级移除）。
 * neutral：mist-gray 底 slate 文（默认）；success：成功绿文字；danger：低饱和红底 +
 * 深一档红边框 + 淡红文字（失败轻提示，聊天优化输入 §3.2 用）。
 */

import { useEffect, useRef, useState } from 'react';

/** 出现后开始淡出的停留时长（ms）。 */
const HOLD_MS = 3000;
/** 淡出时长（--duration-fast = 150ms）。 */
const FADE_MS = 150;

export interface HeaderNoticeProps {
  message: string;
  onDismiss?: () => void;
  /** success：成功绿 15px 文字（写操作成功轻提示，运维端 §7.3）；danger：失败红（§3.2）；默认 neutral slate。 */
  intent?: 'neutral' | 'success' | 'danger';
}

const INTENT_CLASS: Record<NonNullable<HeaderNoticeProps['intent']>, string> = {
  neutral: 'bg-mist-gray text-slate-gray',
  success: 'text-success',
  danger:
    'border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] ' +
    'bg-[color-mix(in_srgb,var(--color-danger)_10%,transparent)] px-4 ' +
    'text-[color-mix(in_srgb,var(--color-danger)_75%,white)]',
};

export function HeaderNotice({ message, onDismiss, intent = 'neutral' }: HeaderNoticeProps) {
  const [fading, setFading] = useState(false);
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;

  useEffect(() => {
    const fadeTimer = setTimeout(() => setFading(true), HOLD_MS);
    const dismissTimer = setTimeout(() => onDismissRef.current?.(), HOLD_MS + FADE_MS);
    return () => {
      clearTimeout(fadeTimer);
      clearTimeout(dismissTimer);
    };
  }, []);

  return (
    <div
      role="status"
      className={
        'rounded-[var(--radius-images)] px-3 py-2 text-[15px] ' +
        `${INTENT_CLASS[intent]} ` +
        `transition-opacity duration-[var(--duration-fast)] ${fading ? 'opacity-0' : 'opacity-100'}`
      }
    >
      {message}
    </div>
  );
}
