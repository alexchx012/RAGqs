/*
 * 知识库模块首页（settings-personal §4；共用基座 §5.6）。
 * - 顶行：无边框「上传结果」（进行中任务带数量徽标）+「我的投稿」（user/minister）；
 *   右侧配额计数器一行小字（未满 slate / 耗尽整行危险红，仅耗尽时出现「申请增加页数」；
 *   unlimited 显示「不限」；pending_request 常驻行）。
 * - 配额申请对话框（仅 user/minister）：1–500 整数校验，保留原始非法输入（-1/1.5 不静默改写），
 *   非法值即时显示危险红边 + 15px 提示，空/非法禁用确认键；201 后关闭并淡入常驻行；
 *   409 pending_request_exists 提示；写操作携带 Idempotency-Key（网络未知重试同键，
 *   明确业务响应清键不自动重发，含 idempotency_key_conflict）。
 * - 文档列表：消费服务端 spaces 返回的实际 space_id（个人库 = kind=personal && permission=manage；
 *   不拼 personal:${user.id}）；工具行=搜索框 + 上传按钮；manage 行操作：
 *   上传新版本（真实 §6.4 链路）/版本记录/重建索引/删除；active_operation 非空时隐藏全部冲突入口。
 * - 部长入口严格为 kind=department && permission=manage（右栏 48px 下钻项）。
 * - 异步读取带 request sequence fence：旧响应不得覆盖新 query/space 结果。
 */

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ChevronRight } from 'lucide-react';
import { useNavigate } from 'react-router';
import { ApiError } from '../api/errors';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import { copy } from '../copy';
import { formatDrawerLocation } from '../router/drawer-params';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { MeatballMenu } from '../ui/MeatballMenu';
import { Paginator } from '../ui/Paginator';
import { Pill } from '../ui/Pill';
import { StatusDot } from '../ui/StatusDot';
import { TextLink } from '../ui/TextLink';
import { EmptyState, ErrorState, LoadingRows } from '../ui/states';
import { useSettings } from './SettingsProvider';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import { useModalDialog } from './use-modal-dialog';
import type { DocumentListItem, QuotaSnapshot, SpaceItem } from './types';
import { UploadDialog } from './UploadDialog';
import { readUploadHistory, subscribeUploadHistory } from './upload-history';
import { NewVersionDialog } from './NewVersionDialog';

const PAGE_SIZE = 10;

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

/** 顶行「上传结果」按钮的进行中数量徽标：会话内存档里最近一批仍在 pending 的入库任务数。 */
function pendingUploadCount(sessionKey: string | null): number {
  const entry = readUploadHistory(sessionKey);
  if (entry === null) {
    return 0;
  }
  return entry.response.items.filter((item) => 'filename' in item && item.status === 'pending').length;
}

/** 个人库 = kind=personal && permission=manage（消费服务端返回项，不拼 id）。 */
function personalSpaceOf(items: readonly SpaceItem[]): SpaceItem | null {
  return items.find((space) => space.kind === 'personal' && space.permission === 'manage') ?? null;
}

/** 部长部门库入口：严格为 kind=department && permission=manage。 */
function manageDepartmentSpacesOf(items: readonly SpaceItem[]): SpaceItem[] {
  return items.filter((space) => space.kind === 'department' && space.permission === 'manage');
}

