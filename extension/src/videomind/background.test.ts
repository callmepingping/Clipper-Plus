// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';

let runtimeMessage: ((message: any, sender: any, reply: (value?: any) => void) => any) | undefined;
let nativeRequests: any[];

beforeEach(async () => {
	vi.resetModules();
	runtimeMessage = undefined;
	nativeRequests = [];
	vi.stubGlobal('chrome', {
		runtime: {
			id: 'extension-id',
			getURL: () => 'chrome-extension://extension-id/',
			onMessage: { addListener: (listener: typeof runtimeMessage) => { runtimeMessage = listener; } },
			sendNativeMessage: vi.fn((_host: string, request: any, callback: (response: any) => void) => {
				nativeRequests.push(request);
				callback({ id: request.id, status: 'ok', data: {} });
			}),
		},
	});
	const module = await import('./background');
	module.installVideoMindBridge();
});

describe('Clipper Plus Native bridge', () => {
	it('does not handle or delete cache for the removed Download Markdown action', () => {
		const reply = vi.fn();
		const handled = runtimeMessage?.({ action: 'trackMarkdownDownload', downloadId: 42, url: 'https://youtu.be/abc123DEF_0' },
			{ id: 'extension-id', url: 'chrome-extension://extension-id/popup.html' }, reply);
		expect(handled).toBeUndefined();
		expect(reply).not.toHaveBeenCalled();
		expect(nativeRequests).toHaveLength(0);
	});

	it('forwards validated native requests with a matching response ID', async () => {
		const reply = vi.fn();
		runtimeMessage?.({ action: 'videomind', request: {
			id: 'request-1', protocol_version: 2, type: 'GET_CONFIGURATION', payload: {},
		} }, { id: 'extension-id', url: 'chrome-extension://extension-id/popup.html' }, reply);
		await vi.waitFor(() => expect(reply).toHaveBeenCalledWith({
			id: 'request-1', status: 'ok', data: {},
		}));
		expect(nativeRequests).toHaveLength(1);
	});
});
