/*
 * 亮/暗双主题机制（规格 §4）。
 * - token 名不变、按主题切换变量值：本模块只切换 <html data-theme> 与 color-scheme，
 *   具体取值由 src/styles/tokens.css 的 :root / [data-theme='dark'] 承载。
 * - 显式亮/暗偏好持久化 localStorage（THEME_STORAGE_KEY）：index.html 内联脚本在 CSS
 *   生效前优先读取（审查 P1#13 防闪烁），缺省回退系统 prefers-color-scheme。
 * - 主题切换即时生效：setPreference 与系统偏好变化都立即重解析并应用；
 *   resolved 主题未变化时不挂 theme-switching 过渡（避免同色重挂过渡）。
 */

export type ThemePreference = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'ragqs-theme-preference';

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

function readStoredPreference(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
  } catch {
    return 'system';
  }
}

function writeStoredPreference(preference: ThemePreference): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // localStorage 可能被浏览器策略禁用；运行时主题仍然正常生效。
  }
}

export class ThemeController {
  private preference: ThemePreference;
  private resolved: ResolvedTheme;
  private readonly onMediaChange: (event: { matches: boolean }) => void;
  private transitionTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly target: ThemeTarget,
    private readonly media: ThemeMedia,
  ) {
    this.preference = readStoredPreference();
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

  /** 切换主题偏好，立即生效并持久化供下一次首屏读取。 */
  setPreference(preference: ThemePreference): void {
    if (preference === this.preference) {
      return;
    }
    this.preference = preference;
    writeStoredPreference(preference);
    this.apply();
  }

  dispose(): void {
    this.media.removeEventListener('change', this.onMediaChange);
    if (this.transitionTimer !== undefined) {
      clearTimeout(this.transitionTimer);
      this.transitionTimer = undefined;
    }
  }

  private apply(transition = true): void {
    const nextResolved = resolveTheme(this.preference, this.media.matches);
    const resolvedChanged = nextResolved !== this.resolved;
    this.resolved = nextResolved;
    this.target.dataset.theme = nextResolved;
    this.target.style.colorScheme = nextResolved;
    if (!transition || !resolvedChanged) {
      return;
    }
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

/** 浏览器入口：偏好优先读 localStorage（与 index.html 内联脚本同键），缺省跟随系统。 */
export function initTheme(root: HTMLElement = document.documentElement): ThemeController {
  return new ThemeController(root, window.matchMedia('(prefers-color-scheme: dark)'));
}
