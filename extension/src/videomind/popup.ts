import { request, Configuration, Task, NativeError, MarkdownEntry, Result, terminal } from './protocol';
import { texts } from './i18n';
import { translatePage, setupLanguageAndDirection } from '../utils/i18n';
import type { ContentResponse } from '../utils/content-extractor';

type CachedResponse = { entry: MarkdownEntry | null; language: Configuration['language'] };
type VideoMarkdownSaveBridge = Window & { clipperPlusPrepareSave?: () => Promise<void> };

let text = texts('en');
let config: Configuration | null = null;
let task: Task | null = null;
let pending: { url: string; metadata: Record<string, unknown> } | null = null;
let originalTranscriptText = '';
let startingTranscription = false;
let generation = 0;
let popupOpenedAt = performance.now();
let timer: ReturnType<typeof setTimeout> | undefined;
let editRevision = 0;
let savedRevision = 0;
let editSaveTimer: ReturnType<typeof setTimeout> | undefined;
let persistenceQueue: Promise<void> = Promise.resolve();
let sourceUrl = '';
let controlsAttached = false;
let originalPopupReadyResolve!: (success: boolean) => void;
let originalPopupReadySignalled = false;
const originalPopupReady = new Promise<boolean>(resolve => { originalPopupReadyResolve = resolve; });
const element = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;
const titleField = (): HTMLTextAreaElement => element('note-name-field');
const markdownField = (): HTMLTextAreaElement => element('note-content-field');

function measure(stage: string, started: number): void {
	console.info('[Clipper Plus] video_init_timing', { stage, duration_ms: Math.round(performance.now() - started),
		at_ms: Math.round(performance.timeOrigin + performance.now()) });
}

export function markOriginalPopupReady(success = true): void {
	if (originalPopupReadySignalled) return;
	originalPopupReadySignalled = true;
	originalPopupReadyResolve(success);
}

function localize(): void {
	document.documentElement.lang = config?.language || 'en';
	element('cancel').textContent = text.cancel;
	element('confirmation-title').textContent = text.confirm;
	element('yes').textContent = text.yes;
	element('no').textContent = text.no;
}

function setStatus(value: string): void {
	element('status').textContent = value;
}

function setError(error: unknown): void {
	const target = element('error');
	const native = error instanceof NativeError ? error : null;
	const message = native ? ((text.errors as Record<string, string>)[native.code] || text.failed) : text.failed;
	target.textContent = native ? `${message} (${native.code})` : message;
	target.hidden = false;
	setStatus(message);
}

function showControls(): void {
	element('videomind-controls').hidden = false;
	localize();
}

function updateControls(): void {
	const active = !!task && !terminal(task);
	const progress = element<HTMLProgressElement>('progress');
	progress.hidden = !active || (task?.state !== 'DOWNLOADING' && task?.state !== 'TRANSCRIBING');
	progress.value = task?.progress || 0;
	element('cancel').hidden = !active;
	element<HTMLButtonElement>('cancel').disabled = false;
	element<HTMLButtonElement>('clip-btn').disabled = active;
}

function setMarkdown(markdown: string, title: string, url: string, taskId?: string): void {
	sourceUrl = url;
	titleField().value = title || 'Video';
	markdownField().value = markdown;
	markdownField().dataset.clipperPlusMarkdown = 'true';
	titleField().dispatchEvent(new Event('input', { bubbles: true }));
	editRevision = 0;
	savedRevision = 0;
	if (editSaveTimer) clearTimeout(editSaveTimer);
	editSaveTimer = undefined;
	const clip = element<HTMLButtonElement>('clip-btn');
	clip.textContent = text.save;
	void taskId;
}

function enqueueCacheUpdate(revision: number, url: string, markdown: string, title: string, epoch = generation): Promise<void> {
	const operation = persistenceQueue.catch(() => undefined).then(async () => {
		await request('UPDATE_CACHED_MARKDOWN', { url, markdown, title });
		if (epoch === generation && url === sourceUrl) savedRevision = Math.max(savedRevision, revision);
	});
	persistenceQueue = operation;
	return operation;
}

