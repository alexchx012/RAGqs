/*
 * 部门库管理（settings-personal §9，仅部长且本部门 permission=manage）。
 * - 子界面与个人库文档列表同构（工具行仅搜索框 + 文档列表）；配额计数器仅知识库模块首页
 *   一处呈现，本层不重复；操作范围：上传新版本（真实 §6.4 链路）/版本记录/重建索引/删除（二次确认）；
 *   初始上传不在本层发起、不提供上传按钮。
 * - 「投稿审核」为部门库管理下的正确子层（/settings/knowledge/manage/approvals）：返回回到部门库
 *   管理并保留空间上下文；待处理计数徽标（GET /approvals/summary，为 0 不显示）。
 * - 通过：202 后行 250ms 淡出收起 + 页头下轻提示；驳回：对话框可选填单行输入框，填了随铃铛送达。
 * - 失败：duplicate_document 仅行内提示行不移除不刷新；version_conflict 刷新该行 version 后重试；
 *   submission_already_reviewed / submission_scope_changed / 投稿人冻结删除均刷新列表。
 */

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { ChevronRight } from 'lucide-react';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { formatDrawerLocation } from '../router/drawer-params';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { CountBadge } from '../ui/CountBadge';
import { EmptyState, ErrorState, LoadingRows } from '../ui/states';
import { Paginator } from '../ui/Paginator';
import { Pill } from '../ui/Pill';
import { TextLink } from '../ui/TextLink';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import { useSettings } from './SettingsProvider';
import { downloadSubmissionContent } from './download-submission-content';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import { useModalDialog } from './use-modal-dialog';
import { getManageSpaceSelection, setManageSpaceSelection } from './manage-context';
import type {
  ApprovalListItem,
  DocumentListItem,
  SpaceItem,
} from './types';
import { KnowledgeDocumentRow } from './KnowledgeModule';
import { NewVersionDialog } from './NewVersionDialog';

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const kb = bytes / 1024;
  if (kb < 1024) {
    return `${kb.toFixed(1)} KB`;
  }
  return `${(kb / 1024).toFixed(1)} MB`;
}

const PAGE_SIZE = 10;

