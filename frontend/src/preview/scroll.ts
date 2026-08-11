/*
 * 预览区平滑滚动（fe-doc-preview；共用基座 §6：切换命中 400ms ease-in-out 平滑滚动并置于视口中央）。
 * 原生 scrollIntoView 的时长不可控，这里对最近的滚动容器做 rAF 动画；
 * prefers-reduced-motion 或无动画环境直接落位（base.css 已统一直出语义）。
 */

const SCROLL_DURATION_MS = 400;

/** ease-in-out 多项式近似（Material standard easing 的常用闭式逼近）。 */
function easeInOut(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2;
}

function findScrollContainer(element: Element): HTMLElement | null {
  let node: HTMLElement | null = element.parentElement;
  while (node !== null) {
    const style = window.getComputedStyle(node);
    const overflowY = style.overflowY;
    if ((overflowY === 'auto' || overflowY === 'scroll') && node.scrollHeight > node.clientHeight) {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

const running = new WeakMap<HTMLElement, number>();

/** 把元素平滑滚动到滚动容器视口中央；容器不可滚动（jsdom 等无布局环境）时静默跳过。 */
export function scrollToCenter(element: Element | null): void {
  if (element === null || typeof window === 'undefined') {
    return;
  }
  const container = findScrollContainer(element);
  if (container === null) {
    return;
  }
  const containerRect = container.getBoundingClientRect();
  const elementRect = element.getBoundingClientRect();
  const delta =
    elementRect.top - containerRect.top - (containerRect.height - elementRect.height) / 2;
  const from = container.scrollTop;
  const to = from + delta;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || typeof window.requestAnimationFrame !== 'function' || delta === 0) {
    const previous = running.get(container);
    if (previous !== undefined) {
      window.cancelAnimationFrame(previous);
    }
    container.scrollTop = to;
    return;
  }
  const previous = running.get(container);
  if (previous !== undefined) {
    window.cancelAnimationFrame(previous);
  }
  const startedAt = performance.now();
  const step = (now: number): void => {
    const t = Math.min(1, (now - startedAt) / SCROLL_DURATION_MS);
    container.scrollTop = from + (to - from) * easeInOut(t);
    if (t < 1) {
      running.set(container, window.requestAnimationFrame(step));
    } else {
      running.delete(container);
    }
  };
  running.set(container, window.requestAnimationFrame(step));
}
