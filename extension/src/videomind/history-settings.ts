import { request, HistoryItem, MarkdownEntry, NativeError } from './protocol';
import { renderMarkdown } from './render';
import { getUrlParameters } from '../utils/routing';

const PAGE_SIZE = 50;
const element = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;

let generation = 0;
let viewerGeneration = 0;
let offset = 0;
let total = 0;
let query = '';
let searchTimer: ReturnType<typeof setTimeout> | undefined;
let initialized = false;

function setStatus(message: string): void {
	const status = document.getElementById('history-status');
	if (status) status.textContent = message;
}

function makeButton(text: string, className = ''): HTMLButtonElement {
	const button = document.createElement('button');
	button.type = 'button';
	button.textContent = text;
	if (className) button.className = className;
	return button;
}

function availability(item: HistoryItem): string {
	if (item.markdown_available) return 'Markdown cache available';
	if (item.file_available) return 'Saved Markdown file available';
	return 'Markdown unavailable';
}

function showViewer(entry: MarkdownEntry | null, sourceUrl: string): void {
	const viewer = element('history-viewer');
	const content = element('history-viewer-content');
	const title = element('history-viewer-title');	
	viewer.hidden = false;
	content.replaceChildren();
	const url = element<HTMLAnchorElement>('history-viewer-url');
	url.href = sourceUrl;
	url.textContent = sourceUrl;
	if (!entry) {
		title.textContent = 'Markdown is not available';
		element('history-viewer-status').textContent = 'The cache and recorded local file are unavailable. Open the video tab and use Clipper Plus to process it again.';
		return;
	}
	title.textContent = entry.title;
	element('history-viewer-status').textContent = entry.source === 'file'
		? `Restored from the saved Markdown file (${entry.completed_at}).`
		: `Restored from the completed Markdown cache (${entry.completed_at}).`;
	renderMarkdown(content, entry.markdown);
}

function showViewerError(error: unknown): void {
	element('history-viewer').hidden = false;
	element('history-viewer-title').textContent = 'Could not load Markdown';
	element('history-viewer-status').textContent = error instanceof Error ? error.message : 'The saved Markdown could not be loaded.';
}

async function loadViewer(url: string, requestGeneration: number): Promise<void> {
	try {
		const { entry } = await request<{ entry: MarkdownEntry | null }>('GET_HISTORY_MARKDOWN', { url });
		if (requestGeneration !== viewerGeneration) return;
		showViewer(entry, url);
	} catch (error) {
		if (requestGeneration === viewerGeneration) showViewerError(error);
	}
}

function addRecord(item: HistoryItem): HTMLElement {
	const row = document.createElement('div');
	row.className = 'setting-item mod-horizontal';
	const info = document.createElement('div');
	info.className = 'setting-item-info';
	const title = document.createElement('div');
	title.className = 'setting-item-name';
	title.textContent = item.title || item.url;
	const details = document.createElement('div');
	details.className = 'setting-item-description';
	details.textContent = `${availability(item)} · ${new Date(item.completed_at).toLocaleString()}`;
	const link = document.createElement('button');
	link.type = 'button';
	link.className = 'history-url';
	link.textContent = item.url;
	link.title = 'Open video and Clipper Plus';
	link.addEventListener('click', () => { void openVideo(item.url); });
	info.append(title, details, link);
	const controls = document.createElement('div');
	controls.className = 'setting-item-control';
	const open = makeButton('Open video', 'mod-cta');
	open.addEventListener('click', () => { void openVideo(item.url); });
	const remove = makeButton('Delete');
	remove.addEventListener('click', () => { void deleteItem(item); });
	controls.append(open, remove);
	row.append(info, controls);
	return row;
}

