/*
 * 上传对话框（settings-personal §5，全角色唯一上传入口；review A2/A3/D16）。
 * - 目标空间单选列表：GET /spaces?usage=upload；manage 目标 = 直接写入、contribute 目标 =
 *   需审核分支提示；任何子界面不提供独立上传按钮、不预填目标、不直接调用上传接口。
 * - 多文件上传：逐文件结果呈现（accepted 含 deduplicated / 投稿项含 submission_id 与
 *   quota_exempt / 失败项按服务端错误对象），前端不以浏览器解码结果预拒文件。
 * - 409 quota_exceeded 整批拒绝提示（不预扣不冻结）；投稿创建不检查页额度。
 * - 确认后：manage 目标自动下钻上传结果层；contribute 目标自动下钻「我的投稿」层。
 * - Idempotency-Key 绑定 target(space)+payload(文件指纹)：未知网络/超时复用同键同体，
 *   明确业务响应清键不自动重发（含 idempotency_key_conflict）。
 * - 上传结果历史按 sessionKey 隔离写入（旧会话回调拒绝落库）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { X } from 'lucide-react';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { formatDrawerLocation } from '../router/drawer-params';
import { Pill } from '../ui/Pill';
import { useSettings } from './SettingsProvider';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import { useModalDialog, useModalPresence } from './use-modal-dialog';
import type { SpaceItem, UploadItem, UploadResponse } from './types';
import { recordUploadHistory } from './upload-history';

export interface UploadDialogProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  /** 当前逻辑会话 key（sessionId:userId）；null 时上传历史拒绝落库（未认证）。 */
  readonly sessionKey: string | null;
}

type UploadPhase = 'idle' | 'uploading' | 'done';

