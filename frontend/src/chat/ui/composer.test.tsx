import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { EscStackProvider } from '../../lib/esc-stack-provider';
import { copy } from '../../copy';
import { Composer } from './composer';
import type { SpaceItem, ConversationScope } from '../types';

/*
 * 输入区组件测试（共用基座 §3.3）：发送键禁用/启用、努力档位分段开关切换、
 * 检索范围 chip 浮层（行来自 retrieval 返回、个人库下钻文档、摘要与墨点）。
 * useEscShield 需要 EscStackProvider；selection 为受控 prop，摘要断言用有状态包装。
 */

const SPACES: readonly SpaceItem[] = [
  { id: 'personal:u_user', kind: 'personal', name: '个人库', permission: 'manage', document_count: 12 },
  { id: 'department:d_finance', kind: 'department', name: '财务部', permission: 'read', document_count: 40, department_status: 'active' },
  { id: 'public', kind: 'public', name: '公共库', permission: 'contribute', document_count: 300 },
];

function renderComposer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  const props = {
    effortLevel: 'quick' as const,
    onEffortChange: vi.fn(),
    spaces: SPACES,
    onFetchDocuments: vi.fn(async () => [{ id: 'doc_1', name: '员工手册.pdf' }]),
    selection: { space_ids: [] as string[], document_ids: [] as string[] },
    onSelectionChange: vi.fn(),
    generating: false,
    canStop: false,
    stopping: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    ...overrides,
  };
  render(
    <EscStackProvider>
      <Composer {...props} />
    </EscStackProvider>,
  );
  return props;
}

