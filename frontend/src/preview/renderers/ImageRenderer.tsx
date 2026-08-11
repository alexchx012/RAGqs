/*
 * 图片渲染器（fe-doc-preview；共用基座 §6）：原图居中展示（radius-images）。
 * img 无法携带 Bearer：内容经客户端取 Blob 转 objectURL；空 locator 只有文档名，无锚点。
 */

import { useEffect, useState } from 'react';
import { copy } from '../../copy';
import { ErrorState } from '../../ui/states';
import type { PreviewApi } from '../api';

export interface ImageRendererProps {
  readonly api: PreviewApi;
  readonly documentId: string;
  readonly documentVersionId: string | null;
  /** 文档名（alt）。 */
  readonly name: string;
}

export function ImageRenderer({ api, documentId, documentVersionId, name }: ImageRendererProps) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    setObjectUrl(null);
    setLoadError(false);
    api.getImageContent(documentId, { documentVersionId }).then(
      (blob) => {
        if (cancelled) {
          return;
        }
        if (typeof URL.createObjectURL === 'function') {
          created = URL.createObjectURL(blob);
          setObjectUrl(created);
        } else {
          // 无 createObjectURL 的环境（jsdom 单测外不应出现）：回退直链
          setObjectUrl(api.buildContentUrl(`/documents/${encodeURIComponent(documentId)}/content`, documentVersionId));
        }
      },
      () => {
        if (!cancelled) {
          setLoadError(true);
        }
      },
    );
    return () => {
      cancelled = true;
      if (created !== null && typeof URL.revokeObjectURL === 'function') {
        URL.revokeObjectURL(created);
      }
    };
  }, [api, documentId, documentVersionId, retryNonce]);

  if (loadError) {
    return <ErrorState text={copy.preview.error} retryLabel={copy.preview.retry} onRetry={() => setRetryNonce((nonce) => nonce + 1)} />;
  }
  if (objectUrl === null) {
    return (
      <div aria-busy="true" className="ui-skeleton mx-auto h-[320px] max-w-[480px] rounded-[var(--radius-images)] bg-mist-gray" />
    );
  }
  return (
    <img
      src={objectUrl}
      alt={name}
      className="mx-auto block max-w-full rounded-[var(--radius-images)]"
    />
  );
}
