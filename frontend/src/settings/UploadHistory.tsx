/*
 * 上传结果历史呈现（Major3 / review A2）：最近一次上传响应的逐文件结果，稳定可访问。
 * 供知识库首页工具行下方与上传结果层顶部共用；按 auth session 隔离（sessionKey），
 * 旧会话写入不覆盖新会话槽位；通过 subscribe 在写入后刷新（页面已挂载时最小订阅机制）。
 */

import { useEffect, useState } from 'react';
import { copy } from '../copy';
import type { UploadItem } from './types';
import { readUploadHistory, subscribeUploadHistory } from './upload-history';

export function UploadHistorySection({ sessionKey }: { readonly sessionKey: string | null }) {
  const [entry, setEntry] = useState(() => readUploadHistory(sessionKey));

  useEffect(() => {
    // 会话切换：重读；写入/清空经订阅通知刷新（页面已挂载的最小刷新机制）
    setEntry(readUploadHistory(sessionKey));
    return subscribeUploadHistory(() => {
      setEntry(readUploadHistory(sessionKey));
    });
  }, [sessionKey]);

  if (entry === null) {
    return null;
  }
  const acceptedCount = entry.response.items.filter((item) => item.accepted).length;
  const failedCount = entry.response.items.length - acceptedCount;

  return (
    <section aria-label={copy.settings.knowledge.uploads.historyTitle} className="rounded-[var(--radius-elevatedcards)] border border-[var(--color-hairline)] p-4">
      <div className="flex items-center justify-between gap-4">
        <h3 className="text-subheading font-medium text-ink-black">
          {copy.settings.knowledge.uploads.historyTitle}
        </h3>
        <p className="text-caption text-smoke-gray">
          {entry.target !== null
            ? `${copy.settings.knowledge.uploads.historyTarget(entry.target.name)} · ${copy.settings.knowledge.uploads.historyAt(formatTime(entry.at))}`
            : copy.settings.knowledge.uploads.historyAt(formatTime(entry.at))}
        </p>
      </div>
      <ul className="mt-3 flex flex-col gap-1">
        {entry.response.items.map((item, index) => (
          <UploadHistoryItemRow key={`${uploadItemKey(item)}:${index}`} item={item} />
        ))}
      </ul>
      <p className="mt-2 text-caption text-slate-gray">
        {copy.settings.knowledge.upload.resultSummary(acceptedCount, failedCount)}
      </p>
    </section>
  );
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleTimeString('zh-CN');
}

function UploadHistoryItemRow({ item }: { item: UploadItem }) {
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
