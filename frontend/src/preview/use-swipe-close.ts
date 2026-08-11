/*
 * 窄屏命中面板下滑关闭手势（fe-preview-swipe-close；共用基座 §6「Esc / 下滑关闭」）。
 * Pointer Events 实现，无新依赖：拖动跟手（translateY），松手超过关闭阈值或快速下滑（flick）
 * 则滑出关闭，未达阈值回弹展开位置；关闭/回弹 250ms --ease-out，prefers-reduced-motion 直出。
 * 滚动冲突：仅当面板滚动容器 scrollTop 为 0 且向下拖过 slop 才进入手势，其余保持列表原生滚动；
 * 面板标题行 touch-action: none，保证触屏从标题行拖动时浏览器不接管（列表区保持 pan-y 原生滚动）。
 */

import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';

/** 关闭阈值上限（px）；实际阈值取 min(面板高 40%, 80)。 */
const CLOSE_THRESHOLD_MAX_PX = 80;
const CLOSE_THRESHOLD_RATIO = 0.4;
/** flick 速度阈值（px/ms）：松手时平均速度超过即关闭。 */
const FLICK_VELOCITY_PX_PER_MS = 0.3;
/** 判定拖动意图的最小位移（px）。 */
const DRAG_SLOP_PX = 6;
/** 关闭滑出 / 回弹动画时长（--duration-base 250ms）。 */
const ANIMATION_MS = 250;

interface GestureState {
  pointerId: number;
  startY: number;
  startTime: number;
  tracking: boolean;
  dragging: boolean;
}

export interface SwipeClosePanelProps {
  readonly ref: (node: HTMLDivElement | null) => void;
  readonly style: CSSProperties;
  readonly onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  readonly onPointerMove: (event: ReactPointerEvent<HTMLDivElement>) => void;
  readonly onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => void;
  readonly onPointerCancel: (event: ReactPointerEvent<HTMLDivElement>) => void;
}

export function useSwipeClose(onClose: () => void): { readonly panelProps: SwipeClosePanelProps } {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const gestureRef = useRef<GestureState | null>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // offset：当前跟手位移；closing：滑出关闭中；engaged：手势已接管（关闭入场动画，交给内联 transform）
  const [offset, setOffset] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [closing, setClosing] = useState(false);
  const [engaged, setEngaged] = useState(false);

  const reducedMotion = useCallback(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    [],
  );

  useEffect(
    () => () => {
      if (closeTimerRef.current !== null) {
        clearTimeout(closeTimerRef.current);
      }
    },
    [],
  );

  const setPanelNode = useCallback((node: HTMLDivElement | null) => {
    panelRef.current = node;
    gestureRef.current = null;
    if (closeTimerRef.current !== null) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    if (node !== null) {
      // 面板每次重新打开都从零会话开始：清掉上次关闭/拖动残留的位移与动画状态，
      // 否则再打开会停在 translateY(100%) 屏外、入场动画被 animation:'none' 抑制、手势被守卫拒绝
      setOffset(0);
      setDragging(false);
      setClosing(false);
      setEngaged(false);
    }
  }, []);

  const finishClose = useCallback(() => {
    if (reducedMotion()) {
      onCloseRef.current();
      return;
    }
    setClosing(true);
    closeTimerRef.current = setTimeout(() => {
      closeTimerRef.current = null;
      onCloseRef.current();
    }, ANIMATION_MS);
  }, [reducedMotion]);

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (gestureRef.current !== null || closeTimerRef.current !== null) {
      return; // 已在手势或关闭动画中
    }
    gestureRef.current = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startTime: Date.now(),
      tracking: true,
      dragging: false,
    };
  }, []);

  const onPointerMove = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = gestureRef.current;
    const panel = panelRef.current;
    if (gesture === null || !gesture.tracking || event.pointerId !== gesture.pointerId || panel === null) {
      return;
    }
    const dy = event.clientY - gesture.startY;
    if (!gesture.dragging) {
      if (dy > DRAG_SLOP_PX) {
        // 滚动容器不在顶部时向下拖属于列表回弹/滚动，不进入关闭手势
        const scroller = panel.querySelector<HTMLElement>('[data-swipe-scroll]');
        if (scroller !== null && scroller.scrollTop > 0) {
          gesture.tracking = false;
          return;
        }
        gesture.dragging = true;
        panel.setPointerCapture?.(event.pointerId);
        setDragging(true);
        setEngaged(true);
      } else if (dy < -DRAG_SLOP_PX) {
        gesture.tracking = false; // 向上拖：列表原生滚动
        return;
      }
    }
    if (gesture.dragging) {
      setOffset(Math.max(0, dy));
    }
  }, []);

  const settleGesture = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const gesture = gestureRef.current;
      const panel = panelRef.current;
      if (gesture === null || event.pointerId !== gesture.pointerId) {
        return;
      }
      gestureRef.current = null;
      if (!gesture.dragging) {
        return;
      }
      panel?.releasePointerCapture?.(event.pointerId);
      setDragging(false);

      const dy = Math.max(0, event.clientY - gesture.startY);
      const elapsed = Math.max(1, Date.now() - gesture.startTime);
      const velocity = dy / elapsed;
      const panelHeight = panel?.getBoundingClientRect().height ?? 0;
      const threshold =
        panelHeight > 0 ? Math.min(panelHeight * CLOSE_THRESHOLD_RATIO, CLOSE_THRESHOLD_MAX_PX) : CLOSE_THRESHOLD_MAX_PX;

      if (dy >= threshold || velocity >= FLICK_VELOCITY_PX_PER_MS) {
        finishClose();
        return;
      }
      // 未达阈值：回弹展开位置（reduced-motion 由 transition 直出语义一致，直接归零）
      setOffset(0);
    },
    [finishClose],
  );

  const style: CSSProperties = engaged
    ? {
        animation: 'none',
        transform: closing ? 'translateY(100%)' : `translateY(${offset}px)`,
        transition: dragging ? 'none' : 'transform var(--duration-base) var(--ease-out)',
      }
    : {};

  return {
    panelProps: {
      ref: setPanelNode,
      style,
      onPointerDown,
      onPointerMove,
      onPointerUp: settleGesture,
      onPointerCancel: settleGesture,
    },
  };
}