/** 部长部门库入口：严格为 kind=department && permission=manage 的服务端返回项。 */
export function ManageLayer(_props: { readonly path: readonly string[] }) {
  const { api } = useSettings();
  const { user } = useAuthState();
  const authStore = useAuthStore();
  const navigate = useNavigate();
  const authSessionId = authStore.getAuthSessionId();
  const sessionKey = user !== null && authSessionId !== null ? `${authSessionId}:${user.id}` : null;
  const [manageSpaces, setManageSpaces] = useState<readonly SpaceItem[]>([]);
  const [spacesLoading, setSpacesLoading] = useState(true);
  const [spacesError, setSpacesError] = useState(false);
  // 部门空间选择：恢复自跨层上下文（approvals 返回保留原部门）；null 时默认第一个。
  const [selectedSpaceId, setSelectedSpaceId] = useState<string | null>(() =>
    getManageSpaceSelection(sessionKey),
  );
  const [approvalCount, setApprovalCount] = useState(0);
  const [newVersionTarget, setNewVersionTarget] = useState<DocumentListItem | null>(null);
  const spacesSeqRef = useRef(0);
  // 当前部门文档列表 reload 句柄（由 ManageDocuments 注册；NewVersionDialog 冲突刷新用，
  // 不只 reload spaces；review A2/A3）
  const docListReloadRef = useRef<(() => void) | null>(null);

  const loadManageSpaces = useCallback(async () => {
    const seq = ++spacesSeqRef.current;
    setSpacesLoading(true);
    setSpacesError(false);
    try {
      const response = await api.listManageSpaces();
      if (seq !== spacesSeqRef.current) {
        return;
      }
      const managed = response.items.filter(
        (space) => space.kind === 'department' && space.permission === 'manage',
      );
      setManageSpaces(managed);
      setSelectedSpaceId((current) => {
        const restored = getManageSpaceSelection(sessionKey);
        // 刷新后验证当前 space 仍在列表，否则回退有效 space（review A2）
        const candidate =
          restored !== null && managed.some((space) => space.id === restored)
            ? restored
            : current !== null && managed.some((space) => space.id === current)
              ? current
              : managed[0]?.id ?? null;
        if (candidate !== null && candidate !== restored) {
          setManageSpaceSelection(sessionKey, candidate);
        }
        return candidate;
      });
      const summary = await api.getApprovalSummary().catch(() => null);
      if (seq === spacesSeqRef.current) {
        setApprovalCount(summary?.submission_pending ?? 0);
      }
    } catch {
      if (seq === spacesSeqRef.current) {
        setSpacesError(true);
      }
    } finally {
      if (seq === spacesSeqRef.current) {
        setSpacesLoading(false);
      }
    }
  }, [api, sessionKey]);

  useEffect(() => {
    void loadManageSpaces();
  }, [loadManageSpaces]);

  // 选中变化：写入跨层上下文（approvals 返回时恢复）
  const selectSpace = (spaceId: string | null) => {
    setSelectedSpaceId(spaceId);
    setManageSpaceSelection(sessionKey, spaceId);
  };

  return (
    <section aria-label={copy.settings.knowledge.manage.title} className="pb-10">
      {/* 投稿审核下钻入口：部门库管理下的正确子层（返回回到本层并保留空间上下文） */}
      <div className="mb-4">
        <button
          type="button"
          onClick={() => drillApprovals()}
          className="flex h-10 w-full items-center justify-between rounded-[var(--radius-images)] px-3 text-left text-body transition-colors duration-150 hover:bg-mist-gray"
        >
          <span className="flex items-center gap-2">
            {copy.settings.knowledge.manage.approvals}
            <CountBadge count={approvalCount} />
          </span>
          <ChevronRight size={16} className="text-slate-gray" aria-hidden />
        </button>
      </div>
      {/* 部门空间切换（多部门部长场景） */}
      {manageSpaces.length > 1 && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          {manageSpaces.map((space) => (
            <button
              key={space.id}
              type="button"
              aria-pressed={selectedSpaceId === space.id}
              onClick={() => selectSpace(space.id)}
              className={`inline-flex h-8 items-center rounded-[var(--radius-buttons)] border px-3 text-[14px] ${
                selectedSpaceId === space.id
                  ? 'border-ink-black bg-mist-gray text-ink-black'
                  : 'border-[var(--color-hairline)] text-ink-black'
              }`}
            >
              {space.name}
            </button>
          ))}
        </div>
      )}
      {spacesLoading ? (
        <LoadingRows count={2} />
      ) : spacesError ? (
        <ErrorState onRetry={() => void loadManageSpaces()} />
      ) : selectedSpaceId === null ? (
        <EmptyState text={copy.states.empty} />
      ) : (
        <ManageDocuments
          spaceId={selectedSpaceId}
          onUploadNewVersion={(doc) => setNewVersionTarget(doc)}
          onReloadRequested={(reload) => {
            docListReloadRef.current = reload;
          }}
        />
      )}

      <NewVersionDialog
        target={newVersionTarget}
        onClose={() => setNewVersionTarget(null)}
        onSubmitted={() => {
          setNewVersionTarget(null);
          navigate(
            formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'uploads'] }),
          );
        }}
        onConflictRefresh={() => {
          // 普通 409：关闭旧 target，刷新当前部门文档列表（不只 reload spaces），
          // 并要求用户从最新列表重新发起
          setNewVersionTarget(null);
          docListReloadRef.current?.();
        }}
      />
    </section>
  );

  function drillApprovals() {
    navigate(
      formatDrawerLocation({
        open: true,
        segment: 'personal',
        drill: ['knowledge', 'manage', 'approvals'],
      }),
    );
  }
}

