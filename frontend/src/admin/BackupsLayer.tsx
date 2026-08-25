/*
 * 备份与恢复（backup-restore-operations-layer 规格 §2/§3/§9；深链 /admin/operations/backups）。
 * - 严格 ops-only：抽屉注册项 roles=['ops'] 隐藏入口并按角色截断深链；本层进入再校验角色，
 *   非 ops 渲染拒绝态（双重防护，如降权后仍停留在本层）。
 * - 三个页面级分段视图（备份 / 恢复 / 策略）经 SegmentedControl 切换，分段状态同步到 URL
 *   查询参数 ?view=（刷新 / 粘贴链接可恢复）；切换只换视图，已提交的服务端状态由重新拉取恢复。
 * - ops 下备份 / 恢复列表 5s 轮询（卸载 / 翻页清除；sequence fence 作废旧响应）；
 *   命令 single-flight + Idempotency-Key；行内状态、HeaderNotice 局部提示、
 *   loading / error / retry 复用既有原语，不新增全局 toast。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { ApiError } from '../api/errors';
import { useAuthState } from '../auth/AuthProvider';
import { copy } from '../copy';
import { createIdempotencyScope, isBusinessResponse } from '../settings/idempotency';
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  HeaderNotice,
  LoadingRows,
  Paginator,
  Pill,
  SegmentedControl,
  Switch,
  TextLink,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { formatDateTime } from './format';
import type {
  BackupPolicy,
  BackupPolicyFrequency,
  BackupPolicyPatchInput,
  OpsBackupDetail,
  OpsBackupItem,
  OpsBackupListResponse,
  OpsRepairTarget,
  OpsRestoreDetail,
  OpsRestoreItem,
  OpsRestoreListResponse,
} from './types';

/** 状态轮询间隔（与任务队列同一约定）。 */
const POLL_INTERVAL_MS = 5000;
/** 备份 / 恢复历史分页大小。 */
const PAGE_SIZE = 10;
/** 恢复来源候选拉取窗口（第一页内的可恢复备份）。 */
const SOURCE_PAGE_SIZE = 50;

type BackupsView = 'backups' | 'restores' | 'policy';
const VIEWS: readonly BackupsView[] = ['backups', 'restores', 'policy'];

function parseView(raw: string | null): BackupsView {
  return VIEWS.includes(raw as BackupsView) ? (raw as BackupsView) : 'backups';
}

function totalPagesOf(total: number): number {
  return Math.max(1, Math.ceil(total / PAGE_SIZE));
}

export function BackupsLayer() {
  const { user } = useAuthState();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = parseView(searchParams.get('view'));
  const copyBackups = copy.admin.operations.backups;

  if (user?.role !== 'ops') {
    // 非 ops：注册项已隐藏入口并截断深链；直接渲染本层（如角色降权后仍停留）显示拒绝态
    return <EmptyState text={copyBackups.denied} />;
  }

  return (
    <section aria-label={copyBackups.title} className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-[20px] font-medium text-ink-black">{copyBackups.title}</h2>
        <SegmentedControl
          options={[
            { value: 'backups', label: copyBackups.viewBackups },
            { value: 'restores', label: copyBackups.viewRestores },
            { value: 'policy', label: copyBackups.viewPolicy },
          ]}
          value={view}
          onChange={(value) => setSearchParams({ view: value })}
          ariaLabel={copyBackups.title}
        />
      </div>
      {view === 'backups' && <BackupsSegment />}
      {view === 'restores' && <RestoresSegment />}
      {view === 'policy' && <PolicySegment />}
    </section>
  );
}

/* ---------- 「备份」分段：一键备份、分页历史、组成物详情、刷新 ---------- */

