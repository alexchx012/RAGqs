/*
 * 系统运维测试（§10 任务队列 + §9.2 指标看板；验收 A7、A40–A44）。
 * 经契约 mock（MockKnowledgeController 直接代理，与真实 handler 同源；MockHttpError 归一化为
 * ApiError）：四档分段默认 all 与切换拉取；行各列（等宽任务 ID、进入时间、停留时长格式化）；
 * stale 行 fog-white + 琥珀点 + 原状态标签保留 + 停留时长琥珀；cancel 二次确认（无 body 无 key）
 * 后行更新；replay loading + 202 后「解析中」+ 轮询收敛到 pending；admin 空 allowed_actions
 * 整列不渲染 + 手动刷新 TextLink + 不轮询（fake timers）；ops 5s 轮询 keyed 行复用不打断滚动、
 * 轮询行移除保留 250ms 淡出 + 高度收起过渡后卸载（§10.1 行增删过渡）；
 * 竞态 409 刷新列表；指标看板固定三卡（OCR 低置信琥珀行）沿用 provider 窗口、无切换控件。
 */

import { act, fireEvent, render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { AuthProvider } from '../auth/AuthProvider';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { MockHttpError } from '../mocks/auth-contract';
import { mockAdmin, mockAuth, mockKnowledge } from '../mocks/testing';
import type { NotificationsStore } from '../notifications/store';
import type { SettingsApi } from '../settings/api';
import { SettingsProvider } from '../settings/SettingsProvider';
import { createAuthedStore, fakeAdminApi } from '../test/auth-fixtures';
import type { ThemeController } from '../theme/theme';
import { AdminProvider } from './AdminProvider';
import type { AdminApi } from './api';
import { OperationsMetricsLayer, OpsJobsLayer } from './OperationsModule';
import type { OpsJobsResponse, OpsJobsView } from './types';

const copyOperations = copy.admin.operations;
const copyUploads = copy.settings.knowledge.uploads;

afterEach(() => {
  vi.useRealTimers();
});

/** controller 同步抛出的 MockHttpError 归一化为 ApiError（与真实 client 错误形态一致）。 */
function call<T>(fn: () => T): Promise<T> {
  try {
    return Promise.resolve(fn());
  } catch (error) {
    if (error instanceof MockHttpError) {
      return Promise.reject(
        new ApiError({
          status: error.status,
          code: error.code,
          message: error.message,
          details: error.details,
          requestId: null,
        }),
      );
    }
    return Promise.reject(error);
  }
}

function loginToken(username: string): string {
  const { accessToken } = mockAuth.login(username, 'password123', 'vitest');
  return `Bearer ${accessToken}`;
}

function opsUser(): User {
  return {
    id: 'u_ops',
    username: 'ops-wang',
    display_name: '王运维',
    real_name: '王运维',
    department: null,
    role: 'ops',
    avatar_url: null,
  };
}

function adminUser(): User {
  return {
    id: 'u_admin',
    username: 'admin',
    display_name: '系统管理员',
    real_name: '系统管理员',
    department: null,
    role: 'admin',
    avatar_url: null,
  };
}

/** 记录每次响应的 listOpsJobs 代理（断言以组件实际渲染的响应为准，规避 wait_seconds 时基漂移）。 */
function contractAdminApi(
  token: string,
  captured?: OpsJobsResponse[],
  overrides: Partial<AdminApi> = {},
): AdminApi {
  return fakeAdminApi({
    listOpsJobs: vi.fn((view: OpsJobsView) =>
      call(() => mockKnowledge.listOpsJobs(token, view)).then((response) => {
        captured?.push(response);
        return response;
      }),
    ),
    getOperationsMetrics: vi.fn((window) =>
      call(() => mockAdmin.getOperationsMetrics(token, window)),
    ),
    ...overrides,
  });
}

function contractSettingsApi(token: string, overrides: Partial<SettingsApi> = {}): SettingsApi {
  return {
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    cancelJob: vi.fn((jobId: string, key: string) => call(() => mockKnowledge.cancelJob(token, jobId, key))),
    replayJob: vi.fn((jobId: string, key: string) => call(() => mockKnowledge.replayJob(token, jobId, key))),
    ...overrides,
  } as unknown as SettingsApi;
}

async function renderLayer(
  ui: ReactElement,
  user: User,
  adminApi: AdminApi,
  settingsApi: SettingsApi,
): Promise<RenderResult> {
  const store = await createAuthedStore(user);
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <SettingsProvider
            api={settingsApi}
            authStore={store}
            theme={{ setPreference: vi.fn() } as unknown as ThemeController}
            notifications={{} as NotificationsStore}
          >
            <AdminProvider api={adminApi}>
              <EscStackProvider>{ui}</EscStackProvider>
            </AdminProvider>
          </SettingsProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

function nameCell(documentName: string): string {
  return `${copyOperations.taskTypeIngestion} · ${documentName}`;
}

function rowOf(documentName: string): HTMLElement {
  const row = screen.getByText(nameCell(documentName)).closest('li');
  if (row === null) {
    throw new Error(`row not found: ${documentName}`);
  }
  return row;
}

function opsJobId(token: string, documentName: string): string {
  const item = mockKnowledge
    .listOpsJobs(token, 'all')
    .items.find((job) => job.document_name === documentName);
  if (item === undefined) {
    throw new Error(`ops job not found: ${documentName}`);
  }
  return item.job_id;
}

describe('任务队列：四档分段与行渲染（§10.1，A41）', () => {
  it('默认 all 拉取全量；切换处理中 / 待人工处理 / 超时按 view 重新拉取', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderLayer(<OpsJobsLayer />, opsUser(), adminApi, contractSettingsApi(token));

    expect(await screen.findByText(nameCell('事故报告.pdf'))).toBeInTheDocument();
    expect(adminApi.listOpsJobs).toHaveBeenCalledWith('all');
    // 全部 9 条种子
    expect(screen.getByText(nameCell('已完成入库.pdf'))).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('radio', { name: copyOperations.viewActive }));
    expect(await screen.findByText(nameCell('票据扫描.pdf'))).toBeInTheDocument();
    expect(adminApi.listOpsJobs).toHaveBeenCalledWith('active');
    expect(screen.queryByText(nameCell('损坏的文档.pdf'))).toBeNull();

    await user.click(screen.getByRole('radio', { name: copyOperations.viewReplayable }));
    expect(await screen.findByText(nameCell('损坏的文档.pdf'))).toBeInTheDocument();
    expect(adminApi.listOpsJobs).toHaveBeenCalledWith('replayable');
    expect(screen.queryByText(nameCell('事故报告.pdf'))).toBeNull();

    await user.click(screen.getByRole('radio', { name: copyOperations.viewStale }));
    expect(await screen.findByText(nameCell('事故报告.pdf'))).toBeInTheDocument();
    expect(adminApi.listOpsJobs).toHaveBeenCalledWith('stale');
    expect(screen.getByText(nameCell('库存盘点.pdf'))).toBeInTheDocument();
    expect(screen.queryByText(nameCell('月度归档.pdf'))).toBeNull();
  });

  it('行各列：任务类型 + 文档名、等宽任务 ID、进入时间、停留时长格式化、状态标签', async () => {
    const token = loginToken('ops-wang');
    const captured: OpsJobsResponse[] = [];
    await renderLayer(
      <OpsJobsLayer />,
      opsUser(),
      contractAdminApi(token, captured),
      contractSettingsApi(token),
    );

    const row = await screen.findByText(nameCell('事故报告.pdf'));
    const li = rowOf('事故报告.pdf');
    const response = captured[captured.length - 1];
    const job = response.items.find((item) => item.document_name === '事故报告.pdf');
    if (job === undefined) {
      throw new Error('job not found in captured response');
    }
    // 任务类型 + 文档名单行
    expect(row.textContent).toBe(`${copyOperations.taskTypeIngestion} · 事故报告.pdf`);
    // 任务 ID 等宽回退字体
    const idCell = within(li).getByText(job.job_id);
    expect(idCell.className).toContain('font-mono');
    // 停留时长格式化（以组件实际渲染响应的 wait_seconds 为准）
    expect(within(li).getByText(copyOperations.waitDuration(job.wait_seconds))).toBeInTheDocument();
    // 状态标签与 §6.6 同一映射
    expect(within(li).getByText(copyUploads.stateLabel('running'))).toBeInTheDocument();
    expect(within(rowOf('票据扫描.pdf')).getByText(copyUploads.stateLabel('pending'))).toBeInTheDocument();
    expect(within(rowOf('人事表格.xlsx')).getByText(copyUploads.stateLabel('retry_wait'))).toBeInTheDocument();
    expect(within(rowOf('超大附件.pdf')).getByText(copyUploads.stateLabel('dead_letter'))).toBeInTheDocument();
  });

  it('超时行：整行 fog-white + 琥珀状态点 + 停留时长琥珀，状态标签仍显示原 state', async () => {
    const token = loginToken('ops-wang');
    await renderLayer(<OpsJobsLayer />, opsUser(), contractAdminApi(token), contractSettingsApi(token));

    const li = await screen.findByText(nameCell('事故报告.pdf')).then(() => rowOf('事故报告.pdf'));
    expect(li.className).toContain('bg-fog-white');
    expect(li.querySelector('.bg-warning')).not.toBeNull();
    expect(li.querySelector('.text-warning')).not.toBeNull();
    // stale 是派生标记：状态标签仍显示 running 原文
    expect(within(li).getByText(copyUploads.stateLabel('running'))).toBeInTheDocument();
    // 非超时行无 fog-white 底、无琥珀
    const normal = rowOf('月度归档.pdf');
    expect(normal.className).not.toContain('bg-fog-white');
    expect(normal.querySelector('.bg-warning')).toBeNull();
  });

  it('stale_count 徽标在页头呈现', async () => {
    const token = loginToken('ops-wang');
    await renderLayer(<OpsJobsLayer />, opsUser(), contractAdminApi(token), contractSettingsApi(token));
    expect(await screen.findByText(nameCell('事故报告.pdf'))).toBeInTheDocument();
    const title = screen.getByText(copyOperations.jobs).closest('h2');
    expect(within(title as HTMLElement).getByText('2')).toBeInTheDocument();
  });
});

