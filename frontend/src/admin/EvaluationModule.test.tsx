/*
 * 评测与校准测试（§11；验收 A6、A35–A39）。
 * 经契约 mock（MockAdminController 直接代理，与真实 handler 同源；MockHttpError 归一化为
 * ApiError）：三区块同页渲染；窗口卡 open/closing/合成 closed 状态点与信息行、采样率百分比、
 * 收口倒计时；ops 开关（拨动弹确认、取消还原、open 必填 window_kind、确认后 POST + 刷新 +
 * invalidateSummaries、409 四码对话框错误行 + 按服务端状态刷新）；admin 无开关只读；
 * 排行榜 metrics 动态列 / is_active 行底 / 无奖牌彩色 / eligible 行内说明；policy 只读行无覆盖入口；
 * 影子触发入口不存在（§11.4 非目标）；两读接口错误行 + 重试。
 */

import { act, render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { AuthProvider } from '../auth/AuthProvider';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { MockHttpError } from '../mocks/auth-contract';
import { mockAdmin, mockAuth } from '../mocks/testing';
import { createAuthedStore, fakeAdminApi } from '../test/auth-fixtures';
import { AdminProvider } from './AdminProvider';
import type { AdminApi } from './api';
import { EvaluationModule } from './EvaluationModule';
import { formatTime } from './format';
import { EvaluationWindowDot } from './summaries';

const evaluation = copy.admin.evaluation;

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

/** 经契约 mock 的 AdminApi：评测读 / 校准读直接代理 controller，可按用例覆盖。 */
function contractAdminApi(token: string, overrides: Partial<AdminApi> = {}): AdminApi {
  return fakeAdminApi({
    getLeaderboard: vi.fn(() => call(() => mockAdmin.getLeaderboard(token))),
    getCalibrationWindow: vi.fn(() => call(() => mockAdmin.getCalibrationWindow(token))),
    postCalibrationWindow: vi.fn((action, windowKind, key) =>
      call(() => mockAdmin.postCalibrationWindow(token, action, windowKind, key)),
    ),
    ...overrides,
  });
}

async function renderEvaluation(
  ui: ReactElement,
  user: User,
  adminApi: AdminApi,
): Promise<RenderResult> {
  const store = await createAuthedStore(user);
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <AdminProvider api={adminApi}>
            <EscStackProvider>{ui}</EscStackProvider>
          </AdminProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

function calibrationSwitch(): HTMLElement {
  return screen.getByRole('switch', { name: evaluation.switchAria });
}

async function confirmDialog(dialog: HTMLElement): Promise<void> {
  const user = userEvent.setup();
  await user.click(within(dialog).getByRole('button', { name: copy.controls.confirm }));
}

async function cancelDialog(dialog: HTMLElement): Promise<void> {
  const user = userEvent.setup();
  await user.click(within(dialog).getByRole('button', { name: copy.controls.cancel }));
}

describe('评测与校准：三区块同页渲染（A6）', () => {
  it('校准窗口 + 评测榜单 + 影子评测排名三区块同页，无下钻入口', async () => {
    const token = loginToken('ops-wang');
    await renderEvaluation(<EvaluationModule />, opsUser(), contractAdminApi(token));

    expect(await screen.findByText(evaluation.windowCardTitle)).toBeInTheDocument();
    expect(screen.getByText(evaluation.leaderboardTitle)).toBeInTheDocument();
    expect(screen.getByText(evaluation.shadowTitle)).toBeInTheDocument();
    // 正式榜单与影子榜单行
    expect(screen.getByText('config-2026-07-b')).toBeInTheDocument();
    expect(screen.getByText('shadow-cfg-3')).toBeInTheDocument();
  });

  it('排行榜读接口失败：一条错误行 + 重试（窗口卡读接口独立自管）', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token, {
      getLeaderboard: vi.fn(async () => {
        throw new ApiError({
          status: 500,
          code: 'internal',
          message: 'boom',
          details: {},
          requestId: null,
        });
      }),
    });
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);

    expect(await screen.findByText(evaluation.loadError)).toBeInTheDocument();
    // 窗口卡仍可用（独立读接口）
    expect(screen.getByText(evaluation.windowCardTitle)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.states.retry }));
    expect(adminApi.getLeaderboard).toHaveBeenCalledTimes(2);
  });

  it('窗口读接口失败：卡内错误行 + 重试', async () => {
    const token = loginToken('ops-wang');
    let fail = true;
    const adminApi = contractAdminApi(token, {
      getCalibrationWindow: vi.fn(() => {
        if (fail) {
          return Promise.reject(
            new ApiError({ status: 500, code: 'internal', message: 'boom', details: {}, requestId: null }),
          );
        }
        return call(() => mockAdmin.getCalibrationWindow(token));
      }),
    });
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);

    expect(await screen.findByText(evaluation.windowLoadError)).toBeInTheDocument();
    fail = false;
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.states.retry }));
    expect(await screen.findByText(evaluation.statusClosed)).toBeInTheDocument();
  });
});