async function loadHistory(reset = false): Promise<void> {
	if (reset) offset = 0;
	const currentGeneration = ++generation;
	setStatus('Loading history…');
	const list = element('history-list');
	list.replaceChildren();
	try {
		const response = await request<{ items: HistoryItem[]; total: number; limit: number; offset: number }>('GET_HISTORY', {
			query, limit: PAGE_SIZE, offset,
		});
		if (currentGeneration !== generation) return;
		total = response.total;
		response.items.forEach(item => list.appendChild(addRecord(item)));
		setStatus(total ? `${total} history record${total === 1 ? '' : 's'}` : 'No history records.');
		element('history-page').textContent = total ? `${offset + 1}–${Math.min(offset + response.items.length, total)} of ${total}` : '';
		(element('history-previous') as HTMLButtonElement).disabled = offset <= 0;
		(element('history-next') as HTMLButtonElement).disabled = offset + response.items.length >= total;
	} catch (error) {
		if (currentGeneration !== generation) return;
		setStatus(error instanceof Error ? error.message : 'Could not load history.');
	}
}

async function openVideo(url: string, allowViewer = true): Promise<void> {
	setStatus('Opening video…');
	let result: { success?: boolean; popupOpened?: boolean; tabId?: number; error?: string } | undefined;
	try {
		result = await chrome.runtime.sendMessage({ action: 'openHistoryVideo', url });
	} catch {
		result = undefined;
	}
	if (result?.success && result.popupOpened) {
		setStatus('Video tab and Clipper Plus popup opened.');
		return;
	}
 if (!allowViewer) {
  setStatus('The video tab is active. Use the Clipper Plus toolbar button to open its popup.');
  return;
 }
	// Chrome may reject programmatic popup opening. Open a full extension page
	// that reads the cache or the verified history file, and be explicit about it.
	try {
		const viewerUrl = chrome.runtime.getURL(`settings.html?section=history&history_url=${encodeURIComponent(url)}`);
		await chrome.tabs.create({ url: viewerUrl, active: true });
		setStatus('Chrome did not open the popup. The History viewer was opened in a tab.');
	} catch {
		setStatus(result?.error || 'Chrome could not open the popup or History viewer. Use the extension action on the video tab.');
	}
}

async function deleteItem(item: HistoryItem): Promise<void> {
	if (!window.confirm(`Delete the history record for “${item.title || item.url}”?`)) return;
	const deleteCache = window.confirm('Also delete its cached Markdown? Any downloaded files will be kept.');
	try {
		await request('DELETE_HISTORY', { url: item.url, delete_cache: deleteCache });
		await loadHistory();
	} catch (error) {
		setStatus(error instanceof Error ? error.message : 'Could not delete the history record.');
	}
}

async function clearHistory(): Promise<void> {
	if (!window.confirm('Clear all Clipper Plus History records?')) return;
	if (!window.confirm('This second confirmation will clear all History records. Also delete all cached Markdown? Downloaded files will not be deleted.')) return;
	const deleteCache = window.confirm('Delete all cached Markdown too? Choose Cancel to keep cached Markdown.');
	try {
		await request('CLEAR_HISTORY', { delete_cache: deleteCache });
		offset = 0;
		await loadHistory();
	} catch (error) {
		setStatus(error instanceof Error ? error.message : 'Could not clear history.');
	}
}

function openVideoFromViewer(): void {
	const url = new URLSearchParams(location.search).get('history_url');
	if (url) void openVideo(url, false);
}

export function initializeVideoHistory(): void {
	if (initialized || !document.getElementById('history-section')) return;
	initialized = true;
	element<HTMLInputElement>('history-search').addEventListener('input', event => {
		query = (event.currentTarget as HTMLInputElement).value.trim();
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = setTimeout(() => { void loadHistory(true); }, 300);
	});
	element('history-refresh').addEventListener('click', () => { void loadHistory(); });
	element('history-clear').addEventListener('click', () => { void clearHistory(); });
	element('history-previous').addEventListener('click', () => {
		offset = Math.max(0, offset - PAGE_SIZE);
		void loadHistory();
	});
	element('history-next').addEventListener('click', () => {
		offset += PAGE_SIZE;
		void loadHistory();
	});
	element('history-viewer-open-video').addEventListener('click', openVideoFromViewer);
	void loadHistory(true);
	const { historyUrl } = getUrlParameters();
	if (historyUrl) void loadViewer(historyUrl, ++viewerGeneration);
}