describe('任务队列：行操作（§10.2 / §6.7，A42）', () => {
  it('cancel：ghost Pill 二次确认 → POST 无 key → 成功后行状态更新为已取消', async () => {
    const token = loginToken('ops-wang');
    const settingsApi = contractSettingsApi(token);
    await renderLayer(<OpsJobsLayer />, opsUser(), contractAdminApi(token), settingsApi);
    const jobId = opsJobId(token, '月度归档.pdf');
    const user = userEvent.setup();

    const li = await screen.findByText(nameCell('月度归档.pdf')).then(() => rowOf('月度归档.pdf'));
    await user.click(within(li).getByRole('button', { name: copyUploads.cancel }));
    const dialog = await screen.findByRole('dialog', { name: copyUploads.cancelConfirmTitle });
    await user.click(within(dialog).getByRole('button', { name: copyUploads.cancel }));

    // §6.7：cancel 无 body 无 Idempotency-Key（空键透传）
    await waitFor(() => expect(settingsApi.cancelJob).toHaveBeenCalledWith(jobId, ''));
    expect(
      await within(rowOf('月度归档.pdf')).findByText(copyUploads.stateLabel('cancelled')),
    ).toBeInTheDocument();
  });

  it('cancel 竞态 409：提示已刷新并重新拉取列表', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const settingsApi = contractSettingsApi(token, {
      cancelJob: vi.fn(async () => {
        throw new ApiError({
          status: 409,
          code: 'job_state_conflict',
          message: 'conflict',
          details: {},
          requestId: null,
        });
      }),
    });
    await renderLayer(<OpsJobsLayer />, opsUser(), adminApi, settingsApi);
    const user = userEvent.setup();

    const li = await screen.findByText(nameCell('月度归档.pdf')).then(() => rowOf('月度归档.pdf'));
    const callsBefore = (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length;
    await user.click(within(li).getByRole('button', { name: copyUploads.cancel }));
    const dialog = await screen.findByRole('dialog', { name: copyUploads.cancelConfirmTitle });
    await user.click(within(dialog).getByRole('button', { name: copyUploads.cancel }));

    expect(await screen.findByText(copyUploads.actionConflict)).toBeInTheDocument();
    await waitFor(() =>
      expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
        callsBefore,
      ),
    );
    // 行保持服务端刷新后的状态（mock 未变更，仍为处理中）
    expect(
      await within(rowOf('月度归档.pdf')).findByText(copyUploads.stateLabel('running')),
    ).toBeInTheDocument();
  });

  it('replay：按钮转 loading → 202 后行内「解析中」→ 轮询收敛到 pending 呈现排队中', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const settingsApi = contractSettingsApi(token);
    await renderLayer(<OpsJobsLayer />, opsUser(), adminApi, settingsApi);
    const jobId = opsJobId(token, '损坏的文档.pdf');
    const user = userEvent.setup();

    // 初次加载完成后，把收敛轮询的第一次读换成受控 deferred（mock 状态在 replay 后即 pending）
    let releaseFetch!: (value: OpsJobsResponse) => void;
    const pendingFetch = new Promise<OpsJobsResponse>((resolve) => {
      releaseFetch = resolve;
    });
    (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mockImplementationOnce(() => pendingFetch);

    const li = await screen.findByText(nameCell('损坏的文档.pdf')).then(() => rowOf('损坏的文档.pdf'));
    await user.click(within(li).getByRole('button', { name: copyUploads.replay }));

    await waitFor(() =>
      expect(settingsApi.replayJob).toHaveBeenCalledWith(jobId, expect.stringMatching(/^idem_/)),
    );
    // 202 后收敛轮询进行中：行内呈现「解析中」，按钮保持 loading（loading 时 Pill 内容换成加载点，无 accessible name，按唯一按钮取）
    expect(await within(rowOf('损坏的文档.pdf')).findByText(copyUploads.stage.parsing)).toBeInTheDocument();
    expect(within(rowOf('损坏的文档.pdf')).getByRole('button')).toHaveAttribute('aria-busy', 'true');

    // 收敛读到 pending：「解析中」撤下，状态标签呈现服务端值
    await act(async () => {
      releaseFetch(mockKnowledge.listOpsJobs(token, 'all'));
    });
    await waitFor(() =>
      expect(within(rowOf('损坏的文档.pdf')).queryByText(copyUploads.stage.parsing)).toBeNull(),
    );
    expect(
      within(rowOf('损坏的文档.pdf')).getByText(copyUploads.stateLabel('pending')),
    ).toBeInTheDocument();
  });

  it('replay 竞态 403：提示已刷新并重新拉取列表', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const settingsApi = contractSettingsApi(token, {
      replayJob: vi.fn(async () => {
        throw new ApiError({
          status: 403,
          code: 'job_forbidden',
          message: 'forbidden',
          details: {},
          requestId: null,
        });
      }),
    });
    await renderLayer(<OpsJobsLayer />, opsUser(), adminApi, settingsApi);
    const user = userEvent.setup();

    const li = await screen.findByText(nameCell('损坏的文档.pdf')).then(() => rowOf('损坏的文档.pdf'));
    const callsBefore = (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length;
    await user.click(within(li).getByRole('button', { name: copyUploads.replay }));

    expect(await screen.findByText(copyUploads.actionConflict)).toBeInTheDocument();
    await waitFor(() =>
      expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
        callsBefore,
      ),
    );
  });

  it('admin：服务端返回空 allowed_actions → 整列不渲染操作（无禁用态）；手动刷新可用且不轮询', async () => {
    vi.useFakeTimers();
    const token = loginToken('admin');
    const adminApi = contractAdminApi(token);
    const settingsApi = contractSettingsApi(token);
    await renderLayer(<OpsJobsLayer />, adminUser(), adminApi, settingsApi);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(nameCell('事故报告.pdf'))).toBeInTheDocument();
    // 数据驱动：全部行空数组 → 操作列整列不渲染（非角色分支）
    expect(screen.queryByRole('button', { name: copyUploads.cancel })).toBeNull();
    expect(screen.queryByRole('button', { name: copyUploads.replay })).toBeNull();

    // 手动「刷新」文字链（fireEvent 同步触发，不依赖 fake timers 下的 userEvent 延迟）
    const callsBefore = (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: copy.admin.common.refresh }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsBefore + 1,
    );

    // admin 不轮询：推进 20s 无新增请求
    const callsAfterRefresh = (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsAfterRefresh,
    );
  });

  it('ops：5s 轮询再次拉取；keyed 行复用（节点保持），静默刷新不打断滚动', async () => {
    vi.useFakeTimers();
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderLayer(<OpsJobsLayer />, opsUser(), adminApi, contractSettingsApi(token));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(nameCell('事故报告.pdf'))).toBeInTheDocument();
    const callsInitial = (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length;
    const rowBefore = rowOf('事故报告.pdf');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsInitial + 1,
    );
    // 行 DOM 节点 keyed 复用未重挂载（滚动位置与行内操作不被打断）
    expect(rowOf('事故报告.pdf')).toBe(rowBefore);
    // 静默刷新不回落骨架屏
    expect(document.querySelector('[aria-busy="true"]')).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsInitial + 2,
    );
  });

  it('轮询行移除：离开行保留 250ms 淡出 + 高度收起过渡后再卸载（§10.1 行增删过渡）', async () => {
    vi.useFakeTimers();
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderLayer(<OpsJobsLayer />, opsUser(), adminApi, contractSettingsApi(token));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(nameCell('事故报告.pdf'))).toBeInTheDocument();
    const callsInitial = (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length;

    // 下一次轮询（5s）返回去掉「事故报告.pdf」的列表
    (adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mockImplementationOnce(
      (view: OpsJobsView) =>
        call(() => mockKnowledge.listOpsJobs(token, view)).then((response) => ({
          ...response,
          items: response.items.filter((job) => job.document_name !== '事故报告.pdf'),
        })),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect((adminApi.listOpsJobs as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsInitial + 1,
    );

    // 离开行仍挂载并播放退出动画（250ms 淡出 + 高度收起）
    const leavingRow = rowOf('事故报告.pdf');
    expect(leavingRow.className).toContain('ui-row-exit');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(screen.queryByText(nameCell('事故报告.pdf'))).toBeNull();
  });
});

describe('指标看板（§9.2，A44）', () => {
  it('固定三卡：缓存命中率 stat + sparkline；OCR 低置信行琥珀；建树 / basic 两行；无窗口切换控件', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const { container } = await renderLayer(
      <OperationsMetricsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );

    expect(await screen.findByText('缓存命中率')).toBeInTheDocument();
    expect(screen.getByText('OCR 置信度分布')).toBeInTheDocument();
    expect(screen.getByText('建树 / basic 分流比例')).toBeInTheDocument();
    // 缓存命中率卡带 sparkline
    expect(container.querySelector('[data-card-key="cache_hit_rate"] svg')).not.toBeNull();
    // OCR 低置信区间行 tone=warning 琥珀
    expect(screen.getByText('<90%').className).toContain('text-warning');
    // 建树 / basic 两行 distribution
    expect(screen.getByText('建树')).toBeInTheDocument();
    expect(screen.getByText('basic')).toBeInTheDocument();
    // 时间窗口沿用 AdminProvider（默认 7d），本层无切换控件
    expect(adminApi.getOperationsMetrics).toHaveBeenCalledWith('7d');
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });
});
