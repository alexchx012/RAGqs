/*
 * 原文预览占位页（m12；brief：本 change 落占位路由并透传 document_id + document_version_id，
 * 预览页本体是 fe-doc-preview 的事）。引用角标点击新窗口打开此路由，不再落 NotFoundPage。
 */

import { useParams, useSearchParams } from 'react-router';
import { copy } from '../../copy';

export function DocumentPreviewPlaceholder() {
  const { documentId: pathDocumentId } = useParams<{ documentId: string }>();
  const [params] = useSearchParams();
  const documentId = pathDocumentId ?? params.get('document_id') ?? '';
  const documentVersionId = params.get('document_version_id') ?? '';
  return (
    <div className="flex min-h-screen flex-col bg-paper-white px-10 pt-10 text-ink-black">
      <h1 className="font-signifier text-[44px] font-normal leading-[1.3] tracking-[-0.66px]">
        {copy.chat.preview.title}
      </h1>
      <p className="mt-4 text-[15px] text-slate-gray">{copy.chat.preview.placeholderBody}</p>
      <dl className="mt-8 flex flex-col gap-2 text-[15px]">
        <div className="flex gap-2">
          <dt className="w-24 shrink-0 text-slate-gray">{copy.chat.preview.documentLabel}</dt>
          <dd className="truncate text-ink-black">{documentId || '—'}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-24 shrink-0 text-slate-gray">{copy.chat.preview.versionLabel}</dt>
          <dd className="truncate text-ink-black">{documentVersionId || '—'}</dd>
        </div>
      </dl>
    </div>
  );
}
