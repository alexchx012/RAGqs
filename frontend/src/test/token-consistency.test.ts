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

/** Steep 颜色 9 色（亮色值）。 */
const LIGHT_COLORS: Record<string, string> = {
  'ink-black': '#17191c',
  'paper-white': '#ffffff',
  'mist-gray': '#f2f2f3',
  'fog-white': '#fafafb',
  'slate-gray': '#777b86',
  /* 次级文字（审查 A6）：对 #ffffff/#fafafb/#f2f2f3 ≥4.5:1（6.21/5.95/5.55）。 */
  'slate-strong': '#5c616c',
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
  /* 次级文字（审查 A6）：对 #1c1f24/#23262c/#2c3038 ≥4.5:1（8.66/7.95/6.94）。 */
  'slate-strong': '#b6bcc7',
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

/** 结构分隔线（行间 / 页脚顶边框）：与控件描边 hairline 是两个设计取值。
 * 全局取与 hairline 同值（抽屉外无人消费，取同值使行为与今天一致），抽屉作用域取设计图实测 #efeff1；
 * 设计图只有亮色，暗色两个作用域都沿用 hairline 的 #2f333b，不发明新值。 */
const DIVIDER: [string, string, string] = ['#ececec', '#2f333b', '#efeff1'];

/** 抽屉作用域覆盖值（settings-ui-ux-refresh）：设计图实测值，仅作用于抽屉子树。 */
const DRAWER_SCOPE_LIGHT: Record<string, string> = {
  'ink-black': '#1a1a18',
  hairline: '#e4e3e7',
  divider: DIVIDER[2],
  danger: '#d64545',
};
const DRAWER_SCOPE_DARK: Record<string, string> = {
  'ink-black': '#f2f2f3',
  hairline: '#2f333b',
  divider: DIVIDER[1],
  danger: '#d1826f',
};
const DRAWER_CANVAS: [string, string] = ['#f7f7f8', '#202329'];

function collectHexes(css: string): string[] {
  return [...css.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((match) => match[0].toLowerCase());
}

function collectColorTokenNames(css: string): string[] {
  return [...css.matchAll(/--color-([a-z0-9-]+):\s*#/g)].map((match) => match[1]);
}

/** 截出某个选择器的声明块正文（取紧随选择器之后的第一个 { } 对）。
 * 用途：assert-on-a-specific-block。naive 的「从某标记切到文件尾」写法会把后面块的
 * **同名同值**声明一并算进来，于是「块 B 声明了 X」的断言被块 C 的声明满足——块 B 真漏声明时
 * 它照样通过（假绿）。本次加固的四条暗色全局断言正踩在这个洞上：它们覆盖的
 * ink-black(#f2f2f3) / hairline(#2f333b) / divider(#2f333b) / danger(#d1826f) 四个 token，
 * 在全局暗色 :root[data-theme='dark'] 与抽屉暗色 [data-theme='dark'] [data-drawer-scope]
 * 逐字节相同，故这四条断言必须落在唯一确定的块上（见 darkRootBlock）。 */
function blockOf(css: string, selector: string): string {
  const start = css.indexOf(selector);
  expect(start, `未找到选择器 ${selector}`).toBeGreaterThan(-1);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  return css.slice(open + 1, close);
}

/** 全局暗色基线块，精确切出，只含 :root[data-theme='dark'] 自己的声明。
 * 曾经的写法是从暗色标记一直切到文件尾，会把随后的 [data-drawer-scope] 与
 * [data-theme='dark'] [data-drawer-scope] 一并卷进来（后者对这四个 token 取值与全局暗色相同，
 * 于是全局暗色漏声明也照样通过）；现在一律用上面的 blockOf 按精确选择器切块。 */
const darkRootBlock = blockOf(tokensCss, ":root[data-theme='dark']");

describe('设计 token 与 Steep 事实源一致', () => {
  it('颜色 9 色亮色值逐项一致', () => {
    for (const [name, value] of Object.entries(LIGHT_COLORS)) {
      expect(lightScope).toContain(`--color-${name}: ${value};`);
    }
  });

  it('暗色映射逐项与 共用基座 §2.1 对照表一致（token 名不变换值）', () => {
    for (const [name, value] of Object.entries(DARK_COLORS)) {
      expect(darkRootBlock).toContain(`--color-${name}: ${value};`);
    }
    // 精确块断言：hairline 的暗色值在抽屉暗色块同样出现，从暗色标记切到文件尾的写法
    // 会在全局暗色漏声明时照样通过。
    expect(darkRootBlock).toContain(`--color-hairline: ${HAIRLINE[1]};`);
  });

  it('功能色例外仅危险红/警告琥珀/成功绿三种，亮暗值一致', () => {
    for (const [name, [light, dark]] of Object.entries(FUNCTIONAL_COLORS)) {
      expect(lightScope).toContain(`--color-${name}: ${light};`);
      // danger 暗色值在抽屉暗色块同样出现，必须断言在全局暗色块上
      expect(darkRootBlock).toContain(`--color-${name}: ${dark};`);
    }
  });

  it('发丝边统一 #ececec（暗色 #2f333b）；结构分隔线全局与发丝边同值', () => {
    expect(lightScope).toContain(`--color-hairline: ${HAIRLINE[0]};`);
    // 抽屉外无人消费 border-divider；全局取与 hairline 同值是为了「万一将来有人用」行为不突变。
    expect(lightScope).toContain(`--color-divider: ${DIVIDER[0]};`);
    // 全局暗色这处声明当前无消费者（border-divider 只在抽屉内用），但 brief 要求四处取值齐备：
    // 缺了就会在某个主题下静默回落。断言必须落在全局暗色块，否则被抽屉暗色块的同值声明满足。
    expect(darkRootBlock).toContain(`--color-divider: ${DIVIDER[1]};`);
  });

  it('色彩纪律：token 表中不允许 9 色 + 次级文字 slate-strong + 发丝边 + 结构分隔线 + 三功能色之外的任何色值', () => {
    const allowedHexes = new Set(
      [
        ...Object.values(LIGHT_COLORS),
        ...Object.values(DARK_COLORS),
        ...Object.values(FUNCTIONAL_COLORS).flat(),
        ...HAIRLINE,
        // 结构分隔线专用中性色（#ececec/#2f333b/#efeff1）经审查加入白名单：
        // #efeff1 是设计图实测的行分隔线中性色，与 9 色、功能色、控件描边都不重复，
        // 属新增一个设计取值而非放宽既有纪律；白名单其余部分未动。
        ...DIVIDER,
        ...Object.values(DRAWER_SCOPE_LIGHT),
        ...Object.values(DRAWER_SCOPE_DARK),
        ...DRAWER_CANVAS,
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
      'divider',
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

describe('抽屉作用域（settings-ui-ux-refresh）', () => {
  it('作用域块存在并声明全部覆盖值', () => {
    const scopeStart = tokensCss.indexOf('[data-drawer-scope]');
    expect(scopeStart).toBeGreaterThan(-1);
    const scopeBlock = tokensCss.slice(scopeStart);

    expect(scopeBlock).toContain(`--color-ink-black: ${DRAWER_SCOPE_LIGHT['ink-black']};`);
    expect(scopeBlock).toContain(`--color-hairline: ${DRAWER_SCOPE_LIGHT.hairline};`);
    // 结构分隔线在抽屉里是另一个更浅的取值：与上面的控件描边 #e4e3e7 并存，不得合并
    expect(scopeBlock).toContain(`--color-divider: ${DRAWER_SCOPE_LIGHT.divider};`);
    expect(scopeBlock).toContain(`--color-danger: ${DRAWER_SCOPE_LIGHT.danger};`);
    expect(scopeBlock).toContain(`--surface-drawer-canvas: ${DRAWER_CANVAS[0]};`);
    expect(scopeBlock).toContain('--radius-buttons: 8px;');
    expect(scopeBlock).toContain('--radius-inputs: 8px;');
    expect(scopeBlock).toContain('--radius-cards: 16px;');
    expect(scopeBlock).toContain('--segmented-height: 40px;');
    expect(scopeBlock).toContain('--segmented-padding: 2px;');
    // 分段控件标签字号：设计图实测 15px（全局 14px 见下方「全局默认值保持不变」用例）
    expect(scopeBlock).toContain('--segmented-font-size: 15px;');

    expect(tokensCss).toContain("[data-theme='dark'] [data-drawer-scope]");
    expect(scopeBlock).toContain(`--surface-drawer-canvas: ${DRAWER_CANVAS[1]};`);
  });

  it('抽屉暗色作用域自身声明分隔线 #2f333b：漏了就回落到亮色 #efeff1，在深底上是一条亮线', () => {
    const darkDrawerBlock = blockOf(tokensCss, "[data-theme='dark'] [data-drawer-scope]");
    expect(darkDrawerBlock).toContain(`--color-divider: ${DRAWER_SCOPE_DARK.divider};`);
    expect(darkDrawerBlock).not.toContain(DIVIDER[2]);
    expect(darkDrawerBlock).toContain(`--color-hairline: ${DRAWER_SCOPE_DARK.hairline};`);
  });

  it('全局默认值保持不变（作用域不得反向污染全局）', () => {
    expect(lightScope).toContain('--color-ink-black: #17191c;');
    expect(lightScope).toContain('--color-hairline: #ececec;');
    expect(lightScope).toContain('--color-divider: #ececec;');
    expect(lightScope).toContain('--color-danger: #b6492f;');
    expect(lightScope).toContain('--radius-buttons: 9999px;');
    expect(lightScope).toContain('--radius-inputs: 16px;');
    expect(lightScope).toContain('--radius-cards: 24px;');
    expect(lightScope).toContain('--segmented-height: 32px;');
    expect(lightScope).toContain('--segmented-padding: 4px;');
    expect(lightScope).toContain('--segmented-font-size: 14px;');
  });
});
