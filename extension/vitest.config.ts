import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

// Upstream fixture baselines use this timezone; product runtime keeps user time.
process.env.TZ = 'America/Los_Angeles';

export default defineConfig({
	define: {
		DEBUG_MODE: false,
	},
	test: {
		env: { TZ: 'America/Los_Angeles' },
		include: ['src/**/*.test.ts'],
		globals: true,
		alias: {
			'webextension-polyfill': fileURLToPath(new URL('./src/utils/__mocks__/webextension-polyfill.ts', import.meta.url)),
		},
	},
});
