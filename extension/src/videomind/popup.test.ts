// @vitest-environment jsdom
import { beforeEach, describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Task } from './protocol';

vi.mock('../utils/i18n', () => ({ translatePage: async () => {}, setupLanguageAndDirection: async () => {} }));
vi.mock('../utils/browser-detection', () => ({ addBrowserClassToHtml: async () => {} }));

const markup = readFileSync(resolve('src/popup.html'), 'utf8');
let calls: string[];
let requests: Array<{ type: string; payload: Record<string, unknown> }>;
let current: Task | null;
let statusCalls: number;
let delayedStatusCall: number | null;
let resolveDelayedStatus: ((value: { task: Task | null }) => void) | null;
let activeUrl: string;
let pageHasVideo: boolean;
let cachedEntry: any;
let cacheLookupFails: boolean;
let selectedUiLanguage: string | undefined;
let backendUiLanguage: 'en' | 'zh-CN' | 'ja';
let originalTranscript: unknown;
let originalExtractionCalls: number;

beforeEach(async () => {
	vi.resetModules();
	document.documentElement.innerHTML = markup.replace(/^[\s\S]*?<html[^>]*>|<\/html>[\s\S]*$/g, '');
	calls = []; requests = []; current = null; cachedEntry = null; cacheLookupFails = false; selectedUiLanguage = undefined; backendUiLanguage = 'en';
	statusCalls = 0; delayedStatusCall = null; resolveDelayedStatus = null;
	activeUrl = 'https://youtube.com/watch?v=abc'; pageHasVideo = true;
	originalTranscript = ''; originalExtractionCalls = 0;
	vi.stubGlobal('chrome', {
		tabs: { query: async () => [{ id: 7, url: activeUrl, title: 'A video' }] },
		scripting: { executeScript: async () => [{ result: { video: pageHasVideo } }] },
		storage: { local: { set: async (value: any) => { selectedUiLanguage = value.language; } } },
		runtime: { sendMessage: async ({ request: req }: any) => {
			calls.push(req.type); requests.push({ type: req.type, payload: req.payload || {} });
			let data: any = {};
			if (req.type === 'GET_CACHED_MARKDOWN') {
				if (cacheLookupFails) return { id: req.id, status: 'error', error: { code: 'CACHE_READ_FAILED', message: 'Could not read saved Markdown.', retryable: true } };
				data = { entry: cachedEntry, language: backendUiLanguage };
			}
			if (req.type === 'GET_TASK_STATUS') {
				statusCalls += 1;
				if (statusCalls === delayedStatusCall) return new Promise(resolve => {
					resolveDelayedStatus = value => resolve({ id: req.id, status: 'ok', data: value });
				});
				data = { task: current };
			}
			if (req.type === 'START_TRANSCRIPTION') {
				current = { task_id: 'transcript', kind: 'transcription', source_url: req.payload.url, state: 'COMPLETED', progress: 1, message: '', error: null,
					result: { markdown: '# Original\n\n[00:00:01] Spoken words.', title: 'A video' } };
				data = { task_id: 'transcript' };
			}
			return { id: req.id, status: req.type === 'START_TRANSCRIPTION' ? 'accepted' : 'ok', data };
		} },
	});
	await import('./popup');
});

const click = (id: string) => (document.getElementById(id) as HTMLButtonElement).click();
async function open(): Promise<void> {
	const { initializeVideoPopupIfNeeded, markOriginalPopupReady } = await import('./popup');
	(document.getElementById('note-content-field') as HTMLTextAreaElement).value =
		`# A video\n\nOriginal description\n\n## Transcript\n\n${typeof originalTranscript === 'string' ? originalTranscript : ''}`;
	markOriginalPopupReady();
	await initializeVideoPopupIfNeeded(activeUrl, async () => {
		originalExtractionCalls += 1;
		return { title: 'A video', author: 'Channel', description: 'Description',
			extractedContent: { transcript: originalTranscript } } as any;
	});
}

