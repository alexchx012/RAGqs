/*
 * 版本记录下钻层（settings-personal §7）。
 * - active 标识当前版本；仅 superseded + content_available + 未过 purge_after_at 显示预览与恢复；
 *   failed/cancelled 不显示恢复；purging/purged 显示内容不可用；processing 不显示恢复/预览
 *   （任务成功后自动切 active）；不显示对象存储路径或清理进度。
 * - 恢复：POST .../restore { expected_version } + Idempotency-Key（网络未知重试同键，已收业务错误换键）；
 *   确认文案说明恢复创建新版本并重新处理、处理成功前当前版本继续服务；成功后任务进上传结果层。
 * - 410 document_version_purged 刷新本层、该行转内容不可用。
 * - documentId 来自抽屉下钻路径（/settings/knowledge/versions/<documentId>）；无 id 时清空旧状态。
 * - 预览：新窗口打开原文预览页（fe-doc-preview /preview/:document_id），带明确 version id、不带 message_id（只读形态）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { formatDrawerLocation } from '../router/drawer-params';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { EmptyState, ErrorState, LoadingRows } from '../ui/states';
import { Pill } from '../ui/Pill';
import { useSettings } from './SettingsProvider';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import type { DocumentVersionItem } from './types';

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

/** 版本记录层：documentId 来自抽屉下钻路径（/settings/knowledge/versions/<documentId>）。 */
export function VersionsLayer({ path }: { readonly path: readonly string[] }) {
  const { api } = useSettings();
  const navigate = useNavigate();
  const documentId = path[2] ?? '';
  const [versions, setVersions] = useState<readonly DocumentVersionItem[]>([]);
  const [rowVersion, setRowVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingRestore, setPendingRestore] = useState<DocumentVersionItem | null>(null);
  const [confirmingRestore, setConfirmingRestore] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const restoreIdem = useRef(createIdempotencyScope());
  // 恢复操作 token（review A3）：确认 A 飞行中关闭/切换 document 后打开 B，A completion 不得导航/关 B
  const restoreTokenRef = useRef(0);
  const invalidateRestoreOperation = () => {
    restoreTokenRef.current += 1;
  };
  // request sequence fence：旧 document 的响应不得覆盖新 document（review Major 2）
  const versionsSeqRef = useRef(0);

  const loadVersions = useCallback(async () => {
    const seq = ++versionsSeqRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.listVersions(documentId);
      if (seq !== versionsSeqRef.current) {
        return;
      }
      setVersions(response.items);
      setRowVersion(response.version);
    } catch {
      if (seq === versionsSeqRef.current) {
        setLoadError(true);
      }
    } finally {
      if (seq === versionsSeqRef.current) {
        setLoading(false);
      }
    }
  }, [api, documentId]);

  // documentId 任意变化（A→B 或 A→空）与卸载：作废旧 restore operation、
  // 释放 confirming、清空旧文档状态；旧 A success/error/refresh 不得关闭 B confirmation
  // 或污染 B 版本列表（review Medium 2）。
  // null 哨兵：首挂载必然走一次加载（后续任意 A→B/→空变化也走）
  const previousDocumentIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (previousDocumentIdRef.current === documentId) {
      return;
    }
    previousDocumentIdRef.current = documentId;
    versionsSeqRef.current += 1; // 作废在途 listVersions
    invalidateRestoreOperation(); // 作废在途 restore mutation
    setConfirmingRestore(false);
    setPendingRestore(null);
    setActionError(null);
    setNotice(null);
    setVersions([]);
    setRowVersion(0);
    restoreIdem.current.clear();
    if (documentId === '') {
      setLoading(false);
      setLoadError(false);
      return;
    }
    void loadVersions();
  }, [documentId]);

  // 卸载：作废旧 restore operation（迟到 completion no-op）
  useEffect(() => {
    return () => {
      invalidateRestoreOperation();
    };
  }, []);

  const confirmRestore = async () => {
    const version = pendingRestore;
    if (version === null || documentId === '' || confirmingRestore) {
      return;
    }
    const token = restoreTokenRef.current;
    setConfirmingRestore(true);
    setActionError(null);
    // key 绑定 operation+target+expected_version（review Major 3）：行版本变化自动换键
    const idempotencyKey = restoreIdem.current.keyFor(
      'restore-version',
      `${documentId}:${version.document_version_id}`,
      `version:${rowVersion}`,
    );
    try {
      await api.restoreVersion(documentId, version.document_version_id, rowVersion, idempotencyKey);
      if (token !== restoreTokenRef.current || documentId === '') {
        return; // 已关闭/切换 document：A completion no-op（不导航 uploads）
      }
      restoreIdem.current.clear();
      setPendingRestore(null);
      setNotice(copy.settings.knowledge.versions.restoreSuccess);
      // 恢复任务进上传结果层跟踪
      navigate(formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge', 'uploads'] }));
    } catch (error) {
      if (token !== restoreTokenRef.current) {
        return; // 旧恢复 error/conflict no-op
      }
      if (isBusinessResponse(error)) {
        // 明确业务响应（含 document_version_purged / version_conflict）：清键，
        // 刷新本层最新状态，不用旧 expected_version 重试（review Major 4）
        restoreIdem.current.businessResponse();
        setPendingRestore(null);
        if (error instanceof ApiError && error.status === 410 && error.code === 'document_version_purged') {
          setActionError(copy.settings.knowledge.versions.versionPurged);
        } else {
          setActionError(copy.settings.knowledge.versions.restoreError);
        }
        await loadVersions();
      } else {
        // 网络未知/超时：复用同键同体重试
        setActionError(copy.settings.knowledge.versions.restoreError);
      }
    } finally {
      if (token === restoreTokenRef.current) {
        setConfirmingRestore(false);
      }
    }
  };

  /** 仅 superseded + content_available + 未过 purge_after_at 可恢复；其余状态一律不渲染恢复。 */
  const canRestore = (version: DocumentVersionItem): boolean =>
    version.status === 'superseded' &&
    version.content_available &&
    (version.purge_after_at === null || new Date(version.purge_after_at).valueOf() > Date.now());

  /** 仅 superseded 且可预览（内容可用且未过 purge_after_at）才渲染预览（review Major 5）。 */
  const canPreview = (version: DocumentVersionItem): boolean =>
    version.status === 'superseded' &&
    version.content_available &&
    (version.purge_after_at === null || new Date(version.purge_after_at).valueOf() > Date.now());

  const contentUnavailable = (version: DocumentVersionItem): boolean =>
    version.status === 'purging' ||
    version.status === 'purged' ||
    version.status === 'processing' ||
    !version.content_available ||
    // purge_after_at 已到期的 superseded 版本：内容不可用（review 低风险项）
    (version.status === 'superseded' &&
      version.purge_after_at !== null &&
      new Date(version.purge_after_at).valueOf() <= Date.now());

  return (
    <section aria-label={copy.settings.knowledge.versions.title} className="pb-10">
      {documentId === '' ? (
        <EmptyState text={copy.settings.knowledge.versions.empty} />
      ) : loading ? (
        <LoadingRows count={3} />
      ) : loadError ? (
        <ErrorState onRetry={() => void loadVersions()} />
      ) : versions.length === 0 ? (
        <EmptyState text={copy.settings.knowledge.versions.empty} />
      ) : (
        <ul className="divide-y divide-[var(--color-hairline)]">
          {versions.map((version) => (
            <li key={version.document_version_id} className="py-4">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="text-body text-ink-black">
                      {copy.settings.knowledge.versions.versionNumber(version.version_number)}
                    </p>
                    {version.status === 'active' && (
                      <span className="rounded-[var(--radius-buttons)] bg-mist-gray px-2 py-0.5 text-caption text-slate-gray">
                        {copy.settings.knowledge.versions.active}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-caption text-smoke-gray">
                    {copy.settings.knowledge.versions.createdAt(formatDateTime(version.created_at))}
                  </p>
                  {contentUnavailable(version) && (
                    <p className="mt-1 text-caption text-smoke-gray">
                      {copy.settings.knowledge.versions.contentUnavailable}
                    </p>
                  )}
                </div>
                {!contentUnavailable(version) && (
                  <div className="flex shrink-0 items-center gap-2">
                    {canPreview(version) && (
                      <Pill variant="ghost" size="xs" onClick={() => openPreview(version)}>
                        {copy.settings.knowledge.versions.preview}
                      </Pill>
                    )}
                    {canRestore(version) && (
                      <Pill variant="ghost" size="xs" onClick={() => setPendingRestore(version)}>
                        {copy.settings.knowledge.versions.restore}
                      </Pill>
                    )}
                  </div>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {notice !== null && (
        <p role="status" className="mt-4 text-caption text-success">
          {notice}
        </p>
      )}
      {actionError !== null && (
        <p role="alert" className="mt-4 text-caption text-danger">
          {actionError}
        </p>
      )}

      <ConfirmDialog
        open={pendingRestore !== null}
        confirming={confirmingRestore}
        onOpenChange={(open) => {
          if (!open) {
            invalidateRestoreOperation();
            setConfirmingRestore(false);
            setPendingRestore(null);
          }
        }}
        title={copy.settings.knowledge.versions.restoreConfirmTitle}
        description={copy.settings.knowledge.versions.restoreConfirmDescription}
        confirmLabel={copy.settings.knowledge.versions.restore}
        onConfirm={() => void confirmRestore()}
      />
    </section>
  );

  function openPreview(version: DocumentVersionItem) {
    // 原文预览页（fe-doc-preview）：新窗口打开，不携带 message_id = 管理侧只读形态（hits 为空）
    window.open(
      `/preview/${encodeURIComponent(documentId)}?document_version_id=${encodeURIComponent(version.document_version_id)}`,
      '_blank',
      'noopener,noreferrer',
    );
  }
}
