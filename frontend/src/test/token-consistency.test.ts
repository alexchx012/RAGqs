import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/*
 * token 一致性：样式中引用的 token 与 Steep 事实源一致
 * （docs/项目设计/前端/tokens.json、theme.css、variables.css、docs/设计规范/DESIGN.md；
 * 暗色映射：共用基座设计.md §2.1）。
 */

function readSrc(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');
}

const tokensCss = readSrc('../styles/tokens.css');
const baseCss = readSrc('../styles/base.css');

const DARK_MARKER = ":root[data-theme='dark']";
const darkIndex = tokensCss.indexOf(DARK_MARKER);
const lightScope = darkIndex >= 0 ? tokensCss.slice(0, darkIndex) : tokensCss;
const darkScope = darkIndex >= 0 ? tokensCss.slice(darkIndex) : '';

/** Steep 颜色 9 色（亮色值）。 */
const LIGHT_COLORS: Record<string, string> = {
  'ink-black': '#17191c',
  'paper-white': '#ffffff',
  'mist-gray': '#f2f2f3',
  'fog-white': '#fafafb',
  'slate-gray': '#777b86',
  'ash-gray': '#979799',
  'smoke-gray': '#a3a6af',
  'blush-peach': '#fbe1d1',
  'sienna-brown': '#5d2a1a',
};

/** 暗色映射（共用基座 §2.1 对照表，token 名不变换值）。 */
const DARK_COLORS: Record<string, string> = {
  'ink-black': '#f2f2f3',
  'paper-white': '#1c1f24',
  'fog-white': '#23262c',
  'mist-gray': '#2c3038',
  'slate-gray': '#9aa0ab',
  'ash-gray': '#7d828c',
  'smoke-gray': '#6b7079',
  'blush-peach': '#3a2d25',
  'sienna-brown': '#dfa88d',
};

/** 功能色例外仅三种：[亮色值, 暗色值]。 */
const FUNCTIONAL_COLORS: Record<string, [string, string]> = {
  danger: ['#b6492f', '#d1826f'],
  warning: ['#8f6410', '#d3a24f'],
  success: ['#4a7c59', '#8ab69b'],
};

const HAIRLINE: [string, string] = ['#ececec', '#2f333b'];

