import { marked } from 'marked';
import hljs from 'highlight.js';

marked.setOptions({
  breaks: true,
  gfm: true,
  // NOTE: headerIds and mangle options were removed in marked v11.
  // - headerIds: heading IDs are only generated when the marked-gfm-heading-id
  //   extension is explicitly loaded (not loaded here), so no IDs are generated.
  // - mangle: email address mangling was removed entirely in v11.
  // The default behavior without these options achieves the desired result.
});

marked.use({
  renderer: {
    code(code: string, infostring: string | undefined): string | false {
      const lang = infostring && hljs.getLanguage(infostring) ? infostring : undefined;
      try {
        if (lang) {
          const result = hljs.highlight(code, { language: lang });
          return `<pre><code class="hljs language-${lang}">${result.value}</code></pre>`;
        }
        const result = hljs.highlightAuto(code);
        return `<pre><code class="hljs">${result.value}</code></pre>`;
      } catch {
        return `<pre><code>${escapeHtml(code)}</code></pre>`;
      }
    },
  },
});

export function renderMarkdown(content: string): string {
  if (!content) return '';
  try {
    const result = marked.parse(content);
    if (typeof result === 'string') return result;
    return String(result);
  } catch {
    return escapeHtml(content);
  }
}

export function escapeHtml(text: string): string {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
