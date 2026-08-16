/*
 * 用户管理 / 人员与权限（§12.1–12.7；验收 A4、A45–A54、A61）。
 * - 骨架两端同构，差异仅操作可用性：ops 视图 = 用户管理单区块（无部门管理入口、无权限矩阵、
 *   不调用部门写接口）；admin 视图 = 用户管理 →「部门管理」整行下钻项 → 权限矩阵，间距 32px。
 * - 工具行：UserSearchBox 聚合搜索（300ms 防抖传 q，五字段聚合匹配在服务端）+ 部门 / 角色
 *   筛选下拉（ghost pill 触发，浮层 280px / max-h 360 内部滚动，可与搜索叠加）+ filled pill
 *   「新增用户」（高 36）；筛选变更重新拉取并回到第一页。
 * - 用户表：姓名 / 用户名 / 部门（无部门「—」smoke）/ 角色 / 最近活跃 / 操作；行高 56、发丝线、
 *   hover mist-gray；默认含 active 与 pending_delete（只读），无 deleted 墓碑；分页 Paginator。
 * - 操作可见性按 §12 规则表推导（冻结 / 自己 / admin 目标 / ops 视角其他 ops 行均不渲染任何
 *   操作；不做禁用态），后端 403 兜底；渲染规则见 canManageUser。
 * - 编辑对话框（400px）：姓名只读 17px → 角色单选（admin 三选 / ops 两选，选中行左侧 6px 墨点）
 *   → 部门下拉含「无部门」（每次打开重拉 active 目录，不长期缓存）；expected_version 必带，
 *   未改部门省略 department_id，仅显式「无部门」提交 null；原部门已停用 → 下拉只读禁用项呈现、
 *   不静默改写；422 minister_department_required 部门框红边 + 框下说明；409 department_inactive /
 *   404 department_not_found 顶部 danger 行 + 重拉目录；409 version_conflict 刷新目标行后框内
 *   提示重新确认；403 forbidden_target / cannot_modify_self 关框走列表错误行；保存成功行底
 *   fog-white 闪现 400ms（会话撤销说明常驻框内）。
 * - 新增对话框：用户名 / 姓名 / 显示名（可空缺省同姓名）/ 部门下拉 / 角色单选（默认普通用户，
 *   ops 无「运维」选项）/ 初始密码（掩码 + 登录页眼睛控件，至少 8 位字母数字混合，线下传达）；
 *   409 username_exists 用户名框下说明；成功新行自列表顶部插入（opacity 0→1 + 下移 8px→0）。
 * - 永久禁用：二次确认固定三点说明；DELETE {expected_version} 202 后不移除行，原地冻结展示
 *   （fog-white 闪现 400ms）。冻结行完全只读：角色列右侧「已冻结，待清理」tag、最近活跃列改显
 *   「将于 YYYY-MM-DD 清理」，不提供恢复 / 撤销 / 归档下载 / 清理进度任何入口。
 * - 权限矩阵（§12.7，仅 admin 第三区块）：行 = 能力项、列 = 四角色；✓ ink / — smoke；无编辑
 *   控件、无悬停交互；下方固定说明行；三态（骨架 / 空 / 错误行 + 重试）。
 */

