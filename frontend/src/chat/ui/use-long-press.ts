/*
 * 触屏长按唤起会话条目「⋯」菜单（共用基座 §3.2「触屏长按唤起同一菜单」；D1）。
 * Pointer Events 自实现，无新依赖：非 mouse 指针按住 500ms 触发；位移超 10px（滚动/侧滑）
 * 或松手/取消即放弃。触发后吞掉随之而来的 click（避免松手误触打开会话），并抑制按压期间
 * 与触发后的原生 contextmenu（触屏长按默认弹系统菜单）；mouse 指针全程不参与，桌面行为不变。
 */

import {
  useCallback,
  useEffect,
  useRef,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';

/** 长按判定时长（ms）。 */
const LONG_PRESS_MS = 500;
/** 取消长按的位移阈值（px）：超过视为滚动/侧滑手势。 */
const MOVE_SLOP_PX = 10;

interface PressState {
  readonly pointerId: number;
  readonly startX: number;
  readonly startY: number;
}

export interface LongPressItemProps {
  readonly onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  readonly onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void;
  readonly onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void;
  readonly onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => void;
  readonly onContextMenu: (event: ReactMouseEvent<HTMLElement>) => void;
}

export interface LongPress {
  readonly itemProps: LongPressItemProps;
  /** 长按已触发时返回 true 并复位——用于吞掉松手后的 click。 */
  readonly consumeFired: () => boolean;
}

export function useLongPress(onLongPress: () => void): LongPress {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pressRef = useRef<PressState | null>(null);
  const firedRef = useRef(false);
  const onLongPressRef = useRef(onLongPress);
  onLongPressRef.current = onLongPress;

  const cancelPress = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    pressRef.current = null;
  }, []);

  useEffect(() => cancelPress, [cancelPress]);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (event.pointerType === 'mouse') return; // 桌面端既有 hover/focus 行为不变
      cancelPress();
      firedRef.current = false;
      pressRef.current = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY };
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        pressRef.current = null;
        firedRef.current = true;
        onLongPressRef.current();
      }, LONG_PRESS_MS);
    },
    [cancelPress],
  );

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      const press = pressRef.current;
      if (press === null || event.pointerId !== press.pointerId) return;
      if (
        Math.abs(event.clientX - press.startX) > MOVE_SLOP_PX ||
        Math.abs(event.clientY - press.startY) > MOVE_SLOP_PX
      ) {
        cancelPress();
      }
    },
    [cancelPress],
  );

  const settlePress = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      const press = pressRef.current;
      if (press === null || event.pointerId !== press.pointerId) return;
      cancelPress();
    },
    [cancelPress],
  );

  const onContextMenu = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    // 按压进行中（部分平台 contextmenu 先于长按计时触发）或长按已触发：抑制原生菜单
    if (pressRef.current !== null || firedRef.current) {
      event.preventDefault();
    }
  }, []);

  const consumeFired = useCallback(() => {
    const fired = firedRef.current;
    firedRef.current = false;
    return fired;
  }, []);

  return {
    itemProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: settlePress,
      onPointerCancel: settlePress,
      onContextMenu,
    },
    consumeFired,
  };
}
