import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

/**
 * Comet `scoped-text-safety` 会扫描生产产物 `static/assets/*.js` 的 trailing-whitespace。
 * 两处噪声均来自依赖被打包后的多行字符串，不是业务源码：
 * 1) react-remove-scroll-bar 的 CSS 模板里含「仅两个空格」的空行；
 * 2) highlight.js php 语言里 `WHITESPACE = '[ \t\n]'` 被压成跨行模板字面量，
 *    使某一行以 tab 结尾。
 *
 * 必须在 renderChunk 阶段做语义等价替换，让最终 chunk hash 基于清理后的内容计算；
 * 同时只白名单处理已知依赖噪声，任何未知 trailing whitespace 都让构建失败。
 */
function sanitizeBuiltJsForTextSafety(): Plugin {
  const phpSourceWhitespaceClass = "'[ \\t\\n]'";
  const phpWhitespaceFactory = '[91,32,9,10,93].map((code)=>String.fromCharCode(code)).join("")';
  const phpMultilineWhitespaceClass = String.fromCharCode(0x60, 0x5b, 0x20, 0x09, 0x0a, 0x5d, 0x60);
  const scrollBarCssBlankLine = /\n  \n  (?=\.|body\[)/g;
  const scrollBarEscapedCssBlankLine = /\\n  \\n  (?=\.|body\[)/g;

  function replaceKnownNoise(source: string, chunkName: string): string {
    let next = source;

    const phpSourceMatches = countOccurrences(next, phpSourceWhitespaceClass);
    const phpTemplateMatches = countOccurrences(next, phpMultilineWhitespaceClass);
    if (phpSourceMatches + phpTemplateMatches > 1) {
      throw new Error(
        `Unexpected highlight.js PHP WHITESPACE sanitizer count (${phpSourceMatches + phpTemplateMatches}) in ${chunkName}`,
      );
    }
    if (phpSourceMatches === 1) {
      next = next.replace(phpSourceWhitespaceClass, phpWhitespaceFactory);
    }
    if (phpTemplateMatches === 1) {
      next = next.replace(phpMultilineWhitespaceClass, phpWhitespaceFactory);
    }

    const cssSourceMatches = countRegexMatches(next, scrollBarEscapedCssBlankLine);
    const cssTemplateMatches = countRegexMatches(next, scrollBarCssBlankLine);
    const cssMatches = cssSourceMatches + cssTemplateMatches;
    if (cssMatches > 0 && !next.includes('data-scroll-locked')) {
      throw new Error(`react-remove-scroll-bar CSS sanitizer matched outside its chunk in ${chunkName}`);
    }
    if (cssMatches !== 0 && cssMatches !== 5) {
      throw new Error(
        `Unexpected react-remove-scroll-bar CSS sanitizer count (${cssMatches}) in ${chunkName}`,
      );
    }
    next = next
      .replace(scrollBarEscapedCssBlankLine, '\\n\\n  ')
      .replace(scrollBarCssBlankLine, '\n\n  ');

    return next;
  }

  function countOccurrences(source: string, needle: string): number {
    let count = 0;
    let offset = 0;
    while (true) {
      const index = source.indexOf(needle, offset);
      if (index === -1) return count;
      count += 1;
      offset = index + needle.length;
    }
  }

  function countRegexMatches(source: string, pattern: RegExp): number {
    pattern.lastIndex = 0;
    let count = 0;
    while (pattern.exec(source) !== null) {
      count += 1;
    }
    pattern.lastIndex = 0;
    return count;
  }

  function assertNoUnknownTrailingWhitespace(source: string, chunkName: string): void {
    const lines = source.split('\n');
    const badLines: number[] = [];
    for (let index = 0; index < lines.length; index += 1) {
      const body = lines[index]?.replace(/\r$/, '') ?? '';
      if (/[ \t]$/.test(body)) {
        badLines.push(index + 1);
      }
    }
    if (badLines.length > 0) {
      throw new Error(
        `Unknown trailing whitespace in ${chunkName} at line(s) ${badLines.slice(0, 10).join(', ')}`,
      );
    }
  }

  return {
    name: 'sanitize-built-js-for-text-safety',
    apply: 'build',
    enforce: 'post',
    renderChunk: {
      order: 'post',
      handler(code, chunk) {
        const chunkName = chunk.fileName || chunk.name;
        const sanitized = replaceKnownNoise(code, chunkName);
        assertNoUnknownTrailingWhitespace(sanitized, chunkName);
        return sanitized === code ? null : { code: sanitized, map: null };
      },
    },
    generateBundle(_, bundle) {
      for (const [fileName, chunk] of Object.entries(bundle)) {
        if (chunk.type !== 'chunk' || !fileName.endsWith('.js')) continue;
        const sanitized = replaceKnownNoise(chunk.code, fileName);
        assertNoUnknownTrailingWhitespace(sanitized, fileName);
        chunk.code = sanitized;
      }
    },
  };
}

// 生产构建资源挂在 /static/（FastAPI 静态挂载，单端口托管）；开发/e2e 用根路径。
// API 联调基准为契约 mock（MSW，规格 §1）：public/ 只放 mockServiceWorker.js，
// 生产构建关闭 publicDir，mock 资产不进生产构建；真实后端联调代理待后端就绪后另行配置。
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  publicDir: command === 'build' ? false : 'public',
  server: {
    port: 5173,
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    assetsDir: 'assets',
    rolldownOptions: {
      output: {
        plugins: [sanitizeBuiltJsForTextSafety()],
      },
    },
  },
  base: command === 'build' ? '/static/' : '/',
}));