function onMarkdownEdited(): void {
	if (!sourceUrl || markdownField().dataset.clipperPlusMarkdown !== 'true') return;
	const heading = markdownField().value.match(/^#\s+(.+?)\s*#*\s*$/m)?.[1]?.trim();
	if (heading) titleField().value = heading;
	editRevision += 1;
	const revision = editRevision;
	const epoch = generation;
	if (editSaveTimer) clearTimeout(editSaveTimer);
	editSaveTimer = setTimeout(() => {
		editSaveTimer = undefined;
		void enqueueCacheUpdate(revision, sourceUrl, markdownField().value, titleField().value, epoch).catch(setError);
	}, 350);
}

function onTitleEdited(): void {
	if (!sourceUrl || markdownField().dataset.clipperPlusMarkdown !== 'true') return;
	const title = titleField().value.replace(/[\r\n]+/g, ' ').trim();
	let markdown = markdownField().value;
	if (/^---\r?\n/.test(markdown)) {
		markdown = markdown.replace(/^(---\r?\n[\s\S]*?^title:\s*).*?$/m, (_all, prefix: string) => prefix + JSON.stringify(title));
	}
	markdown = markdown.replace(/^(#\s+).*$/m, (_all, prefix: string) => prefix + title);
	if (markdown !== markdownField().value) markdownField().value = markdown;
	onMarkdownEdited();
}

async function flushEditedMarkdown(): Promise<void> {
	if (editSaveTimer) {
		clearTimeout(editSaveTimer);
		editSaveTimer = undefined;
	}
	if (savedRevision >= editRevision || !sourceUrl || markdownField().dataset.clipperPlusMarkdown !== 'true') {
		await persistenceQueue.catch(() => undefined);
		return;
	}
	await enqueueCacheUpdate(editRevision, sourceUrl, markdownField().value, titleField().value, generation);
}

function taskLabel(current: Task): string {
	const states: Record<string, string> = {
		QUEUED: text.queued, DOWNLOADING: text.downloading,
		TRANSCRIBING: text.transcribing, GENERATING_MARKDOWN: text.generating,
		COMPLETED: text.completed, FAILED: text.failed, CANCELLED: text.cancelled,
	};
	return `${states[current.state] || text.ready}${terminal(current) ? '' : ` · ${Math.round((current.progress || 0) * 100)}%`}`;
}

function nextGeneration(): number {
	generation += 1;
	if (timer) clearTimeout(timer);
	timer = undefined;
	task = null;
	originalTranscriptText = '';
	startingTranscription = false;
	return generation;
}

async function currentTabStillMatches(url: string, epoch: number): Promise<boolean> {
	if (epoch !== generation) return false;
	const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
	return epoch === generation && tab?.url === url;
}

async function detectVideoPage(): Promise<{ url: string; metadata: Record<string, unknown> } | null> {
	const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
	if (!tab?.id || !tab.url || !/^https?:\/\//i.test(tab.url)) return null;
	const url = tab.url;
	const parsed = new URL(url);
	const hostname = parsed.hostname;
	let candidate = isYouTubeVideo(url) || (
		/(^|\.)(bilibili\.com|vimeo\.com|douyin\.com|tiktok\.com|twitch\.tv|dailymotion\.com)$/i.test(hostname)
		&& parsed.pathname !== '/');
	if (!candidate) {
		try {
			const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => ({
				video: !!document.querySelector('video,meta[property="og:video"],meta[property="og:video:url"]') ||
					(document.querySelector('meta[property="og:type"]')?.getAttribute('content') || '').startsWith('video'),
			}) });
			candidate = !!result?.result?.video;
		} catch { return null; }
	}
	return candidate ? { url, metadata: { title: tab.title || '', url } } : null;
}

function isYouTubeVideo(url: string): boolean {
	const parsed = new URL(url);
	const host = parsed.hostname.toLowerCase();
	if (host === 'youtu.be' || host === 'www.youtu.be') return !!parsed.pathname.split('/')[1];
	if (!/(^|\.)youtube\.com$/.test(host)) return false;
	return (parsed.pathname === '/watch' && !!parsed.searchParams.get('v')) ||
		/^\/(shorts|live|embed)\/[^/]+/.test(parsed.pathname);
}

/** Defuddle's variables.transcript is timestamped Markdown text, not a track list. */
function hasTranscriptText(value: unknown): boolean {
	if (typeof value !== 'string') return false;
	const spokenText = value.replace(/\*\*\d+(?::\d+){1,2}\*\*/g, '')
		.replace(/\b\d+(?::\d+){1,2}\b/g, '');
	return /[\p{L}\p{N}]/u.test(spokenText);
}

async function startTranscription(epoch = generation): Promise<void> {
	if (!pending || epoch !== generation || startingTranscription || (task && !terminal(task))) return;
	startingTranscription = true;
	element('confirmation').hidden = true;
	element('error').hidden = true;
	setStatus(text.queued);
	try {
		// The engine persists the completed result even after this Popup closes.
		// Send the upstream note with its existing transcript removed so Whisper
		// replaces that text without discarding the rest of the clip.
		const upstreamMarkdown = markdownField().value;
		const preservedMarkdown = originalTranscriptText
			? upstreamMarkdown.replace(originalTranscriptText, '').trimEnd()
			: upstreamMarkdown;
		const accepted = await request<{ task_id: string }>('START_TRANSCRIPTION', {
			url: pending.url, metadata: { ...pending.metadata, markdown: preservedMarkdown },
		});
		measure('T5_whisper_task_started', popupOpenedAt);
		if (epoch !== generation || !await currentTabStillMatches(pending.url, epoch)) return;
		task = { task_id: accepted.task_id, kind: 'transcription', source_url: pending.url,
			state: 'QUEUED', progress: 0, message: '', error: null, result: null };
		sourceUrl = pending.url;
		updateControls();
		await poll(epoch, pending.url, accepted.task_id);
	} catch (error) {
		if (epoch === generation) setError(error);
	} finally {
		if (epoch === generation) startingTranscription = false;
	}
}

async function poll(epoch: number, url: string, taskId: string): Promise<void> {
	if (timer) clearTimeout(timer);
	try {
		const { task: latest } = await request<{ task: Task | null }>('GET_TASK_STATUS');
		if (epoch !== generation || !await currentTabStillMatches(url, epoch)) return;
		if (!latest || latest.task_id !== taskId || latest.source_url !== url) {
			task = null;
			setStatus(text.ready);
			updateControls();
			return;
		}
		task = latest;
		setStatus(taskLabel(latest));
		updateControls();
		if (latest.state === 'FAILED' && latest.error) setError(new NativeError(latest.error.code, latest.error.message));
		if (latest.state === 'COMPLETED') {
			if (latest.result) {
				const result = latest.result as Result;
				setMarkdown(result.markdown, result.title, url, latest.task_id);
				setStatus(text.completed);
			}
			updateControls();
			return;
		}
		if (!terminal(latest)) timer = setTimeout(() => { void poll(epoch, url, taskId); }, 600);
	} catch (error) {
		if (epoch === generation) setError(error);
	}
}

function attachVideoControls(): void {
	if (controlsAttached) return;
	controlsAttached = true;
	const content = markdownField();
	content.addEventListener('input', onMarkdownEdited);
	titleField().addEventListener('input', onTitleEdited);
	(window as VideoMarkdownSaveBridge).clipperPlusPrepareSave = flushEditedMarkdown;
	element('yes').addEventListener('click', () => { void startTranscription(); });
	element('no').addEventListener('click', () => {
		element('confirmation').hidden = true;
		setStatus(text.ready);
	});
	element('cancel').addEventListener('click', async () => {
		if (!task || terminal(task)) return;
		const active = task;
		element<HTMLButtonElement>('cancel').disabled = true;
		try {
			await request('CANCEL_TASK', { task_id: active.task_id });
			await poll(generation, active.source_url, active.task_id);
		} catch (error) { setError(error); }
	});
	window.addEventListener('pagehide', () => {
		void flushEditedMarkdown().catch(() => undefined);
		generation += 1;
		if (timer) clearTimeout(timer);
	});
}

function useLanguage(language: Configuration['language']): Promise<void> {
	config = { language, protocol_version: 2 };
	text = texts(language);
	localize();
	return chrome.storage.local.set({ language: language === 'zh-CN' ? 'zh_CN' : language });
}

async function applyConfiguredLanguage(): Promise<void> {
	await translatePage();
	await setupLanguageAndDirection();
	localize();
}

async function useStoredLanguage(): Promise<void> {
	const stored = await chrome.storage.local.get('language');
	const language = stored.language === 'zh_CN' ? 'zh-CN'
		: stored.language === 'ja' ? 'ja' : 'en';
	config = { language, protocol_version: 2 };
	text = texts(language);
	localize();
}

/** The upstream popup owns extraction and rendering; only video pages use the engine cache. */
export async function initializeVideoPopupIfNeeded(activeUrl: string | undefined,
	getOriginalContent: () => Promise<ContentResponse | null>): Promise<void> {
	const epoch = nextGeneration();
	popupOpenedAt = performance.now();
	measure('T0_popup_initialization', popupOpenedAt);
	const urlStarted = performance.now();
	const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
	const pageUrl = activeUrl || tab?.url || '';
	measure('active_url', urlStarted);
	if (!pageUrl || !/^https?:\/\//i.test(pageUrl) || (tab?.url && pageUrl !== tab.url)) return;
	const detectionStarted = performance.now();
	const candidate = await detectVideoPage();
	measure('page_type_detection', detectionStarted);
	if (!candidate || candidate.url !== pageUrl || epoch !== generation) return;

	let cached: CachedResponse;
	const cacheStarted = performance.now();
	try {
		cached = await request<CachedResponse>('GET_CACHED_MARKDOWN', { url: pageUrl });
		measure('cache_lookup', cacheStarted);
		measure('T1_history_lookup_completed', popupOpenedAt);
	} catch (error) {
		measure('cache_lookup_failed', cacheStarted);
		await useStoredLanguage();
		await originalPopupReady;
		if (!await currentTabStillMatches(pageUrl, epoch)) return;
		await applyConfiguredLanguage();
		showControls();
		attachVideoControls();
		setError(error);
		return;
	}
	if (epoch !== generation) return;
	// Setup's interface language applies to ordinary clips too. Start applying
	// it without holding up the original popup initialization or page detection.
	const languageReady = useLanguage(cached.language).then(async () => {
		await originalPopupReady;
		if (epoch === generation && await currentTabStillMatches(pageUrl, epoch)) {
			await applyConfiguredLanguage();
		}
	});
	void languageReady.catch(() => undefined);
	if (cached.entry) {
		await languageReady;
		if (!await currentTabStillMatches(pageUrl, epoch)) return;
		showControls();
		attachVideoControls();
		setMarkdown(cached.entry.markdown, cached.entry.title, pageUrl, cached.entry.task_id);
		setStatus(text.completed);
		updateControls();
		return;
	}

	let extracted: ContentResponse | null = null;
	try {
		extracted = await getOriginalContent();
	} catch {
		// The upstream popup reports extraction failures through its own error UI.
	}
	const upstreamSucceeded = await originalPopupReady;
	measure('original_extraction', detectionStarted);
	measure('T2_original_extraction_completed', popupOpenedAt);
	if (!upstreamSucceeded || !extracted || epoch !== generation) return;
	const readyStarted = performance.now();
	if (!await currentTabStillMatches(pageUrl, epoch)) return;
	measure('original_popup_ready', readyStarted);
	showControls();
	attachVideoControls();
	pending = { url: candidate.url, metadata: {
		...candidate.metadata,
		title: extracted.title || candidate.metadata.title,
		author: extracted.author || '',
		description: extracted.description || '',
	} };
	const hasTranscript = hasTranscriptText(extracted.extractedContent?.transcript);
	originalTranscriptText = hasTranscript ? extracted.extractedContent.transcript : '';
	if (hasTranscript) {
		element('confirmation').hidden = false;
		setStatus(text.confirm);
	}
	measure('transcript_detection', readyStarted);
	measure('T3_transcript_detection_completed', popupOpenedAt);
	try {
		const { task: current } = await request<{ task: Task | null }>('GET_TASK_STATUS');
		if (epoch !== generation || !await currentTabStillMatches(pageUrl, epoch)) return;
		if (startingTranscription || (task && !terminal(task))) return;
		if (current && !terminal(current) && current.kind !== 'transcription') {
			element('confirmation').hidden = true;
			setStatus(text.busy);
			return;
		}
		if (current && !terminal(current)) {
			if (current.source_url !== pageUrl) {
				element('confirmation').hidden = true;
				setStatus(text.busy);
				return;
			}
			element('confirmation').hidden = true;
			task = current;
			setStatus(taskLabel(current));
			updateControls();
			await poll(epoch, pageUrl, current.task_id);
			return;
		}
		if (!hasTranscript) await startTranscription(epoch);
	} catch (error) {
		if (epoch === generation) setError(error);
	}
}