describe('校准窗口状态卡（§11.2–11.3，A36）', () => {
  it('无窗口：合成 closed 展示（已关闭 + slate 点 + 0 值信息行），ops 见未勾选开关', async () => {
    const token = loginToken('ops-wang');
    const { container } = await renderEvaluation(
      <EvaluationModule />,
      opsUser(),
      contractAdminApi(token),
    );

    expect(await screen.findByText(evaluation.statusClosed)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(evaluation.pairsCollected(0)))).toBeInTheDocument();
    expect(screen.getByText(new RegExp('实际采样率 0%'))).toBeInTheDocument();
    expect(container.querySelector('.bg-slate-gray')).not.toBeNull();
    expect(container.querySelector('.bg-success')).toBeNull();
    const toggle = calibrationSwitch();
    expect(toggle.getAttribute('aria-checked')).toBe('false');
  });

  it('open：成功绿脉冲点 + 冷启动 + 信息行（对比数 / 采样率 40% / 策略版本 / 开窗时间）', async () => {
    const token = loginToken('ops-wang');
    mockAdmin.seedCalibrationWindow('open', 'cold_start');
    const { container } = await renderEvaluation(
      <EvaluationModule />,
      opsUser(),
      contractAdminApi(token),
    );

    expect(await screen.findByText(evaluation.statusOpen)).toBeInTheDocument();
    expect(screen.getByText(`· ${evaluation.kindColdStart}`)).toBeInTheDocument();
    expect(
      screen.getByText(new RegExp(`${evaluation.pairsCollected(12)}`)),
    ).toBeInTheDocument();
    expect(screen.getByText(new RegExp('实际采样率 40%'))).toBeInTheDocument();
    // 窗口卡与 policy 只读行都含策略版本；至少窗口卡信息行命中即可
    expect(screen.getAllByText(/策略版本 eval_2026_v1/).length).toBeGreaterThan(0);
    expect(screen.getByText(new RegExp('开窗时间'))).toBeInTheDocument();
    const dot = container.querySelector('.bg-success.ui-status-pulse');
    expect(dot).not.toBeNull();
    expect(calibrationSwitch().getAttribute('aria-checked')).toBe('true');
  });

  it('closing：slate 点 + 收口倒计时（将于 HH:mm 收口）', async () => {
    const token = loginToken('ops-wang');
    mockAdmin.seedCalibrationWindow('closing', 'manual');
    const { container } = await renderEvaluation(
      <EvaluationModule />,
      opsUser(),
      contractAdminApi(token),
    );

    expect(await screen.findByText(evaluation.statusClosing)).toBeInTheDocument();
    expect(
      screen.getByText(evaluation.closingDeadline(formatTime('2026-08-05T02:00:00Z'))),
    ).toBeInTheDocument();
    expect(screen.getByText(new RegExp(evaluation.pairsCollected(132)))).toBeInTheDocument();
    expect(container.querySelector('.bg-success')).toBeNull();
    expect(container.querySelector('.bg-slate-gray')).not.toBeNull();
    expect(calibrationSwitch().getAttribute('aria-checked')).toBe('false');
  });
});

