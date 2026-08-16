/*
 * 审批中心（§8；验收 A2、A20–A22、A34）。
 * - QuotaRequestsLayer（§8.2–8.3，仅 ops 可达）：按申请时间正序（mock 已正序，前端不重排）；
 *   四列（申请人 / 当前用量 / 申请量 / 申请时间）+ 批准 filled / 驳回 ghost 小 pill；
 *   批准对话框可选 approved_pages（1–requested_pages 整数，缺省 = 申请量；非法红边 + 15px danger 说明）；
 *   驳回为无输入框的危险确认浮层；成功行 250ms 淡出 + 页头下成功绿轻提示 + invalidateSummaries；
 *   409 version_conflict 刷新后对话框内顶部说明重新确认，already_processed / quota_request_not_approvable
 *   刷新列表 + 页头说明；idempotency_key_conflict 不换键不自动重发（scope 管）。
 * - ApprovalSubmissionsLayer（§8.4–8.5，ops 仅公共库 / admin 公共库+全部部门库）：
 *   最小行（文件名 / 类型 / 投稿时间）+ 查看内容 TextLink +
 *   通过 filled / 驳回 ghost 小 pill（驳回对话框 400px 可选单行原因）；通过 202 行淡出；
 *   duplicate_document 仅行内提示不移除不刷新；version_conflict 刷新该行 version 后重试；
 *   submission_already_reviewed / scope_changed / 投稿人冻结删除均刷新列表。
 *   后端按当前角色决定审核范围，前端不附加范围筛选——抽屉注册表两处均挂载本组件且无 props。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { downloadSubmissionContent } from '../settings/download-submission-content';
import { createIdempotencyScope, isBusinessResponse } from '../settings/idempotency';
import { useSettings } from '../settings/SettingsProvider';
import { useModalDialog } from '../settings/use-modal-dialog';
import type { ApprovalListItem } from '../settings/types';
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  HeaderNotice,
  LoadingRows,
  Pill,
  TextLink,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { formatDateTime } from './format';
import type { QuotaRequestItem } from './types';

/* ---------- 配额申请（§8.2–8.3） ---------- */

interface QuotaAction {
  readonly kind: 'approve' | 'reject';
  readonly request: QuotaRequestItem;
}

