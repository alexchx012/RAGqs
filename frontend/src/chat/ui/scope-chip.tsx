/*
 * 检索范围 chip（共用基座 §3.3；契约 §6.1/§6.2；spec §3）。
 * ghost pill，展开浮层 280px max-h 360px；行完全由 GET /spaces?usage=retrieval 返回项决定，
 * 前端不硬编码、不过滤、不按角色补出；>8 行时浮层顶部搜索框按空间名实时过滤。
 * 本人个人库行尾下钻箭头 → 行内缩进展开单文档多选（GET /spaces/{id}/documents，按文档名过滤）；
 * 只有本人个人库有此箭头。默认「全部范围」；非默认时文案摘要 + 左侧 6px 墨点。
 * 范围选择本组件本地记忆（当前会话内记住，新会话重置由父组件卸载/重置实现）。
 */

import * as Popover from '@radix-ui/react-popover';
import { Check, ChevronDown, ChevronRight, Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { copy } from '../../copy';
import { useEscShield } from '../../lib/esc-stack-provider';
import type { SpaceItem } from '../types';

export interface ScopeDocument {
  readonly id: string;
  readonly name: string;
}

export interface ScopeSelection {
  readonly space_ids: string[];
  readonly document_ids: string[];
}

export interface ScopeChipProps {
  readonly spaces: readonly SpaceItem[];
  /** 从检索空间拉取文档列表（个人库下钻）；失败返回 null 由本组件降级为空列表。 */
  readonly onFetchDocuments: (spaceId: string, q?: string) => Promise<ScopeDocument[] | null>;
  readonly selection: ScopeSelection;
  readonly onSelectionChange: (selection: ScopeSelection) => void;
}

/** 本人个人库：usage=retrieval 只返回本人个人库（契约 §6.1），故 kind=personal 即本人个人库。 */
function personalSpaceId(spaces: readonly SpaceItem[]): string | null {
  return spaces.find((space) => space.kind === 'personal')?.id ?? null;
}

export function ScopeChip({
  spaces,
  onFetchDocuments,
  selection,
  onSelectionChange,
}: ScopeChipProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [documents, setDocuments] = useState<ScopeDocument[]>([]);
  const [docFilter, setDocFilter] = useState('');
  const [docOpen, setDocOpen] = useState<string | null>(null);
  /** 文档列表请求序号：过滤/清空/下钻并发时只采纳最新一次结果，避免乱序覆盖。 */
  const docFetchSeq = useRef(0);
  useEscShield(open);

  const personalId = personalSpaceId(spaces);
  const selectedAll = selection.space_ids.length === 0 && selection.document_ids.length === 0;

  const applyDocuments = (seq: number, docs: ScopeDocument[] | null) => {
    if (seq !== docFetchSeq.current) return;
    if (docs !== null) setDocuments(docs);
  };

  // 摘要：选中空间名（全部 → 「全部范围」）；非默认时 + 墨点
  const selectedSpaces = spaces.filter((space) => selection.space_ids.includes(space.id));
  const summary =
    selectedAll && selection.document_ids.length === 0
      ? copy.chat.composer.scopeAll
      : selectedSpaces.map((space) => space.name).join(' + ') || copy.chat.composer.scopeAll;

  const visibleSpaces = spaces.filter((space) =>
    filter.trim() === '' ? true : space.name.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  const toggleSpace = (spaceId: string) => {
    const next = selection.space_ids.includes(spaceId)
      ? selection.space_ids.filter((id) => id !== spaceId)
      : [...selection.space_ids, spaceId];
    // 取消本人个人库时同步清空其文档级选择（document_ids 只属于个人库收窄）
    const nextDocs =
      spaceId === personalId && !next.includes(spaceId) ? [] : selection.document_ids;
    onSelectionChange({ space_ids: next, document_ids: nextDocs });
  };

  const toggleDocument = (documentId: string) => {
    const next = selection.document_ids.includes(documentId)
      ? selection.document_ids.filter((id) => id !== documentId)
      : [...selection.document_ids, documentId];
    // m3：选文档时自动带上本人个人库 space_id（document_ids 只属于个人库收窄，不得出现
    // space_ids:[] + document_ids:[...] 的畸形 scope）
    const nextSpaces =
      personalId !== null && !selection.space_ids.includes(personalId)
        ? [...selection.space_ids, personalId]
        : selection.space_ids;
    onSelectionChange({ space_ids: nextSpaces, document_ids: next });
  };

  const toggleDrill = (spaceId: string) => {
    const next = docOpen === spaceId ? null : spaceId;
    setDocOpen(next);
    if (next !== null) {
      setDocFilter('');
      setDocuments([]);
      // m4：文档过滤 q 传进请求（§6.2 支持 q + 分页；分页仅取首页，全量在注释说明限制）
      const seq = ++docFetchSeq.current;
      void onFetchDocuments(next).then((docs) => applyDocuments(seq, docs));
    }
  };

  // 浮层打开时若处于文档级选择态且个人库文档未加载，则自动加载
  useEffect(() => {
    if (!open || personalId === null || selection.document_ids.length === 0 || documents.length > 0) return;
    const seq = ++docFetchSeq.current;
    void onFetchDocuments(personalId).then((docs) => applyDocuments(seq, docs));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // m4：文档名过滤即时把 q 传给服务端（客户端同时过滤已加载集；>分页页量的服务端过滤
  // 受「仅首页」限制，注释登记：全量分页拉取留给上传结果层，检索 chip 收窄按名称命中足够）。
  // 清空搜索框时须重新拉取未过滤列表：documents 可能已被上一轮 q 结果覆盖，仅靠本地 filter 不够。
  const onDocFilterChange = (value: string) => {
    setDocFilter(value);
    if (personalId === null) return;
    const q = value.trim();
    const seq = ++docFetchSeq.current;
    void onFetchDocuments(personalId, q === '' ? undefined : q).then((docs) =>
      applyDocuments(seq, docs),
    );
  };

  const visibleDocuments = documents.filter((doc) =>
    docFilter.trim() === '' ? true : doc.name.toLowerCase().includes(docFilter.trim().toLowerCase()),
  );

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={copy.chat.composer.scopeAria}
          className={
            'relative inline-flex h-8 items-center gap-1 rounded-[var(--radius-buttons)] border px-3 ' +
            'text-[14px] text-ink-black transition-colors duration-[var(--duration-fast)] ' +
            (open ? 'border-hairline bg-mist-gray' : 'border-hairline hover:bg-mist-gray')
          }
        >
          {!selectedAll && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-ink-black" />}
          <span className="max-w-[160px] truncate">{summary}</span>
          <ChevronDown
            aria-hidden="true"
            className={`h-4 w-4 text-slate-gray transition-transform duration-[var(--duration-base)] ease-[var(--ease-in-out)] ${
              open ? 'rotate-180' : ''
            }`}
          />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="top"
          sideOffset={8}
          align="start"
          className="ui-menu-content flex max-h-[360px] w-[280px] flex-col rounded-[var(--radius-elevatedcards)] bg-paper-white p-1 shadow-[var(--shadow-subtle)]"
        >
          {spaces.length > 8 && (
            <div className="border-b border-hairline p-2">
              <div className="relative">
                <Search aria-hidden="true" className="absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-smoke-gray" />
                <input
                  type="search"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder={copy.chat.composer.scopeSearchPlaceholder}
                  aria-label={copy.chat.composer.scopeSearchPlaceholder}
                  className="h-9 w-full rounded-[var(--radius-inputs)] border border-hairline bg-paper-white pl-9 pr-3 text-[15px] outline-none placeholder:text-smoke-gray focus:border-ink-black"
                />
              </div>
            </div>
          )}
          <div className="overflow-y-auto">
            {visibleSpaces.map((space) => {
              const checked = selection.space_ids.includes(space.id);
              const isPersonal = space.id === personalId;
              return (
                <div key={space.id}>
                  <div className="group flex h-9 items-center rounded-[var(--radius-images)] px-3 hover:bg-mist-gray">
                    <button
                      type="button"
                      role="checkbox"
                      aria-checked={checked}
                      onClick={() => toggleSpace(space.id)}
                      className="flex min-w-0 flex-1 items-center justify-between text-left"
                    >
                      <span className="truncate text-[15px] text-ink-black">{space.name}</span>
                      {checked && <Check aria-hidden="true" className="ml-2 h-4 w-4 shrink-0 text-ink-black" />}
                    </button>
                    {isPersonal && (
                      <button
                        type="button"
                        aria-label={copy.chat.composer.scopeDocumentDrillAria}
                        onClick={() => toggleDrill(space.id)}
                        className="ml-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[var(--radius-images)] text-slate-gray transition-colors duration-[var(--duration-fast)] hover:text-ink-black"
                      >
                        <ChevronRight
                          aria-hidden="true"
                          className={`h-4 w-4 transition-transform duration-[var(--duration-base)] ease-[var(--ease-in-out)] ${
                            docOpen === space.id ? 'rotate-90' : ''
                          }`}
                        />
                      </button>
                    )}
                  </div>
                  {isPersonal && docOpen === space.id && (
                    <div className="ml-4 border-l border-hairline pl-2">
                      <div className="relative p-1">
                        <Search aria-hidden="true" className="absolute top-1/2 left-3 h-3.5 w-3.5 -translate-y-1/2 text-smoke-gray" />
                        <input
                          type="search"
                          value={docFilter}
                          onChange={(event) => onDocFilterChange(event.target.value)}
                          placeholder={copy.chat.composer.scopeDocumentSearchPlaceholder}
                          aria-label={copy.chat.composer.scopeDocumentSearchPlaceholder}
                          className="h-8 w-full rounded-[var(--radius-inputs)] border border-hairline bg-paper-white pl-8 pr-2 text-[14px] outline-none placeholder:text-smoke-gray focus:border-ink-black"
                        />
                      </div>
                      {visibleDocuments.length === 0 ? (
                        <p className="px-3 py-2 text-[14px] text-smoke-gray">{copy.states.empty}</p>
                      ) : (
                        visibleDocuments.map((doc) => {
                          const docChecked = selection.document_ids.includes(doc.id);
                          return (
                            <button
                              key={doc.id}
                              type="button"
                              role="checkbox"
                              aria-checked={docChecked}
                              onClick={() => toggleDocument(doc.id)}
                              className="flex h-8 w-full items-center justify-between rounded-[var(--radius-images)] px-3 text-left hover:bg-mist-gray"
                            >
                              <span className="truncate text-[14px] text-ink-black">{doc.name}</span>
                              {docChecked && <Check aria-hidden="true" className="ml-2 h-3.5 w-3.5 shrink-0 text-ink-black" />}
                            </button>
                          );
                        })
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            {visibleSpaces.length === 0 && (
              <p className="px-3 py-4 text-center text-[15px] text-smoke-gray">{copy.states.empty}</p>
            )}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
