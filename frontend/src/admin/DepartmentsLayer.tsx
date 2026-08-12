/*
 * 部门管理（§12.5；验收 A5、A55–A59；仅 admin 下钻层，深链回落见 registry roles）。
 * - 工具行：状态筛选分段（在用 / 已停用 / 全部 ↔ active/inactive/all，默认在用）+ 右侧「刷新」
 *   TextLink（15px ink）+ filled pill「新增部门」（高 36）；接口未定义分页与搜索参数 →
 *   不提供分页器与搜索框。进入层、切换筛选、点击「刷新」按当前 status 重新请求。
 * - 部门表：部门名（一行截断）/ 状态（6px 点：在用 success / 已停用 slate + 15px 文字）/
 *   成员 / 文档 / 进行中任务 / 待审投稿（四列计数 15px slate，仅决策参考）/ 停用时间
 *   （YYYY-MM-DD；在用「—」）/ 操作；行高 56、发丝线、hover mist、表头 14px ash。
 * - 行操作唯一依据 = 该行 allowed_actions：rename →「改名」、deactivate →「停用」（danger）；
 *   空数组「—」；未知值不渲染；不按角色 / 状态 / 计数推导、补出或预先放行。
 * - 写操作提交体固定：新增 { name }（201）、改名 { expected_version, name }、停用
 *   { expected_version }；均经 createIdempotencyScope 携带 Idempotency-Key（每次确认新键；
 *   网络结果未知复用同键同体；已收业务错误不静默重发；409 idempotency_key_conflict 不换键）。
 * - 停用阻断系列：409 department_has_members / department_has_active_work → 框内 danger 说明 +
 *   刷新该行；409 version_conflict 刷新该行（version 以最新为准）后重新确认；503
 *   department_deactivation_unverified（details.retryable=true）→ 说明 + 重试入口保留（新提交
 *   新键）；404 department_not_found / 409 department_inactive → 关框刷新目录；被阻断后不出现
 *   「强制停用」替代入口；不做乐观标记，状态切换以 200 响应为准（成功行更新 + fog-white 闪现
 *   400ms）。
 * - 能力边界：仅创建 / 改名 / 停用三入口；不提供重新启用、物理删除、部门描述、成员批量迁移、
 *   部门合并或多部门归属任何入口。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { createIdempotencyScope, isBusinessResponse } from '../settings/idempotency';
import {
  EmptyState,
  ErrorState,
  LoadingRows,
  Pill,
  SegmentedControl,
  StatusDot,
  TextLink,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { DialogFrame } from './dialog-frame';
import { formatDate } from './format';
import type { AdminDepartmentItem, DepartmentAction, DepartmentStatusFilter } from './types';

/** 行底 fog-white 闪现时长（--duration-slow = 400ms）。 */
const FLASH_MS = 400;
/** 新行插入动画时长（--duration-base = 250ms，略留余量后清理 class）。 */
const ENTER_MS = 300;
/** 改名成功名称就地交叉淡变时长（--duration-fast = 150ms，略留余量后清理 class）。 */
const RENAME_FADE_MS = 200;

// 单行字面量：Tailwind 按源码原文扫描生成 CSS，多行拼接不会被识别（列塌陷，行内容堆叠遮挡）
const DEPARTMENT_GRID =
  'grid-cols-[minmax(0,1.3fr)_minmax(0,0.9fr)_minmax(0,0.5fr)_minmax(0,0.5fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,0.9fr)_auto]';

const KNOWN_ACTIONS: readonly DepartmentAction[] = ['rename', 'deactivate'];

/** 行操作唯一依据：allowed_actions 内的已知值；未知值不渲染。 */
function rowActions(department: AdminDepartmentItem): readonly DepartmentAction[] {
  return department.allowed_actions.filter((action) => KNOWN_ACTIONS.includes(action));
}