function BackupsSegment() {
  const { api } = useAdmin();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<OpsBackupListResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  /** 一键备份 single-flight（异步命令进行中禁用重入）。 */
  const [creating, setCreating] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<OpsBackupDetail | null>(null);
  const [detailFailed, setDetailFailed] = useState(false);
  // 读序列代际（sequence fence）：翻页 / 卸载推进，过期响应一律作废
  const generationRef = useRef(0);
  const pageRef = useRef(page);
  pageRef.current = page;
  const expandedRef = useRef<string | null>(null);
  expandedRef.current = expandedId;
  const createIdem = useRef(createIdempotencyScope());
  const copyBackups = copy.admin.operations.backups;

  /** 静默读：成功替换数据（keyed 行复用）；展开中的备份详情随同一读序列刷新。 */
  const load = useCallback(async (): Promise<void> => {
    const gen = generationRef.current;
    try {
      const response = await api.listOpsBackups(pageRef.current, PAGE_SIZE);
      if (gen !== generationRef.current) {
        return;
      }
      setData(response);
      setLoadFailed(false);
      const expanded = expandedRef.current;
      if (expanded !== null) {
        try {
          const detailResponse = await api.getOpsBackup(expanded);
          if (gen !== generationRef.current) {
            return;
          }
          setDetail(detailResponse);
          setDetailFailed(false);
        } catch {
          if (gen === generationRef.current) {
            setDetailFailed(true);
          }
        }
      }
    } catch {
      if (gen === generationRef.current) {
        setLoadFailed(true);
      }
    }
  }, [api]);

  // 首次加载 + 5s 轮询（翻页 / 卸载清除定时器并作废旧响应）
  useEffect(() => {
    const gen = ++generationRef.current;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setData(null);
    setDetail(null);
    setDetailFailed(false);
    setExpandedId(null);
    setInitialLoading(true);
    setLoadFailed(false);
    const tick = async () => {
      await load();
      if (cancelled || gen !== generationRef.current) {
        return;
      }
      setInitialLoading(false);
      timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      generationRef.current += 1;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [load, page]);

  /** 初载失败重试：补一次读（轮询由 effect 管，重试成功即恢复链）。 */
  async function retryInitial(): Promise<void> {
    setInitialLoading(true);
    setLoadFailed(false);
    await load();
    setInitialLoading(false);
  }

  async function createBackup(): Promise<void> {
    if (creating) {
      return; // single-flight
    }
    setCreating(true);
    setNotice(null);
    setErrorNotice(null);
    // key 绑定 op/target/payload：实例级手动备份指纹固定，网络未知重试同键，业务响应后换键
    const key = createIdem.current.keyFor('ops-backup-create', 'instance', 'manual');
    try {
      const response = await api.createOpsBackup(key);
      createIdem.current.clear();
      setNotice(copyBackups.backupCreated(response.backup_id));
      await load();
    } catch (error) {
      if (isBusinessResponse(error)) {
        createIdem.current.businessResponse();
      }
      if (error instanceof ApiError && error.status === 503 && error.code === 'maintenance_mode') {
        setErrorNotice(copyBackups.maintenanceMode);
      } else {
        setErrorNotice(copyBackups.actionError);
      }
    } finally {
      setCreating(false);
    }
  }

  /** 展开 / 收起组成物详情；展开时立即拉取一次（之后随轮询刷新）。 */
  function toggleExpand(backupId: string): void {
    if (expandedId === backupId) {
      setExpandedId(null);
      setDetail(null);
      setDetailFailed(false);
      return;
    }
    setExpandedId(backupId);
    setDetail(null);
    setDetailFailed(false);
    const gen = generationRef.current;
    void api
      .getOpsBackup(backupId)
      .then((response) => {
        if (gen === generationRef.current && expandedRef.current === backupId) {
          setDetail(response);
          setDetailFailed(false);
        }
      })
      .catch(() => {
        if (gen === generationRef.current && expandedRef.current === backupId) {
          setDetailFailed(true);
        }
      });
  }

  const gridColumns =
    'grid-cols-[minmax(0,1.2fr)_minmax(0,0.7fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.6fr)_minmax(0,0.6fr)]';

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-end gap-4">
        <Pill size="sm" loading={creating} onClick={() => void createBackup()}>
          {copyBackups.createBackup}
        </Pill>
        <TextLink className="text-ink-black" onClick={() => void load()}>
          {copy.admin.common.refresh}
        </TextLink>
      </div>
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {errorNotice !== null && (
        <p role="alert" className="text-[15px] text-danger">
          {errorNotice}
        </p>
      )}
      {initialLoading && <LoadingRows count={3} />}
      {!initialLoading && loadFailed && data === null && (
        <ErrorState text={copyBackups.loadError} onRetry={() => void retryInitial()} />
      )}
      {!initialLoading && data !== null && (
        <>
          {loadFailed && (
            <ErrorState text={copyBackups.loadError} onRetry={() => void load()} />
          )}
          {data.items.length === 0 ? (
            <EmptyState text={copyBackups.emptyBackups} />
          ) : (
            <div role="table" aria-label={copyBackups.backupTableAria}>
              <div role="row" className={`grid ${gridColumns} items-center gap-x-4 px-4 py-2`}>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colBackupId}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colBackupStatus}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colBackupCreatedAt}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colBackupCompletedAt}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colBackupRestorable}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colBackupActions}</span>
              </div>
              <ul
                role="rowgroup"
                className="flex flex-col rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white px-0 py-1"
              >
                {data.items.map((backup) => (
                  <BackupRow
                    key={backup.backup_id}
                    backup={backup}
                    gridColumns={gridColumns}
                    expanded={expandedId === backup.backup_id}
                    detail={expandedId === backup.backup_id ? detail : null}
                    detailFailed={expandedId === backup.backup_id && detailFailed}
                    onToggleExpand={() => toggleExpand(backup.backup_id)}
                    onRetryDetail={() => {
                      setDetail(null);
                      setDetailFailed(false);
                      const gen = generationRef.current;
                      void api
                        .getOpsBackup(backup.backup_id)
                        .then((response) => {
                          if (gen === generationRef.current) {
                            setDetail(response);
                          }
                        })
                        .catch(() => {
                          if (gen === generationRef.current) {
                            setDetailFailed(true);
                          }
                        });
                    }}
                  />
                ))}
              </ul>
            </div>
          )}
          {data.total > PAGE_SIZE && (
            <Paginator page={page} totalPages={totalPagesOf(data.total)} onChange={setPage} />
          )}
        </>
      )}
    </div>
  );
}

