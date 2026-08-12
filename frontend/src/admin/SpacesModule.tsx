/*
 * 知识空间（§7 管理段；验收 A12、A23–A33）。
 * - PublicSpaceLayer：公共库文档列表（§6.2 行为；行高 56、状态列 6px 点 + 15px、上传时间、用量；
 *   行操作「版本记录 / 删除 / 重建 / 上传新版本」仅服务端 permission=manage 渲染，渲染唯一依据
 *   服务端 permission 字段，前端不做角色分支；行点击新窗口打开 /preview/:id 只读形态）+
 *   图谱维护区（§6.12，仅 ops 挂载；其余角色不渲染不调用）。
 * - PersonalLibsLayer：用户列表（顶部 UserSearchBox 聚合搜索，防抖传 q）→ 行内态下钻该用户
 *   个人库只读文档列表（无上传、无行操作、页头「只读」标记；打开即记审计由后端负责）；
 *   pending_delete 行保留可见但不可点击（aria-disabled），行尾「已冻结，待清理」tag。
 * - DepartmentLibsLayer：部门列表 → 该部门库文档列表；active 按服务端 permission 渲染行操作，
 *   inactive 固定只读 + 页头「已停用 / 只读」标记；两种状态均无上传入口。
 * 投稿审核下钻复用 ApprovalsModule 的 ApprovalSubmissionsLayer（见 placeholder-modules 注册）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Lock } from 'lucide-react';
import { ApiError } from '../api/errors';
import { useAuthState } from '../auth/AuthProvider';
import { copy } from '../copy';
import { NewVersionDialog } from '../settings/NewVersionDialog';
import { createIdempotencyScope, isBusinessResponse } from '../settings/idempotency';
import { useSettings } from '../settings/SettingsProvider';
import { useModalDialog } from '../settings/use-modal-dialog';
import { VersionsLayer } from '../settings/VersionsLayer';
import type { DocumentListItem } from '../settings/types';
import {
  ConfirmDialog,
  CountBadge,
  EmptyState,
  ErrorState,
  HeaderNotice,
  LoadingRows,
  MeatballMenu,
  Pill,
  StatusDot,
  TextLink,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { UserSearchBox } from './UserSearchBox';
import { formatDateTime } from './format';
import { useAdminRead } from './use-admin-read';
import type {
  AdminDepartmentItem,
  AdminUserItem,
  GraphAvailability,
  GraphBuildCurrent,
  GraphBuildStatus,
} from './types';

/* ---------- 共享：只读 / 可管理文档列表 ---------- */

/** 行点击新窗口打开原文预览只读形态（无 message_id，命中导航为空态；审计由后端落库）。 */
function openDocumentPreview(documentId: string): void {
  window.open(`/preview/${encodeURIComponent(documentId)}`, '_blank', 'noopener,noreferrer');
}

interface AdminDocumentListProps {
  readonly spaceId: string;
  /** 行级管理操作（版本记录 / 删除 / 重建 / 上传新版本）：仅服务端 permission=manage 渲染。 */
  readonly manage: boolean;
  /** 打开版本记录层（仅 manage 行提供入口）。 */
  readonly onOpenVersions: (documentId: string) => void;
}

/**
 * 管理侧空间文档列表（§6.2 + §12.6）：行高 56、状态列 6px 点 + 15px、上传时间、用量；
 * manage=false 即只读（无行操作列）；本模块不设上传新文档入口（「上传新版本」是行级版本操作）。
 */