export function DepartmentsLayer() {
  const { api } = useAdmin();
  const copyDepartments = copy.admin.departments;
  const [filter, setFilter] = useState<DepartmentStatusFilter>('active');
  const [items, setItems] = useState<readonly AdminDepartmentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  /** 列表错误行统一通道（404 / 409 inactive 等状态变化）。 */
  const [actionError, setActionError] = useState<string | null>(null);
  const [flashIds, setFlashIds] = useState<ReadonlySet<string>>(new Set());
  const [enterIds, setEnterIds] = useState<ReadonlySet<string>>(new Set());
  const [renamedIds, setRenamedIds] = useState<ReadonlySet<string>>(new Set());
  const seqRef = useRef(0);
  const idem = useRef(createIdempotencyScope());

  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<AdminDepartmentItem | null>(null);
  const [deactivating, setDeactivating] = useState<AdminDepartmentItem | null>(null);
  const [dialogFieldError, setDialogFieldError] = useState<string | null>(null);
  const [dialogTopError, setDialogTopError] = useState<string | null>(null);
  const [deactivateNote, setDeactivateNote] = useState<string | null>(null);
  /** 503 department_deactivation_unverified：保留重试入口（错误行 + 重试 TextLink）。 */
  const [deactivateRetry, setDeactivateRetry] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  /** 读序列：generation fence；成功返回最新行集（供 409 后定位刷新行），失败 / 过期返回 null。 */
  const loadDepartments = useCallback(async (): Promise<readonly AdminDepartmentItem[] | null> => {
    const seq = ++seqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.listDepartments(filter);
      if (seq !== seqRef.current) {
        return null;
      }
      setItems(response.items);
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
  }, [api, filter]);

  useEffect(() => {
    void loadDepartments();
  }, [loadDepartments]);

  function flashRow(departmentId: string): void {
    setFlashIds((current) => new Set(current).add(departmentId));
    window.setTimeout(() => {
      setFlashIds((current) => {
        const next = new Set(current);
        next.delete(departmentId);
        return next;
      });
    }, FLASH_MS);
  }

  function markEnter(departmentId: string): void {
    setEnterIds((current) => new Set(current).add(departmentId));
    window.setTimeout(() => {
      setEnterIds((current) => {
        const next = new Set(current);
        next.delete(departmentId);
        return next;
      });
    }, ENTER_MS);
  }

  function markRenamed(departmentId: string): void {
    setRenamedIds((current) => new Set(current).add(departmentId));
    window.setTimeout(() => {
      setRenamedIds((current) => {
        const next = new Set(current);
        next.delete(departmentId);
        return next;
      });
    }, RENAME_FADE_MS);
  }

  function applyDepartmentUpdate(updated: AdminDepartmentItem): void {
    setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    flashRow(updated.id);
  }

  /** 新增（201）：提交体 { name }；当前筛选「已停用」先切回「在用」，其余新行自列表顶部插入。 */
  async function submitCreate(name: string): Promise<void> {
    if (submitting) {
      return;
    }
    setSubmitting(true);
    setDialogFieldError(null);
    setDialogTopError(null);
    const key = idem.current.keyFor('department-create', 'directory', JSON.stringify({ name }));
    try {
      const created = await api.createDepartment(name, key);
      idem.current.clear();
      setCreating(false);
      if (filter === 'inactive') {
        setFilter('active');
      } else {
        setItems((current) => [created, ...current]);
        markEnter(created.id);
      }
    } catch (error) {
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 idempotency_key_conflict）：清键，不自动重发
        idem.current.businessResponse();
      }
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'department_name_exists'
      ) {
        setDialogFieldError(copyDepartments.nameExists);
      } else if (error instanceof ApiError && error.status === 422) {
        setDialogFieldError(copyDepartments.validationError);
      } else if (error instanceof ApiError && error.status === 403) {
        // 对话框内顶部 danger 说明 + 刷新目录（入口与操作以服务端结论为准）
        setDialogTopError(copyDepartments.actionForbidden);
        await loadDepartments();
      } else {
        // 网络未知 / 超时：复用同键同体，由用户显式重试；其余 409 不换键不重发
        setDialogTopError(copyDepartments.actionError);
      }
    } finally {
      setSubmitting(false);
    }
  }

  /** 改名（200）：提交体 { expected_version, name }；成功名称就地更新 + fog-white 闪现。 */
  async function submitRename(name: string): Promise<void> {
    const target = renaming;
    if (target === null || submitting) {
      return;
    }
    setSubmitting(true);
    setDialogFieldError(null);
    setDialogTopError(null);
    const key = idem.current.keyFor(
      'department-rename',
      target.id,
      JSON.stringify({ version: target.version, name }),
    );
    try {
      const updated = await api.renameDepartment(target.id, target.version, name, key);
      idem.current.clear();
      setRenaming(null);
      applyDepartmentUpdate(updated);
      markRenamed(updated.id);
    } catch (error) {
      if (isBusinessResponse(error)) {
        idem.current.businessResponse();
      }
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'department_name_exists'
      ) {
        setDialogFieldError(copyDepartments.nameExists);
      } else if (error instanceof ApiError && error.status === 422) {
        setDialogFieldError(copyDepartments.validationError);
      } else if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'version_conflict'
      ) {
        // 重新请求目录刷新该行（名称与 version 以最新为准），用户基于新 version 重新确认（新键）
        const freshItems = await loadDepartments();
        const fresh = freshItems?.find((item) => item.id === target.id) ?? null;
        if (fresh !== null) {
          setRenaming(fresh);
          setDialogTopError(copyDepartments.versionConflict);
        } else {
          setRenaming(null);
        }
      } else if (
        error instanceof ApiError &&
        ((error.status === 404 && error.code === 'department_not_found') ||
          (error.status === 409 && error.code === 'department_inactive'))
      ) {
        setRenaming(null);
        setActionError(copyDepartments.statusChanged);
        await loadDepartments();
      } else if (error instanceof ApiError && error.status === 403) {
        setDialogTopError(copyDepartments.actionForbidden);
        await loadDepartments();
      } else {
        setDialogTopError(copyDepartments.actionError);
      }
    } finally {
      setSubmitting(false);
    }
  }

  /** 停用（200）：提交体 { expected_version }；不乐观标记，状态切换以 200 响应为准。 */
  async function confirmDeactivate(): Promise<void> {
    const target = deactivating;
    if (target === null || submitting) {
      return;
    }
    setSubmitting(true);
    setDeactivateNote(null);
    setDeactivateRetry(false);
    const key = idem.current.keyFor(
      'department-deactivate',
      target.id,
      JSON.stringify({ version: target.version }),
    );
    try {
      const updated = await api.deactivateDepartment(target.id, target.version, key);
      idem.current.clear();
      setDeactivating(null);
      applyDepartmentUpdate(updated);
    } catch (error) {
      if (isBusinessResponse(error)) {
        idem.current.businessResponse();
      }
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'department_has_members'
      ) {
        setDeactivateNote(copyDepartments.blockedHasMembers);
        await loadDepartments();
      } else if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'department_has_active_work'
      ) {
        setDeactivateNote(copyDepartments.blockedHasWork);
        await loadDepartments();
      } else if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === 'version_conflict'
      ) {
        const freshItems = await loadDepartments();
        const fresh = freshItems?.find((item) => item.id === target.id) ?? null;
        if (fresh !== null) {
          setDeactivating(fresh);
          setDeactivateNote(copyDepartments.versionConflict);
        } else {
          setDeactivating(null);
        }
      } else if (
        error instanceof ApiError &&
        error.status === 503 &&
        error.code === 'department_deactivation_unverified'
      ) {
        // details.retryable=true：说明「稍后重试」+ 保留重试入口（重新点击属新提交，新键）
        setDeactivateNote(copyDepartments.unverified);
        setDeactivateRetry(true);
      } else if (
        error instanceof ApiError &&
        ((error.status === 404 && error.code === 'department_not_found') ||
          (error.status === 409 && error.code === 'department_inactive'))
      ) {
        setDeactivating(null);
        setActionError(copyDepartments.statusChanged);
        await loadDepartments();
      } else if (error instanceof ApiError && error.status === 403) {
        setDeactivateNote(copyDepartments.actionForbidden);
        await loadDepartments();
      } else {
        setDeactivateNote(copyDepartments.actionError);
      }
    } finally {
      setSubmitting(false);
    }
  }

  /** 关闭停用确认框：复位提示与重试态，并清掉可能挂起的幂等键（与新增 / 改名一致）。 */
  function closeDeactivate(): void {
    setDeactivating(null);
    setDeactivateNote(null);
    setDeactivateRetry(false);
    idem.current.clear();
  }

  return (
    <section
      aria-label={copy.shell.drawer.modules.departments}
      className="flex flex-col gap-3 pb-10"
    >
      <div className="flex items-center justify-between gap-4">
        <SegmentedControl
          options={[
            { value: 'active', label: copyDepartments.filterActive },
            { value: 'inactive', label: copyDepartments.filterInactive },
            { value: 'all', label: copyDepartments.filterAll },
          ]}
          value={filter}
          onChange={(value) => setFilter(value as DepartmentStatusFilter)}
          ariaLabel={copyDepartments.colStatus}
        />
        <div className="flex items-center gap-3">
          <TextLink ink onClick={() => void loadDepartments()}>
            {copy.admin.common.refresh}
          </TextLink>
          <Pill onClick={() => setCreating(true)}>{copyDepartments.add}</Pill>
        </div>
      </div>
      {actionError !== null && (
        <p role="alert" className="text-[15px] text-danger">
          {actionError}
        </p>
      )}
      {loading ? (
        <LoadingRows count={5} />
      ) : loadError ? (
        <ErrorState text={copyDepartments.loadError} onRetry={() => void loadDepartments()} />
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-3">
          <EmptyState text={copyDepartments.empty} />
          {filter === 'active' && (
            <Pill onClick={() => setCreating(true)}>{copyDepartments.add}</Pill>
          )}
        </div>
      ) : (
        <div>
          <div
            className={
              `grid ${DEPARTMENT_GRID} items-center gap-3 px-4 pb-1 text-[14px] text-ash-gray`
            }
          >
            <span>{copyDepartments.colName}</span>
            <span>{copyDepartments.colStatus}</span>
            <span>{copyDepartments.colMembers}</span>
            <span>{copyDepartments.colDocuments}</span>
            <span>{copyDepartments.colTasks}</span>
            <span>{copyDepartments.colSubmissions}</span>
            <span>{copyDepartments.colDeactivatedAt}</span>
            <span>{copyDepartments.colActions}</span>
          </div>
          <ul className="divide-y divide-[var(--color-hairline)]">
            {items.map((item) => {
              const active = item.status === 'active';
              const actions = rowActions(item);
              return (
                <li
                  key={item.id}
                  className={
                    `${enterIds.has(item.id) ? 'ui-row-insert ' : ''}` +
                    `transition-colors duration-[var(--duration-slow)] ` +
                    `${flashIds.has(item.id) ? 'bg-fog-white' : ''}`
                  }
                >
                  <div
                    className={
                      `grid h-14 ${DEPARTMENT_GRID} items-center gap-3 px-4 ` +
                      'transition-colors duration-150 hover:bg-mist-gray'
                    }
                  >
                    <span
                      key={item.name}
                      className={
                        `truncate text-[15px] font-medium text-ink-black ` +
                        `${renamedIds.has(item.id) ? 'ui-fade-enter-fast' : ''}`
                      }
                    >
                      {item.name}
                    </span>
                    <span className="flex items-center gap-2 text-[15px] text-slate-gray">
                      <StatusDot intent={active ? 'success' : 'slate'} />
                      {active ? copyDepartments.statusActive : copyDepartments.statusInactive}
                    </span>
                    <span className="truncate text-[15px] text-slate-gray">
                      {item.member_count}
                    </span>
                    <span className="truncate text-[15px] text-slate-gray">
                      {item.document_count}
                    </span>
                    <span className="truncate text-[15px] text-slate-gray">
                      {item.nonterminal_job_count}
                    </span>
                    <span className="truncate text-[15px] text-slate-gray">
                      {item.pending_submission_count}
                    </span>
                    <span className="truncate text-[15px] text-slate-gray">
                      {item.deactivated_at !== null
                        ? formatDate(item.deactivated_at)
                        : copyDepartments.noActions}
                    </span>
                    <span className="flex items-center gap-3">
                      {actions.length === 0 ? (
                        <span className="text-[15px] text-smoke-gray">
                          {copyDepartments.noActions}
                        </span>
                      ) : (
                        <>
                          {actions.includes('rename') && (
                            <TextLink
                              ink
                              onClick={() => {
                                setDialogFieldError(null);
                                setDialogTopError(null);
                                setRenaming(item);
                              }}
                            >
                              {copyDepartments.rename}
                            </TextLink>
                          )}
                          {actions.includes('deactivate') && (
                            <TextLink
                              danger
                              onClick={() => {
                                setDeactivateNote(null);
                                setDeactivateRetry(false);
                                setDeactivating(item);
                              }}
                            >
                              {copyDepartments.deactivate}
                            </TextLink>
                          )}
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

      {creating && (
        <DepartmentNameDialog
          title={copyDepartments.addDialogTitle}
          initialName=""
          showNameNote
          confirmLabel={copy.controls.confirm}
          disableUnchanged={false}
          submitting={submitting}
          fieldError={dialogFieldError}
          topError={dialogTopError}
          onClose={() => {
            setCreating(false);
            idem.current.clear();
          }}
          onSubmit={(name) => void submitCreate(name)}
        />
      )}
      {renaming !== null && (
        <DepartmentNameDialog
          title={copyDepartments.renameDialogTitle}
          initialName={renaming.name}
          showNameNote={false}
          confirmLabel={copy.admin.users.save}
          disableUnchanged
          submitting={submitting}
          fieldError={dialogFieldError}
          topError={dialogTopError}
          onClose={() => {
            setRenaming(null);
            idem.current.clear();
          }}
          onSubmit={(name) => void submitRename(name)}
        />
      )}
      {deactivating !== null && (
        <DialogFrame ariaLabel={copyDepartments.deactivateDialogTitle} onClose={closeDeactivate}>
          <h2 className="text-[20px] font-medium text-ink-black">
            {copyDepartments.deactivateDialogTitle}
          </h2>
          <div className="mt-2 flex flex-col gap-2 text-[15px] text-slate-gray">
            <p>{copyDepartments.deactivatePoint1}</p>
            <p>{copyDepartments.deactivatePoint2}</p>
            <p>{copyDepartments.deactivatePoint3}</p>
          </div>
          <p className="mt-3 text-[15px] text-slate-gray">
            {copyDepartments.deactivateCounts(
              deactivating.member_count,
              deactivating.nonterminal_job_count,
              deactivating.pending_submission_count,
            )}
          </p>
          {deactivateNote !== null && (
            <p role="alert" className="mt-3 text-[15px] text-danger">
              {deactivateNote}
              {deactivateRetry && (
                <>
                  {' '}
                  <TextLink onClick={() => void confirmDeactivate()}>{copy.states.retry}</TextLink>
                </>
              )}
            </p>
          )}
          <div className="mt-6 flex justify-end gap-2">
            <Pill
              variant="ghost"
              size="sm"
              disabled={submitting}
              onClick={closeDeactivate}
            >
              {copy.controls.cancel}
            </Pill>
            <Pill
              size="sm"
              danger
              loading={submitting}
              disabled={submitting}
              onClick={() => void confirmDeactivate()}
            >
              {copyDepartments.deactivateConfirm}
            </Pill>
          </div>
        </DialogFrame>
      )}
    </section>
  );
}

/* ---------- 新增 / 改名共用名称对话框（400px） ---------- */

interface DepartmentNameDialogProps {
  readonly title: string;
  readonly initialName: string;
  /** 新增：框下说明行（名称规范化后唯一）。 */
  readonly showNameNote: boolean;
  readonly confirmLabel: string;
  /** 改名：名称未变更或为空时确认键禁用。 */
  readonly disableUnchanged: boolean;
  readonly submitting: boolean;
  readonly fieldError: string | null;
  readonly topError: string | null;
  readonly onClose: () => void;
  readonly onSubmit: (name: string) => void;
}

function DepartmentNameDialog({
  title,
  initialName,
  showNameNote,
  confirmLabel,
  disableUnchanged,
  submitting,
  fieldError,
  topError,
  onClose,
  onSubmit,
}: DepartmentNameDialogProps) {
  const copyDepartments = copy.admin.departments;
  const [name, setName] = useState(initialName);
  const trimmed = name.trim();
  const invalidEmpty = trimmed === '' && name !== '';
  const confirmDisabled =
    submitting || trimmed === '' || (disableUnchanged && trimmed === initialName.trim());
  const shownFieldError = fieldError ?? (invalidEmpty ? copyDepartments.nameRequired : null);

  return (
    <DialogFrame ariaLabel={title} onClose={onClose}>
      <h2 className="text-[20px] font-medium text-ink-black">{title}</h2>
      {topError !== null && (
        <p role="alert" className="mt-3 text-[15px] text-danger">
          {topError}
        </p>
      )}
      <div className="mt-4">
        <label htmlFor="department-name-input" className="mb-2 block text-[15px] text-slate-gray">
          {copyDepartments.nameLabel}
        </label>
        <input
          id="department-name-input"
          type="text"
          autoComplete="off"
          value={name}
          aria-invalid={shownFieldError !== null}
          onChange={(event) => setName(event.target.value)}
          className={
            'h-10 w-full rounded-[var(--radius-inputs)] border bg-paper-white px-3 text-[15px] ' +
            `text-ink-black focus:border-ink-black ${
              shownFieldError !== null ? 'border-danger' : 'border-[var(--color-hairline)]'
            }`
          }
        />
        {shownFieldError !== null && (
          <p role="alert" className="mt-2 text-[15px] text-danger">
            {shownFieldError}
          </p>
        )}
        {showNameNote && shownFieldError === null && (
          <p className="mt-2 text-[15px] text-smoke-gray">{copyDepartments.nameNote}</p>
        )}
      </div>
      <div className="mt-6 flex justify-end gap-2">
        <Pill variant="ghost" size="sm" disabled={submitting} onClick={onClose}>
          {copy.controls.cancel}
        </Pill>
        <Pill
          size="sm"
          loading={submitting}
          disabled={confirmDisabled}
          onClick={() => onSubmit(trimmed)}
        >
          {confirmLabel}
        </Pill>
      </div>
    </DialogFrame>
  );
}
