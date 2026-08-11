/*
 * 原文预览页（fe-doc-preview；共用基座 §6；契约 §4）。
 * 独立窗口页（不套 AppShell：无侧边栏 / 抽屉 / 通知轮询），路由 /preview/:document_id，
 * query 携带 message_id? / document_version_id? / sheet?，深链粘贴可恢复。
 * - 页头：文档名（Signifier 400 44px 一行截断）+ 载体类型标签（14px ash-gray）；右侧关闭（window.close()）。
 * - 主体两栏：左预览区（flex、padding 40px、内容限宽 880px 居中），右命中导航 280px + 1px hairline。
 * - 窄屏（<768px）：导航收起为「命中点 N」按钮，点击自底部滑上半屏面板（250ms ease-out），
 *   Esc / 下滑关闭（use-swipe-close：拖动跟手、阈值或 flick 关闭、未达阈值回弹）。
 * - 不可用态（文档删除 / 版本 purging/purged / 无权限 → 404/410/403）：只显示「内容已不可用」，
 *   不显示文档名、snippet、原文或下载入口，不从缓存 / 旧状态恢复已删除元数据。
 */

import { X } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useSearchParams, useParams } from 'react-router';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { SkeletonRow } from '../ui/Skeleton';
import { EmptyState } from '../ui/states';
import { TextLink } from '../ui/TextLink';
import type { PreviewApi } from './api';
import { HitNav } from './HitNav';
import { useSwipeClose } from './use-swipe-close';
import { ImageRenderer } from './renderers/ImageRenderer';
import { SheetRenderer } from './renderers/SheetRenderer';
import { TextRenderer } from './renderers/TextRenderer';
import { WordRenderer } from './renderers/WordRenderer';
import { previewStrategy } from './strategy';
import type { DocumentPreviewResponse, PreviewMediaKind } from './types';
import { useAuthState } from '../auth/AuthProvider';

/*
 * PdfRenderer 按需加载：react-pdf/pdfjs 体积大且依赖浏览器 canvas（jsdom 不可求值），
 * 仅 PDF 载体渲染时经 React.lazy 拉取；加载期回退骨架块。
 */
const PdfRenderer = lazy(() => import('./renderers/PdfRenderer').then((module) => ({ default: module.PdfRenderer })));

/** 不可用态映射：文档删除 / 版本内容不可用（purging/purged）/ 无读取权限。 */
function isUnavailableError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.status === 410 || error.status === 403)
  );
}

type LoadState =
  | { readonly status: 'loading' }
  | { readonly status: 'unavailable' }
  | { readonly status: 'error' }
  | { readonly status: 'ready'; readonly preview: DocumentPreviewResponse };

function mediaKindLabel(mediaKind: PreviewMediaKind): string {
  const labels = copy.preview.mediaKind;
  switch (mediaKind) {
    case 'pdf':
      return labels.pdf;
    case 'word':
      return labels.word;
    case 'md':
      return labels.md;
    case 'txt':
      return labels.txt;
    case 'excel':
      return labels.excel;
    case 'csv':
      return labels.csv;
    case 'image':
      return labels.image;
    case 'code':
      return labels.code;
    case 'data':
      return labels.data;
    default:
      return labels.fallback;
  }
}

function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query);
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    },
    () => window.matchMedia(query).matches,
  );
}

/** 预览区骨架块（呼吸动画）。 */
function PreviewSkeleton() {
  return (
    <div aria-busy="true" aria-label={copy.preview.loadingAria} className="flex flex-col gap-4">
      <div aria-hidden="true" className="ui-skeleton h-[480px] rounded-[var(--radius-cards)] bg-mist-gray" />
      <div aria-hidden="true" className="ui-skeleton h-[320px] rounded-[var(--radius-cards)] bg-mist-gray" />
    </div>
  );
}

export interface PreviewPageProps {
  readonly api: PreviewApi;
}

