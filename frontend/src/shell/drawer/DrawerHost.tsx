/*
 * 全屏抽屉框架（shared-shell 规格 §1–§3、§7；共用基座 §5.1–§5.2）。
 * - 自底部滑上（400ms --ease-out）；四种关闭方式：Esc、关闭按钮、浏览器返回键、下滑手势
 *   （跟手位移，松手超过抽屉高度 25% 关闭，否则回弹 250ms --ease-in-out）。
 * - 聊天主页在抽屉下方保持挂载不卸载（抽屉为覆盖层，路由不替换主页组件实例）。
 * - URL 为唯一状态源：刷新、铃铛跳转、粘贴链接均恢复到对应层；
 *   未注册层深链落抽屉首层占位（规格 §3）。
 * - 五步层级下钻动画（§5.2）：左栏列表与原内容淡出（150ms）→ 被点击项名称 FLIP 到左栏
 *   第一位（400ms）→ 下级菜单右移 8px 淡入（250ms，延迟 150ms 启动）→ 返回按钮落位
 *   （150ms，第 3 步结束后启动）；返回为完整反向回放（--ease-in-out）。
 * - 下钻层数不限：由 registry 递归 children 表达，无硬编码上限（规格 §2）。
 * - Esc 逐层向上：下钻层先返回上一层，顶层关闭抽屉（经全局 Esc 栈，Radix 浮层由空盾隔离）。
 * - 窄屏（<768px）：左右两栏单栏化——首屏模块名列表，点模块整页下钻，复用同一套动画。
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { ArrowLeft, ChevronRight, X } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router';
import { useAuthState, useAuthStore } from '../../auth/AuthProvider';
import type { Role } from '../../auth/types';
import { copy } from '../../copy';
import { useEscLayer } from '../../lib/esc-stack-provider';
import {
  formatDrawerLocation,
  parseDrawerLocation,
  type DrawerSegment,
} from '../../router/drawer-params';
import { useDrawerRegistry } from './DrawerRegistryProvider';
import type { DrawerLayer } from './registry';

const SLIDE_MS = 400;
const EXIT_MS = 150;
const FLIP_MS = 400;
const BACK_IN_MS = 150;
const TOTAL_DRILL_MS = FLIP_MS + BACK_IN_MS;
/** 内容进入 / 同层切换动画时长（--duration-base = 250ms）。 */
const SWITCH_MS = 250;

interface Rect {
  top: number;
  left: number;
  fontSize: string;
  fontWeight: string;
}

interface DrillTransition {
  kind: 'drill' | 'back';
  /** 离开 / 到达的 drill 路径。 */
  from: readonly string[];
  to: readonly string[];
  /** FLIP 移动的层名。 */
  movingTitle: string;
  /** drill：from 内容里的下钻行 id；back：to 内容里的下钻行 id。 */
  rowId: string | null;
  phase: 'exit' | 'flip' | 'back-in';
  clone: { from: Rect; to: Rect } | null;
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 767px)').matches);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px)');
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);
  return narrow;
}

function samePath(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((segment, index) => segment === b[index]);
}

function isPrefix(prefix: readonly string[], path: readonly string[]): boolean {
  return prefix.length <= path.length && prefix.every((segment, index) => segment === path[index]);
}