export function KnowledgeModule() {
  const { api } = useSettings();
  const { user } = useAuthState();
  const authStore = useAuthStore();
  const navigate = useNavigate();
  const role = user?.role ?? 'user';
  const isMember = role === 'user' || role === 'minister';
  const isMinister = role === 'minister';
  const authSessionId = authStore.getAuthSessionId();
  const sessionKey = user !== null && authSessionId !== null ? `${authSessionId}:${user.id}` : null;

  const [quota, setQuota] = useState<QuotaSnapshot | null>(null);
  const [, setQuotaLoading] = useState(true);
  const [quotaError, setQuotaError] = useState(false);

  const [documents, setDocuments] = useState<readonly DocumentListItem[]>([]);
  const [documentTotal, setDocumentTotal] = useState(0);
  const [documentPage, setDocumentPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState('');
  const [committedQuery, setCommittedQuery] = useState('');
  const [documentsLoading, setDocumentsLoading] = useState(true);
  const [documentsError, setDocumentsError] = useState(false);

  const [requestDialogOpen, setRequestDialogOpen] = useState(false);
  const [requestPages, setRequestPages] = useState('');
  const [requestInvalid, setRequestInvalid] = useState(false);
  const [requestPending, setRequestPending] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const requestIdem = useRef(createIdempotencyScope());

  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [newVersionTarget, setNewVersionTarget] = useState<DocumentListItem | null>(null);

  const [pendingDeleteDoc, setPendingDeleteDoc] = useState<DocumentListItem | null>(null);
  const [pendingReindexDoc, setPendingReindexDoc] = useState<DocumentListItem | null>(null);

  const [uploadPendingCount, setUploadPendingCount] = useState(() => pendingUploadCount(sessionKey));
  useEffect(() => {
    const recount = () => setUploadPendingCount(pendingUploadCount(sessionKey));
    recount();
    return subscribeUploadHistory(recount);
  }, [sessionKey]);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingReindex, setConfirmingReindex] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const deleteIdem = useRef(createIdempotencyScope());
  const reindexIdem = useRef(createIdempotencyScope());
  // mutation epoch（review A1）：视图变化/Esc/取消/切换查询/分页时递增，
  // 立即使已启动的旧 mutation 失效——迟到成功/409 不得 setDocuments/total，不得用旧 query refresh。
  const mutationEpochRef = useRef(0);
  const invalidateMutations = useCallback(() => {
    mutationEpochRef.current += 1;
  }, []);

  const [manageSpaces, setManageSpaces] = useState<SpaceItem[]>([]);

  // 个人库与部长部门库空间：都来自服务端 spaces 返回项（Major2：不拼 personal:${user.id}）。
  const [personalSpace, setPersonalSpace] = useState<SpaceItem | null>(null);

  // ---- request sequence fence（B5）：旧响应不得覆盖新 query/space 结果 ----
  const spacesSeqRef = useRef(0);
  const quotaSeqRef = useRef(0);
  const documentsSeqRef = useRef(0);

  const loadSpaces = useCallback(async () => {
    const seq = ++spacesSeqRef.current;
    try {
      const response = await api.listUploadSpaces();
      if (seq !== spacesSeqRef.current) {
        return;
      }
      const personal = personalSpaceOf(response.items);
      setPersonalSpace(personal);
      if (isMinister) {
        const managed = manageDepartmentSpacesOf(response.items);
        setManageSpaces(managed);
      }
    } catch {
      // 空间加载失败：列表保持错误态由 listDocuments 呈现
    }
  }, [api, isMinister]);

  const loadQuota = useCallback(async () => {
    const seq = ++quotaSeqRef.current;
    setQuotaLoading(true);
    setQuotaError(false);
    try {
      const snapshot = await api.getQuota();
      if (seq === quotaSeqRef.current) {
        setQuota(snapshot);
      }
    } catch {
      if (seq === quotaSeqRef.current) {
        setQuotaError(true);
      }
    } finally {
      if (seq === quotaSeqRef.current) {
        setQuotaLoading(false);
      }
    }
  }, [api]);

  const loadDocuments = useCallback(
    async (q: string, page: number) => {
      const seq = ++documentsSeqRef.current;
      if (personalSpace === null) {
        setDocuments([]);
        setDocumentTotal(0);
        setDocumentsLoading(false);
        return;
      }
      setDocumentsLoading(true);
      setDocumentsError(false);
      try {
        const result = await api.listDocuments({
          spaceId: personalSpace.id,
          q: q === '' ? undefined : q,
          page,
          pageSize: PAGE_SIZE,
        });
        if (seq !== documentsSeqRef.current) {
          return;
        }
        setDocuments(result.items);
        setDocumentTotal(result.total);
        setDocumentPage(result.page);
      } catch {
        if (seq === documentsSeqRef.current) {
          setDocumentsError(true);
        }
      } finally {
        if (seq === documentsSeqRef.current) {
          setDocumentsLoading(false);
        }
      }
    },
    [api, personalSpace],
  );

  useEffect(() => {
    void loadSpaces();
    void loadQuota();
  }, [loadSpaces, loadQuota]);

  useEffect(() => {
    void loadDocuments('', 1);
  }, [loadDocuments]);

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    invalidateMutations();
    // 视图切换：作废旧操作并释放旧 confirming（受控 Dialog 不会自动回调 onOpenChange）
    setConfirmingDelete(false);
    setConfirmingReindex(false);
    setCommittedQuery(searchQuery);
    void loadDocuments(searchQuery.trim(), 1);
  };

  const changePage = (page: number) => {
    invalidateMutations();
    setConfirmingDelete(false);
    setConfirmingReindex(false);
    void loadDocuments(committedQuery, page);
  };

  // ---- 配额申请（保留原始输入：-1/1.5 不静默改写；非法即时提示） ----
  const onRequestPagesChange = (value: string) => {
    setRequestPages(value);
    setRequestInvalid(false);
    setRequestError(null);
  };

  const submitQuotaRequest = async () => {
    if (requestPending) {
      return;
    }
    const pages = Number(requestPages);
    if (!Number.isInteger(pages) || pages < 1 || pages > 500) {
      setRequestInvalid(true);
      return;
    }
    setRequestPending(true);
    setRequestError(null);
    // key 绑定 operation+target(pages)+payload：网络未知复用同键，明确业务响应清键
    const payloadFingerprint = String(pages);
    const idempotencyKey = requestIdem.current.keyFor('quota-request', 'me', payloadFingerprint);
    try {
      await api.requestQuota(pages, idempotencyKey);
      requestIdem.current.clear();
      setRequestDialogOpen(false);
      setRequestPages('');
      await loadQuota();
    } catch (error) {
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 idempotency_key_conflict / pending_request_exists）：清键，不自动重发
        requestIdem.current.businessResponse();
        if (error instanceof ApiError && error.status === 409 && error.code === 'pending_request_exists') {
          setRequestError(copy.settings.knowledge.quota.pendingRequestExists);
        } else {
          setRequestError(copy.settings.knowledge.quota.requestError);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        setRequestError(copy.settings.knowledge.quota.requestError);
      }
    } finally {
      setRequestPending(false);
    }
  };

  // ---- 文档行操作（key 绑定 document+expected_version；未知重试同键；业务响应清键） ----
  const confirmDelete = async () => {
    const doc = pendingDeleteDoc;
    if (doc === null || confirmingDelete) {
      return;
    }
    // mutation 启动时捕获 view generation：视图变化/取消即失效，迟到响应不得写旧视图
    const epoch = mutationEpochRef.current;
    const viewGen = documentsSeqRef.current;
    setConfirmingDelete(true);
    setActionError(null);
    const idempotencyKey = deleteIdem.current.keyFor(
      'delete-document',
      doc.id,
      `version:${doc.version}`,
    );
    try {
      await api.deleteDocument(doc.id, doc.version, idempotencyKey);
      if (epoch !== mutationEpochRef.current || viewGen !== documentsSeqRef.current) {
        return; // 旧 mutation 已失效：不 setDocuments/total，不触碰新确认框
      }
      deleteIdem.current.clear();
      // 202 后立即从列表移除（§4：不展示 pending_delete 行）
      setDocuments((items) => items.filter((item) => item.id !== doc.id));
      setDocumentTotal((total) => Math.max(0, total - 1));
      setPendingDeleteDoc(null);
    } catch (error) {
      // operation identity 先于任何共享 scope 清理：旧 mutation 的 409 不得清掉
      // 新 operation（B）的幂等 key（review Medium：A 关闭后 B 启动、A 迟到 409）
      if (epoch !== mutationEpochRef.current) {
        return;
      }
      if (isBusinessResponse(error)) {
        deleteIdem.current.businessResponse();
        // 陈旧状态（version_conflict / document_operation_in_progress / document_pending_delete）：
        // 先关闭并清理 pending 确认意图与 key，再按当前 query/page/generation 刷新
        setPendingDeleteDoc(null);
        if (viewGen === documentsSeqRef.current) {
          await loadDocuments(committedQuery, documentPage);
        }
        // 刷新挂起期间可能已切换视图：每个 await 后的 UI 写入都检查最新 epoch（review Medium 3）
        if (epoch === mutationEpochRef.current) {
          setActionError(
            error instanceof ApiError && error.status === 409
              ? copy.settings.knowledge.submissions.versionConflict
              : copy.settings.knowledge.manage.actionError,
          );
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
    const doc = pendingReindexDoc;
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
      await api.rebuildDocument(doc.id, doc.version, idempotencyKey);
      if (epoch !== mutationEpochRef.current || viewGen !== documentsSeqRef.current) {
        return;
      }
      reindexIdem.current.clear();
      setPendingReindexDoc(null);
      // 重建索引任务同层呈现：下钻上传结果层查看进度
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
        setPendingReindexDoc(null);
        if (viewGen === documentsSeqRef.current) {
          await loadDocuments(committedQuery, documentPage);
        }
        // 刷新挂起期间可能已切换视图：await 后写 UI 前检查最新 epoch
        if (epoch === mutationEpochRef.current) {
          setActionError(
            error instanceof ApiError && error.status === 409
              ? copy.settings.knowledge.submissions.versionConflict
              : copy.settings.knowledge.manage.actionError,
          );
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

  const openUpload = () => {
    // 全角色唯一上传入口：统一经 UploadDialog，不预填目标（子界面也不提供独立上传按钮）
    setUploadDialogOpen(true);
  };

  const openNewVersion = (doc: DocumentListItem) => {
    // 上传新版本：固定目标（document_id），单文件对话框
    setNewVersionTarget(doc);
  };

  const totalPages = Math.max(1, Math.ceil(documentTotal / PAGE_SIZE));

  return (
    <section aria-label={copy.settings.knowledge.sectionLabel} className="pb-10">
      {/* 顶行（共用基座 §5.6）：左=无边框「上传结果」（进行中任务带数量徽标）+「我的投稿」（user/minister）；
          右=配额计数器一行小字（未满 slate / 耗尽整行危险红；运维与超管显示「不限」） */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => drillUploads()}
            className="inline-flex items-center gap-1.5 text-[15px] text-ink-black underline-offset-2 hover:underline"
          >
            {copy.settings.knowledge.uploads.historyEntry}
            {uploadPendingCount > 0 && (
              <span className="inline-flex items-center rounded-[var(--radius-buttons)] bg-mist-gray px-1.5 text-[12px] font-w480 text-ink-black">
                {uploadPendingCount}
              </span>
            )}
          </button>
          {isMember && (
            <button
              type="button"
              onClick={() => drillSubmissions()}
              className="text-[15px] text-ink-black underline-offset-2 hover:underline"
            >
              {copy.settings.knowledge.submissions.entry}
            </button>
          )}
        </div>
        <div className="flex flex-col items-end">
          {quotaError ? (
            <div className="flex items-center gap-2">
              <p className="text-[15px] text-danger">{copy.states.error}</p>
              <TextLink onClick={() => void loadQuota()}>{copy.states.retry}</TextLink>
            </div>
          ) : quota !== null ? (
            quota.unlimited ? (
              <p className="text-[15px] text-slate-gray">{copy.settings.knowledge.quota.unlimited}</p>
            ) : (
              <p
                className={
                  quota.used >= quota.effective_limit
                    ? 'text-[15px] text-danger'
                    : 'text-[15px] text-slate-gray'
                }
              >
                {copy.settings.knowledge.quota.usedOfLimit(quota.used, quota.effective_limit)}
              </p>
            )
          ) : null}
          {isMember && quota !== null && !quota.unlimited && quota.used >= quota.effective_limit && (
            <Pill
              variant="ghost"
              ghostBorder="ink"
              size="xs"
              className="mt-2"
              onClick={() => setRequestDialogOpen(true)}
            >
              {copy.settings.knowledge.quota.requestMore}
            </Pill>
          )}
          {quota !== null && quota.pending_request !== null && (
            <p className="mt-2 text-caption text-slate-gray">{copy.settings.knowledge.quota.pendingRequest}</p>
          )}
        </div>
      </div>

      {/* 部长：部门库管理下钻项（右栏带下级菜单的项，整行 48px + ›；无 manage 空间不渲染） */}
      {isMinister && manageSpaces.length > 0 && (
        <button
          type="button"
          onClick={() => drillManage()}
          className="mt-6 flex h-12 w-full items-center justify-between rounded-[var(--radius-images)] px-3 text-left transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray"
        >
          <span className="text-body text-ink-black">{copy.settings.knowledge.manage.title}</span>
          <ChevronRight aria-hidden="true" className="h-4 w-4 text-slate-gray" />
        </button>
      )}

      {/* 工具行（共用基座 §5.6）：搜索框占满剩余宽度 + 右侧上传按钮（全角色唯一上传入口） */}
      <div className="mt-4 flex items-center justify-between gap-4">
        <form className="flex min-w-0 flex-1 items-center gap-3" onSubmit={submitSearch} role="search">
          <input
            type="search"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder={copy.settings.knowledge.documents.searchPlaceholder}
            aria-label={copy.settings.knowledge.documents.searchAria}
            className="h-9 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black"
          />
        </form>
        <Pill size="sm" onClick={openUpload}>
          {copy.settings.knowledge.upload.button}
        </Pill>
      </div>

      {/* 文档列表 */}
      <div className="mt-6">
        {documentsLoading ? (
          <LoadingRows count={3} />
        ) : documentsError ? (
          <ErrorState onRetry={() => void loadDocuments(committedQuery, documentPage)} />
        ) : documents.length === 0 ? (
          <EmptyState text={copy.settings.knowledge.documents.empty} />
        ) : (
          <ul className="divide-y divide-[var(--color-hairline)]">
            {documents.map((doc) => (
              <KnowledgeDocumentRow
                key={doc.id}
                doc={doc}
                manage={doc.active_operation === null}
                onUploadNewVersion={() => openNewVersion(doc)}
                onVersions={() => drillVersions(doc.id)}
                onReindex={() => setPendingReindexDoc(doc)}
                onDelete={() => setPendingDeleteDoc(doc)}
              />
            ))}
          </ul>
        )}
        {!documentsLoading && !documentsError && documentTotal > PAGE_SIZE && (
          <div className="mt-6">
            <Paginator page={documentPage} totalPages={totalPages} onChange={changePage} />
          </div>
        )}
      </div>

      {actionError !== null && (
        <p role="alert" className="mt-4 text-caption text-danger">
          {actionError}
        </p>
      )}

      {/* 配额申请对话框：模态 400px；保留原始非法输入；非法值即时危险红边 + 15px 提示；空/非法禁用确认键 */}
      <QuotaRequestDialog
        open={requestDialogOpen}
        onOpenChange={(open) => {
          setRequestDialogOpen(open);
          if (!open) {
            setRequestInvalid(false);
            setRequestError(null);
            requestIdem.current.clear();
          }
        }}
        pages={requestPages}
        onPagesChange={onRequestPagesChange}
        invalid={requestInvalid}
        pending={requestPending}
        error={requestError}
        onConfirm={() => void submitQuotaRequest()}
      />

      <UploadDialog open={uploadDialogOpen} onOpenChange={setUploadDialogOpen} sessionKey={sessionKey} />

      <NewVersionDialog
        target={newVersionTarget}
        onClose={() => setNewVersionTarget(null)}
        onSubmitted={() => {
          setNewVersionTarget(null);
          drillUploads();
        }}
        onConflictRefresh={() => {
          // 普通 409（version_conflict / document_operation_in_progress / document_pending_delete）：
          // 关闭/清空旧 target（用户从最新列表重新发起），并刷新当前文档列表
          invalidateMutations();
          setNewVersionTarget(null);
          void loadDocuments(committedQuery, documentPage);
        }}
      />

      <ConfirmDialog
        open={pendingDeleteDoc !== null}
        confirming={confirmingDelete}
        onOpenChange={(open) => {
          if (!open) {
            // 取消/关闭：立即释放 confirming（不能锁死下次确认框），并作废旧 mutation
            invalidateMutations();
            setConfirmingDelete(false);
            setPendingDeleteDoc(null);
          }
        }}
        title={copy.settings.knowledge.documents.deleteConfirmTitle}
        description={copy.settings.knowledge.documents.deleteConfirmDescription}
        confirmLabel={copy.settings.knowledge.documents.delete}
        danger
        onConfirm={() => void confirmDelete()}
      />
      <ConfirmDialog
        open={pendingReindexDoc !== null}
        confirming={confirmingReindex}
        onOpenChange={(open) => {
          if (!open) {
            invalidateMutations();
            setConfirmingReindex(false);
            setPendingReindexDoc(null);
          }
        }}
        title={copy.settings.knowledge.documents.reindexConfirmTitle}
        description={copy.settings.knowledge.documents.reindexConfirmDescription(pendingReindexDoc?.name ?? '')}
        confirmLabel={copy.settings.knowledge.documents.reindex}
        onConfirm={() => void confirmReindex()}
      />
    </section>
  );

  function drillVersions(documentId: string) {
    navigate(
      formatDrawerLocation({
        open: true,
        segment: 'personal',
        drill: ['knowledge', 'versions', documentId],
      }),
    );
  }

  function drillSubmissions() {
    navigate(
      formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'submissions'] }),
    );
  }

  function drillManage() {
    navigate(
      formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'manage'] }),
    );
  }

  function drillUploads() {
    navigate(
      formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'uploads'] }),
    );
  }
}

