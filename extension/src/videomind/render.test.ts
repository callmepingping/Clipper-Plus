// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './render';

describe('Markdown display', () => {
 it('renders long Markdown including tables and code, without executable content or remote media', () => {
  const target = document.createElement('article');
  renderMarkdown(target, '# Title\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n```js\nconst a = 1;\n```\n\n' +
   '<script>alert(1)</script><img src="https://tracking.test/pixel" onerror="alert(1)"><iframe src="https://evil.test"></iframe>\n\n' +
   '[Unsafe](javascript:alert%281%29) [Safe](https://example.com)\n\n' + 'Long paragraph.\n\n'.repeat(1000));
  expect(target.querySelector('h1')?.textContent).toBe('Title');
  expect(target.querySelector('table')).not.toBeNull();
  expect(target.querySelector('pre code')?.textContent).toContain('const a');
  expect(target.querySelectorAll('script,img,iframe,style,[onerror]')).toHaveLength(0);
  expect(target.querySelector('a[href^="javascript"]')).toBeNull();
  expect(target.querySelector('a[href="https://example.com"]')?.getAttribute('rel')).toContain('noreferrer');
  expect(target.textContent?.length).toBeGreaterThan(15000);
 });
 it('blocks links that could invoke local commands or navigate inside the extension', () => {
  const target = document.createElement('article');
  renderMarkdown(target, '[local](obsidian://new) [relative](settings.html) [anchor](#section)');
  const links = target.querySelectorAll('a');
  expect(links[0].hasAttribute('href')).toBe(false);
  expect(links[1].hasAttribute('href')).toBe(false);
  expect(links[2].getAttribute('href')).toBe('#section');
 });
});
