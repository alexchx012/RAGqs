/*
 * HoverCard 测试（共用基座 §3.4）：openDelay 后悬停展开浮层卡，移出后关闭。
 * 组件内 useEscShield 需要 EscStackProvider。
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { HoverCard } from './HoverCard';

function renderCard(openDelay = 10) {
  return render(
    <EscStackProvider>
      <HoverCard trigger={<button type="button">citation</button>} openDelay={openDelay}>
        <p>card body</p>
      </HoverCard>
    </EscStackProvider>,
  );
}

describe('HoverCard', () => {
  it('悬停触发后经 openDelay 展开 280px 浮层卡', async () => {
    renderCard();
    const user = userEvent.setup();
    expect(screen.queryByText('card body')).not.toBeInTheDocument();

    await user.hover(screen.getByRole('button', { name: 'citation' }));
    const body = await screen.findByText('card body');
    const content = body.closest('.ui-hovercard-content') as HTMLElement;
    expect(content.className).toContain('w-[280px]');
    expect(content.className).toContain('bg-paper-white');
    expect(content.className).toContain('rounded-[var(--radius-elevatedcards)]');
  });

  it('移出后关闭', async () => {
    renderCard();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: 'citation' });
    await user.hover(trigger);
    expect(await screen.findByText('card body')).toBeInTheDocument();

    await user.unhover(trigger);
    await waitFor(
      () => {
        expect(screen.queryByText('card body')).not.toBeInTheDocument();
      },
      { timeout: 2000 },
    );
  });
});
