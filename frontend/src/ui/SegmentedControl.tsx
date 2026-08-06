/*
 * 分段控件（共用基座 §3.3 努力档位分段开关、§5.5 外观分段控件，全站统一规格）。
 * 容器 mist-gray 底、radius-buttons、padding 4px、高 32px；选中滑块 paper-white 底 +
 * shadow-subtle + ink 文（accent 段选中时 blush-peach 底 + sienna-brown 文，桃色小面积功能使用）；
 * 未选段 slate 文、hover 变 ink；滑块按等分宽度平移 --duration-base --ease-in-out；
 * 键盘左右方向键切换（roving tabindex，radiogroup 语义）。
 */

import { useRef, type KeyboardEvent } from 'react';

export interface SegmentedOption {
  value: string;
  label: string;
  /** accent 段（如「深度研究」，共用基座 §3.3）：选中时滑块 blush-peach 底 + sienna-brown 文。 */
  accent?: boolean;
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
  const selected = options[selectedIndex];
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

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
      <span
        aria-hidden="true"
        data-accent={selected?.accent === true || undefined}
        className={
          'absolute top-1 bottom-1 left-1 rounded-[var(--radius-buttons)] transition-transform ' +
          `duration-[var(--duration-base)] ease-[var(--ease-in-out)] ${
            selected?.accent === true
              ? 'bg-blush-peach'
              : 'bg-paper-white shadow-[var(--shadow-subtle)]'
          }`
        }
        style={{
          width: `calc((100% - 8px) / ${options.length})`,
          transform: `translateX(calc(${selectedIndex} * 100%))`,
        }}
      />
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
              'relative z-10 flex-1 rounded-[var(--radius-buttons)] text-[14px] ' +
              `transition-colors duration-[var(--duration-base)] ${
                active
                  ? option.accent === true
                    ? 'text-sienna-brown'
                    : 'text-ink-black'
                  : 'text-slate-gray hover:text-ink-black'
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
