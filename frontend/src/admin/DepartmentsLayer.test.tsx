/*
 * 部门管理测试（§12.5；验收 A5、A55–A59；仅 admin 下钻层）。
 * 经契约 mock（MockAdminController 直接代理；MockHttpError 归一化为 ApiError）：
 * 三档状态筛选默认在用与切换 / 刷新传参（接口无分页与搜索 → 不渲染分页器与搜索框）；
 * 行操作唯一依据 allowed_actions（admin active 行有改名 / 停用、inactive 行「—」、
 * 未知值不渲染）；新增幂等键语义（网络未知同键同体重试、业务错误后新键、
 * idempotency_key_conflict 不自动重发）；改名预填禁用、成功就地更新 + 名称交叉淡变、
 * version_conflict 新 version 新键重试；停用三点说明 + 计数行、409 阻断无「强制」入口、
 * version_conflict、503 unverified 重试新键、成功原地转已停用；空态两档新增入口；
 * 能力边界（无重新启用 / 物理删除 / 批量迁移 / 合并 / 部门描述入口）。
 */

import { act, render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
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
import { DepartmentsLayer } from './DepartmentsLayer';
import type { AdminDepartmentItem, DepartmentStatusFilter } from './types';

const copyDepartments = copy.admin.departments;
const copyCommon = copy.admin.common;
const copyControls = copy.controls;
const copyStates = copy.states;

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

function apiError(status: number | null, code: string): ApiError {
  return new ApiError({ status, code, message: code, details: {}, requestId: null });
}

function contractAdminApi(token: string, overrides: Partial<AdminApi> = {}): AdminApi {
  return fakeAdminApi({
    listDepartments: vi.fn((status?: DepartmentStatusFilter) =>
      call(() => mockAdmin.listDepartments(token, status)),
    ),
    createDepartment: vi.fn((name: string, key: string) =>
      call(() => mockAdmin.createDepartment(token, name, key)),
    ),
    renameDepartment: vi.fn((id: string, version: number, name: string, key: string) =>
      call(() => mockAdmin.renameDepartment(token, id, version, name, key)),
    ),
    deactivateDepartment: vi.fn((id: string, version: number, key: string) =>
      call(() => mockAdmin.deactivateDepartment(token, id, version, key)),
    ),
    ...overrides,
  });
}

type RenderedLayer = RenderResult & { api: AdminApi };

async function renderLayer(overrides: Partial<AdminApi> = {}): Promise<RenderedLayer> {
  const user = adminUser();
  const store = await createAuthedStore(user);
  const api = contractAdminApi(loginToken(user.username), overrides);
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <AdminProvider api={api}>
            <EscStackProvider>
              <DepartmentsLayer />
            </EscStackProvider>
          </AdminProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return Object.assign(result, { api });
}

function rowOf(name: string): HTMLElement {
  const row = screen.getByText(name).closest('li');
  if (row === null) {
    throw new Error(`row not found: ${name}`);
  }
  return row;
}

function createKeys(api: AdminApi): string[] {
  return vi.mocked(api.createDepartment).mock.calls.map((args) => args[1]);
}

function renameKeys(api: AdminApi): string[] {
  return vi.mocked(api.renameDepartment).mock.calls.map((args) => args[3]);
}

function deactivateKeys(api: AdminApi): string[] {
  return vi.mocked(api.deactivateDepartment).mock.calls.map((args) => args[2]);
}

async function openCreate(): Promise<HTMLElement> {
  await userEvent.click(screen.getByRole('button', { name: copyDepartments.add }));
  return screen.findByRole('dialog', { name: copyDepartments.addDialogTitle });
}

async function openRename(name: string): Promise<HTMLElement> {
  await userEvent.click(
    within(rowOf(name)).getByRole('button', { name: copyDepartments.rename }),
  );
  return screen.findByRole('dialog', { name: copyDepartments.renameDialogTitle });
}

async function openDeactivate(name: string): Promise<HTMLElement> {
  await userEvent.click(
    within(rowOf(name)).getByRole('button', { name: copyDepartments.deactivate }),
  );
  return screen.findByRole('dialog', { name: copyDepartments.deactivateDialogTitle });
}

describe('工具行：状态筛选、刷新与无分页无搜索', () => {
  it('部门目录向辅助技术公开列头与单元格关系', async () => {
    await renderLayer();

    const table = await screen.findByRole('table', { name: copy.shell.drawer.modules.departments });
    expect(within(table).getAllByRole('columnheader')).toHaveLength(8);
    const row = within(table).getByRole('row', { name: /财务部/ });
    expect(within(row).getAllByRole('cell')).toHaveLength(8);
  });

  it('默认「在用」；切换分段与点击刷新按当前 status 传参', async () => {
    const { api } = await renderLayer();
    await screen.findByText('财务部');
    expect(api.listDepartments).toHaveBeenLastCalledWith('active');
    await userEvent.click(
      screen.getByRole('radio', { name: copyDepartments.filterInactive }),
    );
    await screen.findByText('档案部');
    expect(api.listDepartments).toHaveBeenLastCalledWith('inactive');
    expect(screen.queryByText('财务部')).toBeNull();
    await userEvent.click(screen.getByRole('radio', { name: copyDepartments.filterAll }));
    await screen.findByText('财务部');
    expect(api.listDepartments).toHaveBeenLastCalledWith('all');
    await userEvent.click(screen.getByRole('button', { name: copyCommon.refresh }));
    await waitFor(() => {
      expect(api.listDepartments).toHaveBeenLastCalledWith('all');
    });
  });

  it('接口未定义分页与搜索参数：不渲染分页器与搜索框', async () => {
    await renderLayer();
    await screen.findByText('财务部');
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(screen.queryByRole('searchbox')).toBeNull();
  });

  it('渲染八列表头', async () => {
    await renderLayer();
    await screen.findByText('财务部');
    for (const label of [
      copyDepartments.colName,
      copyDepartments.colStatus,
      copyDepartments.colMembers,
      copyDepartments.colDocuments,
      copyDepartments.colTasks,
      copyDepartments.colSubmissions,
      copyDepartments.colDeactivatedAt,
      copyDepartments.colActions,
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
});

describe('行操作以 allowed_actions 为唯一依据', () => {
  it('admin 的 active 行有改名 / 停用；inactive 行「—」无按钮', async () => {
    await renderLayer();
    await screen.findByText('财务部');
    for (const name of ['财务部', '人事部', '空壳部']) {
      expect(
        within(rowOf(name)).getByRole('button', { name: copyDepartments.rename }),
      ).toBeInTheDocument();
      expect(
        within(rowOf(name)).getByRole('button', { name: copyDepartments.deactivate }),
      ).toBeInTheDocument();
    }
    await userEvent.click(
      screen.getByRole('radio', { name: copyDepartments.filterInactive }),
    );
    await screen.findByText('档案部');
    const row = rowOf('档案部');
    expect(within(row).queryByRole('button')).toBeNull();
    expect(within(row).getAllByText(copyDepartments.noActions).length).toBeGreaterThanOrEqual(1);
  });

  it('allowed_actions 出现未知值：不渲染该操作，操作列「—」', async () => {
    const fogDepartment: AdminDepartmentItem = {
      id: 'd_fog',
      name: '迷雾部',
      status: 'active',
      version: 1,
      document_count: 0,
      member_count: 0,
      nonterminal_job_count: 0,
      pending_submission_count: 0,
      deactivated_at: null,
      allowed_actions: ['explode'] as unknown as AdminDepartmentItem['allowed_actions'],
    };
    await renderLayer({
      listDepartments: vi.fn(async () => ({ items: [fogDepartment] })),
    });
    await screen.findByText('迷雾部');
    const row = rowOf('迷雾部');
    expect(within(row).queryByRole('button')).toBeNull();
    expect(within(row).getAllByText(copyDepartments.noActions).length).toBeGreaterThanOrEqual(2);
  });
});

describe('新增部门与幂等键语义', () => {
  it('空名称禁用确认；创建成功新行自列表顶部插入', async () => {
    const { api } = await renderLayer();
    await screen.findByText('财务部');
    const dialog = await openCreate();
    const input = within(dialog).getByLabelText(copyDepartments.nameLabel);
    const confirm = within(dialog).getByRole('button', { name: copyControls.confirm });
    expect(confirm).toBeDisabled();
    await userEvent.type(input, '战略部');
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.createDepartment).toHaveBeenCalledWith('战略部', expect.any(String));
    await screen.findByText('战略部');
    expect(rowOf('战略部').className).toContain('ui-row-insert');
  });

  it('名称对话框在有效输入后以 Enter 提交', async () => {
    const { api } = await renderLayer();
    await screen.findByText('财务部');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyDepartments.nameLabel), '回车部门');

    await userEvent.keyboard('{Enter}');

    await waitFor(() => expect(api.createDepartment).toHaveBeenCalledWith('回车部门', expect.any(String)));
  });

  it('网络结果未知：同键同体保留，用户显式重试复用同一键', async () => {
    const { api } = await renderLayer();
    await screen.findByText('财务部');
    vi.mocked(api.createDepartment).mockRejectedValueOnce(apiError(null, 'network_error'));
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyDepartments.nameLabel), '战略二部');
    const confirm = within(dialog).getByRole('button', { name: copyControls.confirm });
    await userEvent.click(confirm);
    await within(dialog).findByText(copyDepartments.actionError);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const keys = createKeys(api);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    await screen.findByText('战略二部');
  });

  it('重名 409：框下说明；业务错误后再次确认使用新键；改名后再确认键再换', async () => {
    const { api } = await renderLayer();
    await screen.findByText('财务部');
    const dialog = await openCreate();
    const input = within(dialog).getByLabelText(copyDepartments.nameLabel);
    const confirm = within(dialog).getByRole('button', { name: copyControls.confirm });
    await userEvent.type(input, '财务部');
    await userEvent.click(confirm);
    await within(dialog).findByText(copyDepartments.nameExists);
    // 已收业务响应：不静默重发；用户再次显式确认 → 新键（仍重名）
    await userEvent.click(confirm);
    await within(dialog).findByText(copyDepartments.nameExists);
    const keysAfterConflict = createKeys(api);
    expect(keysAfterConflict).toHaveLength(2);
    expect(keysAfterConflict[1]).not.toBe(keysAfterConflict[0]);
    // 改成新名称：payload 指纹变化 → 再换新键，创建成功
    await userEvent.clear(input);
    await userEvent.type(input, '战略三部');
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const keys = createKeys(api);
    expect(keys).toHaveLength(3);
    expect(keys[2]).not.toBe(keys[1]);
    await screen.findByText('战略三部');
  });

  it('idempotency_key_conflict：仅提示，不换键不自动重发', async () => {
    const { api } = await renderLayer({
      createDepartment: vi.fn((_name: string, _key: string) =>
        Promise.reject(apiError(409, 'idempotency_key_conflict')),
      ),
    });
    await screen.findByText('财务部');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyDepartments.nameLabel), '战略部');
    await userEvent.click(within(dialog).getByRole('button', { name: copyControls.confirm }));
    await within(dialog).findByText(copyDepartments.actionError);
    expect(within(dialog).queryByText(copyDepartments.nameExists)).toBeNull();
    expect(vi.mocked(api.createDepartment).mock.calls).toHaveLength(1);
  });
});

