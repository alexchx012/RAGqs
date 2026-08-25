/*
 * 备份与恢复测试（backup-restore-operations-layer 规格 §2/§9；深链 /admin/operations/backups）。
 * 经契约 mock（MockAdminController 直接代理，与真实 handler 同源；MockHttpError 归一化为
 * ApiError）：admin 深链按角色截断（下钻行隐藏、整层不渲染）与非 ops 拒绝态；ops 三分段
 * 单选渲染 + 种子备份行；一键备份 single-flight + Idempotency-Key + 受理轻提示；
 * 5s 轮询收敛 creating→complete（读推进语义）；分段切换后返回「备份」已创建备份仍在；
 * 恢复危险确认展示备份 ID / 维护影响 / 备份当前状态，确认后受理轻提示 + 记录行出现；
 * repair retry 受理后同拍详情收敛「已修复」、重试按钮消失；策略 version_conflict 刷新
 * 最新值（表单重置）、随后保存成功、422 field=timezone 映射行内提示。
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
  type RenderResult,
} from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { AuthProvider } from '../auth/AuthProvider';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { MockHttpError } from '../mocks/auth-contract';
import { OPS_BACKUP_SEED_IDS } from '../mocks/admin-contract';
import { mockAdmin, mockAuth } from '../mocks/testing';
import { AppRoutes } from '../router/AppRoutes';
import {
  createAuthedStore,
  fakeAdminApi,
  renderWithShell,
  testUser,
} from '../test/auth-fixtures';
import { AdminProvider } from './AdminProvider';
import type { AdminApi } from './api';
import { BackupsLayer } from './BackupsLayer';
import type { OpsBackupCreateResponse } from './types';

const copyBackups = copy.admin.operations.backups;
const modules = copy.shell.drawer.modules;

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

function contractAdminApi(token: string, overrides: Partial<AdminApi> = {}): AdminApi {
  return fakeAdminApi({
    listOpsBackups: vi.fn((page: number, pageSize: number) =>
      call(() => mockAdmin.listOpsBackups(token, page, pageSize)),
    ),
    createOpsBackup: vi.fn((key: string) => call(() => mockAdmin.createOpsBackup(token, key))),
    getOpsBackup: vi.fn((backupId: string) => call(() => mockAdmin.getOpsBackup(token, backupId))),
    listOpsRestores: vi.fn((page: number, pageSize: number) =>
      call(() => mockAdmin.listOpsRestores(token, page, pageSize)),
    ),
    createOpsRestore: vi.fn((backupId: string, key: string) =>
      call(() => mockAdmin.createOpsRestore(token, backupId, key)),
    ),
    getOpsRestore: vi.fn((restoreId: string) =>
      call(() => mockAdmin.getOpsRestore(token, restoreId)),
    ),
    retryOpsRepairTarget: vi.fn((restoreId: string, targetId: string, key: string) =>
      call(() => mockAdmin.retryOpsRepairTarget(token, restoreId, targetId, key)),
    ),
    getOpsBackupPolicy: vi.fn(() => call(() => mockAdmin.getOpsBackupPolicy(token))),
    patchOpsBackupPolicy: vi.fn((input: Parameters<AdminApi['patchOpsBackupPolicy']>[0], key: string) =>
      call(() => mockAdmin.patchOpsBackupPolicy(token, input, key)),
    ),
    ...overrides,
  });
}

/** 子层直接渲染（非 ops 拒绝态 / 分段交互）；BackupsLayer 不经 SettingsProvider。 */
async function renderLayer(
  ui: ReactElement,
  user: User,
  adminApi: AdminApi,
  initialEntries: string[] = ['/admin/operations/backups'],
): Promise<RenderResult> {
  const store = await createAuthedStore(user);
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={initialEntries}>
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

/** 经共享壳层渲染整套路由（抽屉级深链 / 角色截断断言）。 */
async function renderApp(path: string, role: 'ops' | 'admin', adminApi: AdminApi) {
  const store = await createAuthedStore(testUser({ role }));
  renderWithShell(<AppRoutes />, store, [path], { adminApi });
}

function rowOf(cellText: string): HTMLElement {
  const row = screen.getByText(cellText).closest('li');
  if (row === null) {
    throw new Error(`row not found: ${cellText}`);
  }
  return row;
}

/** 种子 blocked 恢复转终态，清空 active 互斥（避免新建备份 503 / 新建恢复 409 干扰用例）。 */
function clearActiveRestore(): void {
  mockAdmin.completeOpsRestore(OPS_BACKUP_SEED_IDS.repairRestore);
}

describe('权限与接线（规格 §2：严格 ops-only）', () => {
  it('admin 深链 /admin/operations/backups：按角色截断回退系统运维层，下钻行无「备份与恢复」', async () => {
    const token = loginToken('admin');
    await renderApp('/admin/operations/backups', 'admin', contractAdminApi(token));

    const dialog = await screen.findByRole('dialog', { name: modules.operations });
    // 任务队列 / 指标看板下钻行在；备份与恢复注册项 roles=['ops'] 整行隐藏
    expect(
      within(dialog).getByRole('button', { name: new RegExp(`^${modules.opsJobs}`) }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', { name: new RegExp(`^${modules.opsMetrics}`) }),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: new RegExp(modules.backups) }),
    ).toBeNull();
    // 截断后未渲染子层：无三分段单选组、无子层标题
    expect(within(dialog).queryByRole('radiogroup')).toBeNull();
    expect(within(dialog).queryByText(copyBackups.denied)).toBeNull();
  });

  it('非 ops 直接渲染本层：拒绝态文案，无分段控件', async () => {
    const token = loginToken('admin');
    await renderLayer(<BackupsLayer />, testUser({ role: 'admin' }), contractAdminApi(token));

    expect(screen.getByText(copyBackups.denied)).toBeInTheDocument();
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });

  it('ops 在 /admin/operations 见「备份与恢复」下钻行', async () => {
    const token = loginToken('ops-wang');
    await renderApp('/admin/operations', 'ops', contractAdminApi(token));
    const dialog = await screen.findByRole('dialog', { name: modules.operations });
    expect(
      within(dialog).getByRole('button', { name: new RegExp(`^${modules.backups}`) }),
    ).toBeInTheDocument();
  });

  it('ops 深链 /admin/operations/backups：三分段单选 + 种子备份行', async () => {
    const token = loginToken('ops-wang');
    await renderApp('/admin/operations/backups', 'ops', contractAdminApi(token));
    const drilled = await screen.findByRole('dialog', { name: modules.operations });
    const group = within(drilled).getByRole('radiogroup', { name: copyBackups.title });
    expect(within(group).getByRole('radio', { name: copyBackups.viewBackups })).toBeChecked();
    expect(within(group).getByRole('radio', { name: copyBackups.viewRestores })).toBeInTheDocument();
    expect(within(group).getByRole('radio', { name: copyBackups.viewPolicy })).toBeInTheDocument();
    // 种子备份行（complete + creating 两条）
    expect(
      await within(drilled).findByText(OPS_BACKUP_SEED_IDS.completeBackup),
    ).toBeInTheDocument();
    expect(within(drilled).getByText(OPS_BACKUP_SEED_IDS.creatingBackup)).toBeInTheDocument();
  });
});

describe('「备份」分段（规格 §9：single-flight / 轮询 / 分段状态）', () => {
  it('一键备份 single-flight：连点两次只受理一次（Idempotency-Key），202 后轻提示含 backup_id', async () => {
    const token = loginToken('ops-wang');
    let releaseCreate!: (value: OpsBackupCreateResponse) => void;
    const pendingCreate = new Promise<OpsBackupCreateResponse>((resolve) => {
      releaseCreate = resolve;
    });
    const createMock = vi.fn(() => pendingCreate);
    const adminApi = contractAdminApi(token, { createOpsBackup: createMock });
    await renderLayer(<BackupsLayer />, opsUser(), adminApi);
    await screen.findByText(OPS_BACKUP_SEED_IDS.completeBackup);

    const button = screen.getByRole('button', { name: copyBackups.createBackup });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(createMock).toHaveBeenCalledTimes(1);
    expect(createMock).toHaveBeenCalledWith(expect.stringMatching(/^idem_/));

    await act(async () => {
      releaseCreate({ backup_id: 'bk_new_1', status: 'creating' });
    });
    // 受理轻提示（HeaderNotice）；不按 role=status 取——Pill loading 加载点同 role 会抢先命中
    expect(await screen.findByText(copyBackups.backupCreated('bk_new_1'))).toBeInTheDocument();
  });

  it('5s 轮询收敛：creating 种子首轮「创建中」，下一拍行内转「完成」（读推进语义）', async () => {
    vi.useFakeTimers();
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderLayer(<BackupsLayer />, opsUser(), adminApi);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const row = rowOf(OPS_BACKUP_SEED_IDS.creatingBackup);
    expect(within(row).getByText(copyBackups.backupStatus('creating'))).toBeInTheDocument();
    const callsInitial = (adminApi.listOpsBackups as ReturnType<typeof vi.fn>).mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect((adminApi.listOpsBackups as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsInitial + 1,
    );
    // 同一 keyed 行内状态收敛为完成（无骨架屏回落）
    expect(
      within(rowOf(OPS_BACKUP_SEED_IDS.creatingBackup)).getByText(
        copyBackups.backupStatus('complete'),
      ),
    ).toBeInTheDocument();
  });

  it('分段切换保持状态：创建备份 → 切「策略」→ 切回「备份」，已创建备份行仍在', async () => {
    const token = loginToken('ops-wang');
    clearActiveRestore();
    const adminApi = contractAdminApi(token);
    await renderLayer(<BackupsLayer />, opsUser(), adminApi);
    await screen.findByText(OPS_BACKUP_SEED_IDS.completeBackup);

    fireEvent.click(screen.getByRole('button', { name: copyBackups.createBackup }));
    await waitFor(() => expect(adminApi.createOpsBackup).toHaveBeenCalledTimes(1));
    const created = (await (adminApi.createOpsBackup as ReturnType<typeof vi.fn>).mock.results[0]
      .value) as OpsBackupCreateResponse;
    expect(
      await screen.findByText(copyBackups.backupCreated(created.backup_id)),
    ).toBeInTheDocument();

    // 切「策略」：策略版本行渲染
    fireEvent.click(screen.getByRole('radio', { name: copyBackups.viewPolicy }));
    expect(await screen.findByText(copyBackups.policyVersion(1))).toBeInTheDocument();

    // 切回「备份」：重新拉取后已创建备份仍在列表
    fireEvent.click(screen.getByRole('radio', { name: copyBackups.viewBackups }));
    expect(await screen.findByText(created.backup_id)).toBeInTheDocument();
  });
});

describe('「恢复」分段（规格 §9：危险确认 / repair retry）', () => {
  it('一键恢复：来源选择 → 危险确认展示备份 ID / 维护影响 / 当前状态 → 受理轻提示 + 记录行', async () => {
    const token = loginToken('ops-wang');
    clearActiveRestore();
    const adminApi = contractAdminApi(token);
    await renderLayer(<BackupsLayer />, opsUser(), adminApi);

    fireEvent.click(screen.getByRole('radio', { name: copyBackups.viewRestores }));
    // 来源候选仅 restorable 备份：complete 种子可选
    const sourceOption = await screen.findByRole('option', {
      name: new RegExp(`^${OPS_BACKUP_SEED_IDS.completeBackup}`),
    });
    const select = sourceOption.closest('select');
    if (select === null) {
      throw new Error('restore source select missing');
    }
    fireEvent.change(select, { target: { value: OPS_BACKUP_SEED_IDS.completeBackup } });
    fireEvent.click(screen.getByRole('button', { name: copyBackups.startRestore }));

    // 危险确认：备份 ID、维护模式影响说明、备份当前状态三条齐备才可提交
    const dialog = await screen.findByRole('dialog', { name: copyBackups.restoreDialogTitle });
    expect(
      within(dialog).getByText(copyBackups.restoreDialogBackup(OPS_BACKUP_SEED_IDS.completeBackup)),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(copyBackups.restoreDialogImpact)).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        copyBackups.restoreDialogStatus(copyBackups.backupStatus('complete')),
      ),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: copyBackups.restoreConfirm }));
    await waitFor(() =>
      expect(adminApi.createOpsRestore).toHaveBeenCalledWith(
        OPS_BACKUP_SEED_IDS.completeBackup,
        expect.stringMatching(/^idem_/),
      ),
    );
    const created = (await (adminApi.createOpsRestore as ReturnType<typeof vi.fn>).mock.results[0]
      .value) as { restore_id: string };
    expect(
      await screen.findByText(copyBackups.restoreStarted(created.restore_id)),
    ).toBeInTheDocument();
    // 受理后列表刷新含新恢复记录行
    expect(await screen.findByText(created.restore_id)).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: copyBackups.restoreDialogTitle })).toBeNull();
  });

  it('repair retry：受理轻提示 + 同拍详情收敛「已修复」，重试按钮消失', async () => {
    // 真实计时器：全链路为微任务（契约 mock 同步响应），无需推进轮询；fake timers 会冻结 findBy 轮询
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    await renderLayer(<BackupsLayer />, opsUser(), adminApi, [
      '/admin/operations/backups?view=restores',
    ]);

    // 展开种子 blocked 恢复：阶段列表 + open 修复目标
    const seedRow = await screen.findByText(OPS_BACKUP_SEED_IDS.repairRestore);
    fireEvent.click(
      within(seedRow.closest('li') as HTMLElement).getByRole('button', {
        name: copyBackups.progressExpand,
      }),
    );
    // 修复目标行内文本为「向量索引 · doc_seed_1 · 待处理 · 失败分类：…」组合串，按子串匹配
    expect(
      await within(rowOf(OPS_BACKUP_SEED_IDS.repairRestore)).findByText(
        new RegExp(copyBackups.repairStatus('open')),
      ),
    ).toBeInTheDocument();
    expect(
      within(rowOf(OPS_BACKUP_SEED_IDS.repairRestore)).getByText(/doc_seed_1/),
    ).toBeInTheDocument();

    fireEvent.click(
      within(rowOf(OPS_BACKUP_SEED_IDS.repairRestore)).getByRole('button', {
        name: copyBackups.repairRetry,
      }),
    );
    await waitFor(() =>
      expect(adminApi.retryOpsRepairTarget).toHaveBeenCalledWith(
        OPS_BACKUP_SEED_IDS.repairRestore,
        OPS_BACKUP_SEED_IDS.repairTarget,
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(copyBackups.repairRetried)).toBeInTheDocument();
    // 同一读序列详情收敛：修复目标转「已修复」，重试按钮撤下
    expect(
      await within(rowOf(OPS_BACKUP_SEED_IDS.repairRestore)).findByText(
        new RegExp(copyBackups.repairStatus('succeeded')),
      ),
    ).toBeInTheDocument();
    expect(
      within(rowOf(OPS_BACKUP_SEED_IDS.repairRestore)).queryByRole('button', {
        name: copyBackups.repairRetry,
      }),
    ).toBeNull();
  });
});