interface BackupRowProps {
  readonly backup: OpsBackupItem;
  readonly gridColumns: string;
  readonly expanded: boolean;
  readonly detail: OpsBackupDetail | null;
  readonly detailFailed: boolean;
  readonly onToggleExpand: () => void;
  readonly onRetryDetail: () => void;
}

function BackupRow({
  backup,
  gridColumns,
  expanded,
  detail,
  detailFailed,
  onToggleExpand,
  onRetryDetail,
}: BackupRowProps) {
  const copyBackups = copy.admin.operations.backups;
  return (
    <li className="border-t border-[var(--color-hairline)] first:border-t-0">
      <div role="row" className={`grid ${gridColumns} items-center gap-x-4 px-4 py-3`}>
        <span role="cell" className="min-w-0 truncate font-mono text-[15px] text-ash-gray">
          {backup.backup_id}
        </span>
        <span role="cell">
          <span className="rounded-[var(--radius-buttons)] bg-mist-gray px-2 py-1 text-caption text-ink-black">
            {copyBackups.backupStatus(backup.status)}
          </span>
        </span>
        <span role="cell" className="text-[15px] text-slate-gray">
          {formatDateTime(backup.created_at)}
        </span>
        <span role="cell" className="text-[15px] text-slate-gray">
          {backup.completed_at === null ? '—' : formatDateTime(backup.completed_at)}
        </span>
        <span
          role="cell"
          className={`text-[15px] ${backup.restorable ? 'text-success' : 'text-smoke-gray'}`}
        >
          {backup.restorable ? copyBackups.restorableYes : copyBackups.restorableNo}
        </span>
        <span role="cell" className="flex items-center justify-end">
          <TextLink onClick={onToggleExpand}>
            {expanded ? copyBackups.detailCollapse : copyBackups.detailExpand}
          </TextLink>
        </span>
      </div>
      {expanded && (
        <div className="flex flex-col gap-1 px-4 pb-3">
          {detailFailed && (
            <ErrorState text={copyBackups.detailLoadError} onRetry={onRetryDetail} />
          )}
          {!detailFailed && detail === null && <LoadingRows count={1} />}
          {!detailFailed &&
            detail !== null &&
            detail.components.map((component) => (
              <div key={component.kind} className="flex flex-col gap-0.5">
                <p className="text-[14px] text-slate-gray">
                  {copyBackups.componentKind(component.kind)}
                  {' · '}
                  {copyBackups.backupStatus(component.status)}
                </p>
                {component.reference !== null &&
                  component.reference !== undefined && (
                    <p className="font-mono text-[13px] text-smoke-gray">
                      {copyBackups.componentReference(component.reference)}
                    </p>
                  )}
                {component.failure_reason !== null &&
                  component.failure_reason !== undefined && (
                    <p className="text-[14px] text-danger">
                      {copyBackups.componentFailure(component.failure_reason)}
                    </p>
                  )}
              </div>
            ))}
        </div>
      )}
    </li>
  );
}

/* ---------- 「恢复」分段：来源选择、危险确认、恢复记录 / 阶段进度 / repair retry ---------- */

