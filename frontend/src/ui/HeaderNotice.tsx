/*
 * 页头下轻提示条：mist-gray 底 radius-images padding 8px 12px、15px slate-gray。
 * 出现后 3s 开始 --duration-fast 淡出，淡出结束回调 onDismiss（由父级移除）。
 */

import { useEffect, useRef, useState } from 'react';

/** 出现后开始淡出的停留时长（ms）。 */
const HOLD_MS = 3000;
/** 淡出时长（--duration-fast = 150ms）。 */
const FADE_MS = 150;

export interface HeaderNoticeProps {
  message: string;
  onDismiss?: () => void;
  /** success：成功绿 15px 文字（写操作成功轻提示，运维端 §7.3）；默认 neutral slate。 */
  intent?: 'neutral' | 'success';
}

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
        'rounded-[var(--radius-images)] bg-mist-gray px-3 py-2 text-[15px] ' +
        `${intent === 'success' ? 'text-success' : 'text-slate-gray'} ` +
        `transition-opacity duration-[var(--duration-fast)] ${fading ? 'opacity-0' : 'opacity-100'}`
      }
    >
      {message}
    </div>
  );
}
