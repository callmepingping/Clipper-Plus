// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { storage, sendMessage } = vi.hoisted(() => ({
	storage: { template_list: ['existing'], template_existing: ['{}'], vaults: ['My Vault'] } as Record<string, unknown>,
	sendMessage: vi.fn(),
}));

vi.mock('./browser-polyfill', () => ({
	default: {
		storage: { sync: {
			get: vi.fn(async () => ({ ...storage })),
			set: vi.fn(async (updates: Record<string, unknown>) => { Object.assign(storage, updates); }),
		} },
		runtime: { sendMessage },
	},
}));

describe('Obsidian settings migration', () => {
	beforeEach(() => {
		vi.resetModules();
		storage.template_list = ['existing'];
		storage.template_existing = ['{}'];
		storage.vaults = ['My Vault'];
		sendMessage.mockReset();
		sendMessage.mockImplementation(async ({ request }: any) => ({
			id: request.id,
			status: 'ok',
			data: request.type === 'GET_PENDING_SETTINGS_MIGRATION'
				? { migration: { id: '00000000-0000-4000-8000-000000000001', vaults: ['Imported Vault'], templates: [
					{ name: 'Clip', behavior: 'create', noteContentFormat: '{{content}}', properties: [], path: 'Clips' },
				] } }
				: { completed: true },
		}));
	});

	it('merges consented Vaults and Templates without replacing existing settings', async () => {
		const { importPendingObsidianSettings } = await import('./settings-migration');
		expect(await importPendingObsidianSettings()).toBe('imported');
		expect(storage.vaults).toEqual(['My Vault', 'Imported Vault']);
		expect(storage.template_list).toEqual(['existing', 'obsidian-0000000000004000-0']);
		expect(storage.template_existing).toEqual(['{}']);
		expect(sendMessage.mock.calls.map(([message]) => message.request.type)).toEqual([
			'GET_PENDING_SETTINGS_MIGRATION', 'COMPLETE_SETTINGS_MIGRATION',
		]);
	});

	it('does not acknowledge a migration when Extension storage fails', async () => {
		const browser = await import('./browser-polyfill');
		vi.mocked(browser.default.storage.sync.set).mockRejectedValueOnce(new Error('quota'));
		const { importPendingObsidianSettings } = await import('./settings-migration');
		expect(await importPendingObsidianSettings()).toBe('none');
		expect(sendMessage.mock.calls.map(([message]) => message.request.type)).toEqual(['GET_PENDING_SETTINGS_MIGRATION']);
	});
});
