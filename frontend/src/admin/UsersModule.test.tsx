/*
 * 用户管理模块测试（§12.1–12.7；验收 A4、A45–A54、A61）。
 * 经契约 mock（MockAdminController 直接代理，与真实 handler 同源；MockHttpError 归一化为
 * ApiError）：工具行搜索防抖与筛选叠加传参、空态清除条件；六列结构与冻结行只读；admin/ops
 * 操作可见性矩阵；编辑对话框每次打开重拉 active 目录、省略语义、无部门 null、422/409/404/
 * version_conflict/403 错误路径；新增对话框校验、眼睛控件、username_exists、成功顶部插入；
 * 永久禁用三点说明、202 原地冻结、version_conflict/user_pending_delete/403；ops 视图无部门
 * 入口与权限矩阵；admin 只读矩阵；部门管理下钻入口与深链回落（registry）。列可读性（A1/A45）：
 * 截断单元格 title 悬停全文、行栅格窄屏可收缩与 ≥1024px 最小列宽。
 */

import { act, fireEvent, render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
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
import { createDrawerRegistry } from '../shell/drawer/DrawerRegistryProvider';
import { createAuthedStore, fakeAdminApi } from '../test/auth-fixtures';
import { AdminProvider } from './AdminProvider';
import type { AdminApi } from './api';
import { formatDate, formatDateTime } from './format';
import type {
  AdminUserCreateInput,
  AdminUserListQuery,
  AdminUserPatchInput,
  DepartmentStatusFilter,
} from './types';
import { UsersModule } from './UsersModule';

const copyUsers = copy.admin.users;
const copyCommon = copy.admin.common;
const copyProfile = copy.settings.profile;
const copyLogin = copy.login;
const copyControls = copy.controls;

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

function apiError(status: number, code: string): ApiError {
  return new ApiError({ status, code, message: code, details: {}, requestId: null });
}

function contractAdminApi(token: string, overrides: Partial<AdminApi> = {}): AdminApi {
  return fakeAdminApi({
    listUsers: vi.fn((query: AdminUserListQuery) =>
      call(() => mockAdmin.listUsers(token, query)),
    ),
    createUser: vi.fn((input: AdminUserCreateInput) =>
      call(() => mockAdmin.createUser(token, input)),
    ),
    patchUser: vi.fn((userId: string, input: AdminUserPatchInput) =>
      call(() => mockAdmin.patchUser(token, userId, input)),
    ),
    deleteUser: vi.fn((userId: string, expectedVersion: number) =>
      call(() => mockAdmin.deleteUser(token, userId, expectedVersion)),
    ),
    listDepartments: vi.fn((status?: DepartmentStatusFilter) =>
      call(() => mockAdmin.listDepartments(token, status)),
    ),
    getPermissionMatrix: vi.fn(() => call(() => mockAdmin.getPermissionMatrix(token))),
    ...overrides,
  });
}

type RenderedModule = RenderResult & { api: AdminApi };

async function renderModule(
  user: User,
  overrides: Partial<AdminApi> = {},
): Promise<RenderedModule> {
  const store = await createAuthedStore(user);
  const api = contractAdminApi(loginToken(user.username), overrides);
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <AdminProvider api={api}>
            <EscStackProvider>
              <UsersModule />
            </EscStackProvider>
          </AdminProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return Object.assign(result, { api });
}

function rowOf(realName: string): HTMLElement {
  const row = screen.getByText(realName).closest('li');
  if (row === null) {
    throw new Error(`row not found: ${realName}`);
  }
  return row;
}

async function openEdit(realName: string): Promise<HTMLElement> {
  await userEvent.click(within(rowOf(realName)).getByRole('button', { name: copyUsers.edit }));
  return screen.findByRole('dialog', { name: copyUsers.editDialogTitle });
}

async function openCreate(): Promise<HTMLElement> {
  await userEvent.click(screen.getByRole('button', { name: copyUsers.addUser }));
  return screen.findByRole('dialog', { name: copyUsers.addDialogTitle });
}

async function openDisable(realName: string): Promise<HTMLElement> {
  await userEvent.click(within(rowOf(realName)).getByRole('button', { name: copyUsers.disable }));
  return screen.findByRole('dialog', { name: copyUsers.disableDialogTitle });
}

/** 等待编辑 / 新增对话框内部门下拉完成目录加载（加载态为禁用 select）。 */
async function waitDepartmentSelect(dialog: HTMLElement): Promise<HTMLElement> {
  const select = within(dialog).getByRole('combobox');
  await waitFor(() => expect(select).toBeEnabled());
  return select;
}

describe('工具行：搜索防抖与筛选叠加', () => {
  it('用户列表向辅助技术公开列头与单元格关系', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');

    const table = screen.getByRole('table', { name: copy.shell.drawer.modules.usersOps });
    expect(within(table).getAllByRole('columnheader')).toHaveLength(6);
    expect(within(rowOf('张三')).getAllByRole('cell')).toHaveLength(6);
  });

  it('初次拉取第一页；键入 300ms 防抖后带 q 重查第一页', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    expect(api.listUsers).toHaveBeenLastCalledWith({ page: 1, pageSize: 20 });
    await userEvent.type(screen.getByPlaceholderText(copyUsers.searchPlaceholder), '张');
    await waitFor(
      () => {
        expect(api.listUsers).toHaveBeenLastCalledWith({ q: '张', page: 1, pageSize: 20 });
      },
      { timeout: 1500 },
    );
  });

  it('部门与角色筛选叠加为同一组查询参数', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    await userEvent.click(screen.getByRole('button', { name: copyUsers.departmentFilter }));
    const departmentGroup = await screen.findByRole('radiogroup', {
      name: copyUsers.departmentFilter,
    });
    await userEvent.click(
      await within(departmentGroup).findByRole('radio', { name: '财务部' }),
    );
    await waitFor(() => {
      expect(api.listUsers).toHaveBeenLastCalledWith({
        departmentId: 'd_finance',
        page: 1,
        pageSize: 20,
      });
    });
    await userEvent.click(screen.getByRole('button', { name: copyUsers.roleFilter }));
    const roleGroup = await screen.findByRole('radiogroup', { name: copyUsers.roleFilter });
    await userEvent.click(
      within(roleGroup).getByRole('radio', { name: copyProfile.roleMinister }),
    );
    await waitFor(() => {
      expect(api.listUsers).toHaveBeenLastCalledWith({
        departmentId: 'd_finance',
        role: 'minister',
        page: 1,
        pageSize: 20,
      });
    });
  });

  it('无结果时展示空态；点击清除条件回到无筛选第一页', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    await userEvent.type(
      screen.getByPlaceholderText(copyUsers.searchPlaceholder),
      '不存在的人',
    );
    await screen.findByText(copyUsers.empty, {}, { timeout: 1500 });
    await userEvent.click(screen.getByRole('button', { name: copyUsers.clearFilters }));
    await waitFor(() => {
      expect(api.listUsers).toHaveBeenLastCalledWith({ page: 1, pageSize: 20 });
    });
    await screen.findByText('张三');
  });
});

