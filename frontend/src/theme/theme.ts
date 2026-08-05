/*
 * 亮/暗双主题机制（规格 §4）。
 * - token 名不变、按主题切换变量值：本模块只切换 <html data-theme> 与 color-scheme，
 *   具体取值由 src/styles/tokens.css 的 :root / [data-theme='dark'] 承载。
 * - 未登录页面跟随系统 prefers-color-scheme，不放主题切换控件；
 *   登录后按用户 preferences 渲染——preferences 的读取与切换界面在后续 change 接入，
 *   本 change 只建立机制与默认值（默认 'system'）。
 * - 主题切换即时生效：setPreference 与系统偏好变化都立即重解析并应用。
 */

export type ThemePreference = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  if (preference === 'system') {
    return systemDark ? 'dark' : 'light';
  }
  return preference;
}

/** matchMedia('(prefers-color-scheme: dark)') 的最小结构，便于测试注入。 */
export interface ThemeMedia {
  matches: boolean;
  addEventListener(type: 'change', listener: (event: { matches: boolean }) => void): void;
  removeEventListener(type: 'change', listener: (event: { matches: boolean }) => void): void;
}

/** document.documentElement 的最小结构，便于测试注入。 */
export interface ThemeTarget {
  dataset: { theme?: string };
  classList: Pick<DOMTokenList, 'add' | 'remove'>;
  style: { colorScheme: string };
}

const TRANSITION_CLASS = 'theme-switching';
const TRANSITION_MS = 300; // 覆盖 --duration-base(250ms) 的过渡窗口后再移除

export class ThemeController {
  private preference: ThemePreference = 'system';
  private resolved: ResolvedTheme;
  private readonly onMediaChange: (event: { matches: boolean }) => void;
  private transitionTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly target: ThemeTarget,
    private readonly media: ThemeMedia,
  ) {
    this.onMediaChange = () => {
      // 仅跟随系统时响应系统偏好变化；显式亮/暗偏好不受系统切换影响
      if (this.preference === 'system') {
        this.apply();
      }
    };
    this.media.addEventListener('change', this.onMediaChange);
    this.resolved = resolveTheme(this.preference, this.media.matches);
    this.apply();
  }

  getPreference(): ThemePreference {
    return this.preference;
  }

  getResolved(): ResolvedTheme {
    return this.resolved;
  }

  /** 切换主题偏好，立即生效。后续 change 的用户 preferences 由此接入。 */
  setPreference(preference: ThemePreference): void {
    if (preference === this.preference) {
      return;
    }
    this.preference = preference;
    this.apply();
  }

  dispose(): void {
    this.media.removeEventListener('change', this.onMediaChange);
    if (this.transitionTimer !== undefined) {
      clearTimeout(this.transitionTimer);
      this.transitionTimer = undefined;
    }
  }

  private apply(): void {
    this.resolved = resolveTheme(this.preference, this.media.matches);
    this.target.dataset.theme = this.resolved;
    this.target.style.colorScheme = this.resolved;
    // 全站颜色 250ms 过渡；CSS 侧在 prefers-reduced-motion 下自动降级为直出
    this.target.classList.add(TRANSITION_CLASS);
    if (this.transitionTimer !== undefined) {
      clearTimeout(this.transitionTimer);
    }
    this.transitionTimer = setTimeout(() => {
      this.target.classList.remove(TRANSITION_CLASS);
      this.transitionTimer = undefined;
    }, TRANSITION_MS);
  }
}

/** 浏览器入口：默认偏好 'system'（未登录页跟随系统；登录后偏好由后续 change 调用 setPreference 接入）。 */
export function initTheme(root: HTMLElement = document.documentElement): ThemeController {
  return new ThemeController(root, window.matchMedia('(prefers-color-scheme: dark)'));
}