export function DrawerHost({ headerRight }: { headerRight?: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const registry = useDrawerRegistry();
  const authState = useAuthState();
  const authStore = useAuthStore();
  const role: Role = authState.user?.role ?? 'user';
  // 逻辑会话键：authSessionId:userId；变化时抽屉内容子树重挂载（跨会话数据残留防护）。
  const sessionKey =
    authState.status === 'authenticated' && authState.user !== null && authStore.getAuthSessionId() !== null
      ? `${authStore.getAuthSessionId()}:${authState.user.id}`
      : null;
  const reducedMotion = useReducedMotion();
  const narrow = useNarrow();

  const parsed = useMemo(() => parseDrawerLocation(location.pathname), [location.pathname]);
  const resolved = useMemo(
    () =>
      parsed.open && parsed.segment !== null
        ? registry.resolve(parsed.segment, parsed.drill, role)
        : { layers: [] as readonly DrawerLayer[], exact: true },
    [registry, parsed, role],
  );
  const adminAccessDenied =
    parsed.open && parsed.segment === 'admin' && !registry.hasAdminModules(role);
  const drawerOpen = parsed.open && !adminAccessDenied;

  useEffect(() => {
    if (adminAccessDenied) {
      navigate('/', { replace: true });
    }
  }, [adminAccessDenied, navigate]);

  // 管理段顶层缺省选中「总览」（运维 / 超管首屏默认选中，各端 §7.1）
  useEffect(() => {
    if (drawerOpen && parsed.segment === 'admin' && parsed.drill.length === 0) {
      const dashboard = registry.resolve('admin', ['dashboard'], role);
      if (dashboard.layers.length > 0) {
        navigate('/admin/dashboard', { replace: true });
      }
    }
  }, [drawerOpen, parsed, registry, role, navigate]);

  // ---- 滑上 / 滑下（打开与关闭） ----
  const [slide, setSlide] = useState<'closed' | 'enter' | 'open' | 'closing'>(
    drawerOpen ? 'open' : 'closed',
  );
  // 关闭动画期间保留最后打开的渲染快照
  const snapshotRef = useRef({ parsed, layers: resolved.layers });
  if (drawerOpen) {
    snapshotRef.current = { parsed, layers: resolved.layers };
  }
  useEffect(() => {
    if (drawerOpen && (slide === 'closed' || slide === 'closing')) {
      setSlide('enter');
      return;
    }
    if (!drawerOpen && slide === 'open') {
      setSlide('closing');
    }
  }, [drawerOpen, slide]);
  useEffect(() => {
    if (slide === 'enter') {
      // 下一帧切到 open，触发 translateY 100%→0 过渡
      const frame = requestAnimationFrame(() => setSlide('open'));
      return () => cancelAnimationFrame(frame);
    }
    if (slide === 'closing') {
      const timer = setTimeout(() => setSlide('closed'), SLIDE_MS);
      return () => clearTimeout(timer);
    }
  }, [slide]);

  const mounted = slide !== 'closed';
  const shown = snapshotRef.current;
  const shownSegment: DrawerSegment = shown.parsed.segment ?? 'personal';
  const shownDrill = shown.parsed.drill;
  const shownLayers = shown.layers;

  // 铃铛跳转 / 深链（共用基座 §4）：抽屉自关闭直接打开到深层时，
  // 滑上（--duration-slow）完成后内容播一次 250ms（--duration-base）进入动画；
  // 刷新恢复时 slide 直接为 open、不经 enter，不播动画直出。
  const [enterKick, setEnterKick] = useState(false);
  useEffect(() => {
    if (slide === 'enter' && parsed.drill.length > 0) {
      setEnterKick(true);
    }
  }, [slide, parsed.drill.length]);
  useEffect(() => {
    if (!enterKick) {
      return;
    }
    const timer = setTimeout(() => setEnterKick(false), SWITCH_MS);
    return () => clearTimeout(timer);
  }, [enterKick]);

  // ---- 五步下钻动画机 ----
  const panelRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [transition, setTransition] = useState<DrillTransition | null>(null);
  const timersRef = useRef<number[]>([]);
  const lastDrillRef = useRef<readonly string[]>(shownDrill);

  const clearTimers = useCallback(() => {
    for (const timer of timersRef.current) {
      clearTimeout(timer);
    }
    timersRef.current = [];
  }, []);

  const measure = useCallback((selector: string): Rect | null => {
    const panel = panelRef.current;
    const element = panel?.querySelector<HTMLElement>(selector);
    if (panel == null || element == null) {
      return null;
    }
    const panelBox = panel.getBoundingClientRect();
    const box = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return {
      top: box.top - panelBox.top,
      left: box.left - panelBox.left,
      fontSize: style.fontSize,
      fontWeight: style.fontWeight,
    };
  }, []);

  // URL 变化驱动动画：append → drill；pop → back；其余 → 同层切换交叉淡变
  useLayoutEffect(() => {
    if (!drawerOpen) {
      lastDrillRef.current = [];
      setTransition(null);
      clearTimers();
      return;
    }
    const from = lastDrillRef.current;
    const to = parsed.drill;
    if (samePath(from, to)) {
      return;
    }
    lastDrillRef.current = to;
    clearTimers();
    if (reducedMotion) {
      setTransition(null);
      return;
    }

    const drillDown = to.length === from.length + 1 && isPrefix(from, to);
    const back = from.length === to.length + 1 && isPrefix(to, from);
    // 桌面端顶层 ↔ 模块选中为同层切换（§5.2 左栏换选交叉淡变），不下钻动画
    const desktopSwitch = !narrow && from.length <= 1 && to.length <= 1;
    if ((!drillDown && !back) || desktopSwitch || resolved.layers.length === 0) {
      setTransition(null);
      return;
    }

    const kind: DrillTransition['kind'] = drillDown ? 'drill' : 'back';
    const movingLayer = drillDown
      ? resolved.layers[resolved.layers.length - 1]
      : // back：离开的是 from 路径最深层（registry 按角色再解析一次）
        registry.resolve(parsed.segment ?? 'personal', from, role).layers[
          registry.resolve(parsed.segment ?? 'personal', from, role).layers.length - 1
        ];
    if (movingLayer === undefined) {
      setTransition(null);
      return;
    }
    const rowId = movingLayer.id;

    // 立即测量：drill 时源行在 from 内容里（可见），目标槽位在 to 导航里（隐藏渲染）；
    // back 时源在 from 导航标题槽（可见），目标行在 to 内容里（隐藏渲染）。
    const sourceSelector =
      kind === 'drill' ? `[data-drill-row="${rowId}"]` : '[data-drill-title-slot]';
    const targetSelector =
      kind === 'drill' ? '[data-drill-title-slot]' : `[data-drill-row="${rowId}"]`;
    const fromRect = measure(sourceSelector);
    const toRect = measure(targetSelector);

    setTransition({
      kind,
      from,
      to,
      movingTitle: movingLayer.title,
      rowId,
      phase: 'exit',
      clone: fromRect !== null && toRect !== null ? { from: fromRect, to: toRect } : null,
    });
    timersRef.current = [
      window.setTimeout(() => {
        setTransition((current) => (current === null ? null : { ...current, phase: 'flip' }));
      }, EXIT_MS),
      window.setTimeout(() => {
        setTransition((current) => (current === null ? null : { ...current, phase: 'back-in' }));
      }, FLIP_MS),
      window.setTimeout(() => {
        setTransition(null);
      }, TOTAL_DRILL_MS),
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawerOpen, parsed, resolved, narrow, reducedMotion, registry, role, measure, clearTimers]);

  // ---- Esc 逐层向上：下钻层先返回上一层，顶层关闭抽屉 ----
  // esc-stack 监听是原生 DOM 监听，回调可能在下一次 React 提交前触发（快速连按 Esc
  // 实测命中陈旧闭包）。路径经 ref 承载：回调派发时同步推进；与 useLocation 的对齐只放
  // 在提交后的 effect 里做——若每次渲染都同步，navigate 的 pushState 与 RouterContext
  // 传播之间的任何渲染（轮询 / 动画定时器触发）都会用陈旧 location 把已推进的 ref 刷回去。
  const escPathRef = useRef(location.pathname);
  useEffect(() => {
    escPathRef.current = location.pathname;
  }, [location.pathname]);
  useEscLayer(() => {
    const current = parseDrawerLocation(escPathRef.current);
    if (!current.open || current.segment === null) {
      return;
    }
    const next =
      current.drill.length > 0
        ? formatDrawerLocation({
            open: true,
            segment: current.segment,
            drill: current.drill.slice(0, -1),
          })
        : '/';
    escPathRef.current = next;
    navigate(next);
  }, drawerOpen);

  // ---- 下滑手势：跟手位移，超过 25% 关闭，否则回弹 250ms ----
  const [dragOffset, setDragOffset] = useState<number | null>(null);
  const [rebound, setRebound] = useState(false);
  const dragStartRef = useRef<{ y: number; engaged: boolean } | null>(null);

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const scroller = contentRef.current;
    dragStartRef.current = {
      y: event.clientY,
      engaged: scroller === null || scroller.scrollTop <= 0,
    };
  }, []);
  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const start = dragStartRef.current;
      if (start === null || !start.engaged) {
        return;
      }
      const delta = event.clientY - start.y;
      if (delta > 0) {
        setRebound(false);
        setDragOffset(delta);
      }
    },
    [],
  );
  const onPointerUp = useCallback(() => {
    const offset = dragOffset;
    dragStartRef.current = null;
    if (offset === null) {
      return;
    }
    const height = panelRef.current?.getBoundingClientRect().height ?? window.innerHeight;
    if (offset > height * 0.25) {
      setDragOffset(null);
      navigate('/');
    } else {
      setRebound(true);
      setDragOffset(null);
    }
  }, [dragOffset, navigate]);

  // ---- 导航 / 关闭 ----
  const close = useCallback(() => navigate('/'), [navigate]);
  const drillTo = useCallback(
    (id: string) => {
      navigate(
        formatDrawerLocation({
          open: true,
          segment: parsed.segment,
          drill: [...parsed.drill, id],
        }),
      );
    },
    [navigate, parsed],
  );
  const selectModule = useCallback(
    (segment: DrawerSegment, id: string) => {
      navigate(formatDrawerLocation({ open: true, segment, drill: [id] }));
    },
    [navigate],
  );

  if (!mounted) {
    return null;
  }

  const drawerCopy = copy.shell.drawer;
  const deepest = shownLayers[shownLayers.length - 1];
  const drilled = shownDrill.length >= 2;
  const title =
    shownSegment === 'personal'
      ? drawerCopy.personalTitle
      : (shownLayers[0]?.title ?? drawerCopy.adminSegmentLabel);

  // 返回目标：上一层路径与名称
  const backLabel =
    shownDrill.length >= 2
      ? shownDrill.length === 2
        ? shownLayers[0]?.title ?? ''
        : (shownLayers[shownLayers.length - 2]?.title ?? '')
      : '';
  const goBack = () => {
    navigate(
      formatDrawerLocation({
        open: true,
        segment: parsed.segment,
        drill: parsed.drill.slice(0, -1),
      }),
    );
  };

  const renderModuleList = (phase: 'idle' | 'exit' | 'enter') => {
    const personalModules = registry.listModules('personal', role);
    const adminModules = registry.listModules('admin', role);
    const selected = shownDrill[0] ?? null;
    const list = (modules: typeof personalModules, segment: DrawerSegment) => (
      <ul className="flex flex-col gap-0.5">
        {modules.map((module) => (
          <li key={module.id}>
            <button
              type="button"
              data-drill-row={narrow ? module.id : undefined}
              onClick={() => selectModule(segment, module.id)}
              className={`flex h-10 w-full items-center justify-between gap-2 rounded-[var(--radius-images)] px-3 text-left text-body transition-colors duration-150 hover:bg-mist-gray ${
                selected === module.id ? 'bg-mist-gray font-w480' : 'font-normal'
              }`}
            >
              <span className="min-w-0 truncate">{module.title}</span>
              {module.renderSummary !== undefined ? module.renderSummary() : null}
            </button>
          </li>
        ))}
      </ul>
    );
    return (
      <div
        data-nav-variant="modules"
        className={`${phase === 'exit' ? 'drill-exit' : ''} ${phase === 'enter' ? 'drill-content-return' : ''}`}
      >
        <p className="px-3 pb-1 text-caption text-ash-gray">{drawerCopy.personalSegmentLabel}</p>
        {list(personalModules, 'personal')}
        {adminModules.length > 0 && (
          <>
            <hr className="my-3 border-0 border-t border-hairline" />
            <p className="px-3 pb-1 text-caption text-ash-gray">{drawerCopy.adminSegmentLabel}</p>
            {list(adminModules, 'admin')}
          </>
        )}
      </div>
    );
  };

  const renderDrilledNav = (layer: DrawerLayer, phase: 'idle' | 'exit' | 'enter') => (
    <div data-nav-variant="drilled" className={phase === 'exit' ? 'drill-exit' : ''}>
      {phase !== 'exit' && (
        <button
          type="button"
          onClick={goBack}
          aria-label={drawerCopy.backAria(backLabel)}
          className={`flex h-8 items-center gap-1 text-caption text-slate-gray transition-colors duration-150 hover:text-ink-black ${
            transition !== null && transition.phase !== 'back-in' && phase !== 'enter'
              ? 'invisible'
              : 'drill-back-enter'
          }`}
        >
          <ArrowLeft size={16} aria-hidden />
          <span>{backLabel}</span>
        </button>
      )}
      <p
        data-drill-title-slot
        className={`mt-2 text-body-lg font-medium text-ink-black ${
          transition !== null && transition.phase !== 'back-in' && phase !== 'enter'
            ? 'invisible'
            : ''
        }`}
      >
        {layer.title}
      </p>
    </div>
  );

  const renderLayerContent = (
    layers: readonly DrawerLayer[],
    phase: 'idle' | 'exit' | 'enter',
    enterKind: 'enter' | 'return' | 'switch',
  ) => {
    const layer = layers[layers.length - 1];
    const phaseClass =
      phase === 'exit'
        ? 'drill-exit'
        : phase === 'enter'
          ? enterKind === 'switch'
            ? 'drill-switch'
            : enterKind === 'return'
              ? 'drill-content-return'
              : 'drill-content-enter'
          : '';
    if (layer === undefined) {
      // 顶层 / 未注册层：抽屉首层占位（规格 §3）
      return (
        <div data-content-variant="placeholder" className={phaseClass}>
          <p className="text-caption text-smoke-gray">{drawerCopy.topPlaceholderBody}</p>
        </div>
      );
    }
    if (layer.render !== undefined) {
      // 会话键：authSessionId:userId 变化时强制重挂载内容子树，
      // 立即清空账号相关 state（跨逻辑会话数据残留防护；review Major 1）。
      return (
        <div key={sessionKey ?? 'no-session'} className={phaseClass}>
          {layer.render({ path: shownDrill })}
        </div>
      );
    }
    if (layer.children !== undefined && layer.children.length > 0) {
      return (
        <div className={phaseClass}>
          <ul className="flex flex-col">
            {layer.children
              .filter((child) => child.roles === undefined || child.roles.includes(role))
              .map((child) => (
                <li key={child.id}>
                  <button
                    type="button"
                    data-drill-row={child.id}
                    onClick={() => drillTo(child.id)}
                    className="flex h-12 w-full items-center justify-between rounded-[var(--radius-images)] px-3 text-left text-body transition-colors duration-150 hover:bg-mist-gray"
                  >
                    <span>{child.title}</span>
                    <span className="flex items-center gap-2">
                      {child.renderSummary !== undefined ? child.renderSummary() : null}
                      <ChevronRight size={16} className="text-slate-gray" aria-hidden />
                    </span>
                  </button>
                </li>
              ))}
          </ul>
        </div>
      );
    }
    return null;
  };

  // 动画期间的 from/to 渲染：from 为 transition.from 解析结果，to 为当前 URL 解析结果
  const fromLayers =
    transition === null
      ? null
      : registry.resolve(shownSegment, transition.from, role).layers;
  const transitioning = transition !== null && fromLayers !== null;

  const navArea = (() => {
    if (!transitioning) {
      // 空闲：drilled 显示 返回 + 层名；否则模块清单（窄屏 level 0 即全宽列表）
      return drilled ? renderDrilledNav(deepest, 'idle') : renderModuleList('idle');
    }
    const toDrilled = transition.to.length >= 2;
    const toLayer = shownLayers[shownLayers.length - 1];
    const fromDrilled = transition.from.length >= 2;
    const fromLayer = fromLayers[fromLayers.length - 1];
    return (
      <div className="relative h-full">
        <div className="absolute inset-0">
          {fromDrilled && fromLayer !== undefined
            ? renderDrilledNav(fromLayer, 'exit')
            : renderModuleList('exit')}
        </div>
        <div
          className={`absolute inset-0 ${transition.phase === 'back-in' ? '' : 'drill-hidden'}`}
        >
          {toDrilled && toLayer !== undefined
            ? renderDrilledNav(toLayer, transition.kind === 'drill' ? 'enter' : 'idle')
            : renderModuleList('enter')}
        </div>
      </div>
    );
  })();

  const contentArea = (() => {
    if (!transitioning) {
      return renderLayerContent(
        shownLayers,
        enterKick ? 'enter' : 'idle',
        enterKick ? 'enter' : 'switch',
      );
    }
    const enterKind = transition.kind === 'drill' ? 'enter' : 'return';
    return (
      <div className="relative h-full">
        <div className="absolute inset-0">{renderLayerContent(fromLayers, 'exit', enterKind)}</div>
        <div className={`absolute inset-0 ${transition.phase === 'exit' ? 'drill-hidden' : ''}`}>
          {transition.phase === 'exit'
            ? renderLayerContent(shownLayers, 'idle', enterKind)
            : renderLayerContent(shownLayers, 'enter', enterKind)}
        </div>
      </div>
    );
  })();

  const narrowListView = narrow && shownDrill.length === 0 && !transitioning;

  return (
    <div className="fixed inset-0 z-40" role="dialog" aria-modal="true" aria-label={title}>
      <div
        ref={panelRef}
        data-slide={slide}
        data-dragging={dragOffset !== null ? 'true' : undefined}
        data-rebound={rebound ? 'true' : undefined}
        className="drawer-panel absolute inset-0 bg-paper-white shadow-[var(--shadow-subtle-2)]"
        style={dragOffset !== null ? { transform: `translateY(${dragOffset}px)` } : undefined}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <header className="mt-10 flex items-center gap-4 px-5 md:px-10">
          <button
            type="button"
            onClick={close}
            aria-label={drawerCopy.closeAria}
            className="flex h-10 w-10 items-center justify-center rounded-full transition-colors duration-150 hover:bg-mist-gray"
          >
            <X size={20} aria-hidden />
          </button>
          <h1 className="font-sohne text-heading-sm font-medium leading-heading-sm tracking-heading-sm md:font-signifier md:text-heading md:font-normal md:leading-heading md:tracking-heading">
            {title}
          </h1>
          <div className="ml-auto">{headerRight}</div>
        </header>
        <div className="mt-10 flex gap-10 px-5 md:px-10" style={{ height: 'calc(100% - 152px)' }}>
          {!narrowListView && (
            <nav
              className={`${narrow ? 'hidden' : ''} w-60 shrink-0 overflow-y-auto`}
              aria-label={title}
            >
              {navArea}
            </nav>
          )}
          <div
            ref={contentRef}
            className={`min-w-0 flex-1 overflow-y-auto ${narrow ? '' : 'max-w-[720px]'}`}
          >
            {narrowListView ? renderModuleList('idle') : contentArea}
          </div>
        </div>
        {transition?.clone != null && (
          <FlipClone key={`${transition.kind}-${transition.movingTitle}`} clone={transition.clone} title={transition.movingTitle} />
        )}
      </div>
    </div>
  );
}

/** 第 3 步：被点击项名称 FLIP 位移（400ms --ease-in-out），落位字级 Sohne 500 20px。 */
function FlipClone({ clone, title }: { clone: { from: Rect; to: Rect }; title: string }) {
  const ref = useRef<HTMLParagraphElement | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (element === null) {
      return;
    }
    const frame = requestAnimationFrame(() => {
      element.style.top = `${clone.to.top}px`;
      element.style.left = `${clone.to.left}px`;
      element.style.fontSize = clone.to.fontSize;
      element.style.fontWeight = clone.to.fontWeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [clone]);
  return (
    <p
      ref={ref}
      className="drill-flip-clone text-ink-black"
      style={{
        top: clone.from.top,
        left: clone.from.left,
        fontSize: clone.from.fontSize,
        fontWeight: clone.from.fontWeight,
      }}
    >
      {title}
    </p>
  );
}