export function QuotaRequestsLayer() {
  const { api, invalidateSummaries } = useAdmin();
  const [items, setItems] = useState<readonly QuotaRequestItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingAction, setPendingAction] = useState<QuotaAction | null>(null);
  const [approvePages, setApprovePages] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);
  const [fading, setFading] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const [headerNote, setHeaderNote] = useState<string | null>(null);
  const [dialogNote, setDialogNote] = useState<string | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const idem = useRef(createIdempotencyScope());
  const seqRef = useRef(0);
  const copyApprovals = copy.admin.approvals;

  /** 读序列：generation fence；成功返回最新行（供 409 后定位刷新行），失败/过期返回 null。 */
  const loadRequests = useCallback(async (): Promise<readonly QuotaRequestItem[] | null> => {
    const seq = ++seqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.listQuotaRequests('pending');
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
  }, [api]);

  useEffect(() => {
    void loadRequests();
  }, [loadRequests]);

  function closeDialog(): void {
    setPendingAction(null);
    setApprovePages('');
    setDialogNote(null);
    setDialogError(null);
    idem.current.clear();
  }

  /** 成功流转：行 250ms 淡出收起后移除（--duration-base）。 */
  function fadeAndRemove(requestId: string): void {
    setFading((current) => new Set(current).add(requestId));
    window.setTimeout(() => {
      setFading((current) => {
        const next = new Set(current);
        next.delete(requestId);
        return next;
      });
      setItems((current) => current.filter((item) => item.id !== requestId));
    }, 250);
  }

  function openAction(action: QuotaAction): void {
    setDialogNote(null);
    setDialogError(null);
    setApprovePages('');
    setPendingAction(action);
  }

  async function confirm(): Promise<void> {
    const action = pendingAction;
    if (action === null || confirming) {
      return;
    }
    const { kind, request } = action;
    // expected_version 取本地最新行（版本变化由 409 刷新带入）；approved_pages 缺省 = 申请量
    const latest = items.find((item) => item.id === request.id);
    const version = latest?.version ?? request.version;
    let approvedPages: number | null = null;
    if (kind === 'approve' && approvePages.trim() !== '') {
      const parsed = Number(approvePages);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > request.requested_pages) {
        // 保留原始非法输入，不静默改写；对话框即时校验已拦截，此处兜底
        return;
      }
      approvedPages = parsed;
    }
    setConfirming(true);
    setActingId(request.id);
    setDialogNote(null);
    setDialogError(null);
    // key 绑定 op/target/payload 指纹（version + approved_pages）：变更换键；网络未知同键同体重试
    const key = idem.current.keyFor(
      `quota-${kind}`,
      request.id,
      JSON.stringify({ version, approvedPages }),
    );
    try {
      if (kind === 'approve') {
        await api.approveQuotaRequest(request.id, version, approvedPages, key);
        setNotice(copyApprovals.approvedNotice);
      } else {
        await api.rejectQuotaRequest(request.id, version, key);
        setNotice(copyApprovals.rejectedNotice);
      }
      idem.current.clear();
      closeDialog();
      invalidateSummaries();
      fadeAndRemove(request.id);
    } catch (error) {
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 idempotency_key_conflict）：清键，不自动重发
        idem.current.businessResponse();
      }
      if (error instanceof ApiError && error.status === 409) {
        switch (error.code) {
          case 'version_conflict': {
            // 拉最新列表刷新该申请 version：仍在 → 对话框内顶部说明并换最新行重新确认；
            // 已消失（他人已处理）→ 关对话框 + 页头说明
            const freshItems = await loadRequests();
            const fresh = freshItems?.find((item) => item.id === request.id) ?? null;
            if (fresh !== null) {
              setPendingAction({ kind, request: fresh });
              setDialogNote(copyApprovals.versionConflict);
            } else {
              closeDialog();
              setHeaderNote(copyApprovals.alreadyProcessed);
            }
            break;
          }
          case 'already_processed':
            closeDialog();
            setHeaderNote(copyApprovals.alreadyProcessed);
            await loadRequests();
            break;
          case 'quota_request_not_approvable':
            closeDialog();
            setHeaderNote(copyApprovals.notApprovable);
            await loadRequests();
            break;
          default:
            // idempotency_key_conflict 与其余 409：不换键不自动重发，对话框内说明
            setDialogError(copyApprovals.actionError);
            break;
        }
      } else {
        // 网络未知/超时：复用同键同体，由用户显式重试
        setDialogError(copyApprovals.actionError);
      }
    } finally {
      setConfirming(false);
      setActingId(null);
    }
  }

  return (
    <section aria-label={copyApprovals.quota} className="flex flex-col gap-3 pb-10">
      <h2 className="text-[20px] font-medium text-ink-black">{copyApprovals.quota}</h2>
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {headerNote !== null && (
        <HeaderNotice message={headerNote} onDismiss={() => setHeaderNote(null)} />
      )}
      {loading ? (
        <LoadingRows count={4} />
      ) : loadError ? (
        <ErrorState text={copyApprovals.loadError} onRetry={() => void loadRequests()} />
      ) : items.length === 0 ? (
        <EmptyState text={copyApprovals.empty} />
      ) : (
        <ul className="divide-y divide-[var(--color-hairline)]">
          {items.map((request) => {
            const acting = actingId === request.id;
            return (
              <li
                key={request.id}
                aria-busy={acting || undefined}
                className={`transition-opacity duration-[var(--duration-base)] ${
                  fading.has(request.id) ? 'opacity-0' : 'opacity-100'
                }`}
              >
                <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,0.8fr)_minmax(0,1.2fr)_auto] items-center gap-3 px-4 py-4 transition-colors duration-150 hover:bg-mist-gray">
                  <span className="truncate text-[15px] text-ink-black">
                    {request.applicant.display_name}
                  </span>
                  <span className="truncate text-[15px] text-slate-gray">
                    {copyApprovals.usageOf(
                      request.current_usage.used,
                      request.current_usage.effective_limit,
                    )}
                  </span>
                  <span className="truncate text-[15px] font-medium text-ink-black">
                    {copyApprovals.pages(request.requested_pages)}
                  </span>
                  <span className="truncate text-[15px] text-slate-gray">
                    {formatDateTime(request.created_at)}
                  </span>
                  <div className="flex items-center gap-2">
                    <Pill
                      size="sm"
                      disabled={acting}
                      onClick={() => openAction({ kind: 'approve', request })}
                    >
                      {copyApprovals.approve}
                    </Pill>
                    <Pill
                      size="sm"
                      variant="ghost"
                      disabled={acting}
                      onClick={() => openAction({ kind: 'reject', request })}
                    >
                      {copyApprovals.reject}
                    </Pill>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <QuotaApproveDialog
        action={pendingAction !== null && pendingAction.kind === 'approve' ? pendingAction : null}
        pages={approvePages}
        onPagesChange={setApprovePages}
        note={dialogNote}
        error={dialogError}
        confirming={confirming}
        onOpenChange={(open) => {
          if (!open) {
            closeDialog();
          }
        }}
        onConfirm={() => void confirm()}
      />

      {/* 驳回：小型确认浮层（不附说明、无输入框）；确认 danger filled / 取消 ghost */}
      <ConfirmDialog
        open={pendingAction !== null && pendingAction.kind === 'reject'}
        onOpenChange={(open) => {
          if (!open) {
            closeDialog();
          }
        }}
        title={copyApprovals.rejectDialogTitle}
        description={copyApprovals.rejectDialogDescription}
        confirmLabel={copyApprovals.reject}
        danger
        confirming={confirming}
        onConfirm={() => void confirm()}
      />
    </section>
  );
}

interface QuotaApproveDialogProps {
  readonly action: QuotaAction | null;
  readonly pages: string;
  readonly onPagesChange: (value: string) => void;
  /** 对话框内顶部说明（409 version_conflict 刷新后重新确认）。 */
  readonly note: string | null;
  /** 对话框内底部错误行（15px danger）。 */
  readonly error: string | null;
  readonly confirming: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly onConfirm: () => void;
}

/** 批准对话框（400px 模态）：可选 approved_pages（缺省 = 申请量；非法红边 + 15px danger 说明）。 */
function QuotaApproveDialog({
  action,
  pages,
  onPagesChange,
  note,
  error,
  confirming,
  onOpenChange,
  onConfirm,
}: QuotaApproveDialogProps) {
  const open = action !== null;
  const dialogRef = useModalDialog(open, onOpenChange);
  if (action === null) {
    return null;
  }
  const requested = action.request.requested_pages;
  const parsed = Number(pages);
  // 保留原始非法输入（不静默改写）：非空且非 1–requested 整数即非法，空 = 按申请量
  const valueInvalid = pages.trim() !== '' && (!Number.isInteger(parsed) || parsed < 1 || parsed > requested);
  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={copy.admin.approvals.approveDialogTitle}
    >
      <div className="fixed inset-0 bg-ink-black/24" onClick={() => onOpenChange(false)} aria-hidden="true" />
      <div className="fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]">
        <h2 className="text-[20px] font-medium text-ink-black">{copy.admin.approvals.approveDialogTitle}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">
          {copy.admin.approvals.approveDialogDescription(action.request.applicant.display_name, requested)}
        </p>
        {note !== null && (
          <p role="status" className="mt-3 rounded-[var(--radius-images)] bg-mist-gray px-3 py-2 text-[15px] text-slate-gray">
            {note}
          </p>
        )}
        <div className="mt-4">
          <label htmlFor="quota-approve-pages" className="mb-2 block text-[15px] text-slate-gray">
            {copy.admin.approvals.approvePagesLabel}
          </label>
          <input
            id="quota-approve-pages"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={pages}
            onChange={(event) => onPagesChange(event.target.value)}
            placeholder={copy.admin.approvals.approvePagesPlaceholder(requested)}
            aria-invalid={valueInvalid}
            className={`h-10 w-full rounded-[var(--radius-inputs)] border bg-paper-white px-3 text-[15px] text-ink-black placeholder:text-smoke-gray focus:border-ink-black ${
              valueInvalid ? 'border-danger' : 'border-[var(--color-hairline)]'
            }`}
          />
          {valueInvalid && (
            <p role="alert" className="mt-2 text-[15px] text-danger">
              {copy.admin.approvals.approvePagesInvalid(requested)}
            </p>
          )}
          {error !== null && (
            <p role="alert" className="mt-2 text-[15px] text-danger">
              {error}
            </p>
          )}
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={confirming} onClick={() => onOpenChange(false)}>
            {copy.controls.cancel}
          </Pill>
          <Pill size="sm" loading={confirming} disabled={confirming || valueInvalid} onClick={onConfirm}>
            {copy.controls.confirm}
          </Pill>
        </div>
      </div>
    </div>
  );
}

/* ---------- 投稿审核（§8.4–8.5） ---------- */

export function ApprovalSubmissionsLayer() {
  const { api, invalidateSummaries } = useAdmin();
  const { api: settingsApi } = useSettings();
  const [items, setItems] = useState<readonly ApprovalListItem[]>([]);
  const [versionByRow, setVersionByRow] = useState<ReadonlyMap<string, number>>(new Map());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingReject, setPendingReject] = useState<ApprovalListItem | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [actingId, setActingId] = useState<string | null>(null);
  const [fading, setFading] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<ReadonlyMap<string, string>>(new Map());
  const idem = useRef(createIdempotencyScope());
  const seqRef = useRef(0);
  const copyManage = copy.settings.knowledge.manage;
  const copyApprovals = copy.admin.approvals;

  const loadSubmissions = useCallback(async (): Promise<void> => {
    const seq = ++seqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.listApprovalSubmissions();
      if (seq !== seqRef.current) {
        return;
      }
      setItems(response.items);
      setVersionByRow(new Map(response.items.map((item) => [item.submission_id, item.version])));
    } catch {
      if (seq === seqRef.current) {
        setLoadError(true);
      }
    } finally {
      if (seq === seqRef.current) {
        setLoading(false);
      }
    }
  }, [api]);

  useEffect(() => {
    void loadSubmissions();
  }, [loadSubmissions]);

  function setRowError(submissionId: string, message: string | null): void {
    setRowErrors((current) => {
      const next = new Map(current);
      if (message === null) {
        next.delete(submissionId);
      } else {
        next.set(submissionId, message);
      }
      return next;
    });
  }

  /** 查看内容：下载原件；不可用时保留行内提示。 */
  async function viewContent(item: ApprovalListItem): Promise<void> {
    setRowError(item.submission_id, null);
    try {
      const blob = await settingsApi.getSubmissionContent(item.submission_id);
      downloadSubmissionContent(blob, item.file_name);
    } catch (error) {
      setRowError(
        item.submission_id,
        error instanceof ApiError && error.status === 404
          ? copyManage.contentUnavailable
          : copyManage.actionError,
      );
    }
  }

  function fadeAndRemove(submissionId: string): void {
    setFading((current) => new Set(current).add(submissionId));
    window.setTimeout(() => {
      setFading((current) => {
        const next = new Set(current);
        next.delete(submissionId);
        return next;
      });
      setItems((current) => current.filter((item) => item.submission_id !== submissionId));
    }, 250);
  }

  async function decide(item: ApprovalListItem, approved: boolean): Promise<void> {
    if (actingId !== null) {
      return;
    }
    setActingId(item.submission_id);
    setActionError(null);
    setRowError(item.submission_id, null);
    const version = versionByRow.get(item.submission_id) ?? item.version;
    // reject 的 reason 纳入 payload 指纹：理由变化自动换键，不重放上次结果到新理由
    const reason = rejectReason.trim() === '' ? null : rejectReason.trim();
    const key = idem.current.keyFor(
      'approval-decision',
      item.submission_id,
      JSON.stringify({ version, approved, reason }),
    );
    try {
      if (approved) {
        await api.approveSubmission(item.submission_id, version, key);
        setNotice(copyManage.approvedNotice);
      } else {
        await api.rejectSubmission(item.submission_id, version, reason, key);
        setNotice(copyManage.rejectedNotice);
      }
      idem.current.clear();
      setPendingReject(null);
      setRejectReason('');
      invalidateSummaries();
      // 通过 202 / 驳回 200：行 250ms 淡出收起
      fadeAndRemove(item.submission_id);
    } catch (error) {
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 version_conflict / idempotency_key_conflict）：清键，不自动重发；
        // 冲突刷新最新资源后由用户重新确认（不用旧 expected_version 重试）
        idem.current.businessResponse();
        if (error instanceof ApiError && error.status === 409) {
          // 任何冲突：关闭并清理 RejectDialog/reason/pending，要求用户从最新行重新确认
          setPendingReject(null);
          setRejectReason('');
          switch (error.code) {
            case 'version_conflict':
              // 刷新该行最新 version（随列表刷新带入）后允许重试
              setRowError(item.submission_id, copyManage.versionConflict);
              await loadSubmissions();
              break;
            case 'duplicate_document':
              // 仅行内提示，行不移除不刷新（投稿保持待审）
              setRowError(item.submission_id, copyManage.duplicateDocument);
              break;
            default:
              // submission_already_reviewed / submission_scope_changed / 投稿人冻结删除 → 刷新列表
              setRowError(item.submission_id, copyManage.scopeChanged);
              await loadSubmissions();
              break;
          }
        } else {
          setActionError(copyManage.actionError);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        setActionError(copyManage.actionError);
      }
    } finally {
      setActingId(null);
    }
  }

  return (
    <section aria-label={copyApprovals.submissions} className="flex flex-col gap-3 pb-10">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-[20px] font-medium text-ink-black">{copyApprovals.submissions}</h2>
      </div>
      {notice !== null && (
        <HeaderNotice intent="success" message={notice} onDismiss={() => setNotice(null)} />
      )}
      {actionError !== null && (
        <p role="alert" className="text-[15px] text-danger">
          {actionError}
        </p>
      )}
      {loading ? (
        <LoadingRows count={4} />
      ) : loadError ? (
        <ErrorState text={copyManage.approvalsError} onRetry={() => void loadSubmissions()} />
      ) : items.length === 0 ? (
        <EmptyState text={copyManage.approvalsEmpty} />
      ) : (
        <ul className="divide-y divide-[var(--color-hairline)]">
          {items.map((item) => {
            const acting = actingId === item.submission_id;
            return (
              <li
                key={item.submission_id}
                aria-busy={acting || undefined}
                className={`transition-opacity duration-[var(--duration-base)] ${
                  fading.has(item.submission_id) ? 'opacity-0' : 'opacity-100'
                }`}
              >
                <div className="grid grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)_auto] items-center gap-3 px-4 py-4 transition-colors duration-150 hover:bg-mist-gray">
                  <div className="min-w-0">
                    <p className="truncate text-[15px] text-ink-black">{item.file_name}</p>
                    <p className="mt-0.5 truncate text-[14px] text-smoke-gray">
                      {item.media_kind}
                    </p>
                  </div>
                  <span className="truncate text-[15px] text-slate-gray">
                    {formatDateTime(item.created_at)}
                  </span>
                  <div className="flex items-center gap-2">
                    <TextLink disabled={acting} onClick={() => void viewContent(item)}>
                      {copyManage.viewContent}
                    </TextLink>
                    <Pill
                      size="sm"
                      loading={acting}
                      onClick={() => void decide(item, true)}
                    >
                      {copyManage.approve}
                    </Pill>
                    <Pill
                      size="sm"
                      variant="ghost"
                      disabled={acting}
                      onClick={() => {
                        setRejectReason('');
                        setPendingReject(item);
                      }}
                    >
                      {copyManage.reject}
                    </Pill>
                  </div>
                </div>
                {rowErrors.get(item.submission_id) !== undefined && (
                  <p role="alert" className="px-4 pb-3 text-[15px] text-danger">
                    {rowErrors.get(item.submission_id)}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {/* 驳回对话框（400px）：可选填单行原因，填了随铃铛送达投稿人 */}
      <SubmissionRejectDialog
        open={pendingReject !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingReject(null);
            setRejectReason('');
          }
        }}
        reason={rejectReason}
        onReasonChange={setRejectReason}
        pending={actingId !== null}
        onConfirm={() => {
          if (pendingReject !== null) {
            void decide(pendingReject, false);
          }
        }}
      />
    </section>
  );
}

interface SubmissionRejectDialogProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly reason: string;
  readonly onReasonChange: (reason: string) => void;
  readonly pending: boolean;
  readonly onConfirm: () => void;
}

/** 投稿驳回对话框（与 ManageLayer RejectDialog 同规格：400px + 可选单行原因）。 */
function SubmissionRejectDialog({
  open,
  onOpenChange,
  reason,
  onReasonChange,
  pending,
  onConfirm,
}: SubmissionRejectDialogProps) {
  const dialogRef = useModalDialog(open, onOpenChange);
  if (!open) {
    return null;
  }
  const copyManage = copy.settings.knowledge.manage;
  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={copyManage.rejectDialogTitle}
    >
      <div className="fixed inset-0 bg-ink-black/24" onClick={() => onOpenChange(false)} aria-hidden="true" />
      <div className="fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]">
        <h2 className="text-[20px] font-medium text-ink-black">{copyManage.rejectDialogTitle}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">{copyManage.rejectDialogDescription}</p>
        <input
          type="text"
          value={reason}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder={copyManage.rejectReasonPlaceholder}
          className="mt-4 h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-[15px] text-ink-black placeholder:text-smoke-gray focus:border-ink-black"
        />
        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={pending} onClick={() => onOpenChange(false)}>
            {copy.controls.cancel}
          </Pill>
          <Pill size="sm" loading={pending} onClick={onConfirm}>
            {copyManage.reject}
          </Pill>
        </div>
      </div>
    </div>
  );
}