describe('「策略」分段（规格 §9：版本化保存 / 422 映射）', () => {
  it('version_conflict：提示并刷新最新值（表单重置）；再次保存成功版本递增', async () => {
    const token = loginToken('ops-wang');
    clearActiveRestore();
    const adminApi = contractAdminApi(token);
    await renderLayer(<BackupsLayer />, opsUser(), adminApi);

    fireEvent.click(screen.getByRole('radio', { name: copyBackups.viewPolicy }));
    // 初始表单：默认策略 keep_last 7；固定展示保留规则说明与版本
    expect(await screen.findByDisplayValue('7')).toBeInTheDocument();
    expect(screen.getByText(copyBackups.policyRetentionNote)).toBeInTheDocument();
    expect(screen.getByText(copyBackups.policyVersion(1))).toBeInTheDocument();

    // 外部并发修改把版本顶到 2（keep_last=9）
    mockAdmin.patchOpsBackupPolicy(token, { expected_version: 1, keep_last: 9 }, 'idem_external_1');

    // UI 仍持版本 1：改 retention_days 保存 → 409 version_conflict → 表单重置为服务端值
    fireEvent.change(screen.getByDisplayValue('30'), { target: { value: '60' } });
    fireEvent.click(screen.getByRole('button', { name: copyBackups.policySave }));
    expect(await screen.findByText(copyBackups.policyVersionConflict)).toBeInTheDocument();
    expect(await screen.findByDisplayValue('9')).toBeInTheDocument();
    expect(await screen.findByText(copyBackups.policyVersion(2))).toBeInTheDocument();

    // 基于最新版本再次保存成功：轻提示 + 版本递增到 3
    fireEvent.change(screen.getByDisplayValue('30'), { target: { value: '60' } });
    fireEvent.click(screen.getByRole('button', { name: copyBackups.policySave }));
    expect(await screen.findByText(copyBackups.policySaved)).toBeInTheDocument();
    expect(await screen.findByText(copyBackups.policyVersion(3))).toBeInTheDocument();
    expect(adminApi.patchOpsBackupPolicy).toHaveBeenLastCalledWith(
      expect.objectContaining({ expected_version: 2, retention_days: 60, keep_last: 9 }),
      expect.stringMatching(/^idem_/),
    );
  });

  it('422 field=timezone：行内时区无效提示（服务端校验兜底，表单保留输入）', async () => {
    const token = loginToken('ops-wang');
    clearActiveRestore();
    const adminApi = contractAdminApi(token, {
      // 组件测试直连控制器（传输层校验在 handler）：override 复现服务端 422 形态
      patchOpsBackupPolicy: vi.fn(async () => {
        throw new ApiError({
          status: 422,
          code: 'validation_error',
          message: 'invalid timezone',
          details: { field: 'timezone' },
          requestId: null,
        });
      }),
    });
    await renderLayer(<BackupsLayer />, opsUser(), adminApi);

    fireEvent.click(screen.getByRole('radio', { name: copyBackups.viewPolicy }));
    const timezoneInput = await screen.findByDisplayValue('Asia/Shanghai');
    fireEvent.change(timezoneInput, { target: { value: 'Mars/Olympus' } });
    fireEvent.click(screen.getByRole('button', { name: copyBackups.policySave }));

    expect(await screen.findByText(copyBackups.policyTimezoneInvalid)).toBeInTheDocument();
    // 表单保留用户输入（不重置），版本行仍为 1
    expect(screen.getByDisplayValue('Mars/Olympus')).toBeInTheDocument();
    expect(screen.getByText(copyBackups.policyVersion(1))).toBeInTheDocument();
  });
});