describe('Video integration in the upstream popup', () => {
	it('keeps the original controls and uses only compact video status controls', async () => {
		await open();
		await vi.waitFor(() => expect(document.getElementById('status')?.textContent).toContain('Completed'));
		expect(document.getElementById('template-select')).not.toBeNull();
		expect(document.getElementById('note-name-field')).not.toBeNull();
		expect(document.querySelector('.metadata-properties')).not.toBeNull();
		expect(document.getElementById('vault-select')).not.toBeNull();
		expect(document.getElementById('path-name-field')).not.toBeNull();
		expect(document.getElementById('clip-btn')?.textContent).toBe('Save Markdown');
		expect(document.getElementById('download-original')).toBeNull();
		expect(document.querySelectorAll('#popup-header .popup-actions > *').length).toBe(5);
	});

	it('queries cache first, displays a hit without task lookup or video inspection', async () => {
		cachedEntry = { url: activeUrl, title: '缓存标题', markdown: '# 缓存标题\n\n字幕内容。 🎬', completed_at: '2026-11-09T12:00:00Z', task_id: 'cached-task' };
		await open();
		expect(calls).toEqual(['GET_CACHED_MARKDOWN']);
		expect((document.getElementById('note-name-field') as HTMLTextAreaElement).value).toBe('缓存标题');
		expect((document.getElementById('note-content-field') as HTMLTextAreaElement).value).toContain('字幕内容。 🎬');
		expect(calls).not.toContain('VIDEO_INSPECT');
		expect(calls).not.toContain('GET_TASK_STATUS');
	});

	it('lets ordinary pages use the original popup without waiting for native requests', async () => {
		activeUrl = 'https://example.com/article'; pageHasVideo = false;
		const { initializeVideoPopupIfNeeded } = await import('./popup');
		const started = performance.now();
		const result = initializeVideoPopupIfNeeded(activeUrl, async () => {
			originalExtractionCalls += 1;
			return null;
		});
		// The UI integration is not awaited by core/popup.ts; it does not change
		// or replace the original popup for a non-video page.
		expect(performance.now() - started).toBeLessThan(100);
		await result;
		expect(calls).toEqual([]);
		expect(document.getElementById('videomind-controls')?.hasAttribute('hidden')).toBe(true);
		expect(document.getElementById('download-original')).toBeNull();
	});

	it('starts Whisper directly when the original YouTube extractor has no transcript', async () => {
		await open();
		await vi.waitFor(() => expect(calls).toContain('START_TRANSCRIPTION'));
		await vi.waitFor(() => expect((document.getElementById('note-content-field') as HTMLTextAreaElement).value).toContain('Spoken words'));
		expect(calls).not.toContain('VIDEO_INSPECT');
		expect(requests.find(r => r.type === 'START_TRANSCRIPTION')?.payload.metadata).toMatchObject({ author: 'Channel' });
		expect(requests.find(r => r.type === 'START_TRANSCRIPTION')?.payload.metadata).toMatchObject({ markdown: expect.stringContaining('Original description') });
	});

	it('does not mistake caption track metadata for extracted subtitle text', async () => {
		originalTranscript = { tracks: [{ language: 'en', url: 'https://example.com/caption' }] };
		await open();
		expect(calls).not.toContain('VIDEO_INSPECT');
		expect(calls).toContain('START_TRANSCRIPTION');
		expect((document.getElementById('confirmation') as HTMLElement).hidden).toBe(true);
	});

	it('does not show transcription progress while upstream subtitles are being extracted', async () => {
		let finishExtraction!: (content: any) => void;
		const extraction = new Promise<any>(resolve => { finishExtraction = resolve; });
		const { initializeVideoPopupIfNeeded, markOriginalPopupReady } = await import('./popup');
		markOriginalPopupReady();
		const opening = initializeVideoPopupIfNeeded(activeUrl, () => extraction);
		await vi.waitFor(() => expect(calls).toContain('GET_CACHED_MARKDOWN'));
		expect(calls).not.toContain('GET_TASK_STATUS');
		expect((document.getElementById('progress') as HTMLProgressElement).hidden).toBe(true);
		expect(calls).not.toContain('VIDEO_INSPECT');
		finishExtraction({ extractedContent: { transcript: '**0:01** · Caption text' } });
		await opening;
		expect((document.getElementById('confirmation') as HTMLElement).hidden).toBe(false);
	});

	it('waits for a successful upstream extraction before treating a missing transcript as absent', async () => {
		let finishExtraction!: (content: any) => void;
		const extraction = new Promise<any>(resolve => { finishExtraction = resolve; });
		const { initializeVideoPopupIfNeeded, markOriginalPopupReady } = await import('./popup');
		markOriginalPopupReady();
		const opening = initializeVideoPopupIfNeeded(activeUrl, () => extraction);
		await vi.waitFor(() => expect(calls).toContain('GET_CACHED_MARKDOWN'));
		expect(calls).not.toContain('GET_TASK_STATUS');
		expect(calls).not.toContain('START_TRANSCRIPTION');
		finishExtraction({ title: 'Video', extractedContent: {} });
		await opening;
		expect(calls).toContain('START_TRANSCRIPTION');
	});

	it.each(['manual', 'automatic'])('uses the original %s transcript to confirm Whisper without inspection', async _kind => {
		originalTranscript = '**0:01** · Spoken subtitle text';
		await open();
		await vi.waitFor(() => expect((document.getElementById('confirmation') as HTMLElement).hidden).toBe(false));
		expect(calls).not.toContain('VIDEO_INSPECT');
		expect(calls).not.toContain('START_TRANSCRIPTION');
		click('no');
		expect(calls).not.toContain('START_TRANSCRIPTION');
	});

	it('runs the existing audio and Whisper task after Yes', async () => {
		originalTranscript = '**0:01** · Spoken subtitle text';
		await open();
		click('yes');
		await vi.waitFor(() => expect(calls).toContain('START_TRANSCRIPTION'));
		expect(calls).not.toContain('VIDEO_INSPECT');
		const preserved = String((requests.find(r => r.type === 'START_TRANSCRIPTION')?.payload.metadata as Record<string, unknown>).markdown);
		expect(preserved).toContain('Original description');
		expect(preserved).not.toContain('Spoken subtitle text');
	});

	it('reuses upstream transcript on other video sites without inspection', async () => {
		activeUrl = 'https://vimeo.com/12345';
		originalTranscript = '**0:01** · Caption text';
		await open();
		expect(calls).not.toContain('VIDEO_INSPECT');
		expect(originalExtractionCalls).toBe(1);
		expect((document.getElementById('confirmation') as HTMLElement).hidden).toBe(false);
		expect((document.getElementById('progress') as HTMLProgressElement).hidden).toBe(true);
	});

	it('does not start Whisper when upstream extraction fails', async () => {
		const { initializeVideoPopupIfNeeded, markOriginalPopupReady } = await import('./popup');
		markOriginalPopupReady(false);
		await initializeVideoPopupIfNeeded(activeUrl, async () => null);
		expect(calls).toEqual(['GET_CACHED_MARKDOWN']);
		expect((document.getElementById('confirmation') as HTMLElement).hidden).toBe(true);
	});

	it('persists edited Markdown and title while preserving Unicode', async () => {
		cachedEntry = { url: activeUrl, title: 'Cached', markdown: '# Cached', completed_at: '2026-11-09T12:00:00Z', task_id: 'cached-task' };
		await open();
		const title = document.getElementById('note-name-field') as HTMLTextAreaElement;
		const content = document.getElementById('note-content-field') as HTMLTextAreaElement;
		title.value = '日本語の題名'; title.dispatchEvent(new Event('input', { bubbles: true }));
		content.value = '# 日本語の題名\n\n中文、English、にほんご 🌏'; content.dispatchEvent(new Event('input', { bubbles: true }));
		await vi.waitFor(() => expect(requests.some(r => r.type === 'UPDATE_CACHED_MARKDOWN' && String(r.payload.markdown).includes('🌏'))).toBe(true));
		expect(requests.some(r => r.type === 'UPDATE_CACHED_MARKDOWN' && r.payload.title === '日本語の題名')).toBe(true);
	});

	it('does not let an old task error replace a successful cache hit', async () => {
		cachedEntry = { url: activeUrl, title: 'Cached', markdown: '# Cached', completed_at: '2026-11-09T12:00:00Z', task_id: 'cached-task' };
		current = { task_id: 'old-failure', kind: 'transcription', source_url: activeUrl, state: 'FAILED', progress: 0,
			message: '', error: { code: 'TRANSCRIPTION_FAILED', message: 'Old error', retryable: false }, result: null };
		await open();
		expect(calls).toEqual(['GET_CACHED_MARKDOWN']);
		expect((document.getElementById('error') as HTMLElement).hidden).toBe(true);
		expect(document.getElementById('status')?.textContent).toContain('Completed');
	});
});
