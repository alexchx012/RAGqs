import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { createApiClient } from '../../api/client';
import { EscStackProvider } from '../../lib/esc-stack-provider';
import { copy } from '../../copy';
import { createChatApi } from '../api';
import { createPromptEnhanceHandler } from '../enhance';
import { Composer } from './composer';
import type { SpaceItem, ConversationScope } from '../types';

/*
 * 输入区组件测试（共用基座 §3.3；动效 AI Agent Input 移植）：发送键禁用/启用、
 * 「+」菜单内努力档位分段开关与检索范围 flyout（行来自 retrieval 返回、个人库下钻文档、摘要与墨点）、
 * contentEditable 编辑器（「/」技能药丸、「+」附件 chips、onEnhance 优化/还原）。
 * 编辑器为 contentEditable div：无 value，一律断言 textContent；getByRole('textbox') 查询靠 aria-label 不变。
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
  const view = render(
    <EscStackProvider>
      <Composer {...props} />
    </EscStackProvider>,
  );
  return { props, view };
}

describe('输入区（Composer）', () => {
  it('空输入发送键禁用；输入后启用；Enter 发送并清空', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });
    const send = screen.getByRole('button', { name: copy.chat.composer.sendAria });
    expect(send).toBeDisabled();
    const editor = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(editor, '报销流程怎么走');
    expect(screen.getByRole('button', { name: copy.chat.composer.sendAria })).toBeEnabled();
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('报销流程怎么走');
    // contentEditable 编辑器无 value，断言 textContent
    expect(editor.textContent).toBe('');
  });

  it('发送未被接受或失败时保留输入，避免首问 no-op 后丢稿', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn(async () => false);
    renderComposer({ onSend });
    const editor = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(editor, '首问不能丢');
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('首问不能丢');
    expect(editor.textContent).toBe('首问不能丢');

    onSend.mockRejectedValueOnce(new Error('network'));
    await user.keyboard('{Enter}');
    expect(editor.textContent).toBe('首问不能丢');
  });

  it('Shift+Enter 换行不发送', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });
    const editor = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(editor, '第一行');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(editor, '第二行');
    expect(onSend).not.toHaveBeenCalled();
    // jsdom 无真实编辑引擎：Shift+Enter 的换行符不一定进入 textContent，断言两段文字都在即可
    expect(editor.textContent).toContain('第一行');
    expect(editor.textContent).toContain('第二行');
  });

  it('努力档位（「+」菜单内分段开关）：默认快速，切换调用 onEffortChange', async () => {
    const user = userEvent.setup();
    const onEffortChange = vi.fn();
    renderComposer({ onEffortChange });
    await user.click(screen.getByRole('button', { name: copy.chat.composer.addMenuAria }));
    const group = screen.getByRole('radiogroup', { name: copy.chat.composer.effortAria });
    expect(within(group).getByRole('radio', { name: copy.chat.composer.effortQuick })).toHaveAttribute('aria-checked', 'true');
    await user.click(within(group).getByRole('radio', { name: copy.chat.composer.effortDeep }));
    expect(onEffortChange).toHaveBeenCalledWith('deep');
  });

  it('检索范围：菜单行默认「全部范围」；展开 flyout 按 retrieval 返回渲染行（不硬编码）', async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.click(screen.getByRole('button', { name: copy.chat.composer.addMenuAria }));
    const row = screen.getByRole('menuitem', { name: copy.chat.composer.scopeAria });
    expect(within(row).getByText(copy.chat.composer.scopeAll)).toBeInTheDocument();
    await user.click(row);
    // 行名完全来自 spaces 数据
    for (const space of SPACES) {
      expect(await screen.findByText(space.name)).toBeInTheDocument();
    }
  });

  it('检索范围：选中空间后菜单行摘要 + 墨点；非默认态', async () => {
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
    await user.click(screen.getByRole('button', { name: copy.chat.composer.addMenuAria }));
    await user.click(screen.getByRole('menuitem', { name: copy.chat.composer.scopeAria }));
    await user.click(await screen.findByText('公共库'));
    const row = screen.getByRole('menuitem', { name: copy.chat.composer.scopeAria });
    expect(within(row).getByText('公共库')).toBeInTheDocument();
    // 墨点在场
    expect(row.querySelector('span[aria-hidden="true"]')).not.toBeNull();
  });

  it('本人个人库行有下钻箭头：展开后拉取文档并多选', async () => {
    const user = userEvent.setup();
    const onFetchDocuments = vi.fn(async () => [
      { id: 'doc_1', name: '员工手册.pdf' },
      { id: 'doc_2', name: '报销制度.docx' },
    ]);
    const onSelectionChange = vi.fn();
    renderComposer({ onFetchDocuments, onSelectionChange });
    await user.click(screen.getByRole('button', { name: copy.chat.composer.addMenuAria }));
    await user.click(screen.getByRole('menuitem', { name: copy.chat.composer.scopeAria }));
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
    await user.click(screen.getByRole('button', { name: copy.chat.composer.addMenuAria }));
    await user.click(screen.getByRole('menuitem', { name: copy.chat.composer.scopeAria }));
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

  it('「/」斜杠面板：输入 / 打开，Enter 应用后编辑器出现技能药丸且面板关闭', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });
    const editor = screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder });
    await user.type(editor, '/');
    expect(
      await screen.findByRole('listbox', { name: copy.chat.composer.skillsLabel }),
    ).toBeInTheDocument();
    // 空 query 命中全部技能，默认高亮第一项（deep-research）
    await user.keyboard('{Enter}');
    expect(editor.querySelector('[data-skill="deep-research"]')).not.toBeNull();
    expect(editor.textContent).toContain(copy.chat.composer.skillDeepResearch);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    // 斜杠面板消费 Enter（capture 阶段 stopPropagation），不得触发发送
    expect(onSend).not.toHaveBeenCalled();
  });

  it('优化输入药丸：注入 onEnhance 时增强 → 展示结果并变「还原」→ 还原恢复原文', async () => {
    const user = userEvent.setup();
    const onEnhance = vi.fn(async (_prompt: string) => '优化后的内容');
    renderComposer({ onEnhance });
    await user.type(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }),
      '原始问题',
    );
    await user.click(await screen.findByRole('button', { name: copy.chat.composer.enhancePrompt }));
    expect(onEnhance).toHaveBeenCalledWith('原始问题', expect.any(AbortSignal));
    // 增强完成：编辑器展示优化结果，药丸变「还原」（增强期间编辑器换成 shimmer 文本，此处重新查询）
    await screen.findByRole('button', { name: copy.chat.composer.revertEnhance });
    expect(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }).textContent,
    ).toBe('优化后的内容');
    await user.click(screen.getByRole('button', { name: copy.chat.composer.revertEnhance }));
    await waitFor(() =>
      expect(
        screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }).textContent,
      ).toBe('原始问题'),
    );
  });

  it('未注入 onEnhance 时不显示「优化输入」入口', async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.type(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }),
      '有问题',
    );
    expect(
      screen.queryByRole('button', { name: copy.chat.composer.enhancePrompt }),
    ).not.toBeInTheDocument();
  });

  it('「+」菜单：添加图片/文件/技能在场；选文件出附件 chip，点 × 后退场移除', async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.click(screen.getByRole('button', { name: copy.chat.composer.addMenuAria }));
    expect(
      screen.getByRole('menuitem', { name: copy.chat.composer.addPhotos }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('menuitem', { name: copy.chat.composer.attachFiles }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('menuitem', { name: copy.chat.composer.skillsLabel }),
    ).toBeInTheDocument();
    // 检索范围行与努力档位分段开关同在「+」菜单内
    expect(
      screen.getByRole('menuitem', { name: copy.chat.composer.scopeAria }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('radiogroup', { name: copy.chat.composer.effortAria }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('menuitem', { name: copy.chat.composer.attachFiles }));
    // jsdom 无文件选择对话框：直接打 hidden file input（user.upload 无可见性检查）
    const fileInput = document.querySelector('input[type="file"]');
    expect(fileInput).not.toBeNull();
    await user.upload(
      fileInput as HTMLInputElement,
      new File(['x'], '报告.pdf', { type: 'application/pdf' }),
    );
    expect(await screen.findByText('报告.pdf')).toBeInTheDocument();

    await user.click(
      screen.getByRole('button', { name: copy.chat.composer.removeItemAria('报告.pdf') }),
    );
    // 退场动画 200ms：jsdom 不触发 CSS 动画事件，组件内 setTimeout 兜底后移除
    await waitFor(() => expect(screen.queryByText('报告.pdf')).not.toBeInTheDocument());
  });
});

/*
 * 优化输入真实接线（prompt-enhance §3.2 / §4）：onEnhance 为生产同款装配
 * （createApiClient + createChatApi + createPromptEnhanceHandler），fetch 经 fetchFn 注入。
 */