/** 部门库文档列表：与个人库同构（仅搜索框 + 文档列表；无配额计数器、无上传按钮、无初始上传入口）。 */
function ManageDocuments({
  spaceId,
  onUploadNewVersion,
  onReloadRequested,
}: {
  readonly spaceId: string;
  readonly onUploadNewVersion: (doc: DocumentListItem) => void;
  /** 注册当前文档列表 reload（供父层 NewVersionDialog 冲突刷新使用）。 */
  readonly onReloadRequested: (reload: () => void) => void;
}) {
  const { api } = useSettings();
  const navigate = useNavigate();
  const [documents, setDocuments] = useState<readonly DocumentListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState('');
  const [committedQuery, setCommittedQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<DocumentListItem | null>(null);
  const [pendingReindex, setPendingReindex] = useState<DocumentListItem | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingReindex, setConfirmingReindex] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const deleteIdem = useRef(createIdempotencyScope());
  const reindexIdem = useRef(createIdempotencyScope());
  // request sequence fence：旧响应不得覆盖新 space/query/page（review Major 2）
  const documentsSeqRef = useRef(0);
  // mutation epoch（review A2）：视图变化/Esc/取消/切换 space/query/page 即失效旧 mutation
  const mutationEpochRef = useRef(0);
  const invalidateMutations = useCallback(() => {
    mutationEpochRef.current += 1;
  }, []);

  const loadDocuments = useCallback(
    async (q: string, nextPage: number) => {
      const seq = ++documentsSeqRef.current;
      setLoading(true);
      setLoadError(false);
      try {
        const response = await api.listDocuments({ spaceId, q: q === '' ? undefined : q, page: nextPage, pageSize: PAGE_SIZE });
        if (seq !== documentsSeqRef.current) {
          return;
        }
        setDocuments(response.items);
        setTotal(response.total);
        setPage(response.page);
      } catch {
        if (seq === documentsSeqRef.current) {
          setLoadError(true);
        }
      } finally {
        if (seq === documentsSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [api, spaceId],
  );

  // 空间切换：立即作废在途响应与旧 mutation，清空旧文档列表
  useEffect(() => {
    documentsSeqRef.current += 1;
    mutationEpochRef.current += 1;
    setDocuments([]);
    setTotal(0);
    setPage(1);
    setSearchQuery('');
    setCommittedQuery('');
    setPendingDelete(null);
    setPendingReindex(null);
    setConfirmingDelete(false);
    setConfirmingReindex(false);
    deleteIdem.current.clear();
    reindexIdem.current.clear();
    void loadDocuments('', 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spaceId]);

  // Paginator 翻页：作废旧操作并释放 confirming
  const changePage = (next: number) => {
    invalidateMutations();
    setConfirmingDelete(false);
    setConfirmingReindex(false);
    void loadDocuments(committedQuery, next);
  };

  useEffect(() => {
    void loadDocuments('', 1);
  }, [loadDocuments, spaceId]);

  // 注册 reload：父层 NewVersionDialog 冲突时刷新当前 space/query/page 视图
  useEffect(() => {
    onReloadRequested(() => {
      invalidateMutations();
      void loadDocuments(committedQuery, page);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spaceId, committedQuery, page]);

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    invalidateMutations();
    setConfirmingDelete(false);
    setConfirmingReindex(false);
    setCommittedQuery(searchQuery);
    void loadDocuments(searchQuery.trim(), 1);
  };

  const confirmDelete = async () => {
    const doc = pendingDelete;
    if (doc === null || confirmingDelete) {
      return;
    }
    const epoch = mutationEpochRef.current;
    const viewGen = documentsSeqRef.current;
    setConfirmingDelete(true);
    setActionError(null);
    // key 绑定 operation+target+expected_version（review Major 3）
    const idempotencyKey = deleteIdem.current.keyFor(
      'delete-document',
      doc.id,
      `version:${doc.version}`,
    );
    try {
      await api.deleteDocument(doc.id, doc.version, idempotencyKey);
      if (epoch !== mutationEpochRef.current || viewGen !== documentsSeqRef.current) {
        return; // 旧 mutation 失效：不得覆盖新 space/query/page
      }
      deleteIdem.current.clear();
      setDocuments((items) => items.filter((item) => item.id !== doc.id));
      setTotal((value) => Math.max(0, value - 1));
      setPendingDelete(null);
    } catch (error) {
      // operation identity 先于任何共享 scope 清理（旧 mutation 的 409 不得清掉新 operation 的 key）
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      if (isBusinessResponse(error)) {
        deleteIdem.current.businessResponse();
        // 冲突：先清 pending 对象/key，再按当前视图刷新（review A2）
        setPendingDelete(null);
        if (viewGen === documentsSeqRef.current) {
          await loadDocuments(committedQuery, page);
        }
        // 刷新挂起期间可能已切换 space/query/page：await 后写 UI 前检查最新 epoch（review Medium 3）
        if (epoch === mutationEpochRef.current) {
          setActionError(copy.settings.knowledge.manage.actionError);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        if (epoch === mutationEpochRef.current) {
          setActionError(copy.settings.knowledge.manage.actionError);
        }
      }
    } finally {
      if (epoch === mutationEpochRef.current) {
        setConfirmingDelete(false);
      }
    }
  };

  const confirmReindex = async () => {
    const doc = pendingReindex;
    if (doc === null || confirmingReindex) {
      return;
    }
    const epoch = mutationEpochRef.current;
    const viewGen = documentsSeqRef.current;
    setConfirmingReindex(true);
    setActionError(null);
    const idempotencyKey = reindexIdem.current.keyFor(
      'reindex-document',
      doc.id,
      `version:${doc.version}`,
    );
    try {
      const result = await api.rebuildDocument(doc.id, doc.version, idempotencyKey);
      if (epoch !== mutationEpochRef.current || viewGen !== documentsSeqRef.current) {
        return;
      }
      reindexIdem.current.clear();
      setPendingReindex(null);
      // 部门 reindex job 进入统一 uploads 结果跟踪（review Major 5）
      void result;
      navigate(
        formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'uploads'] }),
      );
    } catch (error) {
      // operation identity 先于任何共享 scope 清理（旧 mutation 的 409 不得清掉新 operation 的 key）
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      if (isBusinessResponse(error)) {
        reindexIdem.current.businessResponse();
        setPendingReindex(null);
        if (viewGen === documentsSeqRef.current) {
          await loadDocuments(committedQuery, page);
        }
        if (epoch === mutationEpochRef.current) {
          setActionError(copy.settings.knowledge.manage.actionError);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        if (epoch === mutationEpochRef.current) {
          setActionError(copy.settings.knowledge.manage.actionError);
        }
      }
    } finally {
      if (epoch === mutationEpochRef.current) {
        setConfirmingReindex(false);
      }
    }
  };

  const drillVersions = (documentId: string) => {
    navigate(
      formatDrawerLocation({
        open: true,
        segment: 'personal',
        drill: ['knowledge', 'versions', documentId],
      }),
    );
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div>
      <form className="flex items-center gap-3" onSubmit={submitSearch} role="search">
        <input
          type="search"
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          placeholder={copy.settings.knowledge.documents.searchPlaceholder}
          aria-label={copy.settings.knowledge.documents.searchAria}
          className="h-9 w-full max-w-[280px] rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black"
        />
      </form>
      <div className="mt-4">
        {loading ? (
          <LoadingRows count={3} />
        ) : loadError ? (
          <ErrorState onRetry={() => void loadDocuments(committedQuery, page)} />
        ) : documents.length === 0 ? (
          <EmptyState text={copy.settings.knowledge.documents.empty} />
        ) : (
          <ul className="divide-y divide-[var(--color-hairline)]">
            {documents.map((doc) => (
              // KnowledgeDocumentRow 自身渲染 <li>：外层不再包 <li>（修复嵌套 li）
              <KnowledgeDocumentRow
                key={doc.id}
                doc={doc}
                manage={doc.active_operation === null}
                onUploadNewVersion={() => onUploadNewVersion(doc)}
                onVersions={() => drillVersions(doc.id)}
                onReindex={() => setPendingReindex(doc)}
                onDelete={() => setPendingDelete(doc)}
              />
            ))}
          </ul>
        )}
        {!loading && !loadError && total > PAGE_SIZE && (
          <div className="mt-6">
            <Paginator page={page} totalPages={totalPages} onChange={changePage} />
          </div>
        )}
      </div>
      {actionError !== null && (
        <p role="alert" className="mt-4 text-caption text-danger">
          {actionError}
        </p>
      )}
      <ConfirmDialog
        open={pendingDelete !== null}
        confirming={confirmingDelete}
        onOpenChange={(open) => {
          if (!open) {
            invalidateMutations();
            setConfirmingDelete(false);
            setPendingDelete(null);
          }
        }}
        title={copy.settings.knowledge.documents.deleteConfirmTitle}
        description={copy.settings.knowledge.documents.deleteConfirmDescription(pendingDelete?.name ?? '')}
        confirmLabel={copy.settings.knowledge.documents.delete}
        danger
        onConfirm={() => void confirmDelete()}
      />
      <ConfirmDialog
        open={pendingReindex !== null}
        confirming={confirmingReindex}
        onOpenChange={(open) => {
          if (!open) {
            invalidateMutations();
            setConfirmingReindex(false);
            setPendingReindex(null);
          }
        }}
        title={copy.settings.knowledge.documents.reindexConfirmTitle}
        description={copy.settings.knowledge.documents.reindexConfirmDescription(pendingReindex?.name ?? '')}
        confirmLabel={copy.settings.knowledge.documents.reindex}
        onConfirm={() => void confirmReindex()}
      />
    </div>
  );
}

/** 投稿审核下钻层：部门库管理下的正确子层（返回回部门库管理并保留空间上下文）。 */
export function ApprovalsLayer(_props: { readonly path: readonly string[] }) {
  const { api } = useSettings();
  const navigate = useNavigate();
  const [approvals, setApprovals] = useState<readonly ApprovalListItem[]>([]);
  const [summary, setSummary] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingReject, setPendingReject] = useState<ApprovalListItem | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [rejecting, setRejecting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Map<string, string>>(new Map());
  const [fading, setFading] = useState<Set<string>>(new Set());
  const decisionIdem = useRef(createIdempotencyScope());
  const [versionByRow, setVersionByRow] = useState<Map<string, number>>(new Map());
  const approvalsSeqRef = useRef(0);

  const loadApprovals = useCallback(async () => {
    const seq = ++approvalsSeqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const [list, summaryResponse] = await Promise.all([api.listApprovals(), api.getApprovalSummary()]);
      if (seq !== approvalsSeqRef.current) {
        return;
      }
      setApprovals(list.items);
      setSummary(summaryResponse.submission_pending);
      setVersionByRow(new Map(list.items.map((item) => [item.submission_id, item.version])));
    } catch {
      if (seq === approvalsSeqRef.current) {
        setLoadError(true);
      }
    } finally {
      if (seq === approvalsSeqRef.current) {
        setLoading(false);
      }
    }
  }, [api]);

  useEffect(() => {
    void loadApprovals();
  }, [loadApprovals]);

  const openContent = async (approval: ApprovalListItem) => {
    setRowErrors((errors) => {
      const next = new Map(errors);
      next.delete(approval.submission_id);
      return next;
    });
    try {
      const blob = await api.getSubmissionContent(approval.submission_id);
      downloadSubmissionContent(blob, approval.name);
    } catch (error) {
      setRowErrors((errors) => {
        const next = new Map(errors);
        next.set(
          approval.submission_id,
          error instanceof ApiError && error.status === 404
            ? copy.settings.knowledge.manage.contentUnavailable
            : copy.settings.knowledge.manage.actionError,
        );
        return next;
      });
    }
  };

  const decide = async (approval: ApprovalListItem, approved: boolean) => {
    if (rejecting) {
      return;
    }
    setRejecting(true);
    setActionError(null);
    const version = versionByRow.get(approval.submission_id) ?? approval.version;
    // key 绑定 operation+target+expected_version+payload（reject 的 reason 纳入 payload，
    // 理由变化自动换键，不会把上次驳回结果重放到新理由；review Major 3）
    const reason = rejectReason.trim() === '' ? null : rejectReason.trim();
    const payloadFingerprint = JSON.stringify({ version, approved, reason });
    const idempotencyKey = decisionIdem.current.keyFor(
      'approval-decision',
      approval.submission_id,
      payloadFingerprint,
    );
    try {
      if (approved) {
        await api.approveSubmission(approval.submission_id, version, idempotencyKey);
      } else {
        await api.rejectSubmission(approval.submission_id, version, reason, idempotencyKey);
      }
      decisionIdem.current.clear();
      setPendingReject(null);
      setRejectReason('');
      setNotice(approved ? copy.settings.knowledge.manage.approvedNotice : copy.settings.knowledge.manage.rejectedNotice);
      // 202 后行 250ms 淡出收起（--duration-base）
      setFading((current) => new Set(current).add(approval.submission_id));
      window.setTimeout(() => {
        setFading((current) => {
          const next = new Set(current);
          next.delete(approval.submission_id);
          return next;
        });
        setApprovals((items) => items.filter((item) => item.submission_id !== approval.submission_id));
        setSummary((value) => Math.max(0, value - 1));
      }, 250);
    } catch (error) {
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 version_conflict / idempotency_key_conflict）：清键，不自动重发；
        // 冲突刷新最新资源后由用户重新确认（不用旧 expected_version 重试；review Major 4）
        decisionIdem.current.businessResponse();
        if (error instanceof ApiError && error.status === 409) {
          // 任何冲突：关闭并清理 RejectDialog/reason/pending，要求用户从最新行重新确认
          //（review Major 8：不能依靠静默 version map 替换旧确认对象）
          setPendingReject(null);
          setRejectReason('');
          switch (error.code) {
            case 'version_conflict': {
              setRowErrors((errors) => {
                const next = new Map(errors);
                next.set(approval.submission_id, copy.settings.knowledge.manage.versionConflict);
                return next;
              });
              await loadApprovals();
              break;
            }
            case 'duplicate_document':
              // 仅行内提示，行不移除不刷新
              setRowErrors((errors) => {
                const next = new Map(errors);
                next.set(approval.submission_id, copy.settings.knowledge.manage.duplicateDocument);
                return next;
              });
              break;
            default:
              // submission_already_reviewed / submission_scope_changed / 投稿人冻结删除 → 刷新列表
              await loadApprovals();
              break;
          }
        } else {
          setActionError(copy.settings.knowledge.manage.actionError);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        setActionError(copy.settings.knowledge.manage.actionError);
      }
    } finally {
      setRejecting(false);
    }
  };

  // 返回：回到部门库管理（/settings/knowledge/manage），保留空间上下文由该层自身状态维持
  const goBackToManage = () => {
    navigate(
      formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'manage'] }),
    );
  };

  return (
    <section aria-label={copy.settings.knowledge.manage.approvals} className="pb-10">
      <button
        type="button"
        onClick={goBackToManage}
        className="mb-3 flex items-center gap-1 text-caption text-slate-gray transition-colors duration-150 hover:text-ink-black"
      >
        <ChevronRight size={14} className="rotate-180" aria-hidden />
        {copy.shell.drawer.modules.manage}
      </button>
      <div className="flex items-center gap-2">
        <h2 className="text-subheading font-medium text-ink-black">{copy.settings.knowledge.manage.approvals}</h2>
        <CountBadge count={summary} />
      </div>
      {notice !== null && (
        <p role="status" className="mt-3 rounded-[var(--radius-images)] bg-mist-gray px-3 py-2 text-[15px] text-slate-gray">
          {notice}
        </p>
      )}
      {actionError !== null && (
        <p role="alert" className="mt-3 text-caption text-danger">
          {actionError}
        </p>
      )}
      <div className="mt-4">
        {loading ? (
          <LoadingRows count={3} />
        ) : loadError ? (
          <ErrorState onRetry={() => void loadApprovals()} />
        ) : approvals.length === 0 ? (
          <EmptyState text={copy.settings.knowledge.manage.approvalsEmpty} />
        ) : (
          <ul className="divide-y divide-[var(--color-hairline)]">
            {approvals.map((approval) => (
              <li
                key={approval.submission_id}
                data-approval-id={approval.submission_id}
                className={`py-4 transition-opacity duration-[var(--duration-base)] ${
                  fading.has(approval.submission_id) ? 'opacity-0' : 'opacity-100'
                }`}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <p className="truncate text-body text-ink-black">{approval.name}</p>
                    <p className="mt-1 text-caption text-smoke-gray">
                      {copy.settings.knowledge.manage.submitter(approval.submitter.display_name)} ·{' '}
                      {copy.settings.knowledge.manage.fileMeta(approval.media_kind, formatBytes(approval.size_bytes))} ·{' '}
                      {copy.settings.knowledge.manage.submittedAt(formatDateTime(approval.created_at))}
                    </p>
                    {rowErrors.get(approval.submission_id) !== undefined && (
                      <p className="mt-1 text-caption text-danger">
                        {rowErrors.get(approval.submission_id)}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <TextLink onClick={() => void openContent(approval)}>
                      {copy.settings.knowledge.manage.viewContent}
                    </TextLink>
                    <Pill size="sm" loading={rejecting} onClick={() => void decide(approval, true)}>
                      {copy.settings.knowledge.manage.approve}
                    </Pill>
                    <Pill variant="ghost" size="sm" disabled={rejecting} onClick={() => setPendingReject(approval)}>
                      {copy.settings.knowledge.manage.reject}
                    </Pill>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 驳回对话框：可选填单行输入框（placeholder「可填一句原因」），填了随铃铛送达投稿人 */}
      <RejectDialog
        open={pendingReject !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingReject(null);
            setRejectReason('');
          }
        }}
        reason={rejectReason}
        onReasonChange={setRejectReason}
        pending={rejecting}
        onConfirm={() => {
          if (pendingReject !== null) {
            void decide(pendingReject, false);
          }
        }}
      />
    </section>
  );
}

interface RejectDialogProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly reason: string;
  readonly onReasonChange: (reason: string) => void;
  readonly pending: boolean;
  readonly onConfirm: () => void;
}

function RejectDialog({ open, onOpenChange, reason, onReasonChange, pending, onConfirm }: RejectDialogProps) {
  useModalDialog(open, onOpenChange);
  if (!open) {
    return null;
  }
  return (
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label={copy.settings.knowledge.manage.rejectDialogTitle}>
      <div className="fixed inset-0 bg-ink-black/24" onClick={() => onOpenChange(false)} aria-hidden="true" />
      <div className="fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]">
        <h2 className="text-[20px] font-medium text-ink-black">{copy.settings.knowledge.manage.rejectDialogTitle}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">{copy.settings.knowledge.manage.rejectDialogDescription}</p>
        <input
          type="text"
          value={reason}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder={copy.settings.knowledge.manage.rejectReasonPlaceholder}
          className="mt-4 h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black"
        />
        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={pending} onClick={() => onOpenChange(false)}>
            {copy.controls.cancel}
          </Pill>
          <Pill size="sm" loading={pending} onClick={onConfirm}>
            {copy.settings.knowledge.manage.reject}
          </Pill>
        </div>
      </div>
    </div>
  );
}