interface DocumentRowProps {
  readonly doc: DocumentListItem;
  /** 行是否渲染管理操作（个人库 manage / 部门库 manage；active_operation 非空时隐藏全部冲突入口）。 */
  readonly manage: boolean;
  readonly onUploadNewVersion: () => void;
  readonly onVersions: () => void;
  readonly onReindex: () => void;
  readonly onDelete: () => void;
}

export function KnowledgeDocumentRow({ doc, manage, onUploadNewVersion, onVersions, onReindex, onDelete }: DocumentRowProps) {
  const updating = doc.active_operation !== null;
  const statusText = updating
    ? copy.settings.knowledge.documents.updating
    : copy.settings.knowledge.documents.stored;
  return (
    // 共用基座 §5.6 行形态：桌面=行高 56px 四列（文档名｜状态｜上传时间｜用量）+ hover 底 mist；
    // 窄屏单栏化：文档名整行 + 元信息第二行，避免固定列把文件名挤没
    <li className="group flex flex-col gap-1.5 rounded-[var(--radius-images)] px-3 py-2 transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray md:h-14 md:flex-row md:items-center md:gap-4 md:py-0">
      <p className="min-w-0 truncate text-[15px] font-w480 text-ink-black md:flex-1">{doc.name}</p>
      <div className="flex min-w-0 items-center gap-3 md:contents">
        <span className="flex shrink-0 items-center gap-2">
          {updating ? <StatusDot intent="slate" pulse /> : <StatusDot intent="success" />}
          <span className="text-[15px] text-slate-gray">{statusText}</span>
        </span>
        <span className="min-w-0 flex-1 truncate text-[15px] text-slate-gray md:w-40 md:flex-none">
          {formatDateTime(doc.uploaded_at)}
        </span>
        <span
          title={copy.settings.knowledge.documents.usageDetail(doc.usage.pages, doc.usage.images)}
          className="min-w-0 shrink-0 truncate text-[15px] text-slate-gray md:w-44 md:flex-none"
        >
          {copy.settings.knowledge.documents.usageDetail(doc.usage.pages, doc.usage.images)}
        </span>
      </div>
      {manage && !updating && (
        <MeatballMenu
          ariaLabel={copy.settings.knowledge.documents.rowMenuAria(doc.name)}
          items={[
            { key: 'upload-new-version', label: copy.settings.knowledge.documents.uploadNewVersion, onSelect: onUploadNewVersion },
            { key: 'versions', label: copy.settings.knowledge.documents.versions, onSelect: onVersions },
            { key: 'reindex', label: copy.settings.knowledge.documents.reindex, onSelect: onReindex },
            { key: 'delete', label: copy.settings.knowledge.documents.delete, danger: true, onSelect: onDelete },
          ]}
        />
      )}
    </li>
  );
}