describe('优化输入真实接线', () => {
  const jsonResponse = (status: number, body: unknown): Response =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  function realOnEnhance(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, onFailed = vi.fn()) {
    const client = createApiClient({
      getAccessToken: () => 'tok_1',
      refresh: vi.fn(),
      fetchFn: fetchMock,
    });
    return { onEnhance: createPromptEnhanceHandler(createChatApi(client), onFailed), onFailed };
  }

  it('成功：POST /v1/prompt-enhancements 返回 enhanced_prompt，编辑器整体替换、药丸变「还原」', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { enhanced_prompt: '优化后的内容' }),
    );
    const { onEnhance } = realOnEnhance(fetchMock);
    renderComposer({ onEnhance });
    await user.type(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }),
      '原始问题',
    );
    await user.click(await screen.findByRole('button', { name: copy.chat.composer.enhancePrompt }));
    await screen.findByRole('button', { name: copy.chat.composer.revertEnhance });
    expect(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }).textContent,
    ).toBe('优化后的内容');
    // 请求形状：路径 / 方法 / Bearer / body
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/v1/prompt-enhancements');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_1');
    expect(JSON.parse(String(init.body))).toEqual({ prompt: '原始问题' });
  });

  it('失败（非中止）：onFailed 触发提示，原文不动、药丸回到「优化输入」可重试', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn<typeof fetch>(async () =>
      jsonResponse(503, {
        error: {
          code: 'prompt_enhance_unavailable',
          message: 'prompt_enhance_unavailable',
          details: {},
          request_id: 'req_test_1',
        },
      }),
    );
    const onFailed = vi.fn();
    const { onEnhance } = realOnEnhance(fetchMock, onFailed);
    renderComposer({ onEnhance });
    await user.type(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }),
      '原始问题',
    );
    await user.click(await screen.findByRole('button', { name: copy.chat.composer.enhancePrompt }));
    // 失败后药丸回到「优化输入」（enhancing 期间按钮不在场，findBy 等到恢复）
    await screen.findByRole('button', { name: copy.chat.composer.enhancePrompt });
    expect(onFailed).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }).textContent,
    ).toBe('原始问题');
  });

  it('中止（还原/卸载中止进行中请求）：fetch signal 兑现中止，静默不触发 onFailed', async () => {
    const user = userEvent.setup();
    let fetchedSignal: AbortSignal | undefined;
    const fetchMock = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          fetchedSignal = init?.signal ?? undefined;
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    const onFailed = vi.fn();
    const { onEnhance } = realOnEnhance(fetchMock, onFailed);
    const { view } = renderComposer({ onEnhance });
    await user.type(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }),
      '原始问题',
    );
    await user.click(await screen.findByRole('button', { name: copy.chat.composer.enhancePrompt }));
    // 请求挂起中卸载：composer 卸载中止 AbortController，沿接缝兑现到 fetch
    view.unmount();
    expect(fetchedSignal?.aborted).toBe(true);
    // 冲刷微任务：AbortError 经接缝静默（只 rethrow 还原，不弹错误提示）
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onFailed).not.toHaveBeenCalled();
  });
});