describe('改名', () => {
  it('预填原名称且未变更时确认禁用；成功就地更新 + 名称交叉淡变 + fog-white 闪现', async () => {
    const { api } = await renderLayer();
    await screen.findByText('财务部');
    const dialog = await openRename('财务部');
    const input = within(dialog).getByLabelText(copyDepartments.nameLabel);
    expect(input).toHaveValue('财务部');
    const save = within(dialog).getByRole('button', { name: copy.admin.users.save });
    expect(save).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, '财务一部');
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.renameDepartment).toHaveBeenCalledWith(
      'd_finance',
      1,
      '财务一部',
      expect.any(String),
    );
    await screen.findByText('财务一部');
    const row = rowOf('财务一部');
    expect(row.className).toContain('bg-fog-white');
    expect(within(row).getByText('财务一部').className).toContain('ui-fade-enter-fast');
  });

  it('版本冲突：刷新该行并提示，基于新 version 以新键重试成功', async () => {
    const { api } = await renderLayer();
    const behind = loginToken('admin');
    await screen.findByText('财务部');
    const dialog = await openRename('财务部');
    // 幕后改名：version 1 → 2
    mockAdmin.renameDepartment(behind, 'd_finance', 1, '财务部·改', 'idem-behind-rename');
    const input = within(dialog).getByLabelText(copyDepartments.nameLabel);
    await userEvent.clear(input);
    await userEvent.type(input, '财务二部');
    const save = within(dialog).getByRole('button', { name: copy.admin.users.save });
    await userEvent.click(save);
    await within(dialog).findByText(copyDepartments.versionConflict);
    await userEvent.click(save);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.renameDepartment).toHaveBeenLastCalledWith(
      'd_finance',
      2,
      '财务二部',
      expect.any(String),
    );
    const keys = renameKeys(api);
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
    await screen.findByText('财务二部');
  });
});

