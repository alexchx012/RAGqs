/*
 * 开关（共用基座 §5.4 隐私区开关）：Radix Switch 承载。
 * 轨道 44×24 rounded-buttons，关 = mist-gray 底、开 = ink-black 底；
 * knob 20px paper-white；切换时 knob 平移 + 轨道底色过渡 --duration-fast --ease-in-out。
 */

import * as RadixSwitch from '@radix-ui/react-switch';

export interface SwitchProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  ariaLabel?: string;
}

export function Switch({ checked, onCheckedChange, disabled, ariaLabel }: SwitchProps) {
  return (
    <RadixSwitch.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={ariaLabel}
      className={
        'inline-flex h-6 w-[44px] shrink-0 items-center rounded-[var(--radius-buttons)] ' +
        'bg-mist-gray transition-colors duration-[var(--duration-fast)] ' +
        'ease-[var(--ease-in-out)] data-[state=checked]:bg-ink-black disabled:opacity-50'
      }
    >
      <RadixSwitch.Thumb
        className={
          'block h-5 w-5 translate-x-[2px] rounded-full bg-paper-white ' +
          'transition-transform duration-[var(--duration-fast)] ease-[var(--ease-in-out)] ' +
          'data-[state=checked]:translate-x-[22px]'
        }
      />
    </RadixSwitch.Root>
  );
}