function AdminDocumentList({ spaceId, manage, onOpenVersions }: AdminDocumentListProps) {
  const { api } = useSettings();
  const [documents, setDocuments] = useState<readonly DocumentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<DocumentListItem | null>(null);
  const [pendingReindex, setPendingReindex] = useState<DocumentListItem | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingReindex, setConfirmingReindex] = useState(false);
  const [newVersionTarget, setNewVersionTarget] = useState<DocumentListItem | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const deleteIdem = useRef(createIdempotencyScope());
  const reindexIdem = useRef(createIdempotencyScope());
  const seqRef = useRef(0);
  // mutation epoch：对话框取消/关闭即失效在途 mutation（迟到响应不写视图）
  const mutationEpochRef = useRef(0);
  const copyDocuments = copy.settings.knowledge.documents;
  const copySpaces = copy.admin.spaces;

  const loadDocuments = useCallback(async (): Promise<void> => {
    const seq = ++seqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.listDocuments({ spaceId, page: 1, pageSize: 20 });
      if (seq !== seqRef.current) {
        return;
      }
      setDocuments(response.items);
    } catch {
      if (seq === seqRef.current) {
        setLoadError(true);
      }
    } finally {
      if (seq === seqRef.current) {
        setLoading(false);
      }
    }
  }, [api, spaceId]);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments]);

  async function confirmDelete(): Promise<void> {
    const doc = pendingDelete;
    if (doc === null || confirmingDelete) {
      return;
    }
    const epoch = mutationEpochRef.current;
    setConfirmingDelete(true);
    setActionError(null);
    const key = deleteIdem.current.keyFor('delete-document', doc.id, `version:${doc.version}`);
    try {
      await api.deleteDocument(doc.id, doc.version, key);
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      deleteIdem.current.clear();
      // 202 后立即从列表移除（§6.4：不展示 pending_delete 行）
      setDocuments((current) => current.filter((item) => item.id !== doc.id));
      setPendingDelete(null);
    } catch (error) {
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      if (isBusinessResponse(error)) {
        deleteIdem.current.businessResponse();
        setPendingDelete(null);
        await loadDocuments();
        if (epoch === mutationEpochRef.current) {
          setActionError(copy.settings.knowledge.submissions.versionConflict);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        setActionError(copy.settings.knowledge.manage.actionError);
      }
    } finally {
      if (epoch === mutationEpochRef.current) {
        setConfirmingDelete(false);
      }
    }
  }

  async function confirmReindex(): Promise<void> {
    const doc = pendingReindex;
    if (doc === null || confirmingReindex) {
      return;
    }
    const epoch = mutationEpochRef.current;
    setConfirmingReindex(true);
    setActionError(null);
    const key = reindexIdem.current.keyFor('reindex-document', doc.id, `version:${doc.version}`);
    try {
      await api.rebuildDocument(doc.id, doc.version, key);
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      reindexIdem.current.clear();
      setPendingReindex(null);
      setNotice(copySpaces.reindexStarted);
      // 202：active_operation 非空，刷新呈现「更新处理中」
      await loadDocuments();
    } catch (error) {
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      if (isBusinessResponse(error)) {
        reindexIdem.current.businessResponse();
        setPendingReindex(null);
        await loadDocuments();
        if (epoch === mutationEpochRef.current) {
          setActionError(copy.settings.knowledge.submissions.versionConflict);
        }
      } else {
        setActionError(copy.settings.knowledge.manage.actionError);
      }
    } finally {
      if (epoch === mutationEpochRef.current) {
        setConfirmingReindex(false);
      }
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {loading ? (
        <LoadingRows count={3} />
      ) : loadError ? (
        <ErrorState text={copyDocuments.loadError} onRetry={() => void loadDocuments()} />
      ) : documents.length === 0 ? (
        <EmptyState text={copyDocuments.empty} />
      ) : (
        <ul className="divide-y divide-[var(--color-hairline)]">
          {documents.map((doc) => (
            <li key={doc.id} className="group transition-colors duration-150 hover:bg-mist-gray">
              <div className="flex h-14 items-center gap-3 px-4">
                <button
                  type="button"
                  aria-label={copySpaces.openPreviewAria(doc.name)}
                  onClick={() => openDocumentPreview(doc.id)}
                  className="min-w-0 flex-1 truncate text-left text-[15px] text-ink-black underline-offset-2 hover:underline"
                >
                  {doc.name}
                </button>
                <span className="flex w-28 shrink-0 items-center gap-2 text-[15px] text-slate-gray">
                  {doc.active_operation !== null ? (
                    <>
                      <StatusDot intent="warning" pulse />
                      {copyDocuments.updating}
                    </>
                  ) : (
                    <>
                      <StatusDot intent="success" />
                      {copySpaces.docStatusAvailable}
                    </>
                  )}
                </span>
                <span className="hidden w-40 shrink-0 truncate text-[15px] text-slate-gray md:inline">
                  {formatDateTime(doc.uploaded_at)}
                </span>
                <span className="hidden w-28 shrink-0 truncate text-[15px] text-slate-gray lg:inline">
                  {copyDocuments.usageDetail(doc.usage.pages, doc.usage.images)}
                </span>
                {manage && doc.active_operation === null && (
                  <MeatballMenu
                    ariaLabel={copyDocuments.rowMenuAria(doc.name)}
                    alwaysVisible
                    items={[
                      {
                        key: 'versions',
                        label: copyDocuments.versions,
                        onSelect: () => onOpenVersions(doc.id),
                      },
                      {
                        key: 'reindex',
                        label: copyDocuments.reindex,
                        onSelect: () => setPendingReindex(doc),
                      },
                      {
                        key: 'upload-new-version',
                        label: copyDocuments.uploadNewVersion,
                        onSelect: () => setNewVersionTarget(doc),
                      },
                      {
                        key: 'delete',
                        label: copyDocuments.delete,
                        danger: true,
                        onSelect: () => setPendingDelete(doc),
                      },
                    ]}
                  />
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {actionError !== null && (
        <p role="alert" className="text-[15px] text-danger">
          {actionError}
        </p>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        confirming={confirmingDelete}
        onOpenChange={(open) => {
          if (!open) {
            mutationEpochRef.current += 1;
            setConfirmingDelete(false);
            setPendingDelete(null);
          }
        }}
        title={copyDocuments.deleteConfirmTitle}
        description={copyDocuments.deleteConfirmDescription(pendingDelete?.name ?? '')}
        confirmLabel={copyDocuments.delete}
        danger
        onConfirm={() => void confirmDelete()}
      />
      <ConfirmDialog
        open={pendingReindex !== null}
        confirming={confirmingReindex}
        onOpenChange={(open) => {
          if (!open) {
            mutationEpochRef.current += 1;
            setConfirmingReindex(false);
            setPendingReindex(null);
          }
        }}
        title={copyDocuments.reindexConfirmTitle}
        description={copyDocuments.reindexConfirmDescription(pendingReindex?.name ?? '')}
        confirmLabel={copyDocuments.reindex}
        onConfirm={() => void confirmReindex()}
      />
      <NewVersionDialog
        target={newVersionTarget}
        onClose={() => setNewVersionTarget(null)}
        onSubmitted={() => {
          setNewVersionTarget(null);
          void loadDocuments();
        }}
        onConflictRefresh={() => {
          // 普通 409：关闭旧 target，刷新列表后由用户从最新行重新发起
          setNewVersionTarget(null);
          void loadDocuments();
        }}
      />
    </div>
  );
}

/** 页头「只读」标记（16px 锁图标 + 15px slate）。 */
function ReadOnlyBadge({ label }: { readonly label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[15px] text-slate-gray">
      <Lock aria-hidden="true" className="h-4 w-4" />
      {label}
    </span>
  );
}

/** 下钻页头：后退入口（←）+ 标题 + 右侧标记。 */
function DrillHeader({
  backLabel,
  onBack,
  title,
  badge,
}: {
  readonly backLabel: string;
  readonly onBack: () => void;
  readonly title: string;
  readonly badge?: string | null;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          onClick={onBack}
          className="flex shrink-0 items-center gap-1 text-[15px] text-slate-gray transition-colors duration-150 hover:text-ink-black"
        >
          <ArrowLeft size={14} aria-hidden />
          {backLabel}
        </button>
        <h2 className="truncate text-[20px] font-medium text-ink-black">{title}</h2>
      </div>
      {badge != null && <ReadOnlyBadge label={badge} />}
    </div>
  );
}

/* ---------- 公共库图谱维护区（§6.12，仅 ops 挂载） ---------- */

const GRAPH_POLL_MS = 5000;

const AVAILABILITY_INTENT: Record<GraphAvailability, 'success' | 'warning' | 'slate'> = {
  ready: 'success',
  stale: 'warning',
  disabled: 'slate',
};

function availabilityLabel(availability: GraphAvailability): string {
  const graph = copy.admin.spaces.graph;
  switch (availability) {
    case 'ready':
      return graph.availabilityReady;
    case 'stale':
      return graph.availabilityStale;
    default:
      return graph.availabilityDisabled;
  }
}

function graphStatusLabel(status: GraphBuildStatus): string {
  const graph = copy.admin.spaces.graph;
  switch (status) {
    case 'queued':
      return graph.statusQueued;
    case 'running':
      return graph.statusRunning;
    case 'succeeded':
      return graph.statusSucceeded;
    case 'failed':
      return graph.statusFailed;
    default:
      return graph.statusCancelled;
  }
}

function isRunNonTerminal(status: GraphBuildStatus): boolean {
  return status === 'queued' || status === 'running';
}

function GraphBuildSection() {
  const { api } = useAdmin();
  const [current, setCurrent] = useState<GraphBuildCurrent | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [acting, setActing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [sectionError, setSectionError] = useState<string | null>(null);
  /** 503 graph_build_estimate_unavailable：保留状态，错误行 + 重试 TextLink（重新打开发起确认层）。 */
  const [estimateRetry, setEstimateRetry] = useState(false);
  const idem = useRef(createIdempotencyScope());
  const seqRef = useRef(0);
  const graph = copy.admin.spaces.graph;

  /** 状态投影读：sequence fence 防旧响应覆盖；silent=true（轮询/写后刷新）不闪骨架。 */
  const loadProjection = useCallback(
    async (silent = false): Promise<void> => {
      const seq = ++seqRef.current;
      if (!silent) {
        setLoading(true);
      }
      setLoadError(false);
      try {
        const projection = await api.getCurrentGraphBuild();
        if (seq !== seqRef.current) {
          return;
        }
        setCurrent(projection);
      } catch {
        if (seq === seqRef.current) {
          setLoadError(true);
        }
      } finally {
        if (seq === seqRef.current) {
          setLoading(false);
        }
      }
    },
    [api],
  );

  useEffect(() => {
    void loadProjection();
  }, [loadProjection]);

  const latestRun = current?.latest_run ?? null;
  const nonTerminal = latestRun !== null && isRunNonTerminal(latestRun.status);

  // 非终态期间 5s 轮询；终态 / 卸载 / 离开层清除
  useEffect(() => {
    if (!nonTerminal) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadProjection(true);
    }, GRAPH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [nonTerminal, loadProjection]);

  async function startBuild(): Promise<void> {
    const projection = current;
    if (projection === null || acting) {
      return;
    }
    setActing(true);
    setSectionError(null);
    setEstimateRetry(false);
    // key 绑定 op/target/source_revision：revision 变化自动换键
    const key = idem.current.keyFor('graph-build', projection.space_id, `${projection.source_revision}`);
    try {
      await api.createGraphBuild(projection.source_revision, key);
      idem.current.clear();
      setConfirmOpen(false);
      setNotice(graph.startedNotice);
      // 提交成功后开始轮询（最新投影为非终态 run）
      await loadProjection(true);
    } catch (error) {
      if (isBusinessResponse(error)) {
        idem.current.businessResponse();
      }
      if (error instanceof ApiError && error.status === 409 && error.code === 'graph_source_changed') {
        // 内容 revision 冲突：刷新状态并要求重新确认（不自动重提）
        setConfirmOpen(false);
        setSectionError(graph.sourceChanged);
        await loadProjection(true);
      } else if (error instanceof ApiError && error.status === 409 && error.code === 'graph_build_in_progress') {
        setConfirmOpen(false);
        setSectionError(graph.inProgress);
        await loadProjection(true);
      } else if (error instanceof ApiError && error.status === 422 && error.code === 'graph_source_empty') {
        setConfirmOpen(false);
        setSectionError(graph.sourceEmpty);
      } else if (
        error instanceof ApiError &&
        error.status === 503 &&
        error.code === 'graph_build_estimate_unavailable'
      ) {
        // 预估不可用：保留当前状态，允许稍后重新发起（错误行 + 重试 TextLink）
        setConfirmOpen(false);
        setSectionError(graph.estimateUnavailable);
        setEstimateRetry(true);
      } else {
        setSectionError(graph.actionError);
      }
    } finally {
      setActing(false);
    }
  }

  async function cancelBuild(): Promise<void> {
    const run = current?.latest_run ?? null;
    if (run === null || acting) {
      return;
    }
    setActing(true);
    setSectionError(null);
    setEstimateRetry(false);
    // key 绑定 run id + expected_version：状态转换递增 version 自动换键
    const key = idem.current.keyFor('graph-cancel', run.graph_build_id, `${run.version}`);
    try {
      await api.cancelGraphBuild(run.graph_build_id, run.version, key);
      idem.current.clear();
      setNotice(graph.cancelledNotice);
      await loadProjection(true);
    } catch (error) {
      if (isBusinessResponse(error)) {
        idem.current.businessResponse();
      }
      if (error instanceof ApiError && error.status === 409 && error.code === 'graph_build_not_cancellable') {
        // 刷新状态投影：取消入口随刷新后的 allowed_actions 收起
        setSectionError(graph.notCancellable);
        await loadProjection(true);
      } else if (error instanceof ApiError && error.status === 409 && error.code === 'version_conflict') {
        setSectionError(graph.runVersionConflict);
        await loadProjection(true);
      } else {
        setSectionError(graph.actionError);
      }
    } finally {
      setActing(false);
    }
  }

  const cancellable = latestRun !== null && latestRun.allowed_actions.includes('cancel');
  const buildLabel = current?.graph_availability === 'disabled' ? graph.buildCreate : graph.buildRebuild;

  return (
    <section
      aria-label={graph.title}
      className="flex flex-col gap-2 rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white p-5"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-[15px] font-medium text-ink-black">{graph.title}</h3>
        <TextLink onClick={() => void loadProjection(true)}>{copy.admin.common.refresh}</TextLink>
      </div>
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {sectionError !== null && (
        <div className="flex items-center gap-2">
          <p role="alert" className="text-[15px] text-danger">
            {sectionError}
          </p>
          {estimateRetry && (
            <TextLink
              onClick={() => {
                setSectionError(null);
                setEstimateRetry(false);
                setConfirmOpen(true);
              }}
            >
              {copy.states.retry}
            </TextLink>
          )}
        </div>
      )}
      {loading ? (
        <LoadingRows count={1} />
      ) : loadError ? (
        <ErrorState text={graph.loadError} onRetry={() => void loadProjection()} />
      ) : current !== null ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2 text-[15px] text-slate-gray">
            <StatusDot intent={AVAILABILITY_INTENT[current.graph_availability]} />
            {availabilityLabel(current.graph_availability)}
            {' · '}
            {graph.sourceRevision(current.source_revision)}
          </div>
          {/* 仅 ready 呈现 generation 可用信息；stale 标注已过期，不展示为可用态 */}
          {current.graph_availability === 'ready' && current.active_generation !== null && (
            <p className="text-[14px] text-slate-gray">
              {graph.generationInfo(
                current.active_generation.graph_generation_id,
                formatDateTime(current.active_generation.built_at),
              )}
            </p>
          )}
          {current.graph_availability === 'stale' && current.active_generation !== null && (
            <p className="text-[14px] text-smoke-gray">{graph.generationExpired}</p>
          )}
          {latestRun === null ? (
            <p className="text-[14px] text-smoke-gray">{graph.empty}</p>
          ) : (
            <div className="flex flex-col gap-1">
              <p className="text-[14px] text-slate-gray">
                {graph.latestRunTitle}
                {' · '}
                {graphStatusLabel(latestRun.status)}
                {' · '}
                {graph.sourceRevision(latestRun.source_revision)}
                {' · '}
                {graph.estimatedCalls(latestRun.estimated_primary_model_calls)}
              </p>
              {latestRun.actual_usage !== null && (
                <p className="text-[14px] text-slate-gray">
                  {graph.actualCalls(
                    latestRun.actual_usage.primary_model_calls,
                    latestRun.actual_usage.provider_calls,
                  )}
                </p>
              )}
              <p className="text-[14px] text-smoke-gray">
                {graph.runCreatedAt(formatDateTime(latestRun.created_at))}
                {latestRun.started_at !== null && ` · ${graph.runStartedAt(formatDateTime(latestRun.started_at))}`}
                {latestRun.finished_at !== null && ` · ${graph.runFinishedAt(formatDateTime(latestRun.finished_at))}`}
              </p>
              {latestRun.failure_class !== null && (
                <p className="text-[14px] text-danger">{graph.failureClass(latestRun.failure_class)}</p>
              )}
            </div>
          )}
          <div className="mt-1 flex items-center gap-2">
            {/* 发起：disabled / stale / ready 三态在无非终态 run 时均可发起构建 / 重建 */}
            {!nonTerminal && (
              <Pill size="sm" loading={acting && confirmOpen} onClick={() => setConfirmOpen(true)}>
                {buildLabel}
              </Pill>
            )}
            {cancellable && (
              <Pill size="sm" variant="ghost" loading={acting && !confirmOpen} onClick={() => void cancelBuild()}>
                {graph.cancel}
              </Pill>
            )}
          </div>
        </div>
      ) : null}

      <GraphBuildConfirmDialog
        open={confirmOpen && current !== null}
        availability={current?.graph_availability ?? 'disabled'}
        sourceRevision={current?.source_revision ?? 0}
        referenceEstimate={latestRun?.estimated_primary_model_calls ?? null}
        confirming={acting}
        onOpenChange={(open) => {
          if (!open) {
            setConfirmOpen(false);
            idem.current.clear();
          }
        }}
        onConfirm={() => void startBuild()}
      />
    </section>
  );
}

interface GraphBuildConfirmDialogProps {
  readonly open: boolean;
  readonly availability: GraphAvailability;
  readonly sourceRevision: number;
  /** 上次 run 的预估主模型调用（参考；无 run 时显示服务端提交后计算说明）。 */
  readonly referenceEstimate: number | null;
  readonly confirming: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly onConfirm: () => void;
}

/** 发起确认层（400px）：展示当前 source_revision 与预估主模型调用参考，确认后才提交。 */
function GraphBuildConfirmDialog({
  open,
  availability,
  sourceRevision,
  referenceEstimate,
  confirming,
  onOpenChange,
  onConfirm,
}: GraphBuildConfirmDialogProps) {
  const graph = copy.admin.spaces.graph;
  const title = availability === 'disabled' ? graph.confirmTitleCreate : graph.confirmTitleRebuild;
  const dialogRef = useModalDialog(open, onOpenChange);
  if (!open) {
    return null;
  }
  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="fixed inset-0 bg-ink-black/24" onClick={() => onOpenChange(false)} aria-hidden="true" />
      <div className="fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]">
        <h2 className="text-[20px] font-medium text-ink-black">{title}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">{graph.confirmDescription}</p>
        <p className="mt-4 text-[15px] text-ink-black">{graph.confirmRevision(sourceRevision)}</p>
        <p className="mt-1 text-[15px] text-slate-gray">
          {referenceEstimate !== null ? graph.confirmEstimate(referenceEstimate) : graph.confirmEstimatePending}
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={confirming} onClick={() => onOpenChange(false)}>
            {copy.controls.cancel}
          </Pill>
          <Pill size="sm" loading={confirming} disabled={confirming} onClick={onConfirm}>
            {graph.confirmStart}
          </Pill>
        </div>
      </div>
    </div>
  );
}

/* ---------- 公共库（§7.2 ops / §7.3 admin；行操作按服务端 permission） ---------- */

export function PublicSpaceLayer() {
  const { api: settingsApi } = useSettings();
  const { user } = useAuthState();
  const [versionsDocId, setVersionsDocId] = useState<string | null>(null);
  const [listEpoch, setListEpoch] = useState(0);
  // 行操作渲染唯一依据服务端 permission（GET /spaces?usage=manage 返回项），前端不做角色分支
  const spacesRead = useAdminRead(() => settingsApi.listManageSpaces(), [settingsApi, listEpoch]);
  const publicSpace = spacesRead.data?.items.find((space) => space.id === 'public') ?? null;
  const manage = publicSpace?.permission === 'manage';
  const copyDocuments = copy.settings.knowledge.documents;

  if (versionsDocId !== null) {
    return (
      <div className="flex flex-col gap-3 pb-10">
        <DrillHeader
          backLabel={copy.shell.drawer.modules.publicSpace}
          onBack={() => {
            setVersionsDocId(null);
            setListEpoch((epoch) => epoch + 1);
          }}
          title={copy.settings.knowledge.versions.title}
        />
        {/* 复用 settings 版本记录下钻层（同一 api 与呈现规则）；path[2] 为 documentId */}
        <VersionsLayer path={['spaces', 'versions', versionsDocId]} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 pb-10">
      <h2 className="text-[20px] font-medium text-ink-black">{copy.shell.drawer.modules.publicSpace}</h2>
      {/* 图谱维护区仅 ops 挂载（其余角色不渲染不调用；深链由后端 403 graph_build_forbidden） */}
      {user?.role === 'ops' && <GraphBuildSection />}
      {spacesRead.loading ? (
        <LoadingRows count={3} />
      ) : spacesRead.error ? (
        <ErrorState text={copyDocuments.loadError} onRetry={spacesRead.reload} />
      ) : (
        <AdminDocumentList
          key={`public:${listEpoch}`}
          spaceId="public"
          manage={manage}
          onOpenVersions={(documentId) => setVersionsDocId(documentId)}
        />
      )}
    </div>
  );
}

/* ---------- 用户个人库（§7.3：用户列表 → 只读文档列表） ---------- */

const USER_SEARCH_DEBOUNCE_MS = 300;

export function PersonalLibsLayer() {
  const { api } = useAdmin();
  const [searchValue, setSearchValue] = useState('');
  const [query, setQuery] = useState('');
  const [selectedUser, setSelectedUser] = useState<AdminUserItem | null>(null);
  const copySpaces = copy.admin.spaces;

  // 实时过滤：前端只防抖传 q（聚合匹配姓名 / 显示名 / 用户名 / 部门名 / 角色名在服务端）
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(searchValue.trim()), USER_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchValue]);

  const read = useAdminRead(
    () => api.listUsers({ q: query === '' ? undefined : query, page: 1, pageSize: 50 }),
    [api, query],
  );

  if (selectedUser !== null) {
    return (
      <div className="flex flex-col gap-3 pb-10">
        <DrillHeader
          backLabel={copySpaces.backToUsers}
          onBack={() => setSelectedUser(null)}
          title={copySpaces.personalLibOf(selectedUser.display_name)}
          badge={copy.admin.common.readOnly}
        />
        <AdminDocumentList
          key={`personal:${selectedUser.id}`}
          spaceId={`personal:${selectedUser.id}`}
          manage={false}
          onOpenVersions={() => undefined}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 pb-10">
      <h2 className="text-[20px] font-medium text-ink-black">{copySpaces.personalLibs}</h2>
      <UserSearchBox
        value={searchValue}
        onChange={setSearchValue}
        placeholder={copySpaces.userSearchPlaceholder}
        ariaLabel={copySpaces.userSearchAria}
      />
      {read.loading ? (
        <LoadingRows count={3} />
      ) : read.error ? (
        <ErrorState text={copySpaces.loadError} onRetry={read.reload} />
      ) : read.data !== null && read.data.items.length === 0 ? (
        <EmptyState text={copySpaces.emptyUsers} />
      ) : read.data !== null ? (
        <ul className="divide-y divide-[var(--color-hairline)]">
          {read.data.items.map((item) => {
            const frozen = item.lifecycle_status === 'pending_delete';
            const rowContent = (
              <>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[15px] text-ink-black">
                    {item.display_name}
                    <span className="ml-2 text-[14px] text-smoke-gray">{item.username}</span>
                  </p>
                  <p className="mt-0.5 truncate text-[14px] text-slate-gray">
                    {item.department?.name ?? copy.admin.users.noDepartment}
                    {' · '}
                    {copy.admin.common.roleLabels[item.role]}
                    {' · '}
                    {copySpaces.documents(item.document_count)}
                  </p>
                </div>
                {frozen && (
                  <span className="shrink-0 text-[14px] text-ash-gray">{copy.admin.common.frozenTag}</span>
                )}
              </>
            );
            // 冻结行保留可见但不可点击：无 hover、无跳转、aria-disabled
            if (frozen) {
              return (
                <li key={item.id} aria-disabled="true" className="flex items-center gap-3 px-4 py-4">
                  {rowContent}
                </li>
              );
            }
            return (
              <li key={item.id} className="transition-colors duration-150 hover:bg-mist-gray">
                <button
                  type="button"
                  onClick={() => setSelectedUser(item)}
                  className="flex w-full items-center gap-3 px-4 py-4 text-left"
                >
                  {rowContent}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

/* ---------- 部门库（§7.3：部门列表 → 文档列表；inactive 固定只读） ---------- */

export function DepartmentLibsLayer() {
  const { api } = useAdmin();
  const read = useAdminRead(() => api.listDepartments('all'), [api]);
  const [selectedDepartment, setSelectedDepartment] = useState<AdminDepartmentItem | null>(null);
  const copySpaces = copy.admin.spaces;
  const copyDepartments = copy.admin.departments;

  if (selectedDepartment !== null) {
    return (
      <DepartmentDocumentsView
        key={selectedDepartment.id}
        department={selectedDepartment}
        onBack={() => setSelectedDepartment(null)}
      />
    );
  }

  return (
    <div className="flex flex-col gap-3 pb-10">
      <h2 className="text-[20px] font-medium text-ink-black">{copySpaces.departmentLibs}</h2>
      {read.loading ? (
        <LoadingRows count={3} />
      ) : read.error ? (
        <ErrorState text={copySpaces.loadError} onRetry={read.reload} />
      ) : read.data !== null && read.data.items.length === 0 ? (
        <EmptyState text={copySpaces.empty} />
      ) : read.data !== null ? (
        <ul className="divide-y divide-[var(--color-hairline)]">
          {read.data.items.map((department) => (
            <li key={department.id} className="transition-colors duration-150 hover:bg-mist-gray">
              <button
                type="button"
                onClick={() => setSelectedDepartment(department)}
                className="flex w-full items-center gap-3 px-4 py-4 text-left"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[15px] text-ink-black">{department.name}</p>
                  <p className="mt-0.5 truncate text-[14px] text-slate-gray">
                    {copySpaces.members(department.member_count)}
                    {' · '}
                    {copySpaces.documents(department.document_count)}
                  </p>
                </div>
                <span className="flex shrink-0 items-center gap-2 text-[14px] text-slate-gray">
                  {department.status === 'active' ? (
                    copyDepartments.statusActive
                  ) : (
                    <>
                      <StatusDot intent="slate" />
                      {copyDepartments.statusInactive}
                    </>
                  )}
                  {department.pending_submission_count > 0 && (
                    <CountBadge count={department.pending_submission_count} />
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** 部门库文档下钻：active 按服务端 permission 渲染行操作；inactive 固定只读 + 页头标记。 */
function DepartmentDocumentsView({
  department,
  onBack,
}: {
  readonly department: AdminDepartmentItem;
  readonly onBack: () => void;
}) {
  const { api: settingsApi } = useSettings();
  const [versionsDocId, setVersionsDocId] = useState<string | null>(null);
  const [listEpoch, setListEpoch] = useState(0);
  const spaceId = `department:${department.id}`;
  const active = department.status === 'active';
  // active 部门的行操作唯一依据服务端 permission；inactive 固定只读（不请求 permission）
  const spacesRead = useAdminRead(
    () => settingsApi.listManageSpaces(),
    [settingsApi, listEpoch],
  );
  const space = spacesRead.data?.items.find((item) => item.id === spaceId) ?? null;
  const manage = active && space?.permission === 'manage';
  const copySpaces = copy.admin.spaces;

  if (versionsDocId !== null) {
    return (
      <div className="flex flex-col gap-3 pb-10">
        <DrillHeader
          backLabel={department.name}
          onBack={() => {
            setVersionsDocId(null);
            setListEpoch((epoch) => epoch + 1);
          }}
          title={copy.settings.knowledge.versions.title}
        />
        <VersionsLayer path={['spaces', 'versions', versionsDocId]} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 pb-10">
      <DrillHeader
        backLabel={copySpaces.backToDepartments}
        onBack={onBack}
        title={department.name}
        badge={
          active
            ? manage
              ? null
              : copy.admin.common.readOnly
            : copy.admin.common.deactivatedReadOnly
        }
      />
      {active && spacesRead.loading ? (
        <LoadingRows count={3} />
      ) : active && spacesRead.error ? (
        <ErrorState text={copy.settings.knowledge.documents.loadError} onRetry={spacesRead.reload} />
      ) : (
        <AdminDocumentList
          key={`${spaceId}:${listEpoch}`}
          spaceId={spaceId}
          manage={manage}
          onOpenVersions={(documentId) => setVersionsDocId(documentId)}
        />
      )}
    </div>
  );
}