describe('输入区（Composer）', () => {
  it('空输入发送键禁用；输入后启用；Enter 发送并清空', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });
    const send = screen.getByRole('button', { name: copy.chat.composer.sendAria });
    expect(send).toBeDisabled();
    const textarea = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(textarea, '报销流程怎么走');
    expect(screen.getByRole('button', { name: copy.chat.composer.sendAria })).toBeEnabled();
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('报销流程怎么走');
    expect(textarea).toHaveValue('');
  });

  it('发送未被接受或失败时保留输入，避免首问 no-op 后丢稿', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn(async () => false);
    renderComposer({ onSend });
    const textarea = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(textarea, '首问不能丢');
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('首问不能丢');
    expect(textarea).toHaveValue('首问不能丢');

    onSend.mockRejectedValueOnce(new Error('network'));
    await user.keyboard('{Enter}');
    expect(textarea).toHaveValue('首问不能丢');
  });

  it('Shift+Enter 换行不发送', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });
    const textarea = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(textarea, '第一行');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(textarea, '第二行');
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue('第一行\n第二行');
  });

  it('努力档位分段开关：默认快速，切换调用 onEffortChange', async () => {
    const user = userEvent.setup();
    const onEffortChange = vi.fn();
    renderComposer({ onEffortChange });
    const group = screen.getByRole('radiogroup', { name: copy.chat.composer.effortAria });
    expect(within(group).getByRole('radio', { name: copy.chat.composer.effortQuick })).toHaveAttribute('aria-checked', 'true');
    await user.click(within(group).getByRole('radio', { name: copy.chat.composer.effortDeep }));
    expect(onEffortChange).toHaveBeenCalledWith('deep');
  });

  it('检索范围 chip：默认「全部范围」；展开浮层按 retrieval 返回渲染行（不硬编码）', async () => {
    const user = userEvent.setup();
    renderComposer();
    const chip = screen.getByRole('button', { name: copy.chat.composer.scopeAria });
    expect(within(chip).getByText(copy.chat.composer.scopeAll)).toBeInTheDocument();
    await user.click(chip);
    // 行名完全来自 spaces 数据
    for (const space of SPACES) {
      expect(await screen.findByText(space.name)).toBeInTheDocument();
    }
  });

  it('检索范围 chip：选中空间后摘要 + 墨点；非默认态', async () => {
    const user = userEvent.setup();
    // 受控 selection：有状态包装使摘要随选择更新
    function StatefulHarness() {
      const [selection, setSelection] = useState<ConversationScope>({ space_ids: [], document_ids: [] });
      return (
        <EscStackProvider>
          <Composer
            effortLevel="quick"
            onEffortChange={vi.fn()}
            spaces={SPACES}
            onFetchDocuments={vi.fn(async () => [])}
            selection={selection}
            onSelectionChange={setSelection}
            generating={false}
            canStop={false}
            stopping={false}
            onSend={vi.fn()}
            onStop={vi.fn()}
          />
        </EscStackProvider>
      );
    }
    render(<StatefulHarness />);
    await user.click(screen.getByRole('button', { name: copy.chat.composer.scopeAria }));
    await user.click(await screen.findByText('公共库'));
    const chip = screen.getByRole('button', { name: copy.chat.composer.scopeAria });
    expect(within(chip).getByText('公共库')).toBeInTheDocument();
    // 墨点在场
    expect(chip.querySelector('span[aria-hidden="true"]')).not.toBeNull();
  });

  it('本人个人库行有下钻箭头：展开后拉取文档并多选', async () => {
    const user = userEvent.setup();
    const onFetchDocuments = vi.fn(async () => [
      { id: 'doc_1', name: '员工手册.pdf' },
      { id: 'doc_2', name: '报销制度.docx' },
    ]);
    const onSelectionChange = vi.fn();
    renderComposer({ onFetchDocuments, onSelectionChange });
    await user.click(screen.getByRole('button', { name: copy.chat.composer.scopeAria }));
    await user.click(screen.getByRole('button', { name: copy.chat.composer.scopeDocumentDrillAria }));
    expect(onFetchDocuments).toHaveBeenCalledWith('personal:u_user');
    await user.click(await screen.findByText('员工手册.pdf'));
    const docSelection = onSelectionChange.mock.calls.at(-1)?.[0] as { document_ids: string[] };
    expect(docSelection.document_ids).toContain('doc_1');
  });

  it('文档名过滤：输入 q 后清空应重新拉取并恢复全量文档', async () => {
    const user = userEvent.setup();
    const allDocs = [
      { id: 'doc_1', name: '员工手册.pdf' },
      { id: 'doc_2', name: '报销制度.docx' },
    ];
    const onFetchDocuments = vi.fn(async (_spaceId: string, q?: string) => {
      if (q === undefined || q === '') return allDocs;
      return allDocs.filter((doc) => doc.name.includes(q));
    });
    renderComposer({ onFetchDocuments });
    await user.click(screen.getByRole('button', { name: copy.chat.composer.scopeAria }));
    await user.click(screen.getByRole('button', { name: copy.chat.composer.scopeDocumentDrillAria }));
    expect(await screen.findByText('员工手册.pdf')).toBeInTheDocument();
    expect(screen.getByText('报销制度.docx')).toBeInTheDocument();

    const docSearch = screen.getByRole('searchbox', {
      name: copy.chat.composer.scopeDocumentSearchPlaceholder,
    });
    await user.type(docSearch, '报销');
    expect(onFetchDocuments).toHaveBeenCalledWith('personal:u_user', '报销');
    expect(await screen.findByText('报销制度.docx')).toBeInTheDocument();
    expect(screen.queryByText('员工手册.pdf')).not.toBeInTheDocument();

    await user.clear(docSearch);
    // 清空后须无 q 重新拉取全量，恢复被过滤覆盖的 documents
    expect(onFetchDocuments).toHaveBeenCalledWith('personal:u_user');
    expect(await screen.findByText('员工手册.pdf')).toBeInTheDocument();
    expect(screen.getByText('报销制度.docx')).toBeInTheDocument();
  });

  it('生成中：发送键变停止键；收到 start 前不可停止', () => {
    renderComposer({ generating: true, canStop: false, stopping: false, onStop: vi.fn() });
    const stop = screen.getByRole('button', { name: copy.chat.composer.stopAria });
    expect(stop).toBeDisabled();
  });

  it('停止键：start 已收后可点击，点击调用 onStop', async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    renderComposer({ generating: true, canStop: true, stopping: false, onStop });
    const activeStop = screen.getByRole('button', { name: copy.chat.composer.stopAria });
    expect(activeStop).toBeEnabled();
    await user.click(activeStop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('停止键：stopping 态禁重复操作', () => {
    renderComposer({ generating: true, canStop: true, stopping: true });
    const stoppingBtn = screen.getByRole('button', { name: copy.chat.composer.stoppingAria });
    expect(stoppingBtn).toBeDisabled();
  });
});
