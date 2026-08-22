/*
 * 上传新版本对话框（Blocker1 / §6.4；review A3/A4）。
 * - 从当前文档行菜单发起：固定目标 document_id + 服务端 expected_version，单文件；
 *   不进入目标选择、不调用初始上传接口。
 * - 调用 POST /documents/{id}/versions（multipart 单文件 + expected_version 表单字段）。
 * - Idempotency-Key 由 createIdempotencyScope 管理：key 绑定 document+file 指纹；
 *   未知网络/超时复用同键同体，业务响应（含 idempotency_key_conflict）清键不自动重发；
 *   目标切换/关闭时 clear()，杜绝 A 文件上传到 B 文档。
 * - 服务端返回 deduplicated（job_id 为 null）时提示「内容重复」，不产生任务。
 */

import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { Pill } from '../ui/Pill';
import { useSettings } from './SettingsProvider';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import { useModalDialog, useModalPresence } from './use-modal-dialog';
import type { DocumentListItem } from './types';

export interface NewVersionDialogProps {
  readonly target: DocumentListItem | null;
  readonly onClose: () => void;
  readonly onSubmitted: () => void;
  /** 冲突刷新请求（review Major 4）：父层刷新文档列表后传入最新行，避免旧 expected_version 重试。 */
  /** 普通 409 冲突：父层刷新当前文档列表并关闭旧 target（无参数；不用旧 version 静默替换）。 */
  readonly onConflictRefresh?: () => void;
}

export function NewVersionDialog({ target, onClose, onSubmitted, onConflictRefresh }: NewVersionDialogProps) {
  const { api } = useSettings();
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const idem = useRef(createIdempotencyScope());
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useModalDialog(target !== null, () => {
    invalidateOperation();
    onClose();
  });
  const presence = useModalPresence(target !== null);
  // 退出动画（150ms）期间 target 已为 null：保留最后一个非空 target 供渲染，随 presence 卸载
  const lastTargetRef = useRef<DocumentListItem | null>(null);
  if (target !== null) {
    lastTargetRef.current = target;
  }
  // operation token（review A3）：提交开始捕获；关闭/切换 target 时递增，
  // 旧请求的 success/error/conflict/onSubmitted/onConflictRefresh 全部 no-op——
  // 不能让文档 A 的迟到响应导航 uploads、关闭文档 B dialog 或刷新错误视图。
  const operationTokenRef = useRef(0);
  const invalidateOperation = () => {
    operationTokenRef.current += 1;
  };

  // 目标切换 / 关闭：清理 file/error/key + 递增 token（旧提交失效）
  const targetId = target?.id ?? null;
  useEffect(() => {
    invalidateOperation();
    idem.current.clear();
    setFile(null);
    setError(null);
    setNotice(null);
    setSubmitting(false);
  }, [targetId]);

  if (!presence.mounted || lastTargetRef.current === null) {
    return null;
  }
  const shownTarget = lastTargetRef.current;

  const confirmUpload = async () => {
    if (submitting || file === null || target === null) {
      return;
    }
    const token = operationTokenRef.current;
    setError(null);
    setNotice(null);
    const payloadFingerprint = `${file.name}:${file.size}:${file.lastModified}`;
    const idempotencyKey = idem.current.keyFor('upload-new-version', target.id, payloadFingerprint);
    setSubmitting(true);
    try {
      const response = await api.uploadNewVersion(target.id, file, target.version, idempotencyKey);
      if (token !== operationTokenRef.current) {
        return; // 已关闭/切换 target：迟到成功 no-op（不导航、不关新 dialog）
      }
      idem.current.clear();
      if (response.job_id === null) {
        // deduplicated：内容与当前版本一致，不产生任务
        setNotice(copy.settings.knowledge.upload.deduplicated);
        return;
      }
      onSubmitted();
    } catch (uploadError) {
      if (token !== operationTokenRef.current) {
        return; // 旧提交：error/conflict 全部 no-op
      }
      if (isBusinessResponse(uploadError)) {
        // 明确业务响应（含 idempotency_key_conflict）：清键，不自动换键重发
        idem.current.businessResponse();
        if (uploadError instanceof ApiError && uploadError.status === 409 && uploadError.code === 'idempotency_key_conflict') {
          setError(copy.settings.knowledge.manage.actionError);
        } else if (uploadError instanceof ApiError && uploadError.status === 409 && uploadError.code === 'duplicate_document') {
          // 服务端判定重复：不误报 version conflict，不清除目标；提示后由用户决定
          setNotice(copy.settings.knowledge.upload.deduplicated);
        } else if (uploadError instanceof ApiError && uploadError.status === 409) {
          // 普通 409（version_conflict / document_operation_in_progress / document_pending_delete）：
          // 统一关闭/清空 target、file、error、key 与确认意图，父层刷新当前 document list，
          // 要求用户从最新列表重新发起（review A3；不继续留在旧 target 上用旧 version 重提）
          idem.current.clear();
          setFile(null);
          setError(null);
          onConflictRefresh?.();
          onClose();
        } else {
          setError(copy.settings.knowledge.manage.actionError);
        }
      } else {
        // 网络未知/超时：复用同键同体重试
        setError(copy.settings.knowledge.manage.actionError);
      }
    } finally {
      if (token === operationTokenRef.current) {
        setSubmitting(false);
      }
    }
  };

  const requestClose = () => {
    invalidateOperation();
    onClose();
  };

  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={copy.settings.knowledge.upload.newVersionDialogTitle}
    >
      <div
        className="ui-dialog-overlay fixed inset-0 bg-ink-black/24"
        data-state={presence.state}
        onClick={() => (submitting ? undefined : requestClose())}
        aria-hidden="true"
      />
      <div
        className="ui-dialog-content fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]"
        data-state={presence.state}
      >
        <h2 className="text-[20px] font-medium text-ink-black">{copy.settings.knowledge.upload.newVersionDialogTitle}</h2>
        <p className="mt-2 text-[15px] text-slate-gray">
          {copy.settings.knowledge.upload.newVersionDescription(shownTarget.name)}
        </p>
        <div className="mt-4">
          <input
            ref={fileInputRef}
            type="file"
            aria-label={copy.settings.knowledge.upload.chooseFiles}
            onChange={(event) => {
              const picked = event.target.files?.item(0) ?? null;
              if (picked !== null) {
                setFile(picked);
                setNotice(null);
              }
              event.target.value = '';
            }}
            className="hidden"
          />
          <Pill variant="ghost" size="sm" onClick={() => fileInputRef.current?.click()} disabled={submitting}>
            {copy.settings.knowledge.upload.chooseFiles}
          </Pill>
          {file !== null && <p className="mt-2 truncate text-caption text-slate-gray">{file.name}</p>}
        </div>
        {notice !== null && (
          <p role="status" className="mt-3 text-[15px] text-slate-gray">
            {notice}
          </p>
        )}
        {error !== null && (
          <p role="alert" className="mt-3 text-[15px] text-danger">
            {error}
          </p>
        )}
        <div className="mt-6 flex justify-end gap-2">
          <Pill variant="ghost" size="sm" disabled={submitting} onClick={requestClose}>
            {copy.controls.cancel}
          </Pill>
          <Pill size="sm" loading={submitting} disabled={file === null} onClick={() => void confirmUpload()}>
            {copy.controls.confirm}
          </Pill>
        </div>
      </div>
    </div>
  );
}
