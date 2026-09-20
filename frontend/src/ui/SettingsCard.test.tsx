/*
 * SettingsCard 测试（drawer-visual-system）：880px / 16px 圆角（经作用域 token）/
 * 柔和双层阴影 / 32px 内边距 / 内容区水平居中。
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SettingsCard } from './SettingsCard';

describe('SettingsCard', () => {
  it('880px 上限、圆角与阴影走 token、32px 内边距、水平居中、白底', () => {
    render(<SettingsCard ariaLabel="常规设置">内容</SettingsCard>);
    const card = screen.getByTestId('settings-card');
    expect(card.className).toContain('max-w-[880px]');
    expect(card.className).toContain('rounded-[var(--radius-cards)]');
    expect(card.className).toContain('shadow-[var(--shadow-subtle-2)]');
    expect(card.className).toContain('p-8');
    expect(card.className).toContain('mx-auto');
    expect(card.className).toContain('bg-paper-white');
  });

  it('ariaLabel 透传为 aria-label；缺省时不设置', () => {
    const { unmount } = render(<SettingsCard ariaLabel="常规设置">内容</SettingsCard>);
    expect(screen.getByTestId('settings-card').getAttribute('aria-label')).toBe('常规设置');
    unmount();
    render(<SettingsCard>内容</SettingsCard>);
    expect(screen.getByTestId('settings-card').getAttribute('aria-label')).toBeNull();
  });

  it('渲染 children', () => {
    render(<SettingsCard>卡片内容</SettingsCard>);
    expect(screen.getByText('卡片内容')).toBeInTheDocument();
  });

  it('裁切越界子元素（满宽页脚为方角，不裁会画到卡片圆角之外）', () => {
    render(<SettingsCard>内容</SettingsCard>);
    expect(screen.getByTestId('settings-card').className).toContain('overflow-hidden');
  });
});
