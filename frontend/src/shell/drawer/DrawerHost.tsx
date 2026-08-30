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
  type CSSProperties,
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
const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), [href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface Rect {
  top: number;
  left: number;
  fontSize: string;
  fontWeight: string;
}

interface DrillTransition {
  /** drill/back 走五步动画；switch 为同层切换序列：旧内容原地淡出 150ms → 新内容自下而上淡入 250ms（无 FLIP）。 */
  kind: 'drill' | 'back' | 'switch';
  /** 离开 / 到达的 drill 路径。 */
  from: readonly string[];
  to: readonly string[];
  /** FLIP 移动的层名（switch 不用）。 */
  movingTitle: string;
  /** drill：from 内容里的下钻行 id；back：to 内容里的下钻行 id（switch 为 null）。 */
  rowId: string | null;
  phase: 'exit' | 'switch-in' | 'flip' | 'back-in';
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

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.offsetParent !== null || element === document.activeElement,
  );
}

function useDrawerFocusTrap(open: boolean) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const focusedRef = useRef(false);

  useLayoutEffect(() => {
    if (open === wasOpenRef.current) return;
    wasOpenRef.current = open;
    if (open) {
      restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      focusedRef.current = false;
      return;
    }
    restoreFocusRef.current?.focus();
    restoreFocusRef.current = null;
    focusedRef.current = false;
  }, [open]);

  useLayoutEffect(() => {
    if (!open || focusedRef.current) return;
    const container = dialogRef.current;
    if (container === null) return;
    (focusableIn(container)[0] ?? container).focus();
    focusedRef.current = true;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const container = dialogRef.current;
      if (container === null) return;
      const focusable = focusableIn(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || active === container || !container.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || active === container || !container.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [open]);

  return dialogRef;
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
  // 拒绝判定仅在认证就绪后生效：整页加载深链时 user 短暂为空、role 回退 'user'，
  // 此时误判「无管理段权限」会把 /admin/* 深链弹回主页（违反共用基座 §5.1 粘贴链接恢复）
  const adminAccessDenied =
    parsed.open &&
    parsed.segment === 'admin' &&
    authState.user !== null &&
    !registry.hasAdminModules(role);
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
  const dialogRef = useDrawerFocusTrap(mounted);
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
  /** 定时器已为哪个 transition 上膛（打断旧过渡时据此清膛再上膛）。 */
  const armedForRef = useRef<DrillTransition | null>(null);
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
      // left 计入行内边距：克隆文字落点对齐行文字（而非行盒左缘），落位时与目标文字重合不重影
      left: box.left - panelBox.left + (parseFloat(style.paddingLeft) || 0),
      fontSize: style.fontSize,
      fontWeight: style.fontWeight,
    };
  }, []);

  // URL 变化驱动动画：append → drill；pop → back；其余 → 同层切换序列（先淡出后淡入）。
  // 派生必须在渲染期完成（React「渲染中调整 state」模式：同步重渲染，中间帧不提交 DOM）——
  // 若放在提交后的 effect 里，URL 提交会先落一帧「只有新层」的空闲树，旧层内容节点在该提交
  // 已被卸载，过渡开始时 from 侧只能新挂载（淡出的是骨架屏而非真实内容，且 idle/过渡结构差
  // 异会在过渡结束时再卸载一次新层，闪第二遍骨架屏）。
  if (drawerOpen && !samePath(lastDrillRef.current, parsed.drill)) {
    const from = lastDrillRef.current;
    const to = parsed.drill;
    lastDrillRef.current = to;
    if (reducedMotion) {
      if (transition !== null) setTransition(null);
    } else {
      const drillDown = to.length === from.length + 1 && isPrefix(from, to);
      const back = from.length === to.length + 1 && isPrefix(to, from);
      // 桌面端顶层 ↔ 模块选中为同层切换（§5.2 左栏换选），不下钻动画
      const desktopSwitch = !narrow && from.length <= 1 && to.length <= 1;
      if ((!drillDown && !back) || desktopSwitch || resolved.layers.length === 0) {
        // 同层切换（§5.2）：左右栏不换，右栏先旧内容原地淡出 150ms（--duration-fast），
        // 再接新内容自下而上淡入 250ms（--duration-base，drill-switch）。
        // 抽屉滑上/滑下期间（slide 非 open）不叠加，直出；无层可切（占位）同样直出。
        if (resolved.layers.length > 0 && slide === 'open') {
          setTransition({ kind: 'switch', from, to, movingTitle: '', rowId: null, phase: 'exit', clone: null });
        } else if (transition !== null) {
          setTransition(null);
        }
      } else {
        const kind: DrillTransition['kind'] = drillDown ? 'drill' : 'back';
        const movingLayer = drillDown
          ? resolved.layers[resolved.layers.length - 1]
          : // back：离开的是 from 路径最深层（registry 按角色再解析一次）
            registry.resolve(parsed.segment ?? 'personal', from, role).layers[
              registry.resolve(parsed.segment ?? 'personal', from, role).layers.length - 1
            ];
        if (movingLayer === undefined) {
          if (transition !== null) setTransition(null);
        } else {
          // 两段式测量：先挂过渡渲染（from 侧为已挂载内容的保留节点、to 侧 drill-hidden
          // 隐藏渲染，visibility 隐藏可测量），由下方 layout effect 量 FLIP 起止点。
          setTransition({
            kind,
            from,
            to,
            movingTitle: movingLayer.title,
            rowId: movingLayer.id,
            phase: 'exit',
            clone: null,
          });
        }
      }
    }
  }

  // 抽屉关闭时复位动画机（清理残留计时器；路径基线归零）
  useEffect(() => {
    if (drawerOpen) return;
    lastDrillRef.current = [];
    armedForRef.current = null;
    clearTimers();
    if (transition !== null) setTransition(null);
  }, [drawerOpen, transition, clearTimers]);

  // 五步动画第二遍：过渡渲染已提交，测量 FLIP 起止点并启动相位定时器；switch 仅 150ms 淡出淡入。
  // drill：源行在 from 内容（可见），目标槽位在 to 导航（隐藏渲染）；
  // back：源在 from 导航标题槽（可见），目标行在 to 内容（隐藏渲染）。
  // 源/目标缺失（如从 ⋯ 菜单进入版本记录层）时 clone 保持 null：淡出淡入照播，仅跳过 FLIP。
  useLayoutEffect(() => {
    if (transition === null || transition.phase !== 'exit' || transition.clone !== null) {
      return;
    }
    if (armedForRef.current === transition) {
      return;
    }
    // 打断旧过渡：清掉它的残留定时器，再为本过渡上膛
    clearTimers();
    armedForRef.current = transition;
    if (transition.kind === 'switch') {
      // 两相定时：exit（旧内容原地淡出）→ switch-in（新内容自下而上淡入）→ 复位；
      // to 侧在 exit 期间以 drill-hidden 预挂载（摊薄重模块挂载成本），相位切换后才可见
      timersRef.current = [
        window.setTimeout(() => {
          setTransition((current) =>
            current === null ? null : { ...current, phase: 'switch-in' },
          );
        }, EXIT_MS),
        window.setTimeout(() => {
          armedForRef.current = null;
          setTransition(null);
        }, EXIT_MS + SWITCH_MS),
      ];
      return;
    }
    const rowId = transition.rowId;
    const sourceSelector =
      transition.kind === 'drill' ? `[data-drill-row="${rowId}"]` : '[data-drill-title-slot]';
    const targetSelector =
      transition.kind === 'drill' ? '[data-drill-title-slot]' : `[data-drill-row="${rowId}"]`;
    const fromRect = measure(sourceSelector);
    const toRect = measure(targetSelector);
    if (fromRect !== null && toRect !== null) {
      const measured = transition;
      setTransition((current) =>
        current === measured ? { ...current, clone: { from: fromRect, to: toRect } } : current,
      );
    }
    timersRef.current = [
      window.setTimeout(() => {
        setTransition((current) => (current === null ? null : { ...current, phase: 'flip' }));
      }, EXIT_MS),
      window.setTimeout(() => {
        setTransition((current) => (current === null ? null : { ...current, phase: 'back-in' }));
      }, FLIP_MS),
      window.setTimeout(() => {
        armedForRef.current = null;
        setTransition(null);
      }, TOTAL_DRILL_MS),
    ];
  }, [transition, measure, clearTimers]);

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
              : 'drill-content-rise'
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

  // 左栏区域：空闲/同层切换与过渡共用 relative 包裹 + 路径 key 的子节点——
  // 过渡开始时 from 侧与空闲节点同 key 复用（drill-exit 从真实 opacity 淡出，而非重挂载瞬隐），
  // 过渡结束时 to 侧与空闲节点同 key 复用（进入动画不被二次重挂截断）。
  const navIdle = drilled && deepest !== undefined
    ? renderDrilledNav(deepest, 'idle')
    : renderModuleList('idle');
  const navArea = (() => {
    if (!transitioning || transition.kind === 'switch') {
      return (
        <div className="relative h-full">
          <div key={`nav:${shownDrill.join('/')}`}>{navIdle}</div>
        </div>
      );
    }
    const toDrilled = transition.to.length >= 2;
    const toLayer = shownLayers[shownLayers.length - 1];
    const fromDrilled = transition.from.length >= 2;
    const fromLayer = fromLayers[fromLayers.length - 1];
    // 返回：左栏 to 侧自克隆起飞（flip）即淡入，途中加载、不等落位；
    // 下钻：to 侧为克隆落点（返回按钮 + 标题槽），落位（back-in）后再出现，避免与克隆重影
    const toVisible =
      transition.kind === 'back' ? transition.phase !== 'exit' : transition.phase === 'back-in';
    return (
      <div className="relative h-full">
        <div key={`nav:${transition.from.join('/')}`} className="absolute inset-0">
          {fromDrilled && fromLayer !== undefined
            ? renderDrilledNav(fromLayer, 'exit')
            : renderModuleList('exit')}
        </div>
        <div
          key={`nav:${transition.to.join('/')}`}
          className={`absolute inset-0 ${toVisible ? '' : 'drill-hidden'}`}
        >
          {toDrilled && toLayer !== undefined
            ? renderDrilledNav(toLayer, transition.kind === 'drill' ? 'enter' : 'idle')
            : renderModuleList('enter')}
        </div>
      </div>
    );
  })();

  // 内容子树按「会话 + 段 + 路径」keyed，且无论是否在过渡中都挂在同一父元素下：
  // 进入过渡时 from 侧复用已挂载内容（淡出的是真实离开内容，而非新挂载的骨架屏），
  // 结束过渡时 to 侧原位保留（不二次挂载、不再闪一次骨架屏）。
  const contentKey = (drill: readonly string[]) =>
    `${sessionKey ?? 'no-session'}:${shownSegment}:${drill.join('/')}`;
  const currentContentKey = contentKey(shownDrill);
  const contentArea = (() => {
    if (!transitioning) {
      return (
        <div>
          <div key={currentContentKey}>
            {renderLayerContent(
              shownLayers,
              enterKick ? 'enter' : 'idle',
              enterKick ? 'enter' : 'switch',
            )}
          </div>
        </div>
      );
    }
    const enterKind =
      transition.kind === 'drill' ? 'enter' : transition.kind === 'back' ? 'return' : 'switch';
    // 打断反向导航的瞬时渲染（URL 已回到 from 层、layout effect 尚未重算 transition）：
    // from 与当前层同路径，双侧同 key 会撞键污染 React 树——此时 from 侧即当前内容，跳过一次即可
    // （layout effect 同步重渲染，该中间态不会上屏）。
    const fromIsCurrent = samePath(transition.from, shownDrill);
    // 同层切换两相渲染：exit 相位 from 原地淡出（drill-exit）、to 以 drill-hidden 预挂载不可见；
    // switch-in 相位 from 卸载，to 原位以 drill-switch 自下而上淡入（节点键控保留，结束过渡不重挂）。
    if (transition.kind === 'switch') {
      const entering = transition.phase === 'switch-in';
      return (
        <div className="relative h-full">
          {!fromIsCurrent && !entering && (
            <div key={contentKey(transition.from)} className="absolute inset-0">
              {renderLayerContent(fromLayers, 'exit', enterKind)}
            </div>
          )}
          <div
            key={currentContentKey}
            className={`absolute inset-0 ${entering ? 'drill-switch' : 'drill-hidden'}`}
          >
            {renderLayerContent(shownLayers, 'idle', enterKind)}
          </div>
        </div>
      );
    }
    return (
      <div className="relative h-full">
        {!fromIsCurrent && (
          <div key={contentKey(transition.from)} className="absolute inset-0">
            {renderLayerContent(fromLayers, 'exit', enterKind)}
          </div>
        )}
        <div
          key={currentContentKey}
          className={`absolute inset-0 ${transition.phase === 'exit' ? 'drill-hidden' : ''}`}
        >
          {transition.phase === 'exit'
            ? renderLayerContent(shownLayers, 'idle', enterKind)
            : renderLayerContent(shownLayers, 'enter', enterKind)}
        </div>
      </div>
    );
  })();

  const narrowListView = narrow && shownDrill.length === 0 && !transitioning;

  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-40 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
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
        {/* 第 3 步 FLIP 克隆：exit 相位（第 2 步淡出）期间不移动，进入 flip 相位才挂载起滑 */}
        {transition?.clone != null && transition.phase !== 'exit' && (
          <FlipClone key={`${transition.kind}-${transition.movingTitle}`} clone={transition.clone} title={transition.movingTitle} />
        )}
      </div>
    </div>
  );
}

/** 第 3 步：被点击项名称 FLIP 位移（400ms --ease-in-out）。元素静态定位在终点槽位（终点字级），
 *  起点状态经 CSS 变量 --flip-from 交给 .drill-flip-clone 的 keyframes 动画——挂载即自动播放，
 *  不依赖任何 JS 回调（rAF 在部分嵌入环境无输入时会停摆，transition+内联改写的旧方案因此卡住）。 */
function FlipClone({ clone, title }: { clone: { from: Rect; to: Rect }; title: string }) {
  const dx = clone.from.left - clone.to.left;
  const dy = clone.from.top - clone.to.top;
  const scale = parseFloat(clone.from.fontSize) / parseFloat(clone.to.fontSize);
  const from = `translate(${dx}px, ${dy}px) scale(${Number.isFinite(scale) && scale > 0 ? scale : 1})`;
  return (
    <p
      className="drill-flip-clone text-ink-black"
      style={
        {
          top: clone.to.top,
          left: clone.to.left,
          fontSize: clone.to.fontSize,
          fontWeight: clone.to.fontWeight,
          '--flip-from': from,
        } as CSSProperties
      }
    >
      {title}
    </p>
  );
}