interface QuotaRequestDialogProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly pages: string;
  readonly onPagesChange: (value: string) => void;
  readonly invalid: boolean;
  readonly pending: boolean;
  readonly error: string | null;
  readonly onConfirm: () => void;
}

function QuotaRequestDialog({
  open,
  onOpenChange,
  pages,
  onPagesChange,
  invalid,
  pending,
  error,
  onConfirm,
}: QuotaRequestDialogProps) {
  const dialogRef = useModalDialog(open, onOpenChange);
  const parsed = Number(pages);
  // 保留原始非法输入（-1/1.5 等），不静默改写；非空且非 1–500 整数即非法
  const valueInvalid = pages.trim() !== '' && (!Number.isInteger(parsed) || parsed < 1 || parsed > 500);
  const canConfirm = pages.trim() !== '' && Number.isInteger(parsed) && parsed >= 1 && parsed <= 500;
  const showInvalid = invalid || valueInvalid;
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
      aria-label={copy.settings.knowledge.quota.requestDialogTitle}
    >
      <div className="fixed inset-0 bg-ink-black/24" onClick={() => onOpenChange(false)} aria-hidden="true" />
      <div className="fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]">
        <h2 className="text-[20px] font-medium text-ink-black">{copy.settings.knowledge.quota.requestDialogTitle}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">{copy.settings.knowledge.quota.requestDescription}</p>
        <div className="mt-4">
          <label htmlFor="quota-request-pages" className="mb-2 block text-caption text-slate-gray">
            {copy.settings.knowledge.quota.requestedPagesLabel}
          </label>
          <input
            id="quota-request-pages"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={pages}
            onChange={(event) => onPagesChange(event.target.value)}
            aria-invalid={showInvalid}
            className={`h-10 w-full rounded-[var(--radius-inputs)] border bg-paper-white px-3 text-body text-ink-black focus:border-ink-black ${
              showInvalid ? 'border-danger' : 'border-[var(--color-hairline)]'
            }`}
          />
          {showInvalid && (
            <p role="alert" className="mt-2 text-[15px] text-danger">
              {copy.settings.knowledge.quota.invalidPages}
            </p>
          )}
          {error !== null && (
            <p role="alert" className="mt-2 text-[15px] text-danger">
              {error}
            </p>
          )}
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={pending} onClick={() => onOpenChange(false)}>
            {copy.controls.cancel}
          </Pill>
          <Pill size="sm" loading={pending} disabled={!canConfirm} onClick={onConfirm}>
            {copy.controls.confirm}
          </Pill>
        </div>
      </div>
    </div>
  );
}
