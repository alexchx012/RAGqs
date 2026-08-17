/*
 * 引用角标 + 悬停卡（共用基座 §3.4；规格 spec §4）。
 * 上标数字形如 [1]，Sohne 500 12px slate-gray，多来源并列间距 4px；hover/focus 变 ink；
 * 悬停 120ms 出 280px 悬停卡：首行「引自《文档名》」+ 次行定位信息（按载体固有单位）；
 * 点击角标新窗口打开原文预览页（/preview/{id}?message_id=…&document_version_id=…，fe-doc-preview）：
 * message_id 取所属 assistant 消息（预览接口据此返回该次回答的全部 hits）。
 * 契约 Citation 无可用性字段：mock 引用一律可用，悬停卡始终显示文档名与定位行；
 * 「内容已不可用」分支在契约补充可用性信号前不可达（见 chat-contract 注释）。
 */

import { useCallback } from 'react';
import { copy } from '../../copy';
import { HoverCard } from '../../ui/HoverCard';
import type { Citation } from '../types';

export interface CitationBadgeProps {
  readonly citations: readonly Citation[];
  /** 所属 assistant 消息 id：预览接口 message_id 参数（该次回答引用本文档的全部 hits）。 */
  readonly messageId: string;
}

function locatorLine(citation: Citation): string | null {
  const locator = citation.locator;
  if ('page' in locator) {
    // 定位行只展示载体固有单位（PDF = 第 X 页）；span 是预览页内部的消歧数据，不进用户文案
    return copy.chat.message.citePage(locator.page);
  }
  if ('section_path' in locator) {
    return copy.chat.message.citeSection(locator.section_path, locator.paragraph);
  }
  if ('sheet' in locator) {
    return copy.chat.message.citeSheet(locator.sheet, locator.a1_range);
  }
  // 空 locator（basic 文档 / 图片）：悬停卡无定位行，只有文档名
  return null;
}

/**
 * M9：悬停卡首行展示文档名。契约假设：后端应在 Citation 下发 document_name
 * （§1 容忍新增字段、前端未知字段忽略）；缺失时回退通用「引自文档」措辞，
 * 不显示不透明 document_id。该契约假设待后端确认。
 */
function documentTitle(citation: Citation): string {
  if (citation.document_name !== undefined && citation.document_name.trim() !== '') {
    return copy.chat.message.citeFrom(citation.document_name);
  }
  return copy.chat.message.citeFromFallback;
}

export function CitationBadges({ citations, messageId }: CitationBadgeProps) {
  const openPreview = useCallback(
    (citation: Citation) => {
      const url =
        `/preview/${encodeURIComponent(citation.document_id)}` +
        `?message_id=${encodeURIComponent(messageId)}` +
        `&document_version_id=${encodeURIComponent(citation.document_version_id)}`;
      window.open(url, '_blank', 'noopener,noreferrer');
    },
    [messageId],
  );

  if (citations.length === 0) {
    return null;
  }
  return (
    <span className="ml-1 inline-flex items-center gap-1 align-super">
      {citations.map((citation, index) => {
        const label = `[${index + 1}]`;
        const locator = locatorLine(citation);
        return (
          <HoverCard
            key={`${citation.document_id}:${citation.document_version_id}:${index}`}
            trigger={
              <button
                type="button"
                aria-label={copy.chat.message.citeOpenAria}
                onClick={() => openPreview(citation)}
                className="text-[12px] font-medium text-slate-gray transition-colors duration-[var(--duration-fast)] hover:text-ink-black focus-visible:text-ink-black"
              >
                {label}
              </button>
            }
            closeDelay={150}
          >
            <p className="text-[15px] font-w450 text-ink-black">
              {documentTitle(citation)}
            </p>
            {locator !== null && <p className="mt-1 text-[15px] text-slate-gray">{locator}</p>}
          </HoverCard>
        );
      })}
    </span>
  );
}
