/*
 * 行级过渡定时器登记（审查 A35b）：行闪现 / 插入 / 淡出等短暂 setTimeout 在组件卸载时
 * 统一 clearTimeout，避免离屏后回调仍触发 setState。返回 schedule(callback, ms)；
 * 到点回调只执行一次并在执行前移出登记（不重复清理）。
 */

import { useCallback, useEffect, useRef } from 'react';

export function useRowTimers(): (callback: () => void, ms: number) => void {
  const clearsRef = useRef<Set<() => void>>(new Set());
  useEffect(() => {
    const clears = clearsRef.current;
    return () => {
      for (const clear of clears) {
        clear();
      }
      clears.clear();
    };
  }, []);
  return useCallback((callback: () => void, ms: number): void => {
    const clears = clearsRef.current;
    const timer = window.setTimeout(() => {
      clears.delete(clear);
      callback();
    }, ms);
    const clear = () => window.clearTimeout(timer);
    clears.add(clear);
  }, []);
}
