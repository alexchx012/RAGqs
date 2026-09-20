/*
 * 全屏抽屉框架（shared-shell 规格 §1–§3、§7；共用基座 §5.1–§5.2）。
 * - 自底部滑上（400ms --ease-out）；四种关闭方式：Esc、关闭按钮、浏览器返回键、下滑手势
 *   （跟手位移，松手超过抽屉高度 25% 关闭，否则回弹 250ms --ease-in-out）。
 * - 聊天主页在抽屉下方保持挂载不卸载（抽屉为覆盖层，路由不替换主页组件实例）。
 * - URL 为唯一状态源：刷新、铃铛跳转、粘贴链接均恢复到对应层；
 *   未注册层深链落抽屉首层占位（规格 §3）。
 * - 两相整页下钻动画（§5.2）：当前页主体（左栏 + 右栏）作为整体上滑渐隐（250ms --ease-in-out），
 *   随后新页主体自下方 8px 上移渐显（250ms --ease-out）；返回为完整镜像（下滑渐隐 / 自上方落位）。
 *   页头（关闭按钮 + 页级标题 + 铃铛）不参与过渡。同层切换（左栏换选）仍只动右栏内容。
 *   下钻 / 返回按注册层链深度判定（参数化层的参数尾段不计一层），并以前缀同链约束排除换链导航。
 * - 下钻层数不限：由 registry 递归 children 表达，无硬编码上限（规格 §2）。
 * - Esc 逐层向上：下钻层先返回上一层，顶层关闭抽屉（经全局 Esc 栈，Radix 浮层由空盾隔离）。
 * - 窄屏（<768px）：左右两栏单栏化——首屏模块名列表，点模块整页下钻，复用同一套动画。
 * - 抽屉作用域（drawer-visual-system §抽屉作用域不外溢）：抽屉根容器挂 [data-drawer-scope]，
 *   作用域内的 token 覆盖（近黑 / 发丝边 / 危险色 / 圆角 / 分段控件几何）只作用于抽屉子树，
 *   聊天主页保持全局值；内容区底色改用 --surface-drawer-canvas，左栏保持 paper white。
 *   抽屉内 portal 到 body 的 Radix 浮层拿不到 DOM 继承，改经 DrawerScopeContext 自带属性。
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
import { DrawerScopeContext } from './drawer-scope-context';
import type { DrawerLayer } from './registry';

const SLIDE_MS = 400;
/** 同层切换离开相：旧内容原地淡出（--duration-fast）。 */
const SWITCH_EXIT_MS = 150;
/** 进入相：新页自下而上 / 上一层自上方渐显（--duration-base）。 */
const ENTER_MS = 250;
/** 下钻 / 返回的离开相：整页主体上滑或下滑渐隐（--duration-base）。 */
const PAGE_EXIT_MS = 250;
const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), [href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface DrillTransition {
  /** drill/back 走两相整页过渡；switch 为同层切换序列：旧内容原地淡出 150ms → 新内容自下而上淡入 250ms。 */
  kind: 'drill' | 'back' | 'switch';

  /** 离开 / 到达的 drill 路径。 */
  from: readonly string[];
  to: readonly string[];
  phase: 'exit' | 'enter';
}

/**
 * 相位类：drill/back 为页面级（左栏与右栏挂同一个类，同帧起步 → 整页位移）；
 * switch 只动右栏内容（左栏未换层）：旧内容原地淡出 → 新内容自下而上淡入。
 */
function drillPhaseClass(
  kind: DrillTransition['kind'],
  phase: DrillTransition['phase'],
): string {
  if (kind === 'switch') {
    return phase === 'exit' ? 'drill-exit' : 'drill-switch';
  }
  if (phase === 'exit') {
    return kind === 'drill' ? 'drill-page-leave-up' : 'drill-page-leave-down';
  }
  return kind === 'drill' ? 'drill-page-arrive-from-below' : 'drill-page-arrive-from-above';
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

/** 返回目标：当前层链的上一层 drill。层链长度 k 对应 URL 前 k 段；参数化下钻层
 *  （/settings/knowledge/versions/<documentId>）的参数尾段不注册为层（resolve 返回 exact=false），
 *  按 URL 段数砍一段会落到「同层但参数缺失」的无效路径（空版本记录层），故上一层取 k − 1 段。 */
function parentDrill(drill: readonly string[], layers: readonly DrawerLayer[]): readonly string[] {
  return drill.slice(0, Math.max(layers.length - 1, 0));
}

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) =>
      (element.offsetParent !== null || element === document.activeElement) &&
      // visibility:hidden 元素（含 drill-hidden 隐藏渲染的过渡节点）不可聚焦
      getComputedStyle(element).visibility !== 'hidden',
  );
}

