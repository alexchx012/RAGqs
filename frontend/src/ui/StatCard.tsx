/*
 * 指标卡（shared-shell 规格 §5；表面/圆角/阴影/间距按共用基座 §2.1/§2.3）。
 * paper-white 底 + radius-elevatedcards + shadow-subtle + padding 20px；
 * 标题 Sohne 500 20px ink + 主数值 + 15px slate 说明行；
 * 可选 Sparkline（SVG 折线，无轴无网格，stroke sienna-brown 1.5px，高 32px 宽自适应）
 * 与 DistributionBars（横向条形组，每条高 8px 圆角 4px sienna-brown，带 15px slate 标签行）。
 */

import type { ReactNode } from 'react';

export interface DistributionItem {
  label: string;
  value: number;
}

export interface StatCardProps {
  title: string;
  value: ReactNode;
  description?: string;
  /** 折线数据点（≥2 个才渲染）。 */
  sparkline?: number[];
  distribution?: DistributionItem[];
}

export function StatCard({ title, value, description, sparkline, distribution }: StatCardProps) {
  return (
    <section
      className={
        'rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle)]'
      }
    >
      <h3 className="text-[20px] font-medium text-ink-black">{title}</h3>
      <div className="mt-2 text-[32px] leading-none font-medium text-ink-black">{value}</div>
      {description !== undefined && (
        <p className="mt-2 text-[15px] text-slate-gray">{description}</p>
      )}
      {sparkline !== undefined && sparkline.length > 1 && <Sparkline data={sparkline} />}
      {distribution !== undefined && distribution.length > 0 && (
        <DistributionBars items={distribution} />
      )}
    </section>
  );
}

const SPARK_WIDTH = 100;
const SPARK_HEIGHT = 32;
/** 折线上下留白，避免 stroke 被裁。 */
const SPARK_PAD = 2;

function Sparkline({ data }: { data: number[] }) {
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min > 0 ? max - min : 1;
  const points = data
    .map((value, index) => {
      const x = (index / (data.length - 1)) * SPARK_WIDTH;
      const y =
        SPARK_HEIGHT - SPARK_PAD - ((value - min) / span) * (SPARK_HEIGHT - SPARK_PAD * 2);
      return `${x},${y}`;
    })
    .join(' ');
  return (
    <svg
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      className="mt-4 h-8 w-full"
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

function DistributionBars({ items }: { items: DistributionItem[] }) {
  const max = Math.max(...items.map((item) => item.value), 1);
  return (
    <ul className="mt-4 flex flex-col gap-2">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-3">
          <span className="w-20 shrink-0 truncate text-[15px] text-slate-gray">{item.label}</span>
          <span className="flex-1">
            <span
              className="block h-2 rounded-[4px] bg-sienna-brown"
              style={{ width: `${(item.value / max) * 100}%` }}
            />
          </span>
        </li>
      ))}
    </ul>
  );
}
