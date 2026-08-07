/*
 * Markdown 渲染（fe-chat-home 规格 §4；brief Decisions：marked + highlight.js）。
 * XSS 安全：
 * - marked v18 默认透传原始 HTML，本模块用 renderer.html 把原始 HTML 转义为纯文本；
 * - link / image 走协议白名单：仅 http/https/mailto 与相对路径（以 /、./、../、# 开头）放行，
 *   其余（javascript:、data:、vbscript: 等）降级为纯文本，杜绝存储型 XSS；
 * - 代码块经 highlight.js 高亮后输出为结构化 span（.hljs-* 类），配色由 chat.css 用
 *   Steep token 统一（不引入 hljs 默认主题的非 token 色值）。
 * prefers-reduced-motion 由全局降级，本组件无动画。
 */

import { marked } from 'marked';
import hljs from 'highlight.js/lib/common';
import { useMemo, type ReactElement } from 'react';

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 链接/图片协议白名单：http/https/mailto + 相对路径；其余（javascript: 等）拒绝。 */
function isSafeLink(href: string): boolean {
  const trimmed = href.trim();
  if (trimmed === '') return false;
  // 协议相对 //... 一律拒绝（// 可能指向外部不可信源）；须先于 `/` 相对路径判断，
  // 否则 `//evil.example` 会被 startsWith('/') 误放行。
  if (trimmed.startsWith('//')) return false;
  // 相对路径（含锚点）：以 /、./、../、# 开头
  if (trimmed.startsWith('/') || trimmed.startsWith('./') || trimmed.startsWith('../') || trimmed.startsWith('#')) {
    return true;
  }
  const scheme = trimmed.split(':')[0]?.toLowerCase() ?? '';
  return scheme === 'http' || scheme === 'https' || scheme === 'mailto';
}

/** 单例：marked renderer 覆盖一次（模块加载时）。 */
let configured = false;

function configureMarked(): void {
  if (configured) return;
  configured = true;
  marked.use({
    renderer: {
      // 原始 HTML 一律转义为纯文本（XSS 防护；未知字段按契约忽略规则处理）
      html(token) {
        return escapeHtml(token.text ?? '');
      },
      code(token) {
        const lang = (token.lang ?? '').trim();
        let highlighted = escapeHtml(token.text);
        if (lang !== '' && hljs.getLanguage(lang)) {
          try {
            highlighted = hljs.highlight(token.text, { language: lang }).value;
          } catch {
            // 高亮失败退回转义文本
          }
        } else if (lang === '') {
          try {
            highlighted = hljs.highlightAuto(token.text).value;
          } catch {
            // 自动检测失败退回转义文本
          }
        }
        const langClass = lang === '' ? '' : ` language-${escapeHtml(lang)}`;
        return `<pre><code class="hljs${langClass}">${highlighted}</code></pre>`;
      },
      link(token) {
        // 协议白名单：不安全协议（javascript: 等）降级为纯文本，不产出 <a>
        // 锚文本必须走 parseInline / escape：token.text 可能含原始 HTML（N2 XSS）
        const label =
          token.tokens && token.tokens.length > 0
            ? this.parser.parseInline(token.tokens)
            : escapeHtml(token.text ?? '');
        if (!isSafeLink(token.href)) {
          return label;
        }
        const href = escapeHtml(token.href);
        const title = token.title ? ` title="${escapeHtml(token.title)}"` : '';
        return `<a href="${href}"${title} target="_blank" rel="noopener noreferrer">${label}</a>`;
      },
      image(token) {
        // 图片同样走协议白名单：不安全协议降级为纯文本（不展示不可信图像）
        if (!isSafeLink(token.href)) {
          return escapeHtml(token.text ?? '');
        }
        const src = escapeHtml(token.href);
        const alt = token.text ? escapeHtml(token.text) : '';
        const title = token.title ? ` title="${escapeHtml(token.title)}"` : '';
        return `<img src="${src}" alt="${alt}"${title} loading="lazy" />`;
      },
    },
  });
}

export interface MarkdownProps {
  /** 已转义的原始 Markdown 文本（不做二次转义，渲染器已按行覆盖）。 */
  markdown: string;
}

/** 渲染 Markdown 为带 hljs 高亮的 HTML；仅输出静态 HTML（无事件绑定）。 */
export function Markdown({ markdown }: MarkdownProps): ReactElement {
  const html = useMemo(() => {
    configureMarked();
    return marked.parse(markdown, { async: false });
  }, [markdown]);
  // dangerouslySetInnerHTML 仅接受本模块静态生成的 HTML（原始 HTML 已转义、代码已高亮），
  // 不经手用户输入的可执行内容
  return <div className="chat-markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}