function collectHexes(css: string): string[] {
  return [...css.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((match) => match[0].toLowerCase());
}

function collectColorTokenNames(css: string): string[] {
  return [...css.matchAll(/--color-([a-z0-9-]+):\s*#/g)].map((match) => match[1]);
}

describe('设计 token 与 Steep 事实源一致', () => {
  it('颜色 9 色亮色值逐项一致', () => {
    for (const [name, value] of Object.entries(LIGHT_COLORS)) {
      expect(lightScope).toContain(`--color-${name}: ${value};`);
    }
  });

  it('暗色映射逐项与 共用基座 §2.1 对照表一致（token 名不变换值）', () => {
    for (const [name, value] of Object.entries(DARK_COLORS)) {
      expect(darkScope).toContain(`--color-${name}: ${value};`);
    }
    expect(darkScope).toContain(`--color-hairline: ${HAIRLINE[1]};`);
  });

  it('功能色例外仅危险红/警告琥珀/成功绿三种，亮暗值一致', () => {
    for (const [name, [light, dark]] of Object.entries(FUNCTIONAL_COLORS)) {
      expect(lightScope).toContain(`--color-${name}: ${light};`);
      expect(darkScope).toContain(`--color-${name}: ${dark};`);
    }
  });

  it('发丝边统一 #ececec（暗色 #2f333b）', () => {
    expect(lightScope).toContain(`--color-hairline: ${HAIRLINE[0]};`);
  });

  it('色彩纪律：token 表中不允许 9 色 + 发丝边 + 三功能色之外的任何色值', () => {
    const allowedHexes = new Set(
      [
        ...Object.values(LIGHT_COLORS),
        ...Object.values(DARK_COLORS),
        ...Object.values(FUNCTIONAL_COLORS).flat(),
        ...HAIRLINE,
      ].map((value) => value.toLowerCase()),
    );
    const hexes = collectHexes(`${tokensCss}\n${baseCss}`);
    expect(hexes.length).toBeGreaterThan(0);
    for (const hex of hexes) {
      expect(allowedHexes.has(hex), `未授权的色值 ${hex}`).toBe(true);
    }
    const allowedNames = new Set([
      ...Object.keys(LIGHT_COLORS),
      ...Object.keys(FUNCTIONAL_COLORS).map((name) => name),
      'hairline',
    ]);
    for (const name of collectColorTokenNames(tokensCss)) {
      expect(allowedNames.has(name), `未授权的颜色 token --color-${name}`).toBe(true);
    }
  });

  it('named radii 与 layout 合并自 variables.css', () => {
    for (const declaration of [
      '--radius-cards: 24px;',
      '--radius-images: 12px;',
      '--radius-inputs: 16px;',
      '--radius-buttons: 9999px;',
      '--radius-smallcards: 16px;',
      '--radius-elevatedcards: 20px;',
      '--page-max-width: 1200px;',
      '--section-gap: 80px;',
      '--card-padding: 20px;',
      '--element-gap: 8px;',
      '--spacing-unit: 4px;',
    ]) {
      expect(tokensCss).toContain(declaration);
    }
  });

  it('surface 5 层齐备', () => {
    for (const name of ['canvas', 'card-mist', 'section-fog', 'accent-blush', 'elevated-white']) {
      expect(tokensCss).toContain(`--surface-${name}:`);
    }
  });

  it('动效 token 五件齐备（150/250/400ms + 两条缓动）', () => {
    for (const declaration of [
      '--duration-fast: 150ms;',
      '--duration-base: 250ms;',
      '--duration-slow: 400ms;',
      '--ease-out: cubic-bezier(0.22, 1, 0.36, 1);',
      '--ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);',
    ]) {
      expect(tokensCss).toContain(declaration);
    }
  });

  it('字重纪律：仅 400/430/450/480/500 半级递进，不跳 600+', () => {
    const declared = [...tokensCss.matchAll(/--font-weight-[a-z0-9]+:\s*(\d+)/g)].map((match) =>
      Number(match[1]),
    );
    expect([...declared].sort((a, b) => a - b)).toEqual([400, 430, 450, 480, 500]);
    const directWeights = [
      ...`${tokensCss}\n${baseCss}`.matchAll(/[^-]font-weight:\s*(\d+)/g),
    ].map((match) => Number(match[1]));
    for (const weight of directWeights) {
      expect(weight).toBeLessThan(600);
    }
  });

  it('阴影三档 subtle / subtle-2 / subtle-3', () => {
    for (const name of ['--shadow-subtle:', '--shadow-subtle-2:', '--shadow-subtle-3:']) {
      expect(tokensCss).toContain(name);
    }
  });

  it('窄屏断点 768px', () => {
    expect(tokensCss).toContain('--breakpoint-md: 48rem;');
  });

  it('字号阶梯与事实源一致（抽查 caption/body/heading/display）', () => {
    for (const declaration of [
      '--text-caption: 15px;',
      '--text-body: 17px;',
      '--text-body-lg: 20px;',
      '--text-subheading: 22px;',
      '--text-heading-sm: 26px;',
      '--text-heading: 44px;',
      '--text-heading-lg: 64px;',
      '--text-display: 90px;',
      '--leading-heading: 1.3;',
      '--tracking-display: -2.25px;',
    ]) {
      expect(tokensCss).toContain(declaration);
    }
  });

  it('字体栈：Signifier/Sohne 在前，中文落系统衬线/无衬线，不内嵌商业字体文件', () => {
    expect(tokensCss).toMatch(/--font-signifier:\s*'Signifier',[^;]*serif;/);
    expect(tokensCss).toMatch(/--font-sohne:\s*'Sohne',[^;]*sans-serif;/);
    expect(tokensCss).not.toContain('@font-face');
    expect(baseCss).not.toContain('@font-face');
  });

  it('全局基线：:focus-visible 2px 描边；reduced-motion 降级规则存在', () => {
    expect(baseCss).toContain(':focus-visible');
    expect(baseCss).toContain('outline: 2px solid var(--color-ink-black);');
    expect(baseCss).toContain('prefers-reduced-motion: reduce');
  });
});