describe('停用', () => {
  it('仍有成员：409 阻断 + 框内说明，不出现「强制」入口', async () => {
    await renderLayer();
    await screen.findByText('财务部');
    const dialog = await openDeactivate('财务部');
    expect(within(dialog).getByText(copyDepartments.deactivatePoint1)).toBeInTheDocument();
    expect(within(dialog).getByText(copyDepartments.deactivatePoint2)).toBeInTheDocument();
    expect(within(dialog).getByText(copyDepartments.deactivatePoint3)).toBeInTheDocument();
    expect(
      within(dialog).getByText(copyDepartments.deactivateCounts(3, 0, 2)),
    ).toBeInTheDocument();
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyDepartments.deactivateConfirm }),
    );
    await within(dialog).findByText(copyDepartments.blockedHasMembers);
    expect(screen.queryByText(/强制/)).toBeNull();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('仍有进行中任务或待审投稿：409 阻断 + 框内说明', async () => {
    await renderLayer();
    await screen.findByText('人事部');
    const dialog = await openDeactivate('人事部');
    expect(
      within(dialog).getByText(copyDepartments.deactivateCounts(0, 1, 1)),
    ).toBeInTheDocument();
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyDepartments.deactivateConfirm }),
    );
    await within(dialog).findByText(copyDepartments.blockedHasWork);
    expect(screen.queryByText(/强制/)).toBeNull();
  });

  it('版本冲突后重试成功：行内原地转「已停用」、操作列「—」、记录停用时间', async () => {
    const { api } = await renderLayer();
    const behind = loginToken('admin');
    await screen.findByText('空壳部');
    const dialog = await openDeactivate('空壳部');
    // 幕后改名：version 1 → 2
    mockAdmin.renameDepartment(behind, 'd_empty', 1, '空壳部·改', 'idem-behind-deactivate');
    const confirm = within(dialog).getByRole('button', {
      name: copyDepartments.deactivateConfirm,
    });
    await userEvent.click(confirm);
    await within(dialog).findByText(copyDepartments.versionConflict);
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.deactivateDepartment).toHaveBeenLastCalledWith(
      'd_empty',
      2,
      expect.any(String),
    );
    const row = rowOf('空壳部·改');
    expect(within(row).getByText(copyDepartments.statusInactive)).toBeInTheDocument();
    expect(within(row).queryByRole('button')).toBeNull();
    // 停用时间已填（不再「—」）；仅剩操作列一个「—」
    expect(within(row).getAllByText(copyDepartments.noActions)).toHaveLength(1);
    expect(row.className).toContain('bg-fog-white');
  });

  it('503 unverified：说明 + 重试入口；重试新键；恢复后重试成功', async () => {
    const { api } = await renderLayer();
    await screen.findByText('空壳部');
    mockAdmin.setDepartmentDeactivationUnverified('d_empty', true);
    const dialog = await openDeactivate('空壳部');
    const confirm = within(dialog).getByRole('button', {
      name: copyDepartments.deactivateConfirm,
    });
    await userEvent.click(confirm);
    await within(dialog).findByText(copyDepartments.unverified);
    const retry = within(dialog).getByRole('button', { name: copyStates.retry });
    await userEvent.click(retry);
    await within(dialog).findByText(copyDepartments.unverified);
    const keysAfterRetry = deactivateKeys(api);
    expect(keysAfterRetry).toHaveLength(2);
    expect(keysAfterRetry[1]).not.toBe(keysAfterRetry[0]);
    mockAdmin.setDepartmentDeactivationUnverified('d_empty', false);
    await userEvent.click(within(dialog).getByRole('button', { name: copyStates.retry }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.deactivateDepartment).toHaveBeenLastCalledWith(
      'd_empty',
      1,
      expect.any(String),
    );
    const row = rowOf('空壳部');
    expect(within(row).getByText(copyDepartments.statusInactive)).toBeInTheDocument();
  });

  it('409 department_inactive：关框、列表错误行提示并刷新目录', async () => {
    const { api } = await renderLayer({
      deactivateDepartment: vi.fn((_id: string, _version: number, _key: string) =>
        Promise.reject(apiError(409, 'department_inactive')),
      ),
    });
    await screen.findByText('空壳部');
    const callsBefore = vi.mocked(api.listDepartments).mock.calls.length;
    const dialog = await openDeactivate('空壳部');
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyDepartments.deactivateConfirm }),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(copyDepartments.statusChanged);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => {
      expect(vi.mocked(api.listDepartments).mock.calls.length).toBeGreaterThan(callsBefore);
    });
  });
});

describe('空态与能力边界', () => {
  it('空目录：空态文案；「在用」档附新增入口，「已停用」档仅工具行入口', async () => {
    await renderLayer({
      listDepartments: vi.fn(async () => ({ items: [] })),
    });
    await screen.findByText(copyDepartments.empty);
    expect(
      screen.getAllByRole('button', { name: copyDepartments.add }),
    ).toHaveLength(2);
    await userEvent.click(
      screen.getByRole('radio', { name: copyDepartments.filterInactive }),
    );
    await screen.findByText(copyDepartments.empty);
    expect(
      screen.getAllByRole('button', { name: copyDepartments.add }),
    ).toHaveLength(1);
  });

  it('能力边界：无重新启用 / 物理删除 / 批量迁移 / 合并 / 部门描述任何入口', async () => {
    await renderLayer();
    await screen.findByText('财务部');
    for (const forbidden of ['重新启用', '物理删除', '批量迁移', '合并', '部门描述']) {
      expect(screen.queryByText(forbidden)).toBeNull();
    }
  });
});
