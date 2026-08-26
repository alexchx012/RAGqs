/*
 * 检索 / 生成中状态指示（动效 orbs B2「Searching」）。
 * 28px 舞台上三个等大同色圆点绕轨旋转：前方清晰、后方模糊，以景深读作远近（规格注释：
 * 匀速线性绕圈，缓动会让圆周运动读成抖动）；--orb-k 把舞台几何缩放到 size。
 * 颜色取 currentColor；循环动画 ui-orb-revolve 在 styles/ui.css；reduced-motion 由 base.css 全局直出。
 */

import type { CSSProperties } from 'react';

/** 舞台几何调谐基准边长；--orb-k = size / STAGE 整体缩放。 */
const STAGE = 28;

export interface OrbProps {
  /** 渲染边长 px（默认 20）。 */
  readonly size?: number;
  /** 独立形态时的可访问名（role=img）；省略则纯装饰 aria-hidden，由相邻文案表意。 */
  readonly label?: string;
  readonly className?: string;
}

export function Orb({ size = 20, label, className = '' }: OrbProps) {
  return (
    <span
      className={`ui-orb${className === '' ? '' : ` ${className}`}`}
      role={label === undefined ? undefined : 'img'}
      aria-label={label}
      aria-hidden={label === undefined ? true : undefined}
      style={{ width: size, height: size, '--orb-k': size / STAGE } as CSSProperties}
    >
      <span className="ui-orb-lens">
        <span className="ui-orb-shape" />
        <span className="ui-orb-shape" />
        <span className="ui-orb-shape" />
      </span>
    </span>
  );
}