describe('校准开关（仅 ops，A37）', () => {
  it('拨动即弹确认对话框（含 cold_start/sentinel/manual 单选）；取消对话框 switch 回原位、不发请求', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.openDialogTitle });
    const group = within(dialog).getByRole('radiogroup', { name: evaluation.kindLabel });
    expect(within(group).getByRole('radio', { name: evaluation.kindColdStart })).toBeInTheDocument();
    expect(within(group).getByRole('radio', { name: evaluation.kindSentinel })).toBeInTheDocument();
    expect(within(group).getByRole('radio', { name: evaluation.kindManual })).toBeInTheDocument();

    await cancelDialog(dialog);
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: evaluation.openDialogTitle })).toBeNull(),
    );
    expect(adminApi.postCalibrationWindow).not.toHaveBeenCalled();
    expect(calibrationSwitch().getAttribute('aria-checked')).toBe('false');
  });

  it('确认开窗：默认 cold_start 随请求提交（open 必填 window_kind），成功后 switch 勾选 + 轻提示 + 左栏状态点联动', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(
      <>
        <output data-testid="window-dot">
          <EvaluationWindowDot />
        </output>
        <EvaluationModule />
      </>,
      opsUser(),
      adminApi,
    );
    expect(screen.getByTestId('window-dot').querySelector('.ui-status-pulse')).toBeNull();
    const user = userEvent.setup();

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.openDialogTitle });
    await confirmDialog(dialog);

    await waitFor(() =>
      expect(adminApi.postCalibrationWindow).toHaveBeenCalledWith(
        'open',
        'cold_start',
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(evaluation.openedNotice)).toBeInTheDocument();
    expect(await screen.findByText(evaluation.statusOpen)).toBeInTheDocument();
    await waitFor(() => expect(calibrationSwitch().getAttribute('aria-checked')).toBe('true'));
    // invalidateSummaries：左栏窗口状态点重取后出现
    await waitFor(() =>
      expect(screen.getByTestId('window-dot').querySelector('.ui-status-pulse')).not.toBeNull(),
    );
  });

  it('选择 sentinel 后确认：window_kind 按所选提交', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.openDialogTitle });
    await user.click(within(dialog).getByRole('radio', { name: evaluation.kindSentinel }));
    await confirmDialog(dialog);

    await waitFor(() =>
      expect(adminApi.postCalibrationWindow).toHaveBeenCalledWith(
        'open',
        'sentinel',
        expect.stringMatching(/^idem_/),
      ),
    );
  });

  it('关窗：对话框无 window_kind 单选；确认后 POST close（忽略 kind）+ 状态转收口中', async () => {
    const token = loginToken('ops-wang');
    mockAdmin.seedCalibrationWindow('open', 'manual');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();

    expect(await screen.findByText(evaluation.statusOpen)).toBeInTheDocument();
    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.closeDialogTitle });
    expect(within(dialog).queryByRole('radiogroup')).toBeNull();
    await confirmDialog(dialog);

    await waitFor(() =>
      expect(adminApi.postCalibrationWindow).toHaveBeenCalledWith(
        'close',
        null,
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(evaluation.closingNotice)).toBeInTheDocument();
    expect(await screen.findByText(evaluation.statusClosing)).toBeInTheDocument();
  });

  it('409 calibration_window_not_eligible：对话框错误行 + 按服务端状态刷新（仍已关闭）', async () => {
    const token = loginToken('ops-wang');
    mockAdmin.setCalibrationEligible(false);
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.openDialogTitle });
    await confirmDialog(dialog);

    expect(await within(dialog).findByText(evaluation.errorNotEligible)).toBeInTheDocument();
    await waitFor(() => expect(adminApi.getCalibrationWindow).toHaveBeenCalledTimes(2));
    // 关闭对话框（Radix modal 打开期间主文档 aria-hidden，角色查询需先关闭）后核对刷新结果
    await cancelDialog(dialog);
    await waitFor(() => expect(calibrationSwitch().getAttribute('aria-checked')).toBe('false'));
    expect(await screen.findByText(evaluation.statusClosed)).toBeInTheDocument();
  });

  it('409 calibration_window_already_open：对话框错误行 + 刷新后 switch 勾上（按服务端状态）', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();
    // 面板加载后外部已开窗（本地仍展示已关闭）
    mockAdmin.seedCalibrationWindow('open', 'sentinel');

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.openDialogTitle });
    await confirmDialog(dialog);

    expect(await within(dialog).findByText(evaluation.errorAlreadyOpen)).toBeInTheDocument();
    await cancelDialog(dialog);
    await waitFor(() => expect(calibrationSwitch().getAttribute('aria-checked')).toBe('true'));
    expect(await screen.findByText(evaluation.statusOpen)).toBeInTheDocument();
  });

  it('409 calibration_window_closing：对话框错误行 + 刷新后呈现收口中', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();
    mockAdmin.seedCalibrationWindow('closing', 'manual');

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.openDialogTitle });
    await confirmDialog(dialog);

    expect(await within(dialog).findByText(evaluation.errorClosing)).toBeInTheDocument();
    expect(await screen.findByText(evaluation.statusClosing)).toBeInTheDocument();
  });

  it('409 calibration_window_not_open：关窗竞态 → 对话框错误行 + 按服务端状态刷新', async () => {
    const token = loginToken('ops-wang');
    mockAdmin.seedCalibrationWindow('open', 'manual');
    const adminApi = contractAdminApi(token);
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);
    const user = userEvent.setup();
    expect(await screen.findByText(evaluation.statusOpen)).toBeInTheDocument();
    // 面板加载后窗口已被外部关窗（进入收口），本地仍展示开窗中
    mockAdmin.postCalibrationWindow(token, 'close', null, 'external_close');

    await user.click(calibrationSwitch());
    const dialog = await screen.findByRole('dialog', { name: evaluation.closeDialogTitle });
    await confirmDialog(dialog);

    expect(await within(dialog).findByText(evaluation.errorNotOpen)).toBeInTheDocument();
    expect(await screen.findByText(evaluation.statusClosing)).toBeInTheDocument();
  });

  it('admin：不渲染开关与任何操作按钮，仅状态 + 「开窗由运维操作」', async () => {
    const token = loginToken('admin');
    mockAdmin.seedCalibrationWindow('open', 'cold_start');
    await renderEvaluation(<EvaluationModule />, adminUser(), contractAdminApi(token));

    expect(await screen.findByText(evaluation.statusOpen)).toBeInTheDocument();
    expect(screen.getByText(evaluation.opsOnlyNote)).toBeInTheDocument();
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('排行榜与影子评测排名（§11.1，A38）', () => {
  it('metrics 动态列：正式榜单 faithfulness + hit_at_k_final；影子榜单仅 answer_relevancy（键集各自求并集）', async () => {
    const token = loginToken('ops-wang');
    await renderEvaluation(<EvaluationModule />, opsUser(), contractAdminApi(token));

    const leaderboard = (await screen.findByText(evaluation.leaderboardTitle)).closest('section');
    const shadow = screen.getByText(evaluation.shadowTitle).closest('section');
    if (leaderboard === null || shadow === null) {
      throw new Error('section not found');
    }
    expect(within(leaderboard).getByText('faithfulness')).toBeInTheDocument();
    expect(within(leaderboard).getByText('hit_at_k_final')).toBeInTheDocument();
    expect(within(shadow).getByText('answer_relevancy')).toBeInTheDocument();
    expect(within(shadow).queryByText('faithfulness')).toBeNull();
    expect(within(shadow).queryByText('hit_at_k_final')).toBeNull();
  });

  it('is_active 当前生效行底 fog-white；eligible=false 名称后行内说明；名次无奖牌彩色', async () => {
    const token = loginToken('ops-wang');
    const { container } = await renderEvaluation(
      <EvaluationModule />,
      opsUser(),
      contractAdminApi(token),
    );

    const activeCell = await screen.findByText('config-2026-07-b');
    const activeRow = activeCell.closest('tr');
    expect(activeRow?.className).toContain('bg-fog-white');
    const inactiveRow = screen.getByText('config-2026-07-a').closest('tr');
    expect(inactiveRow?.className).not.toContain('bg-fog-white');
    // eligible=false 行内说明（mock 含该数据）
    expect(screen.getAllByText(evaluation.notEligibleTag).length).toBeGreaterThan(0);
    // 名次无奖牌彩色：无 medal/trophy 类与奖牌符号
    expect(container.innerHTML).not.toMatch(/medal|trophy|🥇|🥈|🥉/i);
    const rankCell = within(activeRow as HTMLElement).getByText('1');
    expect(rankCell.className).toContain('text-ink-black');
  });

  it('policy 只读行：唯一策略数值来源（版本 / 分差阈值 / 采样率百分比 / 上下限），无覆盖入口', async () => {
    const token = loginToken('ops-wang');
    const { container } = await renderEvaluation(
      <EvaluationModule />,
      opsUser(),
      contractAdminApi(token),
    );

    expect(await screen.findByText(/策略版本 eval_2026_v1/)).toBeInTheDocument();
    expect(screen.getByText(/开窗分差阈值 0\.03/)).toBeInTheDocument();
    expect(screen.getByText(/冷启动采样 40%/)).toBeInTheDocument();
    expect(screen.getByText(/哨兵采样 3%/)).toBeInTheDocument();
    expect(screen.getByText(/最小真实提问 50/)).toBeInTheDocument();
    expect(screen.getByText(/影子题目上限 200/)).toBeInTheDocument();
    expect(screen.getByText(/候选配置上限 3/)).toBeInTheDocument();
    // 无任何页面内覆盖入口（无输入控件；开关为 button[role=switch] 非 input）
    expect(container.querySelectorAll('input, select, textarea')).toHaveLength(0);
  });

  it('空榜单：两区块空态', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token, {
      getLeaderboard: vi.fn(() =>
        call(() => {
          const response = mockAdmin.getLeaderboard(token);
          return { ...response, entries: [], shadow_entries: [] };
        }),
      ),
    });
    await renderEvaluation(<EvaluationModule />, opsUser(), adminApi);

    expect((await screen.findAllByText(evaluation.empty)).length).toBe(2);
  });
});

describe('影子评测触发入口（§11.4，A39）', () => {
  it('影子区块无任何触发 / 运行按钮（不在前端调用或渲染）', async () => {
    const token = loginToken('ops-wang');
    await renderEvaluation(<EvaluationModule />, opsUser(), contractAdminApi(token));

    const shadow = (await screen.findByText(evaluation.shadowTitle)).closest('section');
    if (shadow === null) {
      throw new Error('shadow section not found');
    }
    expect(within(shadow).queryByRole('button')).toBeNull();
    expect(within(shadow).queryByRole('link')).toBeNull();
    expect(screen.queryByText(/触发|运行影子|重新评测/)).toBeNull();
  });
});