describe('用户表结构与冻结行', () => {
  it('渲染六列表头与 56px 行高；无部门显示「—」', async () => {
    await renderModule(adminUser());
    await screen.findByText('赵六');
    for (const label of [
      copyUsers.colRealName,
      copyUsers.colUsername,
      copyUsers.colDepartment,
      copyUsers.colRole,
      copyUsers.colLastActive,
      copyUsers.colActions,
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    const row = rowOf('赵六');
    expect(row.firstElementChild?.className).toContain('h-14');
    expect(within(row).getAllByText(copyUsers.noDepartment)).toHaveLength(1);
  });

  it('冻结行展示冻结标记与清理时间，不渲染任何操作入口', async () => {
    await renderModule(adminUser());
    await screen.findByText('鬼影');
    const row = rowOf('鬼影');
    expect(within(row).getByText(copyCommon.frozenTag)).toBeInTheDocument();
    expect(within(row).getByText(/^将于 .* 清理$/)).toBeInTheDocument();
    expect(within(row).queryByRole('button')).toBeNull();
  });
});

describe('列可读性与悬停全文（A1、A45；D1 截断策略）', () => {
  it('「最近活跃」单元格展示完整时间文本并带 title 悬停全文', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    const expected = formatDateTime('2026-08-04T09:00:00Z');
    const cell = within(rowOf('张三')).getByText(expected);
    expect(cell).toHaveAttribute('title', expected);
  });

  it('冻结行角色文本与「将于 … 清理」完整展示且带 title；冻结 tag 不被压缩', async () => {
    await renderModule(adminUser());
    await screen.findByText('鬼影');
    const row = rowOf('鬼影');
    const roleText = within(row).getByText(copyProfile.roleUser);
    expect(roleText).toHaveAttribute('title', copyProfile.roleUser);
    const frozenTag = within(row).getByText(copyCommon.frozenTag);
    expect(frozenTag.className).toContain('shrink-0');
    const purgeText = copyUsers.purgeAfter(formatDate('2026-08-19T08:00:00Z'));
    const purgeCell = within(row).getByText(purgeText);
    expect(purgeCell).toHaveAttribute('title', purgeText);
  });

  it('姓名 / 用户名 / 部门截断列均带 title 悬停全文（无部门行回退「—」）', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    const row = rowOf('张三');
    expect(within(row).getByText('张三')).toHaveAttribute('title', '张三');
    expect(within(row).getByText('zhangsan')).toHaveAttribute('title', 'zhangsan');
    expect(within(row).getByText('财务部')).toHaveAttribute('title', '财务部');
    const noDepartmentCell = within(rowOf('赵六')).getByText(copyUsers.noDepartment);
    expect(noDepartmentCell).toHaveAttribute('title', copyUsers.noDepartment);
  });

  it('行栅格：窄屏基础模板保持可收缩 minmax(0,…)（A4 不变）；≥1024px 角色 / 最近活跃列加最小宽', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    const tokens = (rowOf('张三').firstElementChild?.className ?? '').split(' ');
    // 基础（<1024px，含窄屏）模板与改动前一致：六列均可收缩，不引入固定宽溢出
    expect(tokens).toContain(
      'grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_minmax(0,1.2fr)_minmax(0,1.2fr)_auto]',
    );
    // ≥1024px：角色列 ≥168px（角色 + 冻结 tag）、最近活跃列 ≥136px（完整时间 / 清理日期）
    expect(tokens).toContain(
      'lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_minmax(168px,1.2fr)_minmax(136px,1.2fr)_auto]',
    );
  });
});

