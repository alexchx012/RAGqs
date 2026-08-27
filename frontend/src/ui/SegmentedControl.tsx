/*
 * 分段控件（共用基座 §3.3 努力档位分段开关、§5.5 外观分段控件，全站统一规格）。
 * 容器 mist-gray 底、radius-buttons、padding 4px、高 32px；选中滑块 paper-white 底 +
 * shadow-subtle + ink 文（三段统一，不设高亮例外段）；未选段 slate 文、hover 变 ink；
 * 滑块按等分宽度平移 --duration-base --ease-in-out；键盘左右方向键切换（roving tabindex，
 * radiogroup 语义）。
 */

import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';

export interface SegmentedOption {
  value: string;
  label: string;
}

export interface SegmentedControlProps {
  options: SegmentedOption[];
  value: string;
  onChange: (value: string) => void;
  ariaLabel?: string;
}

export function SegmentedControl({ options, value, onChange, ariaLabel }: SegmentedControlProps) {
  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [thumb, setThumb] = useState<{ left: number; width: number } | null>(null);

  // 滑块按选中段实测定位：段宽随标签内容伸缩（如「深度研究」四字段更宽），不能按等分计算
  const measure = () => {
    const element = itemRefs.current[selectedIndex];
    if (element !== null && element !== undefined) {
      setThumb((current) =>
        current !== null && current.left === element.offsetLeft && current.width === element.offsetWidth
          ? current
          : { left: element.offsetLeft, width: element.offsetWidth },
      );
    }
  };
  useLayoutEffect(measure, [selectedIndex]);
  useLayoutEffect(() => {
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIndex]);

  function move(delta: number): void {
    const next = (selectedIndex + delta + options.length) % options.length;
    onChange(options[next].value);
    itemRefs.current[next]?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      move(1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      move(-1);
    }
  }

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      className="relative flex h-8 items-center rounded-[var(--radius-buttons)] bg-mist-gray p-1"
    >
      {/* 首测前不挂载滑块：无条件渲染会落在 left-1（第一段）起点，测量后的首次定位会
          播放 left/width 过渡——进入模块时表现为「滑块从第一格滑向选中段」。条件挂载让
          滑块以正确位置首次出现（新元素无过渡起点，直接落位），后续切换仍播滑动过渡。 */}
      {thumb !== null && (
        <span
          aria-hidden="true"
          className="absolute top-1 bottom-1 left-1 rounded-[var(--radius-buttons)] bg-paper-white shadow-[var(--shadow-subtle)] transition-[left,width] duration-[var(--duration-base)] ease-[var(--ease-in-out)]"
          style={{ left: `${thumb.left}px`, width: `${thumb.width}px`, transform: 'none' }}
        />
      )}
      {options.map((option, index) => {
        const active = index === selectedIndex;
        return (
          <button
            key={option.value}
            ref={(element) => {
              itemRefs.current[index] = element;
            }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(option.value)}
            className={
              'relative z-10 flex-1 rounded-[var(--radius-buttons)] px-2 text-[14px] ' +
              `whitespace-nowrap transition-colors duration-[var(--duration-base)] ${
                active ? 'text-ink-black' : 'text-slate-gray hover:text-ink-black'
              }`
            }
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