/** 焦点是否在活跃浮层内：Radix 把浮层内容 portal 到 body 末尾（抽屉容器之外），
 *  浮层自身 role 或 popper 包装器可识别；Tab 循环由 Radix 自管，抽屉陷阱放行（审查 P0#2）。 */
function insideFloatingLayer(element: Element | null): boolean {
  let current: Element | null = element;
  while (current !== null) {
    const role = current.getAttribute('role');
    if (
      current.hasAttribute('data-radix-popper-content-wrapper') ||
      role === 'dialog' ||
      role === 'alertdialog' ||
      role === 'menu' ||
      role === 'listbox'
    ) {
      return true;
    }
    current = current.parentElement;
  }
  return false;
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
      const active = document.activeElement;
      // 焦点在活跃浮层内（通知面板/确认框等 portal 到抽屉外）：Radix 自管 Tab 循环，放行
      if (!container.contains(active) && insideFloatingLayer(active)) {
        return;
      }
      const focusable = focusableIn(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
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
    const timer = setTimeout(() => setEnterKick(false), ENTER_MS);
    return () => clearTimeout(timer);
  }, [enterKick]);

  // ---- 两相下钻动画机 ----
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
      // 过渡类型按**注册层链深度**判定，不按 URL 段数：参数化下钻层（versions/<documentId>）的
      // 尾段是参数、不注册为层（resolve 停在 versions 层，exact=false），层链深度与不带参数时
      // 相同；按段数比较会把它算成「跨了两段」，进出该层都落进同层切换分支——左栏瞬时换栏、
      // 只有右栏淡变。层链是「层」的唯一权威表达。
      const fromDepth = registry.resolve(shownSegment, from, role).layers.length;
      const toDepth = resolved.layers.length;
      // 前缀同链约束保留：换链时深度差也可能为 1（铃铛跨段跳转、selectModule 整体替换 drill），
      // 只有路径前缀关系能确认两次导航在同一条层链上。
      const drillDown = toDepth === fromDepth + 1 && isPrefix(from, to);
      const back = fromDepth === toDepth + 1 && isPrefix(to, from);
      // 桌面端顶层 ↔ 模块选中为同层切换（§5.2 左栏换选），不走整页过渡
      const desktopSwitch = !narrow && fromDepth <= 1 && toDepth <= 1;
      if ((!drillDown && !back) || desktopSwitch || resolved.layers.length === 0) {
        // 同层切换（§5.2）：左右栏不换，右栏先旧内容原地淡出 150ms（--duration-fast），
        // 再接新内容自下而上淡入 250ms（--duration-base，drill-switch）。
        // 抽屉滑上/滑下期间（slide 非 open）不叠加，直出；无层可切（占位）同样直出。
        if (resolved.layers.length > 0 && slide === 'open') {
          setTransition({ kind: 'switch', from, to, phase: 'exit' });
        } else if (transition !== null) {
          setTransition(null);
        }
      } else {
        // 下钻 / 返回：两相整页过渡。第 1 相挂过渡渲染（from 侧为已挂载内容的保留节点、
        // to 侧 drill-hidden 隐藏预挂载，摊薄重模块挂载成本），由下方 layout effect 启动相位定时器。
        setTransition({ kind: drillDown ? 'drill' : 'back', from, to, phase: 'exit' });
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

  // 两相定时：过渡渲染已提交后启动。第 1 相 exit（整页上滑/下滑渐隐，或 switch 旧内容原地淡出）
  // → 第 2 相 enter（新页自下方或自上方渐显）→ 复位。to 侧在 exit 期间以 drill-hidden 预挂载
  // （摊薄重模块挂载成本），相位切换后才可见。
  useLayoutEffect(() => {
    if (transition === null || transition.phase !== 'exit') {
      return;
    }
    if (armedForRef.current === transition) {
      return;
    }
    // 打断旧过渡：清掉它的残留定时器，再为本过渡上膛
    clearTimers();
    armedForRef.current = transition;
    const exitMs = transition.kind === 'switch' ? SWITCH_EXIT_MS : PAGE_EXIT_MS;
    timersRef.current = [
      window.setTimeout(() => {
        setTransition((current) => (current === null ? null : { ...current, phase: 'enter' }));
      }, exitMs),
      window.setTimeout(() => {
        armedForRef.current = null;
        setTransition(null);
      }, exitMs + ENTER_MS),
    ];
  }, [transition, clearTimers]);

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
    // 上一层按已解析层链计算（参数化层的参数尾段不算一层）；无层可退（未注册深链）时回段顶层
    const currentLayers = registry.resolve(current.segment, current.drill, role).layers;
    const next =
      current.drill.length > 0
        ? formatDrawerLocation({
            open: true,
            segment: current.segment,
            drill: parentDrill(current.drill, currentLayers),
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

  // 触屏防接管（审查 P2#20）：置顶向下拖动时 preventDefault 掉浏览器滚动/下拉刷新——
  // touch-action 只声明 pan-y 不足以保住手势（置顶 pan 无处滚动时浏览器仍会 pointercancel
  // 中断跟手关闭），需在 touchmove（非 passive）按手势方向拦截；向上滑动放行原生滚动。
  useEffect(() => {
    const panel = panelRef.current;
    if (!mounted || panel === null) {
      return undefined;
    }
    const onTouchMove = (event: TouchEvent) => {
      const start = dragStartRef.current;
      if (start === null || !start.engaged) {
        return;
      }
      const y = event.touches[0]?.clientY ?? start.y;
      if (y > start.y && event.cancelable) {
        event.preventDefault();
      }
    };
    panel.addEventListener('touchmove', onTouchMove, { passive: false });
    return () => panel.removeEventListener('touchmove', onTouchMove);
  }, [mounted]);

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
  const segmentTitle =
    shownSegment === 'personal'
      ? drawerCopy.personalTitle
      : (shownLayers[0]?.title ?? drawerCopy.adminSegmentLabel);
  // 窄屏下钻（drill>0）：页头出现返回控件、标题显示当前层级名（审查 P1#9：触屏无 Esc 也能逐级返回）
  const narrowDrilled = narrow && shownDrill.length > 0;
  const currentTitle = narrowDrilled ? (deepest?.title ?? segmentTitle) : segmentTitle;

  // 左栏返回按钮文案：该层链的上一层名称（层链长度 k 对应 URL 前 k 段）。按**所渲染的层链**取，
  // 不按当前 URL——离开相渲染的是上一层（参数化下钻时两者不同），沿用当前路径会显示成离开页自己的名字。
  const backLabelOf = (drill: readonly string[], layers: readonly DrawerLayer[]): string =>
    drill.length === 2 ? (layers[0]?.title ?? '') : (layers[layers.length - 2]?.title ?? '');
  // 窄屏页头返回按钮的可达名：首层回段顶层（段名），更深层回上一层级
  const narrowBackLabel =
    shownDrill.length === 1 ? segmentTitle : (shownLayers[shownLayers.length - 2]?.title ?? segmentTitle);
  const goBack = () => {
    // 返回上一层：按层链（而非 URL 段数）取上一层，参数化层的参数尾段不占一层
    navigate(
      formatDrawerLocation({
        open: true,
        segment: parsed.segment,
        drill: parentDrill(parsed.drill, resolved.layers),
      }),
    );
  };

  const renderModuleList = (phaseClass: string) => {
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
              className={`ml-6 flex h-10 w-[200px] items-center justify-between gap-2 rounded-[var(--radius-buttons)] px-3 text-left text-body transition-colors duration-150 hover:bg-mist-gray ${
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
      <div data-nav-variant="modules" className={phaseClass}>
        <p className="px-3 pb-1 text-caption text-slate-strong">{drawerCopy.personalSegmentLabel}</p>
        {list(personalModules, 'personal')}
        {adminModules.length > 0 && (
          <>
            <hr className="my-3 border-0 border-t border-hairline" />
            <p className="px-3 pb-1 text-caption text-slate-strong">{drawerCopy.adminSegmentLabel}</p>
            {list(adminModules, 'admin')}
          </>
        )}
      </div>
    );
  };

  const renderDrilledNav = (layer: DrawerLayer, backLabel: string, phaseClass: string) => (
    <div data-nav-variant="drilled" className={phaseClass}>
      <button
        type="button"
        onClick={goBack}
        aria-label={drawerCopy.backAria(backLabel)}
        className="flex h-8 items-center gap-1 text-caption text-slate-strong transition-colors duration-150 hover:text-ink-black"
      >
        <ArrowLeft size={16} aria-hidden />
        <span>{backLabel}</span>
      </button>
      <p data-drill-title-slot className="mt-2 text-body-lg font-medium text-ink-black">
        {layer.title}
      </p>
    </div>
  );

  const renderLayerContent = (
    layers: readonly DrawerLayer[],
    phaseClass: string,
    /** 该层自己的 drill 路径：退出侧传 transition.from、到达侧传当前路径。
     *  参数化层（versions/<documentId>）据此取参数，两侧混用会让退出中的版本详情被清成空态。 */
    path: readonly string[],
  ) => {
    const layer = layers[layers.length - 1];
    if (layer === undefined) {
      // 顶层 / 未注册层：抽屉首层占位（规格 §3）
      return (
        <div data-content-variant="placeholder" className={phaseClass}>
          <p className="text-caption text-slate-strong">{drawerCopy.topPlaceholderBody}</p>
        </div>
      );
    }
    if (layer.render !== undefined) {
      // 会话键：authSessionId:userId 变化时强制重挂载内容子树，
      // 立即清空账号相关 state（跨逻辑会话数据残留防护；review Major 1）。
      return (
        <div key={sessionKey ?? 'no-session'} className={phaseClass}>
          {layer.render({ path })}
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
  // 过渡开始时 from 侧与空闲节点同 key 复用（离开动画从真实 opacity 起播，而非重挂载瞬隐），
  // 过渡结束时 to 侧与空闲节点同 key 复用（进入动画不被二次重挂截断）。
  // 同层切换（switch）左栏不换层，两侧都不挂动画类。
  const navIdle = drilled && deepest !== undefined
    ? renderDrilledNav(deepest, backLabelOf(shownDrill, shownLayers), '')
    : renderModuleList('');
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
    // 两相整页：to 侧在进入相才可见（离开相以 drill-hidden 预挂载，摊薄重模块挂载成本）。
    // 进入类同样只在进入相挂上——visibility:hidden 不阻止 CSS 动画播放，若在离开相就挂类，
    // 动画会在被隐藏的 250ms 里跑完，揭开时已是终态、看起来完全没动。
    const toVisible = transition.phase === 'enter';
    const toPhaseClass = toVisible ? drillPhaseClass(transition.kind, 'enter') : '';
    return (
      <div className="relative h-full">
        <div key={`nav:${transition.from.join('/')}`} className="absolute inset-0">
          {fromDrilled && fromLayer !== undefined
            ? renderDrilledNav(
                fromLayer,
                backLabelOf(transition.from, fromLayers),
                drillPhaseClass(transition.kind, 'exit'),
              )
            : renderModuleList(drillPhaseClass(transition.kind, 'exit'))}
        </div>
        <div
          key={`nav:${transition.to.join('/')}`}
          className={`absolute inset-0 ${toVisible ? '' : 'drill-hidden'}`}
        >
          {toDrilled && toLayer !== undefined
            ? renderDrilledNav(toLayer, backLabelOf(transition.to, shownLayers), toPhaseClass)
            : renderModuleList(toPhaseClass)}
        </div>
      </div>
    );
  })();

  // 内容子树按「会话 + 段 + 路径」keyed，且无论是否在过渡中都挂在同一父元素下：
  // 进入过渡时 from 侧复用已挂载内容（离开的是真实内容，而非新挂载的骨架屏），
  // 结束过渡时 to 侧原位保留（不二次挂载、不再闪一次骨架屏）。
  const contentKey = (drill: readonly string[]) =>
    `${sessionKey ?? 'no-session'}:${shownSegment}:${drill.join('/')}`;
  const currentContentKey = contentKey(shownDrill);
  // 窄屏首屏（drill 为空）的「当前页主体」是模块名列表而非右栏层内容（见下方 narrowListView）。
  // 过渡期间 narrowListView 恒为 false，故 from/to 两侧都要按同一形态渲染——否则窄屏下钻
  // 会把屏幕上真实存在的模块列表丢掉，换成一个从未露面的占位文案去播离开动画。
  const narrowListPath = (drill: readonly string[]) => narrow && drill.length === 0;
  const contentArea = (() => {
    if (!transitioning) {
      return (
        <div>
          <div key={currentContentKey}>
            {renderLayerContent(
              shownLayers,
              enterKick ? 'drill-page-arrive-from-below' : '',
              shownDrill,
            )}
          </div>
        </div>
      );
    }
    const exitClass = drillPhaseClass(transition.kind, 'exit');
    const enterClass = drillPhaseClass(transition.kind, 'enter');
    // 打断反向导航的瞬时渲染（URL 已回到 from 层、layout effect 尚未重算 transition）：
    // from 与当前层同路径，双侧同 key 会撞键污染 React 树——此时 from 侧即当前内容，跳过一次即可
    // （layout effect 同步重渲染，该中间态不会上屏）。
    const fromIsCurrent = samePath(transition.from, shownDrill);
    // 同层切换两相渲染：exit 相位 from 原地淡出（drill-exit）、to 以 drill-hidden 预挂载不可见；
    // enter 相位 from 卸载，to 原位以 drill-switch 自下而上淡入（节点键控保留，结束过渡不重挂）。
    if (transition.kind === 'switch') {
      const entering = transition.phase === 'enter';
      return (
        <div className="relative h-full">
          {!fromIsCurrent && !entering && (
            <div key={contentKey(transition.from)} className="absolute inset-0">
              {renderLayerContent(fromLayers, exitClass, transition.from)}
            </div>
          )}
          <div
            key={currentContentKey}
            className={`absolute inset-0 ${entering ? enterClass : 'drill-hidden'}`}
          >
            {renderLayerContent(shownLayers, '', shownDrill)}
          </div>
        </div>
      );
    }
    return (
      <div className="relative h-full">
        {!fromIsCurrent && (
          <div key={contentKey(transition.from)} className="absolute inset-0">
            {narrowListPath(transition.from)
              ? renderModuleList(exitClass)
              : renderLayerContent(fromLayers, exitClass, transition.from)}
          </div>
        )}
        <div
          key={currentContentKey}
          className={`absolute inset-0 ${transition.phase === 'exit' ? 'drill-hidden' : ''}`}
        >
          {narrowListPath(shownDrill)
            ? renderModuleList(transition.phase === 'exit' ? '' : enterClass)
            : renderLayerContent(
                shownLayers,
                transition.phase === 'exit' ? '' : enterClass,
                shownDrill,
              )}
        </div>
      </div>
    );
  })();

  const narrowListView = narrow && shownDrill.length === 0 && !transitioning;

  // 作用域 provider 包住整棵抽屉树（R3）：抽屉内 portal 到 body 的浮层在 React 树上仍是其后代，
  // 据此给自身内容根节点补挂 data-drawer-scope（DOM 继承到不了 body 下的浮层）。
  const drawerBody = (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-40 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={segmentTitle}
    >
      <div
        ref={panelRef}
        data-slide={slide}
        data-dragging={dragOffset !== null ? 'true' : undefined}
        data-rebound={rebound ? 'true' : undefined}
        data-drawer-scope=""
        className="drawer-panel absolute inset-0 flex flex-col bg-paper-white shadow-[var(--shadow-subtle-2)]"
        style={dragOffset !== null ? { transform: `translateY(${dragOffset}px)` } : undefined}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <header className="mt-10 flex shrink-0 items-center gap-4 px-5 md:px-10">
          {narrowDrilled && (
            <button
              type="button"
              onClick={goBack}
              aria-label={drawerCopy.backAria(narrowBackLabel)}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-colors duration-150 hover:bg-mist-gray"
            >
              <ArrowLeft size={20} aria-hidden />
            </button>
          )}
          <button
            type="button"
            onClick={close}
            aria-label={drawerCopy.closeAria}
            className="flex h-10 w-10 items-center justify-center rounded-full transition-colors duration-150 hover:bg-mist-gray"
          >
            <X size={20} aria-hidden />
          </button>
          {/* 页头小标题（drawer-visual-system §2.2）：个人段「设置」、管理段当前模块名，Sohne 500 20px */}
          <h1 className="font-sohne text-body-lg font-medium leading-body-lg">{currentTitle}</h1>
          <div className="ml-auto">{headerRight}</div>
        </header>
        <div className="mt-10 flex min-h-0 flex-1 gap-10 px-5 md:px-10">
          {!narrowListView && (
            <nav
              className={`${narrow ? 'hidden' : ''} w-60 shrink-0 overflow-y-auto bg-paper-white`}
              aria-label={drawerCopy.navAria}
            >
              {navArea}
            </nav>
          )}
          {/* 滚动容器绘制余量：顶部/左侧 4px 供首行元素（顶行入口、文档名等）与 scrollport
              齐平时的 hover 放大与阴影溢出内容盒（pt/pl + 等量负 margin 保持内容原位）。
              右侧 12px 吸收 ui-touch-target::after 这类不可见触控热区伪元素的外扩（抽屉内最大
              --touch-expand 为 TextLink 的 -11px）；不吸收它们会越出 padding box，被
              overflow-y:auto 顺带算成 overflow-x:auto 而兑现成横向滚动条与横向拖动。
              pr-3/-mr-3 与 pt-1/-mt-1 同理：内容盒左缘与宽度完全不变，只把 padding box 右移边界。
              底部 8px 吸收进入动画：过渡期内容被包成 relative h-full + absolute inset-0，被动画
              块恰为内容盒满高，而 drill-switch / drill-page-arrive-from-below 自 translateY(8px)
              起步；缺这段余量时会把块推过滚动口底部 8px，令 scrollHeight 瞬时 +8px 闪出滚动条
              （反向的 arrive-from-above 自 -8px 起步，越出滚动口顶部的那 8px 不可滚动，无需余量）。
              hide-scrollbar：右栏保留滚动能力但不显示滚动条。
              overflow-x-hidden：兜底——横向没有可滚动内容，任何漏网的外扩都不得变成横向滚动。
              内容列不再限宽（原 max-w-[724px] 与 共用基座设计.md 的 720px 条款一并移除）：
              880px 卡片的水平居中只由 SettingsCard 的 mx-auto max-w-[880px] 单点负责，
              本容器不得再加第二层限宽或居中容器，否则卡片会被双重收缩。 */}
          <div
            ref={contentRef}
            className="hide-scrollbar min-w-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain bg-[var(--surface-drawer-canvas)] pt-1 pb-2 pl-1 pr-3 -mt-1 -ml-1 -mr-3"
          >
            {narrowListView ? renderModuleList('') : contentArea}
          </div>
        </div>
      </div>
    </div>
  );

  return <DrawerScopeContext.Provider value={true}>{drawerBody}</DrawerScopeContext.Provider>;
}