import * as Popover from '@radix-ui/react-popover';
import { Check, ChevronRight } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { ApiError } from '../api/errors';
import { useAuthState } from '../auth/AuthProvider';
import type { Role, User } from '../auth/types';
import { copy } from '../copy';
import { EyeIcon, EyeOffIcon } from '../pages/login/LoginPage';
import { formatDrawerLocation } from '../router/drawer-params';
import {
  Chip,
  EmptyState,
  ErrorState,
  LoadingRows,
  Paginator,
  Pill,
  TextLink,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { DialogFrame } from './dialog-frame';
import { formatDate, formatDateTime } from './format';
import { useAdminRead } from './use-admin-read';
import { UserSearchBox } from './UserSearchBox';
import type {
  AdminDepartmentItem,
  AdminUserItem,
  AdminUserPatchInput,
} from './types';

const PAGE_SIZE = 20;
/** 实时过滤防抖（与知识空间个人库同口径）。 */
const SEARCH_DEBOUNCE_MS = 300;
/** 行底 fog-white 闪现时长（--duration-slow = 400ms）。 */
const FLASH_MS = 400;
/** 新行插入动画时长（--duration-base = 250ms，略留余量后清理 class）。 */
const ENTER_MS = 300;

// 单行字面量：Tailwind 按源码原文扫描生成 CSS，多行拼接不会被识别（列塌陷，行内容堆叠遮挡）
const USER_GRID = 'grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_minmax(0,1.2fr)_minmax(0,1.2fr)_auto]';

/** 角色中文标签：用户列表 / 筛选 / 单选统一取 copy.settings.profile 角色常量。 */
function roleLabel(role: Role): string {
  const profile = copy.settings.profile;
  switch (role) {
    case 'minister':
      return profile.roleMinister;
    case 'ops':
      return profile.roleOps;
    case 'admin':
      return profile.roleAdmin;
    default:
      return profile.roleUser;
  }
}

/**
 * 操作可见性规则（契约 §12 规则表；前端只过滤入口，后端 403 兜底）：
 * 冻结（pending_delete）/ 自己 / admin 目标一律无操作；ops 视角下其他 ops 行同样无操作。
 */
function canManageUser(actor: User, target: AdminUserItem): boolean {
  if (target.lifecycle_status !== 'active') {
    return false;
  }
  if (target.role === 'admin') {
    return false;
  }
  if (target.id === actor.id) {
    return false;
  }
  if (actor.role === 'ops' && target.role === 'ops') {
    return false;
  }
  return actor.role === 'admin' || actor.role === 'ops';
}

/** 操作者可分配的角色集（admin：普通用户 / 部长 / 运维；ops：普通用户 / 部长）。 */
function assignableRoles(actorRole: Role): readonly Role[] {
  return actorRole === 'admin' ? ['user', 'minister', 'ops'] : ['user', 'minister'];
}

/** 部门下拉选择态：keep = 未改动（提交省略）；none = 显式「无部门」（提交 null）。 */
type DepartmentSelection =
  | { readonly kind: 'keep' }
  | { readonly kind: 'none' }
  | { readonly kind: 'id'; readonly id: string };

const SELECTION_KEEP: DepartmentSelection = { kind: 'keep' };
const SELECTION_NONE: DepartmentSelection = { kind: 'none' };
/** select 的「无部门」与原部门已停用只读项取值（不会与部门 id 冲突的保留字）。 */
const VALUE_NONE = '__none__';
const VALUE_INACTIVE_CURRENT = '__inactive_current__';

/* ---------- 筛选下拉（ghost pill 触发 + 280px 浮层） ---------- */

interface FilterOption {
  readonly value: string;
  readonly label: string;
}

interface FilterChipProps {
  readonly ariaLabel: string;
  readonly allLabel: string;
  readonly options: readonly FilterOption[];
  readonly value: string | null;
  readonly onChange: (value: string | null) => void;
}

function FilterChip({ ariaLabel, allLabel, options, value, onChange }: FilterChipProps) {
  const [open, setOpen] = useState(false);
  const currentLabel = options.find((option) => option.value === value)?.label ?? allLabel;
  const choose = (next: string | null) => {
    onChange(next);
    setOpen(false);
  };
  const rowClass =
    'flex h-9 w-full items-center justify-between rounded-[var(--radius-images)] px-3 ' +
    'text-left text-[15px] text-ink-black transition-colors ' +
    'duration-[var(--duration-fast)] hover:bg-mist-gray';
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Chip open={open} nonDefault={value !== null} aria-label={ariaLabel}>
          {currentLabel}
        </Chip>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="bottom"
          sideOffset={8}
          align="start"
          className={
            'ui-menu-content max-h-[360px] w-[280px] overflow-y-auto ' +
            'rounded-[var(--radius-elevatedcards)] bg-paper-white p-1 shadow-[var(--shadow-subtle)]'
          }
        >
          <div role="radiogroup" aria-label={ariaLabel}>
            <button
              type="button"
              role="radio"
              aria-checked={value === null}
              onClick={() => choose(null)}
              className={rowClass}
            >
              <span className="truncate">{allLabel}</span>
              {value === null && <Check aria-hidden="true" className="ml-2 h-4 w-4 shrink-0" />}
            </button>
            {options.map((option) => (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={value === option.value}
                onClick={() => choose(option.value)}
                className={rowClass}
              >
                <span className="truncate">{option.label}</span>
                {value === option.value && (
                  <Check aria-hidden="true" className="ml-2 h-4 w-4 shrink-0" />
                )}
              </button>
            ))}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/* ---------- 角色单选列表（选中行左侧 6px 墨色实心圆点） ---------- */

function RoleRadioGroup({
  roles,
  value,
  onChange,
}: {
  readonly roles: readonly Role[];
  readonly value: Role;
  readonly onChange: (role: Role) => void;
}) {
  return (
    <div role="radiogroup" aria-label={copy.admin.users.colRole} className="flex flex-col">
      {roles.map((role) => {
        const selected = role === value;
        return (
          <button
            key={role}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(role)}
            className={
              'flex h-9 items-center gap-2 rounded-[var(--radius-images)] px-2 text-left ' +
              'transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray'
            }
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${selected ? 'bg-ink-black' : ''}`}
            />
            <span className="text-[15px] text-ink-black">{roleLabel(role)}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ---------- 用户管理模块 ---------- */

export function UsersModule() {
  const { api } = useAdmin();
  const { user } = useAuthState();
  const isAdmin = user?.role === 'admin';
  const navigate = useNavigate();
  const copyUsers = copy.admin.users;

  const [searchValue, setSearchValue] = useState('');
  const [query, setQuery] = useState('');
  const [departmentFilter, setDepartmentFilter] = useState<string | null>(null);
  const [roleFilter, setRoleFilter] = useState<Role | null>(null);
  const [page, setPage] = useState(1);

  const [items, setItems] = useState<readonly AdminUserItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  /** 列表错误行统一通道（403 forbidden_target / cannot_modify_self、user_pending_delete）。 */
  const [actionError, setActionError] = useState<string | null>(null);
  const [flashIds, setFlashIds] = useState<ReadonlySet<string>>(new Set());
  const [enterIds, setEnterIds] = useState<ReadonlySet<string>>(new Set());
  const seqRef = useRef(0);

  const [editing, setEditing] = useState<AdminUserItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [disabling, setDisabling] = useState<AdminUserItem | null>(null);
  const [disableNote, setDisableNote] = useState<string | null>(null);
  const [disableError, setDisableError] = useState<string | null>(null);
  const [confirmingDisable, setConfirmingDisable] = useState(false);

  /** 部门筛选下拉的目录（active）：模块挂载时加载一次；对话框打开的目录由对话框各自重拉。 */
  const [directory, setDirectory] = useState<readonly FilterOption[]>([]);
  useEffect(() => {
    let cancelled = false;
    void api.listDepartments('active').then(
      (response) => {
        if (!cancelled) {
          setDirectory(response.items.map((item) => ({ value: item.id, label: item.name })));
        }
      },
      () => {
        // 筛选目录失败不阻断列表：筛选下拉仅「全部部门」可选，深链 / 表单仍各自重拉。
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api]);

  // 实时过滤：前端只防抖传 q（聚合匹配姓名 / 显示名 / 用户名 / 部门名 / 角色名在服务端）
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQuery(searchValue.trim());
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchValue]);

  /** 读序列：generation fence；成功返回最新行集（供 409 后定位刷新行），失败 / 过期返回 null。 */
  const loadUsers = useCallback(async (): Promise<readonly AdminUserItem[] | null> => {
    const seq = ++seqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.listUsers({
        q: query === '' ? undefined : query,
        departmentId: departmentFilter ?? undefined,
        role: roleFilter ?? undefined,
        page,
        pageSize: PAGE_SIZE,
      });
      if (seq !== seqRef.current) {
        return null;
      }
      setItems(response.items);
      setTotal(response.total);
      return response.items;
    } catch {
      if (seq === seqRef.current) {
        setLoadError(true);
      }
      return null;
    } finally {
      if (seq === seqRef.current) {
        setLoading(false);
      }
    }
  }, [api, query, departmentFilter, roleFilter, page]);

  useEffect(() => {
    void loadUsers();
  }, [loadUsers]);

  function flashRow(userId: string): void {
    setFlashIds((current) => new Set(current).add(userId));
    window.setTimeout(() => {
      setFlashIds((current) => {
        const next = new Set(current);
        next.delete(userId);
        return next;
      });
    }, FLASH_MS);
  }

  function markEnter(userId: string): void {
    setEnterIds((current) => new Set(current).add(userId));
    window.setTimeout(() => {
      setEnterIds((current) => {
        const next = new Set(current);
        next.delete(userId);
        return next;
      });
    }, ENTER_MS);
  }

  /** 409 version_conflict：重拉列表刷新目标行，返回最新行（不在当前结果内返回 null）。 */
  const refreshUserRow = useCallback(
    async (userId: string): Promise<AdminUserItem | null> => {
      const freshItems = await loadUsers();
      return freshItems?.find((item) => item.id === userId) ?? null;
    },
    [loadUsers],
  );

  function applyUserUpdate(updated: AdminUserItem): void {
    setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    flashRow(updated.id);
  }

  function handleSaved(updated: AdminUserItem): void {
    setEditing(null);
    applyUserUpdate(updated);
  }

  /** 新增成功：未满首页可前插；满页或筛选路径按当前筛选重拉首页。 */
  function handleCreated(created: AdminUserItem): void {
    setCreating(false);
    const fitsFilters =
      (roleFilter === null || created.role === roleFilter) &&
      (departmentFilter === null || created.department?.id === departmentFilter);
    if (page === 1 && query === '' && fitsFilters && items.length < PAGE_SIZE) {
      setItems((current) => [created, ...current]);
      setTotal((current) => current + 1);
      markEnter(created.id);
      return;
    }
    markEnter(created.id);
    if (page !== 1) {
      setPage(1);
    } else {
      void loadUsers();
    }
  }

  /** 403 / user_pending_delete 统一通道：关框 + 列表错误行；user_pending_delete 附带刷新。 */
  function handleAbort(message: string): void {
    setEditing(null);
    setCreating(false);
    setDisabling(null);
    setActionError(message);
  }

  async function confirmDisable(): Promise<void> {
    const target = disabling;
    if (target === null || confirmingDisable) {
      return;
    }
    setConfirmingDisable(true);
    setDisableNote(null);
    setDisableError(null);
    try {
      const result = await api.deleteUser(target.id, target.version);
      // 202：不移除该行，原地切换为冻结展示 + fog-white 闪现 400ms
      setDisabling(null);
      setItems((current) =>
        current.map((item) =>
          item.id === result.id
            ? {
                ...item,
                version: result.version,
                lifecycle_status: result.lifecycle_status,
                deletion_requested_at: result.deletion_requested_at,
                purge_after_at: result.purge_after_at,
              }
            : item,
        ),
      );
      flashRow(result.id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === 'version_conflict') {
        const fresh = await refreshUserRow(target.id);
        if (fresh !== null) {
          setDisabling(fresh);
          setDisableNote(copyUsers.versionConflict);
        } else {
          setDisabling(null);
        }
      } else if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'user_pending_delete'
      ) {
        setDisabling(null);
        setActionError(copyUsers.userPendingDelete);
        await loadUsers();
      } else if (
        error instanceof ApiError &&
        error.status === 403 &&
        (error.code === 'forbidden_target' || error.code === 'cannot_modify_self')
      ) {
        handleAbort(
          error.code === 'forbidden_target'
            ? copyUsers.forbiddenTarget
            : copyUsers.cannotModifySelf,
        );
      } else {
        setDisableError(copyUsers.actionError);
      }
    } finally {
      setConfirmingDisable(false);
    }
  }

  function clearFilters(): void {
    setSearchValue('');
    setQuery('');
    setDepartmentFilter(null);
    setRoleFilter(null);
    setPage(1);
  }

  const roleOptions: readonly FilterOption[] = (['user', 'minister', 'ops', 'admin'] as const).map(
    (role) => ({ value: role, label: roleLabel(role) }),
  );
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const usersSection = (
    <section aria-label={copy.shell.drawer.modules.usersOps} className="flex flex-col gap-3">
      <h2 className="text-[20px] font-medium text-ink-black">
        {copy.shell.drawer.modules.usersOps}
      </h2>
      <div className="flex items-center gap-3">
        <UserSearchBox value={searchValue} onChange={setSearchValue} />
        <FilterChip
          ariaLabel={copyUsers.departmentFilter}
          allLabel={copyUsers.allDepartments}
          options={directory}
          value={departmentFilter}
          onChange={(value) => {
            setDepartmentFilter(value);
            setPage(1);
          }}
        />
        <FilterChip
          ariaLabel={copyUsers.roleFilter}
          allLabel={copyUsers.allRoles}
          options={roleOptions}
          value={roleFilter}
          onChange={(value) => {
            setRoleFilter(value as Role | null);
            setPage(1);
          }}
        />
        <Pill className="ml-auto" onClick={() => setCreating(true)}>
          {copyUsers.addUser}
        </Pill>
      </div>
      {actionError !== null && (
        <p role="alert" className="text-[15px] text-danger">
          {actionError}
        </p>
      )}
      {loading ? (
        <LoadingRows count={5} />
      ) : loadError ? (
        <ErrorState text={copyUsers.loadError} onRetry={() => void loadUsers()} />
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-10">
          <p className="text-[15px] text-smoke-gray">{copyUsers.empty}</p>
          <TextLink onClick={clearFilters}>{copyUsers.clearFilters}</TextLink>
        </div>
      ) : (
        <div role="table" aria-label={copy.shell.drawer.modules.usersOps}>
          <div
            role="row"
            className={`grid ${USER_GRID} items-center gap-3 px-4 pb-1 text-[14px] text-ash-gray`}
          >
            <span role="columnheader">{copyUsers.colRealName}</span>
            <span role="columnheader">{copyUsers.colUsername}</span>
            <span role="columnheader">{copyUsers.colDepartment}</span>
            <span role="columnheader">{copyUsers.colRole}</span>
            <span role="columnheader">{copyUsers.colLastActive}</span>
            <span role="columnheader">{copyUsers.colActions}</span>
          </div>
          <ul role="rowgroup" className="divide-y divide-[var(--color-hairline)]">
            {items.map((item) => {
              const frozen = item.lifecycle_status === 'pending_delete';
              const manageable = user !== null && canManageUser(user, item);
              return (
                <li
                  key={item.id}
                  role="row"
                  className={
                    `${enterIds.has(item.id) ? 'ui-row-insert ' : ''}` +
                    `transition-colors duration-[var(--duration-slow)] ` +
                    `${flashIds.has(item.id) ? 'bg-fog-white' : ''}`
                  }
                >
                  <div
                    className={
                      `grid h-14 ${USER_GRID} items-center gap-3 px-4 ` +
                      'transition-colors duration-150 hover:bg-mist-gray'
                    }
                  >
                    <span role="cell" className="truncate text-[15px] font-medium text-ink-black">
                      {item.real_name}
                    </span>
                    <span role="cell" className="truncate text-[15px] text-slate-gray">{item.username}</span>
                    <span
                      role="cell"
                      className={
                        'truncate text-[15px] ' +
                        (item.department === null ? 'text-smoke-gray' : 'text-slate-gray')
                      }
                    >
                      {item.department?.name ?? copyUsers.noDepartment}
                    </span>
                    <span role="cell" className="flex min-w-0 items-center gap-2 text-[15px] text-slate-gray">
                      <span className="truncate">{roleLabel(item.role)}</span>
                      {frozen && (
                        <span className="shrink-0 text-[14px] text-ash-gray">
                          {copy.admin.common.frozenTag}
                        </span>
                      )}
                    </span>
                    <span role="cell" className="truncate text-[15px] text-slate-gray">
                      {frozen
                        ? item.purge_after_at !== null
                          ? copyUsers.purgeAfter(formatDate(item.purge_after_at))
                          : copyUsers.noDepartment
                        : item.last_active_at !== null
                          ? formatDateTime(item.last_active_at)
                          : copyUsers.noDepartment}
                    </span>
                    <span role="cell" className="flex items-center gap-3">
                      {manageable && (
                        <>
                          <TextLink ink onClick={() => setEditing(item)}>
                            {copyUsers.edit}
                          </TextLink>
                          <TextLink danger onClick={() => setDisabling(item)}>
                            {copyUsers.disable}
                          </TextLink>
                        </>
                      )}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {!loading && !loadError && totalPages > 1 && (
        <Paginator page={page} totalPages={totalPages} onChange={setPage} />
      )}
    </section>
  );

  return (
    <>
      {isAdmin ? (
        <div className="flex flex-col gap-8">
          {usersSection}
          {/* 「部门管理」整行下钻项（仅 admin；有 render 的层 DrawerHost 不自动列出 children） */}
          <button
            type="button"
            data-drill-row="departments"
            onClick={() =>
              navigate(
                formatDrawerLocation({
                  open: true,
                  segment: 'admin',
                  drill: ['users', 'departments'],
                }),
              )
            }
            className={
              'flex h-12 w-full items-center justify-between rounded-[var(--radius-images)] px-3 ' +
              'text-left transition-colors duration-150 hover:bg-mist-gray'
            }
          >
            <span className="text-body text-ink-black">{copyUsers.departments}</span>
            <ChevronRight size={16} className="text-slate-gray" aria-hidden />
          </button>
          <PermissionMatrixSection />
        </div>
      ) : (
        usersSection
      )}

      {editing !== null && user !== null && (
        <EditUserDialog
          actorRole={user.role}
          target={editing}
          onClose={() => setEditing(null)}
          onSaved={handleSaved}
          onRefreshTarget={refreshUserRow}
          onAbort={handleAbort}
        />
      )}
      {creating && user !== null && (
        <CreateUserDialog
          actorRole={user.role}
          onClose={() => setCreating(false)}
          onCreated={handleCreated}
          onAbort={handleAbort}
        />
      )}
      {disabling !== null && (
        <DisableUserDialog
          target={disabling}
          note={disableNote}
          error={disableError}
          confirming={confirmingDisable}
          onClose={() => {
            setDisabling(null);
            setDisableNote(null);
            setDisableError(null);
          }}
          onConfirm={() => void confirmDisable()}
        />
      )}
    </>
  );
}

/* ---------- 编辑用户对话框 ---------- */

interface EditUserDialogProps {
  readonly actorRole: Role;
  readonly target: AdminUserItem;
  readonly onClose: () => void;
  readonly onSaved: (updated: AdminUserItem) => void;
  readonly onRefreshTarget: (userId: string) => Promise<AdminUserItem | null>;
  readonly onAbort: (message: string) => void;
}

function EditUserDialog({
  actorRole,
  target: initialTarget,
  onClose,
  onSaved,
  onRefreshTarget,
  onAbort,
}: EditUserDialogProps) {
  const { api } = useAdmin();
  const copyUsers = copy.admin.users;
  const [target, setTarget] = useState(initialTarget);
  const [role, setRole] = useState<Role>(initialTarget.role);
  const [selection, setSelection] = useState<DepartmentSelection>(SELECTION_KEEP);
  /** active 目录：每次打开重拉（不做长期缓存）；null = 加载中。 */
  const [directory, setDirectory] = useState<readonly AdminDepartmentItem[] | null>(null);
  const [directoryError, setDirectoryError] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const dirSeqRef = useRef(0);

  const loadDirectory = useCallback(async (): Promise<void> => {
    const seq = ++dirSeqRef.current;
    setDirectoryError(false);
    try {
      const response = await api.listDepartments('active');
      if (seq === dirSeqRef.current) {
        setDirectory(response.items);
      }
    } catch {
      if (seq === dirSeqRef.current) {
        setDirectoryError(true);
      }
    }
  }, [api]);

  useEffect(() => {
    void loadDirectory();
  }, [loadDirectory]);

  const roles = assignableRoles(actorRole);
  const currentDepartment = target.department;
  /** 原部门已不在 active 目录（已停用）：下拉以只读禁用项呈现原部门，不静默改写。 */
  const currentMissing =
    currentDepartment !== null &&
    directory !== null &&
    !directory.some((item) => item.id === currentDepartment.id);
  const selectValue =
    selection.kind === 'none'
      ? VALUE_NONE
      : selection.kind === 'id'
        ? selection.id
        : currentMissing
          ? VALUE_INACTIVE_CURRENT
          : (currentDepartment?.id ?? VALUE_NONE);

  async function save(): Promise<void> {
    if (saving) {
      return;
    }
    setSaving(true);
    setError(null);
    setDepartmentError(null);
    const body: AdminUserPatchInput = {
      expected_version: target.version,
      ...(role !== target.role ? { role } : {}),
      ...(selection.kind === 'none'
        ? { department_id: null }
        : selection.kind === 'id'
          ? { department_id: selection.id }
          : {}),
    };
    try {
      const updated = await api.patchUser(target.id, body);
      onSaved(updated);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 422) {
        // minister_department_required：部门框边变 danger + 框下 15px 说明
        setDepartmentError(copyUsers.ministerDepartmentRequired);
      } else if (
        caught instanceof ApiError &&
        ((caught.status === 409 && caught.code === 'department_inactive') ||
          (caught.status === 404 && caught.code === 'department_not_found'))
      ) {
        // 顶部 danger 说明 + 重新请求 active 目录刷新选项；选择态回落「未改动」，
        // 避免残留指向已停用 / 已不存在部门的 id 导致下拉空值或再次误提交
        setError(copyUsers.departmentChanged);
        setSelection(SELECTION_KEEP);
        await loadDirectory();
      } else if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        caught.code === 'version_conflict'
      ) {
        const fresh = await onRefreshTarget(target.id);
        if (fresh !== null) {
          setTarget(fresh);
          setRole(fresh.role);
          setSelection(SELECTION_KEEP);
          setError(copyUsers.versionConflict);
        } else {
          onClose();
        }
      } else if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        caught.code === 'user_pending_delete'
      ) {
        await onRefreshTarget(target.id);
        onAbort(copyUsers.userPendingDelete);
      } else if (
        caught instanceof ApiError &&
        caught.status === 403 &&
        (caught.code === 'forbidden_target' || caught.code === 'cannot_modify_self')
      ) {
        onAbort(
          caught.code === 'forbidden_target'
            ? copyUsers.forbiddenTarget
            : copyUsers.cannotModifySelf,
        );
      } else {
        setError(copyUsers.actionError);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogFrame ariaLabel={copyUsers.editDialogTitle} onClose={onClose}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <h2 className="text-[20px] font-medium text-ink-black">{copyUsers.editDialogTitle}</h2>
      <p className="mt-3 text-[17px] text-ink-black">{target.real_name}</p>
      {error !== null && (
        <p role="alert" className="mt-3 text-[15px] text-danger">
          {error}
        </p>
      )}
      <div className="mt-3">
        <RoleRadioGroup roles={roles} value={role} onChange={setRole} />
      </div>
      <div className="mt-3">
        <label
          htmlFor="user-edit-department"
          className="mb-2 block text-[15px] text-slate-gray"
        >
          {copyUsers.colDepartment}
        </label>
        {directory === null && !directoryError ? (
          <select
            id="user-edit-department"
            disabled
            value="__loading__"
            aria-label={copyUsers.colDepartment}
            className={
              'h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] ' +
              'bg-paper-white px-3 text-[15px] text-ink-black'
            }
          >
            <option value="__loading__">
              {currentDepartment?.name ?? copyUsers.noDepartmentOption}
            </option>
          </select>
        ) : (
          <select
            id="user-edit-department"
            value={selectValue}
            aria-invalid={departmentError !== null}
            onChange={(event) => {
              const value = event.target.value;
              setDepartmentError(null);
              setSelection(value === VALUE_NONE ? SELECTION_NONE : { kind: 'id', id: value });
            }}
            className={
              'h-10 w-full rounded-[var(--radius-inputs)] border bg-paper-white px-3 text-[15px] ' +
              `text-ink-black focus:border-ink-black ${
                departmentError !== null ? 'border-danger' : 'border-[var(--color-hairline)]'
              }`
            }
          >
            {currentMissing && currentDepartment !== null && (
              <option value={VALUE_INACTIVE_CURRENT} disabled>
                {copyUsers.departmentInactiveOption(currentDepartment.name)}
              </option>
            )}
            <option value={VALUE_NONE}>{copyUsers.noDepartmentOption}</option>
            {(directory ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        )}
        {directoryError && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {copyUsers.directoryLoadError}{' '}
            <TextLink onClick={() => void loadDirectory()}>{copy.states.retry}</TextLink>
          </p>
        )}
        {departmentError !== null && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {departmentError}
          </p>
        )}
        <p className="mt-2 text-[15px] text-smoke-gray">{copyUsers.sessionRevokedNote}</p>
      </div>
        <div className="mt-6 flex justify-end gap-2">
          <Pill type="button" variant="ghost" size="sm" disabled={saving} onClick={onClose}>
            {copy.controls.cancel}
          </Pill>
          <Pill type="submit" size="sm" loading={saving} disabled={saving}>
            {copyUsers.save}
          </Pill>
        </div>
      </form>
    </DialogFrame>
  );
}

/* ---------- 新增用户对话框 ---------- */

interface CreateUserDialogProps {
  readonly actorRole: Role;
  readonly onClose: () => void;
  readonly onCreated: (created: AdminUserItem) => void;
  readonly onAbort: (message: string) => void;
}

/** 初始密码规则（同修改密码：至少 8 位、字母数字混合）。 */
function passwordValid(password: string): boolean {
  return /^(?=.*[A-Za-z])(?=.*\d).{8,}$/.test(password);
}

function CreateUserDialog({ actorRole, onClose, onCreated, onAbort }: CreateUserDialogProps) {
  const { api } = useAdmin();
  const copyUsers = copy.admin.users;
  const [username, setUsername] = useState('');
  const [realName, setRealName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [selection, setSelection] = useState<DepartmentSelection>(SELECTION_NONE);
  const [role, setRole] = useState<Role>('user');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [directory, setDirectory] = useState<readonly AdminDepartmentItem[] | null>(null);
  const [directoryError, setDirectoryError] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [realNameError, setRealNameError] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const dirSeqRef = useRef(0);

  const loadDirectory = useCallback(async (): Promise<void> => {
    const seq = ++dirSeqRef.current;
    setDirectoryError(false);
    try {
      const response = await api.listDepartments('active');
      if (seq === dirSeqRef.current) {
        setDirectory(response.items);
      }
    } catch {
      if (seq === dirSeqRef.current) {
        setDirectoryError(true);
      }
    }
  }, [api]);

  useEffect(() => {
    void loadDirectory();
  }, [loadDirectory]);

  const roles = assignableRoles(actorRole);
  const selectValue =
    selection.kind === 'none' ? VALUE_NONE : selection.kind === 'id' ? selection.id : VALUE_NONE;

  function inputClass(invalid: boolean): string {
    return (
      'h-10 w-full rounded-[var(--radius-inputs)] border bg-paper-white px-3 text-[15px] ' +
      `text-ink-black placeholder:text-smoke-gray focus:border-ink-black ${
        invalid ? 'border-danger' : 'border-[var(--color-hairline)]'
      }`
    );
  }

  async function save(): Promise<void> {
    if (saving) {
      return;
    }
    // 即时校验：必填缺失 / 密码规则不过 → 对应框边 danger + 框下 15px 说明
    const nextUsernameError = username.trim() === '' ? copyUsers.fieldRequired : null;
    const nextRealNameError = realName.trim() === '' ? copyUsers.fieldRequired : null;
    const nextPasswordError = !passwordValid(password) ? copyUsers.passwordInvalid : null;
    setUsernameError(nextUsernameError);
    setRealNameError(nextRealNameError);
    setPasswordError(nextPasswordError);
    setDepartmentError(null);
    setError(null);
    if (nextUsernameError !== null || nextRealNameError !== null || nextPasswordError !== null) {
      return;
    }
    setSaving(true);
    try {
      const created = await api.createUser({
        username: username.trim(),
        real_name: realName.trim(),
        ...(displayName.trim() === '' ? {} : { display_name: displayName.trim() }),
        department_id: selection.kind === 'id' ? selection.id : null,
        role,
        initial_password: password,
      });
      onCreated(created);
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.status === 409 &&
        caught.code === 'username_exists'
      ) {
        setUsernameError(copyUsers.usernameExists);
      } else if (caught instanceof ApiError && caught.status === 422) {
        setDepartmentError(copyUsers.ministerDepartmentRequired);
      } else if (
        caught instanceof ApiError &&
        ((caught.status === 409 && caught.code === 'department_inactive') ||
          (caught.status === 404 && caught.code === 'department_not_found'))
      ) {
        setError(copyUsers.departmentChanged);
        await loadDirectory();
      } else if (
        caught instanceof ApiError &&
        caught.status === 403 &&
        (caught.code === 'forbidden_target' || caught.code === 'cannot_modify_self')
      ) {
        onAbort(
          caught.code === 'forbidden_target'
            ? copyUsers.forbiddenTarget
            : copyUsers.cannotModifySelf,
        );
      } else {
        setError(copyUsers.actionError);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogFrame ariaLabel={copyUsers.addDialogTitle} onClose={onClose}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <h2 className="text-[20px] font-medium text-ink-black">{copyUsers.addDialogTitle}</h2>
      {error !== null && (
        <p role="alert" className="mt-3 text-[15px] text-danger">
          {error}
        </p>
      )}
      <div className="mt-4">
        <label htmlFor="user-create-username" className="mb-2 block text-[15px] text-slate-gray">
          {copyUsers.colUsername}
        </label>
        <input
          id="user-create-username"
          type="text"
          autoComplete="off"
          value={username}
          aria-invalid={usernameError !== null}
          onChange={(event) => {
            setUsername(event.target.value);
            setUsernameError(null);
          }}
          className={inputClass(usernameError !== null)}
        />
        {usernameError !== null && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {usernameError}
          </p>
        )}
      </div>
      <div className="mt-3">
        <label htmlFor="user-create-real-name" className="mb-2 block text-[15px] text-slate-gray">
          {copyUsers.colRealName}
        </label>
        <input
          id="user-create-real-name"
          type="text"
          autoComplete="off"
          value={realName}
          aria-invalid={realNameError !== null}
          onChange={(event) => {
            setRealName(event.target.value);
            setRealNameError(null);
          }}
          className={inputClass(realNameError !== null)}
        />
        {realNameError !== null && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {realNameError}
          </p>
        )}
      </div>
      <div className="mt-3">
        <label
          htmlFor="user-create-display-name"
          className="mb-2 block text-[15px] text-slate-gray"
        >
          {copyUsers.displayNameLabel}
        </label>
        <input
          id="user-create-display-name"
          type="text"
          autoComplete="off"
          value={displayName}
          placeholder={copyUsers.displayNamePlaceholder}
          onChange={(event) => setDisplayName(event.target.value)}
          className={inputClass(false)}
        />
      </div>
      <div className="mt-3">
        <label
          htmlFor="user-create-department"
          className="mb-2 block text-[15px] text-slate-gray"
        >
          {copyUsers.colDepartment}
        </label>
        {directory === null && !directoryError ? (
          <select
            id="user-create-department"
            disabled
            value={VALUE_NONE}
            aria-label={copyUsers.colDepartment}
            className={
              'h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] ' +
              'bg-paper-white px-3 text-[15px] text-ink-black'
            }
          >
            <option value={VALUE_NONE}>{copyUsers.noDepartmentOption}</option>
          </select>
        ) : (
          <select
            id="user-create-department"
            value={selectValue}
            aria-invalid={departmentError !== null}
            onChange={(event) => {
              const value = event.target.value;
              setDepartmentError(null);
              setSelection(value === VALUE_NONE ? SELECTION_NONE : { kind: 'id', id: value });
            }}
            className={
              'h-10 w-full rounded-[var(--radius-inputs)] border bg-paper-white px-3 text-[15px] ' +
              `text-ink-black focus:border-ink-black ${
                departmentError !== null ? 'border-danger' : 'border-[var(--color-hairline)]'
              }`
            }
          >
            <option value={VALUE_NONE}>{copyUsers.noDepartmentOption}</option>
            {(directory ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        )}
        {directoryError && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {copyUsers.directoryLoadError}{' '}
            <TextLink onClick={() => void loadDirectory()}>{copy.states.retry}</TextLink>
          </p>
        )}
        {departmentError !== null && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {departmentError}
          </p>
        )}
      </div>
      <div className="mt-3">
        <RoleRadioGroup roles={roles} value={role} onChange={setRole} />
      </div>
      <div className="mt-3">
        <label htmlFor="user-create-password" className="mb-2 block text-[15px] text-slate-gray">
          {copyUsers.passwordLabel}
        </label>
        <div
          className={
            'relative flex h-10 items-center rounded-[var(--radius-inputs)] border ' +
            `bg-paper-white px-3 ` +
            `${passwordError !== null ? 'border-danger' : 'border-[var(--color-hairline)]'} ` +
            'focus-within:border-ink-black'
          }
        >
          <input
            id="user-create-password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="new-password"
            value={password}
            aria-invalid={passwordError !== null}
            onChange={(event) => {
              setPassword(event.target.value);
              setPasswordError(null);
            }}
            className="w-full bg-transparent pr-6 text-[15px] text-ink-black"
          />
          <button
            type="button"
            aria-label={showPassword ? copy.login.hidePassword : copy.login.showPassword}
            aria-pressed={showPassword}
            onClick={() => setShowPassword((value) => !value)}
            className={
              'absolute top-1/2 right-3 -translate-y-1/2 text-slate-gray ' +
              'transition-colors duration-150 hover:text-ink-black'
            }
          >
            <span className="block h-4 w-4">{showPassword ? <EyeOffIcon /> : <EyeIcon />}</span>
          </button>
        </div>
        {passwordError !== null && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {passwordError}
          </p>
        )}
        <p className="mt-2 text-[15px] text-smoke-gray">{copyUsers.passwordOfflineNote}</p>
      </div>
        <div className="mt-6 flex justify-end gap-2">
          <Pill type="button" variant="ghost" size="sm" disabled={saving} onClick={onClose}>
            {copy.controls.cancel}
          </Pill>
          <Pill type="submit" size="sm" loading={saving} disabled={saving}>
            {copy.controls.confirm}
          </Pill>
        </div>
      </form>
    </DialogFrame>
  );
}

/* ---------- 永久禁用二次确认对话框 ---------- */

interface DisableUserDialogProps {
  readonly target: AdminUserItem;
  /** 409 version_conflict 刷新后框内提示重新确认。 */
  readonly note: string | null;
  readonly error: string | null;
  readonly confirming: boolean;
  readonly onClose: () => void;
  readonly onConfirm: () => void;
}

function DisableUserDialog({
  target,
  note,
  error,
  confirming,
  onClose,
  onConfirm,
}: DisableUserDialogProps) {
  const copyUsers = copy.admin.users;
  return (
    <DialogFrame ariaLabel={copyUsers.disableDialogTitle} onClose={onClose}>
      <h2 className="text-[20px] font-medium text-ink-black">{copyUsers.disableDialogTitle}</h2>
      <div className="mt-2 flex flex-col gap-2 text-[15px] text-slate-gray">
        <p>{copyUsers.disablePoint1}</p>
        <p>{copyUsers.disablePoint2}</p>
        <p>{copyUsers.disablePoint3}</p>
      </div>
      <p className="mt-3 text-[17px] text-ink-black">{target.real_name}</p>
      {note !== null && (
        <p role="status" className="mt-3 text-[15px] text-danger">
          {note}
        </p>
      )}
      {error !== null && (
        <p role="alert" className="mt-3 text-[15px] text-danger">
          {error}
        </p>
      )}
      <div className="mt-6 flex justify-end gap-2">
        <Pill variant="ghost" size="sm" disabled={confirming} onClick={onClose}>
          {copy.controls.cancel}
        </Pill>
        <Pill size="sm" danger loading={confirming} disabled={confirming} onClick={onConfirm}>
          {copyUsers.disableConfirm}
        </Pill>
      </div>
    </DialogFrame>
  );
}

/* ---------- 权限矩阵（§12.7，超管只读） ---------- */

const MATRIX_ROLES: readonly Role[] = ['user', 'minister', 'ops', 'admin'];

function PermissionMatrixSection() {
  const { api } = useAdmin();
  const read = useAdminRead(() => api.getPermissionMatrix(), [api]);
  const copyUsers = copy.admin.users;

  return (
    <section aria-label={copyUsers.matrixTitle} className="flex flex-col gap-3">
      <h2 className="text-[20px] font-medium text-ink-black">{copyUsers.matrixTitle}</h2>
      {read.loading && <LoadingRows count={3} />}
      {read.error && <ErrorState text={copyUsers.loadError} onRetry={read.reload} />}
      {!read.loading && !read.error && read.data !== null && (
        read.data.capabilities.length === 0 ? (
          <EmptyState text={copy.states.empty} />
        ) : (
          <>
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-[var(--color-hairline)] text-[14px] text-ash-gray">
                  <th className="py-1.5 font-normal"> </th>
                  {MATRIX_ROLES.map((role) => (
                    <th key={role} className="py-1.5 font-normal">
                      {roleLabel(role)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {read.data.capabilities.map((capability) => (
                  <tr key={capability.key} className="border-b border-[var(--color-hairline)]">
                    <td className="py-1.5 text-[15px] text-ink-black">{capability.label}</td>
                    {MATRIX_ROLES.map((role) => (
                      <td key={role} className="py-1.5 text-[15px]">
                        {capability.roles[role] ? (
                          <span className="text-ink-black">✓</span>
                        ) : (
                          <span className="text-smoke-gray">—</span>
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-[15px] text-slate-gray">{copyUsers.matrixNote}</p>
          </>
        )
      )}
    </section>
  );
}