describe('操作可见性（§12 规则表）', () => {
  it('admin 视角：普通用户 / 部长 / 运维可管理；自己与冻结账号无操作', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    for (const name of ['张三', '李部长', '王运维']) {
      expect(
        within(rowOf(name)).getByRole('button', { name: copyUsers.edit }),
      ).toBeInTheDocument();
      expect(
        within(rowOf(name)).getByRole('button', { name: copyUsers.disable }),
      ).toBeInTheDocument();
    }
    for (const name of ['系统管理员', '鬼影', '前管理员']) {
      expect(within(rowOf(name)).queryByRole('button')).toBeNull();
    }
  });

  it('ops 视角：普通用户与部长可管理；自己 / 其他运维 / admin / 冻结行无操作', async () => {
    mockAdmin.createUser(loginToken('admin'), {
      username: 'ops_two',
      real_name: '运维乙',
      department_id: null,
      role: 'ops',
      initial_password: 'passw0rd1',
    });
    await renderModule(opsUser());
    await screen.findByText('张三');
    for (const name of ['张三', '李部长']) {
      expect(
        within(rowOf(name)).getByRole('button', { name: copyUsers.edit }),
      ).toBeInTheDocument();
    }
    for (const name of ['王运维', '运维乙', '系统管理员', '鬼影']) {
      expect(within(rowOf(name)).queryByRole('button')).toBeNull();
    }
  });
});

