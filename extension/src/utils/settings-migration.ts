import browser from './browser-polyfill';
import { compressToUTF16, decompressFromUTF16 } from 'lz-string';

interface PendingSettingsMigration {
	id: string;
	vaults: string[];
	templates: Array<Record<string, unknown>>;
}

interface MigrationEnvelope {
	id: string;
	status: 'ok' | 'error';
	data?: { migration?: PendingSettingsMigration | null; completed?: boolean };
	error?: { code?: string };
}

async function nativeRequest(type: 'GET_PENDING_SETTINGS_MIGRATION' | 'COMPLETE_SETTINGS_MIGRATION',
	payload: Record<string, unknown> = {}): Promise<MigrationEnvelope> {
	const response = await browser.runtime.sendMessage({
		action: 'videomind',
		request: { id: crypto.randomUUID(), protocol_version: 2, type, payload },
	}) as MigrationEnvelope;
	return response;
}

function chunks(template: Record<string, unknown>): string[] {
	const compressed = compressToUTF16(JSON.stringify(template));
	const result: string[] = [];
	for (let offset = 0; offset < compressed.length; offset += 8000) {
		result.push(compressed.slice(offset, offset + 8000));
	}
	return result;
}

/** Merge only explicitly consented Obsidian settings; never clear Extension storage. */
export async function importPendingObsidianSettings(): Promise<'none' | 'imported' | 'skipped'> {
	let envelope: MigrationEnvelope;
	try {
		envelope = await nativeRequest('GET_PENDING_SETTINGS_MIGRATION');
	} catch {
		return 'none';
	}
	if (envelope.status !== 'ok') return 'none';
	const migration = envelope.data?.migration;
	if (!migration || !/^[0-9a-f-]{36}$/i.test(migration.id) || !Array.isArray(migration.vaults) ||
		!Array.isArray(migration.templates)) return 'none';

	try {
		const stored = await browser.storage.sync.get(null) as Record<string, unknown>;
		const templateList = Array.isArray(stored.template_list)
			? (stored.template_list as unknown[]).filter((id): id is string => typeof id === 'string') : [];
		const vaults = Array.isArray(stored.vaults)
			? (stored.vaults as unknown[]).filter((name): name is string => typeof name === 'string') : [];
		const nextIds = [...templateList];
		const updates: Record<string, unknown> = {};
		const migrationKey = migration.id.replace(/-/g, '').slice(0, 16);
		const existingNames = new Set<string>();
		for (const existingId of templateList) {
			try {
				const saved = stored[`template_${existingId}`];
				const decoded = Array.isArray(saved)
					? JSON.parse(decompressFromUTF16(saved.join('')) || '{}') : saved;
				if (decoded && typeof decoded.name === 'string') existingNames.add(decoded.name);
			} catch { /* A malformed existing template is preserved untouched. */ }
		}
		let imported = 0;
		migration.templates.forEach((template, index) => {
			if (typeof template.name !== 'string' || !Array.isArray(template.properties) ||
				typeof template.noteContentFormat !== 'string') return;
			const id = `obsidian-${migrationKey}-${index}`;
			if (nextIds.includes(id) || Object.prototype.hasOwnProperty.call(stored, `template_${id}`)) return;
			const cloned: Record<string, unknown> = { ...template, id };
			let name = template.name;
			let suffix = 1;
			while (existingNames.has(name)) name = `${template.name} (Obsidian ${suffix++})`;
			cloned.name = name;
			existingNames.add(name);
			updates[`template_${id}`] = chunks(cloned);
			nextIds.push(id);
			imported += 1;
		});
		const mergedVaults = [...new Set([...vaults, ...migration.vaults.filter(name => typeof name === 'string')])];
		if (imported > 0) updates.template_list = nextIds;
		if (mergedVaults.length > vaults.length) updates.vaults = mergedVaults;
		await browser.storage.sync.set(updates);
		const completed = await nativeRequest('COMPLETE_SETTINGS_MIGRATION', { id: migration.id });
		if (completed.status !== 'ok' || completed.data?.completed !== true) throw new Error('Migration acknowledgment failed');
		return imported > 0 || mergedVaults.length > vaults.length ? 'imported' : 'skipped';
	} catch (error) {
		// Keep the migration pending so a later Settings visit can retry safely.
		console.error('[Clipper Plus] Settings migration could not be applied.',
			error instanceof Error ? error.name : 'UnknownError');
		return 'none';
	}
}
