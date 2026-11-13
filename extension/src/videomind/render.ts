import DOMPurify from 'dompurify';
import { marked } from 'marked';

export function renderMarkdown(target: HTMLElement, markdown: string): void {
 const html = marked.parse(markdown, { async: false, gfm: true }) as string;
 const fragment = DOMPurify.sanitize(html, {
  RETURN_DOM_FRAGMENT: true,
  FORBID_TAGS: ['img', 'video', 'audio', 'iframe', 'form', 'input', 'style', 'svg', 'math'],
  FORBID_ATTR: ['style', 'srcset'],
 });
 target.replaceChildren(fragment);
 target.querySelectorAll('a').forEach(link => {
  const href = link.getAttribute('href') || '';
  // No JavaScript, data, Obsidian commands, relative extension paths or remote media.
  if (!/^https?:\/\//i.test(href) && !href.startsWith('#')) link.removeAttribute('href');
  link.rel = 'noopener noreferrer';
  link.target = '_blank';
 });
}
