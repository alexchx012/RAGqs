import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from './markdown';

/*
 * Markdown 渲染安全测试（C2）：javascript: 等危险协议降级为纯文本，
 * http/https/mailto/相对路径放行为 <a>；原始 HTML 转义；autolink 同样过白名单。
 */

describe('Markdown（XSS 安全）', () => {
  it('javascript: 链接降级为纯文本，不产出 <a>，不执行', () => {
    render(<Markdown markdown="[x](javascript:alert(1))" />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('x');
    expect(container.innerHTML).not.toContain('javascript:');
  });

  it('data:/vbscript: 协议同样降级', () => {
    render(<Markdown markdown="[a](data:text/html,<script>) [b](vbscript:msgbox)" />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('a');
    expect(container.textContent).toContain('b');
  });

  it('http/https/mailto/相对路径放行并带 noopener', () => {
    render(
      <Markdown markdown="[web](https://example.com) [rel](/docs/x) [mail](mailto:a@b.c)" />,
    );
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    const anchors = container.querySelectorAll('a');
    expect(anchors.length).toBe(3);
    for (const anchor of anchors) {
      expect(anchor.getAttribute('rel')).toContain('noopener');
      expect(anchor.getAttribute('target')).toBe('_blank');
    }
    expect(anchors[0]?.getAttribute('href')).toBe('https://example.com');
    expect(anchors[1]?.getAttribute('href')).toBe('/docs/x');
  });

  it('协议相对 // URL 拒绝：不产出可点击 <a>，不放行外链', () => {
    render(<Markdown markdown="[x](//example.com) [y](//evil.example/path)" />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('x');
    expect(container.textContent).toContain('y');
    // 不得落地为 href="//..." 可点击外链
    expect(container.innerHTML).not.toMatch(/href=["']\/\//);
  });

  it('autolink（裸 URL）同样过协议白名单', () => {
    render(<Markdown markdown="visit https://example.com now" />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    const anchors = container.querySelectorAll('a');
    expect(anchors.length).toBe(1);
    expect(anchors[0]?.getAttribute('href')).toBe('https://example.com');
  });

  it('原始 HTML 转义为纯文本（不产生可执行节点）', () => {
    render(<Markdown markdown={'<script>alert(1)</script> <img src=x onerror=alert(1)>'} />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img[onerror]')).toBeNull();
    expect(container.textContent).toContain('<script>');
  });

  it('安全协议链接的锚文本内嵌 HTML 被转义（N2：不注入可执行节点）', () => {
    render(<Markdown markdown={'[<img src=x onerror=alert(1)>](https://example.com)'} />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    const anchor = container.querySelector('a');
    expect(anchor).not.toBeNull();
    expect(anchor?.getAttribute('href')).toBe('https://example.com');
    // 锚文本不得落地为真实 <img onerror=...>
    expect(anchor?.querySelector('img')).toBeNull();
    expect(container.querySelector('img[onerror]')).toBeNull();
    expect(anchor?.innerHTML).toContain('&lt;img');
    expect(anchor?.textContent).toContain('<img');
  });

  it('代码块经 highlight.js 高亮为结构化 span（无原始 HTML 注入）', () => {
    render(<Markdown markdown={'```ts\nconst a = 1;\n```'} />);
    const container = document.querySelector('.chat-markdown') as HTMLElement;
    expect(container.querySelector('pre code.hljs')).not.toBeNull();
  });
});