export function PreviewPage({ api }: PreviewPageProps) {
  const params = useParams();
  const documentId = params['document_id'] ?? '';
  const [searchParams, setSearchParams] = useSearchParams();
  const messageId = searchParams.get('message_id');
  const documentVersionId = searchParams.get('document_version_id');
  const sheetParam = searchParams.get('sheet');
  const authState = useAuthState();
  const token = authState.status === 'authenticated' ? authState.token : null;

  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [retryNonce, setRetryNonce] = useState(0);
  const [currentHit, setCurrentHit] = useState<number | null>(null);
  const isNarrow = useMediaQuery('(max-width: 767px)');
  const [panelOpen, setPanelOpen] = useState(false);
  const { panelProps } = useSwipeClose(
    useCallback(() => setPanelOpen(false), []),
  );

  // 加载预览元数据（重试经 retryNonce；竞态以 nonce 作废旧响应）
  useEffect(() => {
    let cancelled = false;
    setState({ status: 'loading' });
    api.getPreview(documentId, { messageId, documentVersionId }).then(
      (preview) => {
        if (!cancelled) {
          setState({ status: 'ready', preview });
        }
      },
      (error: unknown) => {
        if (cancelled) {
          return;
        }
        setState(isUnavailableError(error) ? { status: 'unavailable' } : { status: 'error' });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, documentId, messageId, documentVersionId, retryNonce]);

  const hits = state.status === 'ready' ? state.preview.hits : [];

  // 文档 / 消息 / 版本切换：清空当前命中（渲染器锚点随内容重建）
  useEffect(() => {
    setCurrentHit(null);
    setPanelOpen(false);
  }, [documentId, messageId, documentVersionId]);

  // 打开自动定位第一处命中（渲染器负责平滑滚动至视口中央）
  useEffect(() => {
    if (state.status === 'ready' && state.preview.hits.length > 0) {
      setCurrentHit((current) => current ?? 0);
    }
  }, [state]);

  // 窄屏面板：Esc 关闭（页面在 AppShell 外，自管 keydown）
  useEffect(() => {
    if (!panelOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setPanelOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [panelOpen]);

  const onSheetChange = useCallback(
    (sheet: string) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set('sheet', sheet);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const retry = useCallback(() => setRetryNonce((nonce) => nonce + 1), []);

  // 激活 Sheet：?sheet= 合法优先 → 首个表格命中的 Sheet → 第一页签
  const sheets = state.status === 'ready' ? state.preview.sheets : null;
  const activeSheet = (() => {
    if (sheets === null || sheets.length === 0) {
      return null;
    }
    if (sheetParam !== null && sheets.some((sheet) => sheet.name === sheetParam)) {
      return sheetParam;
    }
    const fromHit = hits
      .map((hit) => ('sheet' in hit.locator ? hit.locator.sheet : null))
      .find((name): name is string => name !== null && sheets.some((sheet) => sheet.name === name));
    return fromHit ?? (sheets[0] as { name: string }).name;
  })();

  let body: ReactNode = null;
  let nav: ReactNode = null;

  if (state.status === 'loading') {
    body = <PreviewSkeleton />;
    nav = (
      <div aria-busy="true" className="flex flex-col gap-2 p-2">
        {[0, 1, 2].map((index) => (
          <SkeletonRow key={index} />
        ))}
      </div>
    );
  } else if (state.status === 'error') {
    body = (
      <div className="flex flex-col items-center gap-2 py-20">
        <p className="text-[15px] text-slate-gray">{copy.preview.error}</p>
        <TextLink onClick={retry}>{copy.preview.retry}</TextLink>
      </div>
    );
    nav = <EmptyState text={copy.preview.navEmpty} />;
  } else if (state.status === 'ready') {
    const preview = state.preview;
    const strategy = previewStrategy(preview);
    switch (strategy.renderer) {
      case 'pdf':
        body = (
          <Suspense fallback={<PreviewSkeleton />}>
            <PdfRenderer
              fileUrl={api.buildContentUrl(preview.content_url, documentVersionId)}
              token={token}
              hasTextLayer={preview.has_text_layer}
              hits={hits}
              currentHit={currentHit}
            />
          </Suspense>
        );
        break;
      case 'word':
        body = (
          <WordRenderer
            api={api}
            documentId={documentId}
            documentVersionId={documentVersionId}
            treeIndexed={preview.tree_indexed}
            hits={hits}
            currentHit={currentHit}
          />
        );
        break;
      case 'text':
        body = (
          <TextRenderer
            api={api}
            documentId={documentId}
            documentVersionId={documentVersionId}
            textKind={strategy.textKind ?? 'plain'}
            hits={hits}
            currentHit={currentHit}
          />
        );
        break;
      case 'sheet':
        body =
          sheets !== null && activeSheet !== null ? (
            <SheetRenderer
              api={api}
              documentId={documentId}
              documentVersionId={documentVersionId}
              sheets={sheets}
              activeSheet={activeSheet}
              onSheetChange={onSheetChange}
              hits={hits}
              currentHit={currentHit}
            />
          ) : (
            <EmptyState text={copy.preview.navEmpty} />
          );
        break;
      case 'image':
        body = (
          <ImageRenderer
            api={api}
            documentId={documentId}
            documentVersionId={documentVersionId}
            name={preview.name}
          />
        );
        break;
    }
    nav = <HitNav hits={hits} current={currentHit} onSelect={setCurrentHit} />;
  }

  const showChrome = state.status !== 'unavailable';

  return (
    <div className="flex h-screen flex-col bg-paper-white text-ink-black">
      {/* 页头：距顶 40px、左右留白 40px、下方 1px hairline；不可用态不渲染文档名与类型标签 */}
      <header className="shrink-0 px-10 pt-10">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            {state.status === 'ready' && (
              <>
                <h1 className="truncate font-signifier text-[44px] font-normal leading-[1.3] tracking-[-0.66px]">
                  {state.preview.name}
                </h1>
                <p className="mt-1 text-[14px] text-ash-gray">{mediaKindLabel(state.preview.media_kind)}</p>
              </>
            )}
          </div>
          <button
            type="button"
            aria-label={copy.preview.closeAria}
            onClick={() => window.close()}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray"
          >
            <X aria-hidden="true" className="h-5 w-5" />
          </button>
        </div>
        <div className="mt-4 border-b border-hairline" />
      </header>

      {state.status === 'unavailable' ? (
        <main className="flex flex-1 items-center justify-center px-10">
          <p className="text-[15px] text-slate-gray">{copy.preview.unavailable}</p>
        </main>
      ) : (
        <>
          {isNarrow && (
            <div className="shrink-0 border-b border-hairline px-10">
              <button
                type="button"
                aria-haspopup="dialog"
                aria-expanded={panelOpen}
                onClick={() => setPanelOpen(true)}
                className="flex h-9 items-center text-[15px] text-slate-gray"
              >
                {copy.preview.navTitle(hits.length)}
              </button>
            </div>
          )}
          <div className="flex min-h-0 flex-1">
            <main className="min-w-0 flex-1 overflow-y-auto px-10 py-10">
              <div className="mx-auto max-w-[880px]">{body}</div>
            </main>
            {showChrome && !isNarrow && (
              <aside
                aria-label={copy.preview.navAria}
                className="w-[280px] shrink-0 overflow-y-auto border-l border-hairline"
              >
                {nav}
              </aside>
            )}
          </div>
          {isNarrow && panelOpen && (
            <div
              role="dialog"
              aria-modal="true"
              aria-label={copy.preview.navTitle(hits.length)}
              className="preview-nav-panel fixed inset-x-0 bottom-0 z-50 flex h-[50vh] flex-col rounded-t-[var(--radius-cards)] bg-paper-white shadow-[var(--shadow-subtle-3)]"
              {...panelProps}
            >
              <div className="shrink-0 touch-none border-b border-hairline px-4 py-2 text-[15px] text-slate-gray">
                {copy.preview.navTitle(hits.length)}
              </div>
              <div data-swipe-scroll="" className="min-h-0 flex-1 overflow-y-auto">
                <HitNav
                  hits={hits}
                  current={currentHit}
                  onSelect={(index) => {
                    setCurrentHit(index);
                    setPanelOpen(false);
                  }}
                />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