/** 已选文件行/审核列表的大小显示（KB/MB 简单呈现，避免引入格式化依赖）。 */
export function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`;
  }
  return `${bytes} B`;
}

export function UploadDialog({ open, onOpenChange, sessionKey }: UploadDialogProps) {
  const { api } = useSettings();
  const navigate = useNavigate();
  const dialogRef = useModalDialog(open, () => {
    invalidateOperation();
    onOpenChange(false);
  });
  const presence = useModalPresence(open);
  const [spaces, setSpaces] = useState<readonly SpaceItem[]>([]);
  const [spacesError, setSpacesError] = useState(false);
  const [selectedSpaceId, setSelectedSpaceId] = useState<string | null>(null);
  const [spaceQuery, setSpaceQuery] = useState('');
  const [files, setFiles] = useState<readonly File[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [phase, setPhase] = useState<UploadPhase>('idle');
  const [result, setResult] = useState<UploadResponse | null>(null);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const idem = useRef(createIdempotencyScope());
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const spacesSeqRef = useRef(0);
  // operation token（review A2）：关闭、重开、文件/目标切换时递增，
  // 旧上传的迟到 completion 不得清 B 的 key、污染 B 状态或导航。
  const operationTokenRef = useRef(0);
  const invalidateOperation = () => {
    operationTokenRef.current += 1;
  };

  const loadSpaces = useCallback(async () => {
    const seq = ++spacesSeqRef.current;
    setSpacesError(false);
    try {
      const response = await api.listUploadSpaces();
      if (seq !== spacesSeqRef.current) {
        return;
      }
      setSpaces(response.items);
      // 默认选中第一个 manage 目标（若无 manage 则第一个 contribute 目标）
      const firstManage = response.items.find((space) => space.permission === 'manage');
      const firstContribute = response.items.find((space) => space.permission === 'contribute');
      setSelectedSpaceId(firstManage?.id ?? firstContribute?.id ?? null);
    } catch {
      if (seq === spacesSeqRef.current) {
        setSpacesError(true);
      }
    }
  }, [api]);

  useEffect(() => {
    if (open) {
      // 重开：旧 operation 失效（A 的迟到 completion 不得清 B 的 key/状态）
      invalidateOperation();
      setPhase('idle');
      setResult(null);
      setQuotaError(null);
      setSubmitError(null);
      setFiles([]);
      setSelectedSpaceId(null);
      setSpaceQuery('');
      idem.current.clear();
      void loadSpaces();
    }
  }, [open, loadSpaces]);

  const selectedSpace = spaces.find((space) => space.id === selectedSpaceId) ?? null;
  // 共用基座 §5.6：目标空间返回项超过 8 行时列表内部滚动（max-height 320px）+
  // 顶部搜索框按空间名实时过滤；不超过 8 行维持平铺（选中态与权限标注不变）。
  const spaceListScrollable = spaces.length > 8;
  const normalizedSpaceQuery = spaceQuery.trim().toLowerCase();
  const visibleSpaces =
    spaceListScrollable && normalizedSpaceQuery !== ''
      ? spaces.filter((space) => space.name.toLowerCase().includes(normalizedSpaceQuery))
      : spaces;

  const addFiles = (next: readonly File[]) => {
    if (phase === 'uploading') {
      return; // 上传进行中：禁止变更文件（否则 A completion 可能写 B 的文件集状态）
    }
    // 文件变更：旧 operation 失效（A 迟到 completion 不得清 B 的 key/写 A 结果）
    invalidateOperation();
    setFiles((current) => [...current, ...next]);
    setResult(null);
    setPhase('idle');
    setQuotaError(null);
    setSubmitError(null);
  };

  const confirmUpload = async () => {
    if (phase === 'uploading' || selectedSpace === null || files.length === 0) {
      return;
    }
    const token = operationTokenRef.current;
    // key 绑定 target(space)+payload(文件指纹)：目标/文件变化自动换键
    const payloadFingerprint = files
      .map((file) => `${file.name}:${file.size}:${file.lastModified}`)
      .join('|');
    const idempotencyKey = idem.current.keyFor('upload-documents', selectedSpace.id, payloadFingerprint);
    setPhase('uploading');
    setQuotaError(null);
    setSubmitError(null);
    try {
      const response = await api.uploadDocuments(selectedSpace.id, files, idempotencyKey);
      if (token !== operationTokenRef.current) {
        return; // 已关闭/重开/切换：旧上传 completion no-op（不清 B 的 key、不污染状态、不导航）
      }
      idem.current.clear();
      setResult(response);
      setPhase('done');
      // 上传结果历史：按会话隔离写入（旧会话回调返回 false 被拒，不落库）
      recordUploadHistory(
        { response, target: selectedSpace, at: new Date().toISOString() },
        sessionKey,
      );
      // 确认后自动下钻：manage → 上传结果层；contribute → 我的投稿层
      if (selectedSpace.permission === 'manage') {
        navigate(
          formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'uploads'] }),
        );
      } else {
        navigate(
          formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'submissions'] }),
        );
      }
    } catch (error) {
      if (token !== operationTokenRef.current) {
        return; // 旧上传 error no-op
      }
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 quota_exceeded / idempotency_key_conflict）：清键，不自动重发
        idem.current.businessResponse();
        if (error instanceof ApiError && error.status === 409 && error.code === 'quota_exceeded') {
          setQuotaError(copy.settings.knowledge.upload.quotaExceeded);
        } else {
          setSubmitError(copy.settings.knowledge.upload.itemError('upload_error'));
        }
        setPhase('idle');
      } else {
        // 网络未知/超时：复用同键同体重试
        setSubmitError(copy.settings.knowledge.upload.itemError('upload_error'));
        setPhase('idle');
      }
    }
    // 无 finally 重置（A37）：成功路径保留 done 状态（结果行常驻，phase 由文件变更/重开复位），
    // 失败路径各分支已显式回 idle；恒真 wasUploading 守卫删除。
  };

  const requestClose = () => {
    invalidateOperation();
    onOpenChange(false);
  };

  const resultItems = result?.items ?? [];
  const acceptedCount = resultItems.filter((item) => item.accepted).length;
  const failedCount = resultItems.length - acceptedCount;

  if (!presence.mounted) {
    return null;
  }

  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={copy.settings.knowledge.upload.dialogTitle}
    >
      <div
        className="ui-dialog-overlay fixed inset-0 bg-ink-black/24"
        data-state={presence.state}
        onClick={() => {
          if (phase !== 'uploading') {
            requestClose();
          }
        }}
        aria-hidden="true"
      />
      <div
        className="ui-dialog-content fixed top-1/2 left-1/2 w-[480px] max-w-[calc(100vw-32px)] rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]"
        data-state={presence.state}
      >
        <h2 className="text-[20px] font-medium text-ink-black">{copy.settings.knowledge.upload.dialogTitle}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">{copy.settings.knowledge.upload.dialogDescription}</p>

        {/* 目标空间单选列表 */}
        <fieldset className="mt-4">
          <legend className="mb-2 text-caption text-slate-gray">{copy.settings.knowledge.upload.targetLabel}</legend>
          {spacesError ? (
            <div className="flex items-center gap-3">
              <p className="text-caption text-danger">{copy.states.error}</p>
              <Pill variant="ghost" size="xs" onClick={() => void loadSpaces()}>
                {copy.states.retry}
              </Pill>
            </div>
          ) : spaces.length === 0 ? (
            <p className="text-caption text-smoke-gray">{copy.states.empty}</p>
          ) : (
            <>
              {spaceListScrollable && (
                <input
                  type="search"
                  value={spaceQuery}
                  onChange={(event) => setSpaceQuery(event.target.value)}
                  placeholder={copy.settings.knowledge.upload.spaceSearchPlaceholder}
                  aria-label={copy.settings.knowledge.upload.spaceSearchPlaceholder}
                  className="mb-2 h-9 w-full rounded-[var(--radius-inputs)] border border-hairline bg-paper-white px-3 text-[15px] text-ink-black outline-none placeholder:text-smoke-gray focus:border-ink-black"
                />
              )}
              {visibleSpaces.length === 0 ? (
                <p className="text-caption text-smoke-gray">
                  {copy.settings.knowledge.upload.spaceSearchEmpty}
                </p>
              ) : (
                <ul
                  className={`flex flex-col gap-1 ${
                    spaceListScrollable ? 'max-h-[320px] overflow-y-auto' : ''
                  }`}
                >
                  {visibleSpaces.map((space) => (
                    <li key={space.id}>
                      <label
                        className={`flex cursor-pointer items-center gap-3 rounded-[var(--radius-images)] border px-3 py-2.5 ${
                          selectedSpaceId === space.id ? 'border-ink-black bg-mist-gray' : 'border-[var(--color-hairline)]'
                        }`}
                      >
                        <input
                          type="radio"
                          name="upload-target"
                          value={space.id}
                          checked={selectedSpaceId === space.id}
                          disabled={phase === 'uploading'}
                          onChange={() => {
                            if (phase === 'uploading') {
                              return;
                            }
                            // 目标变化：旧 operation 失效（不跨目标复用 key/不污染）
                            invalidateOperation();
                            setSelectedSpaceId(space.id);
                          }}
                          className="accent-ink-black disabled:cursor-not-allowed"
                        />
                        <span className="min-w-0">
                          <span className="block truncate text-body text-ink-black">{space.name}</span>
                          <span className="block text-caption text-smoke-gray">
                            {space.permission === 'manage'
                              ? copy.settings.knowledge.upload.manageTargetHint
                              : copy.settings.knowledge.upload.contributeTargetHint}
                          </span>
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </fieldset>

        {/* 文件区（共用基座 §5.6）：拖拽区 + 点击选择；已选文件行（名 + 大小 + 移除 ×） */}
        <div className="mt-4">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            disabled={phase === 'uploading'}
            aria-label={copy.settings.knowledge.upload.chooseFiles}
            onChange={(event) => {
              const picked = Array.from(event.target.files ?? []);
              if (picked.length > 0) {
                addFiles(picked);
              }
              event.target.value = '';
            }}
            className="hidden"
          />
          <div
            role="button"
            tabIndex={0}
            aria-label={copy.settings.knowledge.upload.chooseFiles}
            onClick={() => {
              if (phase !== 'uploading') {
                fileInputRef.current?.click();
              }
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                if (phase !== 'uploading') {
                  fileInputRef.current?.click();
                }
              }
            }}
            onDragOver={(event) => {
              event.preventDefault();
              if (phase !== 'uploading') {
                setDragActive(true);
              }
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragActive(false);
              if (phase !== 'uploading') {
                const dropped = Array.from(event.dataTransfer.files ?? []);
                if (dropped.length > 0) {
                  addFiles(dropped);
                }
              }
            }}
            className={
              'flex h-[120px] cursor-pointer items-center justify-center rounded-[var(--radius-inputs)] bg-mist-gray text-[15px] text-slate-gray outline-none transition-colors duration-[var(--duration-fast)] ' +
              (dragActive
                ? 'border border-solid border-ink-black'
                : 'border border-dashed border-smoke-gray hover:text-ink-black')
            }
          >
            {copy.settings.knowledge.upload.dropHint}
          </div>
          {files.length > 0 && (
            <ul aria-label={copy.settings.knowledge.upload.fileListAria} className="mt-3 flex max-h-40 flex-col gap-1 overflow-y-auto">
              {files.map((file) => (
                <li key={`${file.name}:${file.size}`} className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-caption text-ink-black">{file.name}</span>
                  <span className="shrink-0 text-caption text-slate-gray">{formatFileSize(file.size)}</span>
                  <button
                    type="button"
                    aria-label={copy.settings.knowledge.upload.removeFile}
                    disabled={phase === 'uploading'}
                    onClick={() => {
                      if (phase === 'uploading') {
                        return;
                      }
                      invalidateOperation();
                      setFiles((current) => current.filter((item) => !(item.name === file.name && item.size === file.size)));
                      setResult(null);
                      setPhase('idle');
                      setQuotaError(null);
                      setSubmitError(null);
                    }}
                    className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[var(--radius-images)] text-slate-gray transition-colors duration-[var(--duration-fast)] hover:text-danger disabled:text-smoke-gray"
                  >
                    <X aria-hidden="true" className="h-4 w-4" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 逐文件结果呈现（服务端错误对象，不以浏览器解码预拒） */}
        {result !== null && (
          <div className="mt-4">
            <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto" aria-live="polite">
              {result.items.map((item, index) => (
                <UploadItemRow key={`${uploadItemKey(item)}:${index}`} item={item} />
              ))}
            </ul>
            <p className="mt-2 text-caption text-slate-gray">
              {copy.settings.knowledge.upload.resultSummary(acceptedCount, failedCount)}
            </p>
          </div>
        )}

        {quotaError !== null && (
          <p role="alert" className="mt-3 text-[15px] text-danger">
            {quotaError}
          </p>
        )}
        {submitError !== null && (
          <p role="alert" className="mt-3 text-[15px] text-danger">
            {submitError}
          </p>
        )}

        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={phase === 'uploading'} onClick={requestClose}>
            {copy.controls.cancel}
          </Pill>
          <Pill
            size="sm"
            loading={phase === 'uploading'}
            disabled={phase === 'uploading' || selectedSpace === null || files.length === 0}
            onClick={() => void confirmUpload()}
          >
            {phase === 'uploading' ? copy.settings.knowledge.upload.uploading : copy.settings.knowledge.upload.upload}
          </Pill>
        </div>
      </div>
    </div>
  );
}

function UploadItemRow({ item }: { item: UploadItem }) {
  // 拒绝项：逐文件错误对象（name/accepted/error 契约），前端按 code 映射文案。
  if (!item.accepted) {
    return (
      <li className="text-caption text-danger">
        {`${item.name} · ${copy.settings.knowledge.upload.itemError(item.error.code)}`}
      </li>
    );
  }
  if ('submission_id' in item) {
    return (
      <li className="text-caption text-slate-gray">
        {`${item.name} · ${item.status === 'pending' ? copy.settings.knowledge.upload.submissionCreated : item.status}`}
      </li>
    );
  }
  if (item.deduplicated) {
    return (
      <li className="text-caption text-slate-gray">
        {`${item.name} · ${copy.settings.knowledge.upload.deduplicated}`}
      </li>
    );
  }
  return (
    <li className="text-caption text-success">
      {`${item.name} · ${item.status === 'pending' ? copy.settings.knowledge.upload.accepted : item.status}`}
    </li>
  );
}

function uploadItemKey(item: UploadItem): string {
  if (!item.accepted) {
    return `rejected:${item.name}`;
  }
  return 'submission_id' in item ? item.submission_id : item.document_id;
}