describe('编辑用户对话框', () => {
  it('每次打开重新拉取 active 部门目录；未改部门时保存省略 department_id', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const before = vi.mocked(api.listDepartments).mock.calls.length;
    const dialog = await openEdit('张三');
    expect(vi.mocked(api.listDepartments).mock.calls.length).toBe(before + 1);
    expect(api.listDepartments).toHaveBeenLastCalledWith('active');
    const select = await waitDepartmentSelect(dialog);
    expect(select).toHaveValue('d_finance');
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await waitFor(() => {
      expect(api.patchUser).toHaveBeenCalledWith('u_user', { expected_version: 1 }, expect.any(String));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const again = vi.mocked(api.listDepartments).mock.calls.length;
    await openEdit('张三');
    expect(vi.mocked(api.listDepartments).mock.calls.length).toBe(again + 1);
  });

  it('显式选择「无部门」提交 department_id:null', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    const select = await waitDepartmentSelect(dialog);
    await userEvent.selectOptions(select, copyUsers.noDepartmentOption);
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await waitFor(() => {
      expect(api.patchUser).toHaveBeenCalledWith(
        'u_user',
        {
          expected_version: 1,
          department_id: null,
        },
        expect.any(String),
      );
    });
  });

  it('切换角色提交 role（部长目标部门仍在用，服务端放行）', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    await waitDepartmentSelect(dialog);
    await userEvent.click(
      within(dialog).getByRole('radio', { name: copyProfile.roleMinister }),
    );
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await waitFor(() => {
      expect(api.patchUser).toHaveBeenCalledWith(
        'u_user',
        {
          expected_version: 1,
          role: 'minister',
        },
        expect.any(String),
      );
    });
  });

  it('原部门已停用：下拉以禁用项呈现原部门，保存省略部门字段', async () => {
    const behind = loginToken('admin');
    mockAdmin.patchUser(behind, 'u_chen', { expected_version: 1, department_id: 'd_empty' });
    mockAdmin.deactivateDepartment(behind, 'd_empty', 1, 'idem-seed-deactivate');
    const { api } = await renderModule(adminUser());
    await screen.findByText('陈晨');
    const dialog = await openEdit('陈晨');
    const select = await waitDepartmentSelect(dialog);
    const inactiveOption = within(dialog).getByRole('option', {
      name: copyUsers.departmentInactiveOption('空壳部'),
    });
    expect(inactiveOption).toBeDisabled();
    expect(select).toHaveValue(inactiveOption.getAttribute('value'));
    await userEvent.click(within(dialog).getByRole('radio', { name: copyProfile.roleOps }));
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await waitFor(() => {
      expect(api.patchUser).toHaveBeenCalledWith(
        'u_chen',
        {
          expected_version: 2,
          role: 'ops',
        },
        expect.any(String),
      );
    });
  });

  it('部长 + 无部门：服务端 422，部门框 aria-invalid + 框下说明', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('赵六');
    const dialog = await openEdit('赵六');
    await waitDepartmentSelect(dialog);
    await userEvent.click(
      within(dialog).getByRole('radio', { name: copyProfile.roleMinister }),
    );
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await within(dialog).findByText(copyUsers.ministerDepartmentRequired);
    expect(within(dialog).getByRole('combobox')).toHaveAttribute('aria-invalid', 'true');
    expect(api.patchUser).toHaveBeenCalledWith(
      'u_zhao',
      {
        expected_version: 1,
        role: 'minister',
      },
      expect.any(String),
    );
  });

  it('新选部门在编辑期间被停用：409 提示目录已更新并恢复原部门选择', async () => {
    await renderModule(adminUser());
    const behind = loginToken('admin');
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    const select = await waitDepartmentSelect(dialog);
    await userEvent.selectOptions(select, 'd_empty');
    mockAdmin.deactivateDepartment(behind, 'd_empty', 1, 'idem-mid-edit');
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await within(dialog).findByText(copyUsers.departmentChanged);
    await waitFor(() => {
      expect(within(dialog).getByRole('combobox')).toHaveValue('d_finance');
    });
    expect(within(dialog).queryByRole('option', { name: '空壳部' })).toBeNull();
  });

  it('目标部门不存在（404）：同一路径提示并恢复基线选择', async () => {
    await renderModule(adminUser(), {
      patchUser: () => Promise.reject(apiError(404, 'department_not_found')),
    });
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    const select = await waitDepartmentSelect(dialog);
    await userEvent.selectOptions(select, 'd_hr');
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await within(dialog).findByText(copyUsers.departmentChanged);
    await waitFor(() => {
      expect(within(dialog).getByRole('combobox')).toHaveValue('d_finance');
    });
  });

  it('版本冲突：刷新目标版本并提示，重新选择后携带新版本重试', async () => {
    const { api } = await renderModule(adminUser());
    const behind = loginToken('admin');
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    await waitDepartmentSelect(dialog);
    // 幕后同角色保存：版本 1 → 2（不改变可见字段）
    mockAdmin.patchUser(behind, 'u_user', { expected_version: 1, role: 'user' });
    await userEvent.click(within(dialog).getByRole('radio', { name: copyProfile.roleOps }));
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await within(dialog).findByText(copyUsers.versionConflict);
    // 冲突后角色选择已重置为最新行状态；重新选择再确认
    await userEvent.click(within(dialog).getByRole('radio', { name: copyProfile.roleOps }));
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await waitFor(() => {
      expect(api.patchUser).toHaveBeenLastCalledWith(
        'u_user',
        {
          expected_version: 2,
          role: 'ops',
        },
        expect.any(String),
      );
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('403 forbidden_target：关闭对话框并在列表顶部错误行提示', async () => {
    await renderModule(adminUser(), {
      patchUser: () => Promise.reject(apiError(403, 'forbidden_target')),
    });
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    await waitDepartmentSelect(dialog);
    await userEvent.click(within(dialog).getByRole('radio', { name: copyProfile.roleOps }));
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(copyUsers.forbiddenTarget);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('保存成功的行底 fog-white 闪现', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    await waitDepartmentSelect(dialog);
    await userEvent.click(within(dialog).getByRole('radio', { name: copyProfile.roleOps }));
    await userEvent.click(within(dialog).getByRole('button', { name: copyUsers.save }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(rowOf('张三').className).toContain('bg-fog-white');
  });

  it('编辑用户表单响应原生 submit 一次', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openEdit('张三');
    await waitDepartmentSelect(dialog);
    const form = dialog.querySelector('form');
    expect(form).not.toBeNull();

    fireEvent.submit(form!);

    await waitFor(() => expect(api.patchUser).toHaveBeenCalledTimes(1));
  });
});

describe('新增用户对话框', () => {
  it('ops 视角不渲染「运维」选项且默认普通用户；线下传达说明常驻', async () => {
    await renderModule(opsUser());
    await screen.findByText('张三');
    const dialog = await openCreate();
    expect(within(dialog).getByRole('radio', { name: copyProfile.roleUser })).toBeChecked();
    expect(within(dialog).queryByRole('radio', { name: copyProfile.roleOps })).toBeNull();
    expect(within(dialog).getByText(copyUsers.passwordOfflineNote)).toBeInTheDocument();
  });

  it('弱密码前端拦截；眼睛控件切换明文显示', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colUsername), 'newbie');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colRealName), '小新');
    const password = within(dialog).getByLabelText(copyUsers.passwordLabel);
    await userEvent.type(password, 'abc');
    await userEvent.click(within(dialog).getByRole('button', { name: copyControls.confirm }));
    await within(dialog).findByText(copyUsers.passwordInvalid);
    expect(password).toHaveAttribute('aria-invalid', 'true');
    expect(api.createUser).not.toHaveBeenCalled();
    const eye = within(dialog).getByRole('button', { name: copyLogin.showPassword });
    expect(eye).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(eye);
    expect(password).toHaveAttribute('type', 'text');
    expect(eye).toHaveAttribute('aria-pressed', 'true');
  });

  it('用户名重名（409）：用户名框下说明且表单内容保留', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colUsername), 'zhangsan');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colRealName), '另一个张三');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.passwordLabel), 'passw0rd1');
    await userEvent.click(within(dialog).getByRole('button', { name: copyControls.confirm }));
    await within(dialog).findByText(copyUsers.usernameExists);
    expect(within(dialog).getByLabelText(copyUsers.passwordLabel)).toHaveValue('passw0rd1');
  });

  it('创建成功：新行自列表顶部插入并带进入动画', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colUsername), 'newbie');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colRealName), '小新');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.passwordLabel), 'passw0rd1');
    await userEvent.click(within(dialog).getByRole('button', { name: copyControls.confirm }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.createUser).toHaveBeenCalledWith(
      {
        username: 'newbie',
        real_name: '小新',
        department_id: null,
        role: 'user',
        initial_password: 'passw0rd1',
      },
      expect.any(String),
    );
    await screen.findByText('小新');
    expect(rowOf('小新').className).toContain('ui-row-insert');
  });

  it('有效新增用户表单按 Enter 提交一次', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colUsername), 'enter-user');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colRealName), '回车用户');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.passwordLabel), 'passw0rd1');

    await userEvent.keyboard('{Enter}');

    await waitFor(() => expect(api.createUser).toHaveBeenCalledTimes(1));
  });

  it('首页满页时创建用户重载首页，且不会在下一页重复', async () => {
    const token = loginToken('admin');
    const source = mockAdmin.listUsers(token, { page: 1, pageSize: 20 }).items[0]!;
    const firstPage = Array.from({ length: 20 }, (_, index) => ({
      ...source,
      id: `full-page-user-${index}`,
      username: `full-page-${index}`,
      real_name: `满页用户 ${index + 1}`,
    }));
    const created = { ...source, id: 'created-user', username: 'newbie', real_name: '小新' };
    const secondPage = { ...source, id: 'second-page-user', username: 'second-page', real_name: '第二页用户' };
    let createdOnServer = false;
    const listUsers = vi.fn(async (query: AdminUserListQuery) => ({
      items:
        query.page === 2
          ? [secondPage]
          : createdOnServer
            ? [created, ...firstPage.slice(0, 19)]
            : firstPage,
      total: 21,
      page: query.page ?? 1,
      page_size: query.pageSize ?? 20,
    }));
    const createUser = vi.fn(async () => {
      createdOnServer = true;
      return created;
    });
    const { api } = await renderModule(adminUser(), { listUsers, createUser });
    await screen.findByText('满页用户 1');
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colUsername), 'newbie');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.colRealName), '小新');
    await userEvent.type(within(dialog).getByLabelText(copyUsers.passwordLabel), 'passw0rd1');
    await userEvent.click(within(dialog).getByRole('button', { name: copyControls.confirm }));

    await waitFor(() => expect(vi.mocked(api.listUsers)).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('小新')).toBeInTheDocument();
    expect(
      within(screen.getByRole('table', { name: copy.shell.drawer.modules.usersOps })).getAllByRole('row'),
    ).toHaveLength(21);

    await userEvent.click(screen.getByRole('button', { name: copyControls.paginatorNext }));
    expect(await screen.findByText('第二页用户')).toBeInTheDocument();
    expect(screen.queryByText('小新')).toBeNull();
  });
});