function RestoresSegment() {
  const { api } = useAdmin();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<OpsRestoreListResponse | null>(null);
  /** 恢复来源候选（restorable=true 的备份；随读序列刷新）。 */
  const [sources, setSources] = useState<readonly OpsBackupItem[]>([]);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  const [selectedBackupId, setSelectedBackupId] = useState<string>('');
  /** 危险确认目标快照（打开确认框时的来源备份，展示其当前状态）。 */
  const [pendingRestore, setPendingRestore] = useState<OpsBackupItem | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<OpsRestoreDetail | null>(null);
  const [detailFailed, setDetailFailed] = useState(false);
  const [busyTargetIds, setBusyTargetIds] = useState<readonly string[]>([]);
  const generationRef = useRef(0);
  const pageRef = useRef(page);
  pageRef.current = page;
  const expandedRef = useRef<string | null>(null);
  expandedRef.current = expandedId;
  // 确认 token：确认 A 飞行中关闭后打开 B，A completion 不得关 B / 写错状态
  const restoreTokenRef = useRef(0);
  const restoreIdem = useRef(createIdempotencyScope());
  const retryIdem = useRef(createIdempotencyScope());
  const copyBackups = copy.admin.operations.backups;

  /** 静默读：恢复记录 + 恢复来源候选同一读序列刷新；展开中的恢复详情随行刷新。 */
  const load = useCallback(async (): Promise<void> => {
    const gen = generationRef.current;
    try {
      const [restores, backups] = await Promise.all([
        api.listOpsRestores(pageRef.current, PAGE_SIZE),
        api.listOpsBackups(1, SOURCE_PAGE_SIZE),
      ]);
      if (gen !== generationRef.current) {
        return;
      }
      setData(restores);
      setSources(backups.items.filter((backup) => backup.restorable));
      setLoadFailed(false);
      const expanded = expandedRef.current;
      if (expanded !== null) {
        try {
          const detailResponse = await api.getOpsRestore(expanded);
          if (gen !== generationRef.current) {
            return;
          }
          setDetail(detailResponse);
          setDetailFailed(false);
        } catch {
          if (gen === generationRef.current) {
            setDetailFailed(true);
          }
        }
      }
    } catch {
      if (gen === generationRef.current) {
        setLoadFailed(true);
      }
    }
  }, [api]);

  // 首次加载 + 5s 轮询（翻页 / 卸载清除定时器并作废旧响应）
  useEffect(() => {
    const gen = ++generationRef.current;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setData(null);
    setSources([]);
    setDetail(null);
    setDetailFailed(false);
    setExpandedId(null);
    setInitialLoading(true);
    setLoadFailed(false);
    const tick = async () => {
      await load();
      if (cancelled || gen !== generationRef.current) {
        return;
      }
      setInitialLoading(false);
      timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      generationRef.current += 1;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [load, page]);

  async function retryInitial(): Promise<void> {
    setInitialLoading(true);
    setLoadFailed(false);
    await load();
    setInitialLoading(false);
  }

  async function confirmRestore(): Promise<void> {
    const backup = pendingRestore;
    if (backup === null || confirming) {
      return;
    }
    const token = restoreTokenRef.current;
    setConfirming(true);
    setDialogError(null);
    // key 绑定 op/target/payload：目标备份即指纹，改选备份自动换键
    const key = restoreIdem.current.keyFor('ops-restore-create', backup.backup_id, backup.backup_id);
    try {
      const response = await api.createOpsRestore(backup.backup_id, key);
      restoreIdem.current.clear();
      if (token !== restoreTokenRef.current) {
        return;
      }
      setPendingRestore(null);
      setNotice(copyBackups.restoreStarted(response.restore_id));
      await load();
    } catch (error) {
      if (isBusinessResponse(error)) {
        restoreIdem.current.businessResponse();
      }
      if (token !== restoreTokenRef.current) {
        return;
      }
      if (error instanceof ApiError && error.status === 409 && error.code === 'restore_in_progress') {
        // 持久互斥：关闭对话框 + 刷新记录
        setPendingRestore(null);
        setErrorNotice(copyBackups.restoreInProgress);
        await load();
      } else if (
        error instanceof ApiError &&
        ((error.status === 409 && error.code === 'backup_not_restorable') ||
          (error.status === 404 && error.code === 'backup_not_found'))
      ) {
        setPendingRestore(null);
        setErrorNotice(copyBackups.backupNotRestorable);
        await load();
      } else if (error instanceof ApiError && error.status === 503 && error.code === 'maintenance_mode') {
        setPendingRestore(null);
        setErrorNotice(copyBackups.maintenanceMode);
        await load();
      } else {
        // 其余错误：对话框保持打开，框内错误行，可重试或取消
        setDialogError(copyBackups.actionError);
      }
    } finally {
      if (token === restoreTokenRef.current) {
        setConfirming(false);
      }
    }
  }

  async function retryTarget(restoreId: string, target: OpsRepairTarget): Promise<void> {
    if (busyTargetIds.includes(target.target_id)) {
      return; // per-target busy 防重入
    }
    setBusyTargetIds((ids) => [...ids, target.target_id]);
    setNotice(null);
    setErrorNotice(null);
    const key = retryIdem.current.keyFor('ops-repair-retry', target.target_id, restoreId);
    try {
      await api.retryOpsRepairTarget(restoreId, target.target_id, key);
      retryIdem.current.clear();
      setNotice(copyBackups.repairRetried);
      await load();
    } catch (error) {
      if (isBusinessResponse(error)) {
        retryIdem.current.businessResponse();
      }
      if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
        // 竞态：修复目标 / 恢复状态已变化，刷新后按最新状态呈现
        setErrorNotice(copyBackups.repairNotOpen);
        await load();
      } else {
        setErrorNotice(copyBackups.actionError);
      }
    } finally {
      setBusyTargetIds((ids) => ids.filter((id) => id !== target.target_id));
    }
  }

  function toggleExpand(restoreId: string): void {
    if (expandedId === restoreId) {
      setExpandedId(null);
      setDetail(null);
      setDetailFailed(false);
      return;
    }
    setExpandedId(restoreId);
    setDetail(null);
    setDetailFailed(false);
    const gen = generationRef.current;
    void api
      .getOpsRestore(restoreId)
      .then((response) => {
        if (gen === generationRef.current && expandedRef.current === restoreId) {
          setDetail(response);
          setDetailFailed(false);
        }
      })
      .catch(() => {
        if (gen === generationRef.current && expandedRef.current === restoreId) {
          setDetailFailed(true);
        }
      });
  }

  const gridColumns =
    'grid-cols-[minmax(0,1.1fr)_minmax(0,1.1fr)_minmax(0,0.7fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.6fr)]';
  const selectedBackup = sources.find((backup) => backup.backup_id === selectedBackupId) ?? null;

  return (
    <div className="flex flex-col gap-3">
      {/* 来源选择：仅 restorable 备份可发起恢复（候选随读序列刷新） */}
      <div className="flex items-end gap-3">
        <div className="min-w-0 flex-1">
          <label htmlFor="restore-source" className="mb-2 block text-[15px] text-slate-gray">
            {copyBackups.sourceLabel}
          </label>
          <select
            id="restore-source"
            value={selectedBackupId}
            disabled={sources.length === 0}
            onChange={(event) => setSelectedBackupId(event.target.value)}
            className={
              'h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] ' +
              'bg-paper-white px-3 font-mono text-[15px] text-ink-black disabled:text-smoke-gray'
            }
          >
            <option value="">
              {sources.length === 0 ? copyBackups.noRestorableSource : copyBackups.sourcePlaceholder}
            </option>
            {sources.map((backup) => (
              <option key={backup.backup_id} value={backup.backup_id}>
                {`${backup.backup_id} · ${formatDateTime(backup.created_at)}`}
              </option>
            ))}
          </select>
        </div>
        <Pill
          size="sm"
          danger
          disabled={selectedBackup === null}
          onClick={() => {
            if (selectedBackup !== null) {
              setDialogError(null);
              setPendingRestore(selectedBackup);
            }
          }}
        >
          {copyBackups.startRestore}
        </Pill>
      </div>
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {errorNotice !== null && (
        <p role="alert" className="text-[15px] text-danger">
          {errorNotice}
        </p>
      )}
      {initialLoading && <LoadingRows count={3} />}
      {!initialLoading && loadFailed && data === null && (
        <ErrorState text={copyBackups.loadError} onRetry={() => void retryInitial()} />
      )}
      {!initialLoading && data !== null && (
        <>
          {loadFailed && <ErrorState text={copyBackups.loadError} onRetry={() => void load()} />}
          {data.items.length === 0 ? (
            <EmptyState text={copyBackups.emptyRestores} />
          ) : (
            <div role="table" aria-label={copyBackups.restoreTableAria}>
              <div role="row" className={`grid ${gridColumns} items-center gap-x-4 px-4 py-2`}>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colRestoreId}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colRestoreBackup}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colRestoreStatus}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colRestoreCreatedAt}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colRestoreCompletedAt}</span>
                <span role="columnheader" className="truncate text-[14px] text-ash-gray">{copyBackups.colRestoreActions}</span>
              </div>
              <ul
                role="rowgroup"
                className="flex flex-col rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white px-0 py-1"
              >
                {data.items.map((restore) => (
                  <RestoreRow
                    key={restore.restore_id}
                    restore={restore}
                    gridColumns={gridColumns}
                    expanded={expandedId === restore.restore_id}
                    detail={expandedId === restore.restore_id ? detail : null}
                    detailFailed={expandedId === restore.restore_id && detailFailed}
                    busyTargetIds={busyTargetIds}
                    onToggleExpand={() => toggleExpand(restore.restore_id)}
                    onRetryDetail={() => {
                      setDetail(null);
                      setDetailFailed(false);
                      const gen = generationRef.current;
                      void api
                        .getOpsRestore(restore.restore_id)
                        .then((response) => {
                          if (gen === generationRef.current) {
                            setDetail(response);
                          }
                        })
                        .catch(() => {
                          if (gen === generationRef.current) {
                            setDetailFailed(true);
                          }
                        });
                    }}
                    onRetryTarget={(target) => void retryTarget(restore.restore_id, target)}
                  />
                ))}
              </ul>
            </div>
          )}
          {data.total > PAGE_SIZE && (
            <Paginator page={page} totalPages={totalPagesOf(data.total)} onChange={setPage} />
          )}
        </>
      )}
      {/* 危险操作确认（规格 §9）：展示备份 ID、维护模式影响与备份当前状态后才可提交 */}
      <ConfirmDialog
        open={pendingRestore !== null}
        confirming={confirming}
        danger
        onOpenChange={(open) => {
          if (!open) {
            restoreTokenRef.current += 1;
            setConfirming(false);
            setPendingRestore(null);
            setDialogError(null);
            restoreIdem.current.clear();
          }
        }}
        title={copyBackups.restoreDialogTitle}
        description={copyBackups.restoreDialogImpact}
        confirmLabel={copyBackups.restoreConfirm}
        onConfirm={() => void confirmRestore()}
      >
        <p className="font-mono text-[15px] text-ink-black">
          {copyBackups.restoreDialogBackup(pendingRestore?.backup_id ?? '')}
        </p>
        <p className="text-[15px] text-slate-gray">
          {copyBackups.restoreDialogStatus(
            copyBackups.backupStatus(pendingRestore?.status ?? ''),
          )}
        </p>
        {dialogError !== null && (
          <p role="alert" className="text-[15px] text-danger">
            {dialogError}
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}

interface RestoreRowProps {
  readonly restore: OpsRestoreItem;
  readonly gridColumns: string;
  readonly expanded: boolean;
  readonly detail: OpsRestoreDetail | null;
  readonly detailFailed: boolean;
  readonly busyTargetIds: readonly string[];
  readonly onToggleExpand: () => void;
  readonly onRetryDetail: () => void;
  readonly onRetryTarget: (target: OpsRepairTarget) => void;
}

function RestoreRow({
  restore,
  gridColumns,
  expanded,
  detail,
  detailFailed,
  busyTargetIds,
  onToggleExpand,
  onRetryDetail,
  onRetryTarget,
}: RestoreRowProps) {
  const copyBackups = copy.admin.operations.backups;
  return (
    <li className="border-t border-[var(--color-hairline)] first:border-t-0">
      <div role="row" className={`grid ${gridColumns} items-center gap-x-4 px-4 py-3`}>
        <span role="cell" className="min-w-0 truncate font-mono text-[15px] text-ash-gray">
          {restore.restore_id}
        </span>
        <span role="cell" className="min-w-0 truncate font-mono text-[15px] text-ash-gray">
          {restore.backup_id}
        </span>
        <span role="cell">
          <span className="rounded-[var(--radius-buttons)] bg-mist-gray px-2 py-1 text-caption text-ink-black">
            {copyBackups.restoreStatus(restore.status)}
          </span>
        </span>
        <span role="cell" className="text-[15px] text-slate-gray">
          {formatDateTime(restore.created_at)}
        </span>
        <span role="cell" className="text-[15px] text-slate-gray">
          {restore.completed_at === null ? '—' : formatDateTime(restore.completed_at)}
        </span>
        <span role="cell" className="flex items-center justify-end">
          <TextLink onClick={onToggleExpand}>
            {expanded ? copyBackups.detailCollapse : copyBackups.progressExpand}
          </TextLink>
        </span>
      </div>
      {expanded && (
        <div className="flex flex-col gap-2 px-4 pb-3">
          {detailFailed && <ErrorState text={copyBackups.detailLoadError} onRetry={onRetryDetail} />}
          {!detailFailed && detail === null && <LoadingRows count={1} />}
          {!detailFailed && detail !== null && (
            <>
              <p className="text-[14px] font-medium text-ink-black">{copyBackups.stagesTitle}</p>
              {detail.stages.map((stage) => (
                <p key={stage.stage} className="text-[14px] text-slate-gray">
                  {copyBackups.stageLabel(stage.stage)}
                  {' · '}
                  {copyBackups.stageStatus(stage.status)}
                </p>
              ))}
              {detail.failure_reason !== null && detail.failure_reason !== undefined && (
                <p className="text-[14px] text-danger">
                  {copyBackups.restoreFailure(detail.failure_reason)}
                </p>
              )}
              {detail.repair_targets.length > 0 && (
                <>
                  <p className="text-[14px] font-medium text-ink-black">{copyBackups.repairTitle}</p>
                  {detail.repair_targets.map((target) => (
                    <div key={target.target_id} className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 text-[14px] text-slate-gray">
                        {copyBackups.stageLabel(target.stage)}
                        {' · '}
                        <span className="font-mono text-[13px]">{target.resource_id}</span>
                        {' · '}
                        {copyBackups.repairStatus(target.status)}
                        {' · '}
                        {copyBackups.repairFailure(target.failure_classification)}
                      </span>
                      {target.status === 'open' && (
                        <Pill
                          variant="ghost"
                          size="xs"
                          loading={busyTargetIds.includes(target.target_id)}
                          onClick={() => onRetryTarget(target)}
                        >
                          {copyBackups.repairRetry}
                        </Pill>
                      )}
                    </div>
                  ))}
                </>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}

/* ---------- 「策略」分段：schedule 启停 / 周期 / 时区 / retention（版本化保存） ---------- */

interface PolicyFormState {
  readonly enabled: boolean;
  readonly frequency: BackupPolicyFrequency;
  readonly localTime: string;
  readonly weekdays: readonly number[];
  readonly timezone: string;
  readonly keepLast: string;
  readonly retentionDays: string;
}

function formFromPolicy(policy: BackupPolicy): PolicyFormState {
  return {
    enabled: policy.enabled,
    frequency: policy.frequency,
    localTime: policy.local_time,
    weekdays: [...policy.weekdays],
    timezone: policy.timezone,
    keepLast: String(policy.keep_last),
    retentionDays: String(policy.retention_days),
  };
}

const LOCAL_TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;
const WEEKDAY_VALUES: readonly number[] = [0, 1, 2, 3, 4, 5, 6];

function PolicySegment() {
  const { api } = useAdmin();
  const [policy, setPolicy] = useState<BackupPolicy | null>(null);
  const [form, setForm] = useState<PolicyFormState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const generationRef = useRef(0);
  const saveIdem = useRef(createIdempotencyScope());
  const copyBackups = copy.admin.operations.backups;

  /** 策略读：sequence fence；写后 / 冲突后刷新复用（form 随之重置为服务端值）。 */
  const load = useCallback(
    async (silent = false): Promise<void> => {
      const gen = generationRef.current;
      if (!silent) {
        setLoading(true);
      }
      try {
        const response = await api.getOpsBackupPolicy();
        if (gen !== generationRef.current) {
          return;
        }
        setPolicy(response);
        setForm(formFromPolicy(response));
        setLoadFailed(false);
      } catch {
        if (gen === generationRef.current) {
          setLoadFailed(true);
        }
      } finally {
        if (gen === generationRef.current) {
          setLoading(false);
        }
      }
    },
    [api],
  );

  useEffect(() => {
    void load();
    return () => {
      generationRef.current += 1;
    };
  }, [load]);

  function patchForm(patch: Partial<PolicyFormState>): void {
    setForm((current) => (current === null ? current : { ...current, ...patch }));
    setFormError(null);
  }

  function toggleWeekday(value: number): void {
    if (form === null) {
      return;
    }
    patchForm({
      weekdays: form.weekdays.includes(value)
        ? form.weekdays.filter((day) => day !== value)
        : [...form.weekdays, value].sort((a, b) => a - b),
    });
  }

  async function save(): Promise<void> {
    const current = policy;
    const draft = form;
    if (current === null || draft === null || saving) {
      return;
    }
    // 客户端校验（服务端 422 兜底）：HH:MM、weekly 至少一天、正整数
    if (!LOCAL_TIME_PATTERN.test(draft.localTime)) {
      setFormError(copyBackups.policyLocalTimeInvalid);
      return;
    }
    if (draft.frequency === 'weekly' && draft.weekdays.length === 0) {
      setFormError(copyBackups.policyWeekdaysRequired);
      return;
    }
    const keepLast = Number(draft.keepLast);
    const retentionDays = Number(draft.retentionDays);
    if (
      !Number.isInteger(keepLast) ||
      keepLast < 1 ||
      !Number.isInteger(retentionDays) ||
      retentionDays < 1
    ) {
      setFormError(copyBackups.policyPositiveInteger);
      return;
    }
    setSaving(true);
    setFormError(null);
    setNotice(null);
    const input: BackupPolicyPatchInput = {
      expected_version: current.version,
      enabled: draft.enabled,
      frequency: draft.frequency,
      local_time: draft.localTime,
      weekdays: draft.weekdays,
      timezone: draft.timezone.trim(),
      keep_last: keepLast,
      retention_days: retentionDays,
    };
    // key 绑定 op/target/payload 指纹：字段或版本任一变化自动换键
    const key = saveIdem.current.keyFor('ops-backup-policy', 'singleton', JSON.stringify(input));
    try {
      const updated = await api.patchOpsBackupPolicy(input, key);
      saveIdem.current.clear();
      setPolicy(updated);
      setForm(formFromPolicy(updated));
      setNotice(copyBackups.policySaved);
    } catch (error) {
      if (isBusinessResponse(error)) {
        saveIdem.current.businessResponse();
      }
      if (error instanceof ApiError && error.status === 409 && error.code === 'version_conflict') {
        // 版本失配：刷新最新值（form 重置为服务端值），提示确认后重试，不自动重提
        setFormError(copyBackups.policyVersionConflict);
        await load(true);
      } else if (error instanceof ApiError && error.status === 422) {
        setFormError(
          error.details['field'] === 'timezone'
            ? copyBackups.policyTimezoneInvalid
            : copyBackups.policyValidationError,
        );
      } else if (error instanceof ApiError && error.status === 503 && error.code === 'maintenance_mode') {
        setFormError(copyBackups.maintenanceMode);
      } else {
        setFormError(copyBackups.actionError);
      }
    } finally {
      setSaving(false);
    }
  }

  const inputClass =
    'h-10 rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white ' +
    'px-3 text-[15px] text-ink-black placeholder:text-smoke-gray focus:border-ink-black';

  return (
    <div className="flex flex-col gap-3">
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {loading && <LoadingRows count={3} />}
      {!loading && loadFailed && (
        <ErrorState text={copyBackups.loadError} onRetry={() => void load()} />
      )}
      {!loading && !loadFailed && policy !== null && form !== null && (
        <div className="flex flex-col gap-4 rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white p-5">
          <div className="flex items-center justify-between gap-4">
            <span className="text-[15px] text-slate-gray">{copyBackups.policyEnabledLabel}</span>
            <Switch
              checked={form.enabled}
              disabled={saving}
              onCheckedChange={(checked) => patchForm({ enabled: checked })}
              ariaLabel={copyBackups.policyEnabledAria}
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-[15px] text-slate-gray">{copyBackups.policyFrequencyLabel}</span>
            <SegmentedControl
              options={[
                { value: 'daily', label: copyBackups.policyFrequencyDaily },
                { value: 'weekly', label: copyBackups.policyFrequencyWeekly },
              ]}
              value={form.frequency}
              onChange={(value) => patchForm({ frequency: value as BackupPolicyFrequency })}
              ariaLabel={copyBackups.policyFrequencyAria}
            />
          </div>
          <div>
            <label htmlFor="backup-policy-local-time" className="mb-2 block text-[15px] text-slate-gray">
              {copyBackups.policyLocalTimeLabel}
            </label>
            <input
              id="backup-policy-local-time"
              type="text"
              autoComplete="off"
              placeholder="HH:MM"
              value={form.localTime}
              disabled={saving}
              onChange={(event) => patchForm({ localTime: event.target.value })}
              className={`${inputClass} w-32`}
            />
          </div>
          {form.frequency === 'weekly' && (
            <div>
              <span className="mb-2 block text-[15px] text-slate-gray">
                {copyBackups.policyWeekdaysLabel}
              </span>
              <div className="flex flex-wrap gap-2">
                {WEEKDAY_VALUES.map((value) => (
                  <Pill
                    key={value}
                    variant={form.weekdays.includes(value) ? 'filled' : 'ghost'}
                    size="xs"
                    disabled={saving}
                    aria-pressed={form.weekdays.includes(value)}
                    onClick={() => toggleWeekday(value)}
                  >
                    {copyBackups.policyWeekday(value)}
                  </Pill>
                ))}
              </div>
            </div>
          )}
          <div>
            <label htmlFor="backup-policy-timezone" className="mb-2 block text-[15px] text-slate-gray">
              {copyBackups.policyTimezoneLabel}
            </label>
            <input
              id="backup-policy-timezone"
              type="text"
              autoComplete="off"
              placeholder={copyBackups.policyTimezonePlaceholder}
              value={form.timezone}
              disabled={saving}
              onChange={(event) => patchForm({ timezone: event.target.value })}
              className={`${inputClass} w-full`}
            />
          </div>
          <div className="flex gap-4">
            <div>
              <label htmlFor="backup-policy-keep-last" className="mb-2 block text-[15px] text-slate-gray">
                {copyBackups.policyKeepLastLabel}
              </label>
              <input
                id="backup-policy-keep-last"
                type="text"
                inputMode="numeric"
                autoComplete="off"
                value={form.keepLast}
                disabled={saving}
                onChange={(event) => patchForm({ keepLast: event.target.value })}
                className={`${inputClass} w-24`}
              />
            </div>
            <div>
              <label htmlFor="backup-policy-retention-days" className="mb-2 block text-[15px] text-slate-gray">
                {copyBackups.policyRetentionDaysLabel}
              </label>
              <input
                id="backup-policy-retention-days"
                type="text"
                inputMode="numeric"
                autoComplete="off"
                value={form.retentionDays}
                disabled={saving}
                onChange={(event) => patchForm({ retentionDays: event.target.value })}
                className={`${inputClass} w-24`}
              />
            </div>
          </div>
          {/* 规格 §5 保护式 AND：页面固定说明，不得省略 */}
          <p className="text-[14px] text-slate-gray">{copyBackups.policyRetentionNote}</p>
          <div className="flex flex-col gap-1 text-[14px] text-smoke-gray">
            <p>{copyBackups.policyVersion(policy.version)}</p>
            <p>
              {policy.next_run_at === null
                ? copyBackups.policyNextRunDisabled
                : copyBackups.policyNextRun(formatDateTime(policy.next_run_at))}
            </p>
            {policy.last_scheduled_for !== null && policy.last_scheduled_for !== undefined && (
              <p>
                {copyBackups.policyLastScheduled(formatDateTime(policy.last_scheduled_for))}
                {policy.last_outcome !== null &&
                  policy.last_outcome !== undefined &&
                  ` · ${copyBackups.policyLastOutcome(policy.last_outcome)}`}
              </p>
            )}
          </div>
          {formError !== null && (
            <p role="alert" className="text-[15px] text-danger">
              {formError}
            </p>
          )}
          <div className="flex justify-end">
            <Pill size="sm" loading={saving} onClick={() => void save()}>
              {copyBackups.policySave}
            </Pill>
          </div>
        </div>
      )}
    </div>
  );
}
