/*
 * SettingsCard 测试（drawer-visual-system）：880px / 16px 圆角（经作用域 token）/
 * 柔和双层阴影 / 32px 内边距 / 内容区水平居中。
 * 标题与副标题（3.8）：两者都是可选能力；缺省调用（含「仅 ariaLabel」的知识库调用形态）
 * 必须一个节点都不多——由 innerHTML 与子元素数两条断言守住。
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

  it('缺省不渲染标题块：卡片只渲染 children，输出与未加标题能力前逐字节一致', () => {
    // 知识库等既有调用方不传 title/description，其输出必须一个节点都不多。
    const bare = render(<SettingsCard>内容</SettingsCard>);
    expect(screen.getByTestId('settings-card').innerHTML).toBe('内容');
    expect(screen.getByTestId('settings-card').childElementCount).toBe(0);
    bare.unmount();

    // 「仅 ariaLabel」是知识库六张卡片的调用形态，同样不得多出任何节点。
    const labelled = render(<SettingsCard ariaLabel="知识库">内容</SettingsCard>);
    expect(screen.getByTestId('settings-card').innerHTML).toBe('内容');
    expect(screen.getByTestId('settings-card').childElementCount).toBe(0);
    labelled.unmount();

    render(<SettingsCard ariaLabel="知识库">内容</SettingsCard>);
    expect(screen.queryByRole('heading')).not.toBeInTheDocument();
  });

  it('title 渲染为卡内 h2 标题元素（20px 字级），description 渲染为其后的 15px 灰色副标题', () => {
    render(
      <SettingsCard title="常规设置" description="管理界面外观与基础显示行为">
        内容
      </SettingsCard>,
    );

    const heading = screen.getByRole('heading', { level: 2, name: '常规设置' });
    expect(heading.tagName).toBe('H2');
    expect(heading.className.split(/\s+/)).toContain('text-body-lg');
    expect(heading.className.split(/\s+/)).toContain('text-ink-black');

    const subtitle = screen.getByText('管理界面外观与基础显示行为');
    expect(subtitle.tagName).toBe('P');
    expect(subtitle.className.split(/\s+/)).toContain('text-caption');
    // 次级灰走既有 token，不写死 hex
    expect(subtitle.className.split(/\s+/)).toContain('text-slate-strong');
    expect(subtitle.className).not.toMatch(/#[0-9a-fA-F]{3,8}/);
    // 副标题紧随标题之后
    expect(heading.compareDocumentPosition(subtitle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('标题块是卡片第一个元素且不另加上外边距：标题起于卡片顶边 +32px（由 p-8 提供）', () => {
    render(
      <SettingsCard title="常规设置" description="管理界面外观与基础显示行为">
        <div data-testid="first-row">行</div>
      </SettingsCard>,
    );

    const card = screen.getByTestId('settings-card');
    const heading = screen.getByRole('heading', { level: 2 });
    // 标题块在 32px 内边距之内、第一个表单行之前
    expect(card.className.split(/\s+/)).toContain('p-8');
    expect(card.children[0]).toContainElement(heading);
    expect(
      heading.compareDocumentPosition(screen.getByTestId('first-row')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // 标题与副标题都不自带上下外边距/内边距（顶部 32px 只由卡片 p-8 提供，不叠加）
    expect(heading.className.split(/\s+/).filter((token) => /^-?[mp][trblxy]?-/.test(token))).toEqual([]);
  });

  it('只传 title 时不渲染空的副标题', () => {
    render(<SettingsCard title="常规设置">内容</SettingsCard>);

    expect(screen.getByRole('heading', { level: 2, name: '常规设置' })).toBeInTheDocument();
    expect(screen.getByTestId('settings-card').querySelectorAll('p')).toHaveLength(0);
  });

  it('标题元素不挂 aria-label：区域名只在卡片 section 上命名一次', () => {
    render(
      <SettingsCard ariaLabel="安全" title="安全设置" description="管理登录密码与设备会话">
        内容
      </SettingsCard>,
    );

    const card = screen.getByTestId('settings-card');
    const heading = screen.getByRole('heading', { level: 2, name: '安全设置' });
    expect(card).toHaveAccessibleName('安全');
    expect(heading).not.toHaveAttribute('aria-label');
    expect(heading).not.toHaveAttribute('aria-labelledby');
    expect(screen.getAllByRole('region')).toHaveLength(1);
  });
});