describe('永久禁用二次确认', () => {
  it('展示固定三点说明；确认后行内原地冻结且不再渲染操作', async () => {
    const { api } = await renderModule(adminUser());
    await screen.findByText('张三');
    const dialog = await openDisable('张三');
    expect(within(dialog).getByText(copyUsers.disablePoint1)).toBeInTheDocument();
    expect(within(dialog).getByText(copyUsers.disablePoint2)).toBeInTheDocument();
    expect(within(dialog).getByText(copyUsers.disablePoint3)).toBeInTheDocument();
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyUsers.disableConfirm }),
    );
    await waitFor(() => {
      expect(api.deleteUser).toHaveBeenCalledWith('u_user', 1, expect.any(String));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const row = rowOf('张三');
    expect(within(row).getByText(copyCommon.frozenTag)).toBeInTheDocument();
    expect(within(row).queryByRole('button')).toBeNull();
    expect(row.className).toContain('bg-fog-white');
  });

  it('版本冲突：刷新目标版本并框内提示，再次确认携带新版本', async () => {
    const { api } = await renderModule(adminUser());
    const behind = loginToken('admin');
    await screen.findByText('张三');
    const dialog = await openDisable('张三');
    mockAdmin.patchUser(behind, 'u_user', { expected_version: 1, role: 'user' });
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyUsers.disableConfirm }),
    );
    await within(dialog).findByText(copyUsers.versionConflict);
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyUsers.disableConfirm }),
    );
    await waitFor(() => {
      expect(api.deleteUser).toHaveBeenLastCalledWith('u_user', 2, expect.any(String));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('目标已处于删除流程（409）：关框、列表错误行提示并重新拉取', async () => {
    const { api } = await renderModule(adminUser(), {
      deleteUser: () => Promise.reject(apiError(409, 'user_pending_delete')),
    });
    await screen.findByText('张三');
    const callsBefore = vi.mocked(api.listUsers).mock.calls.length;
    const dialog = await openDisable('张三');
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyUsers.disableConfirm }),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(copyUsers.userPendingDelete);
    await waitFor(() => {
      expect(vi.mocked(api.listUsers).mock.calls.length).toBeGreaterThan(callsBefore);
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('403 forbidden_target：关框并在列表错误行提示', async () => {
    await renderModule(adminUser(), {
      deleteUser: () => Promise.reject(apiError(403, 'forbidden_target')),
    });
    await screen.findByText('张三');
    const dialog = await openDisable('张三');
    await userEvent.click(
      within(dialog).getByRole('button', { name: copyUsers.disableConfirm }),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(copyUsers.forbiddenTarget);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});

describe('ops 视图边界、权限矩阵与下钻', () => {
  it('ops 视图：无部门管理入口与权限矩阵，也不发起矩阵请求', async () => {
    const { api } = await renderModule(opsUser());
    await screen.findByText('张三');
    expect(screen.queryByRole('button', { name: copyUsers.departments })).toBeNull();
    expect(screen.queryByText(copyUsers.matrixTitle)).toBeNull();
    expect(api.getPermissionMatrix).not.toHaveBeenCalled();
  });

  it('admin 视图：只读矩阵（四角色列、能力行、无按钮、行无悬停样式）', async () => {
    await renderModule(adminUser());
    const region = await screen.findByRole('region', { name: copyUsers.matrixTitle });
    await within(region).findByText('用户账号管理');
    for (const label of [
      copyProfile.roleUser,
      copyProfile.roleMinister,
      copyProfile.roleOps,
      copyProfile.roleAdmin,
    ]) {
      expect(within(region).getByText(label)).toBeInTheDocument();
    }
    expect(within(region).getAllByText('✓').length).toBeGreaterThan(0);
    expect(within(region).getAllByText('—').length).toBeGreaterThan(0);
    expect(within(region).queryByRole('button')).toBeNull();
    expect(within(region).getByText(copyUsers.matrixNote)).toBeInTheDocument();
    for (const tr of Array.from(region.querySelectorAll('tr'))) {
      expect(tr.className).not.toContain('hover');
    }
  });

  it('admin 视图：渲染「部门管理」整行下钻入口', async () => {
    await renderModule(adminUser());
    await screen.findByText('张三');
    expect(
      screen.getByRole('button', { name: copyUsers.departments }),
    ).toHaveAttribute('data-drill-row', 'departments');
  });

  it('深链回落：ops 访问部门层停在最深已注册层，admin 精确命中两层', () => {
    const registry = createDrawerRegistry();
    const opsResolved = registry.resolve('admin', ['users', 'departments'], 'ops');
    expect(opsResolved.exact).toBe(false);
    expect(opsResolved.layers.map((layer) => layer.id)).toEqual(['users']);
    const adminResolved = registry.resolve('admin', ['users', 'departments'], 'admin');
    expect(adminResolved.exact).toBe(true);
    expect(adminResolved.layers.map((layer) => layer.id)).toEqual(['users', 'departments']);
  });
});
