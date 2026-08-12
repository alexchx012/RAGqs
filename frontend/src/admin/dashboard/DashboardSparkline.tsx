/*
 * dashboard sparkline（运维端 §7.2 / 超管端 §7.2）：无坐标轴无网格线 SVG 折线，
 * --color-sienna-brown 1.5px 描边、高 32px、宽随卡宽（viewBox 归一化 100×32，非缩放描边）。
 * 窗口切换数据变化时 400ms（--duration-slow）--ease-in-out 形变重绘（rAF 逐点插值）；
 * 首渲染与 prefers-reduced-motion（animate=false）直出终态。
 * normalizeSparkPoints / formatSparkPoints 导出供测试断言终态。
 */

import { useEffect, useRef, useState } from 'react';

const SPARK_WIDTH = 100;
const SPARK_HEIGHT = 32;
/** 折线上下留白，避免 stroke 被裁。 */
const SPARK_PAD = 2;
/** 形变重绘时长：--duration-slow = 400ms。 */
export const SPARKLINE_MORPH_MS = 400;

export interface SparkPoint {
  readonly x: number;
  readonly y: number;
}

/** 数据归一化到 viewBox 坐标：宽度 0–100 等分，高度按 min/max 线性映射（上下留白 2px）。 */
export function normalizeSparkPoints(data: readonly number[]): SparkPoint[] {
  if (data.length === 0) {
    return [];
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min > 0 ? max - min : 1;
  if (data.length === 1) {
    const y = SPARK_HEIGHT - SPARK_PAD - ((data[0] - min) / span) * (SPARK_HEIGHT - SPARK_PAD * 2);
    return [
      { x: 0, y },
      { x: SPARK_WIDTH, y },
    ];
  }
  return data.map((value, index) => ({
    x: (index / (data.length - 1)) * SPARK_WIDTH,
    y: SPARK_HEIGHT - SPARK_PAD - ((value - min) / span) * (SPARK_HEIGHT - SPARK_PAD * 2),
  }));
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/** polyline points 序列化（定点两位小数，动画帧与终态同一格式）。 */
export function formatSparkPoints(points: readonly SparkPoint[]): string {
  return points.map((point) => `${round2(point.x)},${round2(point.y)}`).join(' ');
}

/** --ease-in-out 等价的 cubic 近似（cubic-bezier(0.4, 0, 0.2, 1) 标准缓入缓出）。 */
function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** 点数对齐：短序列以末点补齐，形变逐点插值。 */
function padTo(points: readonly SparkPoint[], length: number): SparkPoint[] {
  if (points.length >= length) {
    return [...points];
  }
  const last = points[points.length - 1];
  return [...points, ...Array.from({ length: length - points.length }, () => last)];
}

export interface DashboardSparklineProps {
  readonly data: readonly number[];
  /** false（prefers-reduced-motion）时直出，不做 400ms 形变。 */
  readonly animate?: boolean;
}

export function DashboardSparkline({ data, animate = true }: DashboardSparklineProps) {
  const [points, setPoints] = useState(() => formatSparkPoints(normalizeSparkPoints(data)));
  /** 当前展示点（动画帧值），下一次形变的起点。 */
  const displayRef = useRef<readonly SparkPoint[]>(normalizeSparkPoints(data));
  const mountedRef = useRef(false);

  useEffect(() => {
    const from = displayRef.current;
    const to = normalizeSparkPoints(data);
    const direct = !mountedRef.current || !animate || from.length < 2 || to.length < 2;
    mountedRef.current = true;
    if (direct) {
      displayRef.current = to;
      setPoints(formatSparkPoints(to));
      return;
    }
    const length = Math.max(from.length, to.length);
    const start = padTo(from, length);
    const end = padTo(to, length);
    const raf: (callback: (now: number) => void) => number =
      window.requestAnimationFrame ??
      ((callback) => window.setTimeout(() => callback(performance.now()), 16));
    const cancel: (handle: number) => void = window.cancelAnimationFrame ?? window.clearTimeout;
    const startedAt = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - startedAt) / SPARKLINE_MORPH_MS);
      const eased = easeInOutCubic(t);
      if (t >= 1) {
        displayRef.current = to;
        setPoints(formatSparkPoints(to));
        return;
      }
      const current = start.map((point, index) => ({
        x: point.x + (end[index].x - point.x) * eased,
        y: point.y + (end[index].y - point.y) * eased,
      }));
      displayRef.current = current;
      setPoints(formatSparkPoints(current));
      frame = raf(step);
    };
    frame = raf(step);
    return () => cancel(frame);
  }, [data, animate]);

  return (
    <svg
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      data-morph-ms={animate ? SPARKLINE_MORPH_MS : 0}
      className="mt-3 h-8 w-full"
    >
      <polyline
        points={points}
        fill="none"
        stroke="var(--color-sienna-brown)"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
