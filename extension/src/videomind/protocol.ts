export type Language = 'en' | 'zh-CN' | 'ja';
export const PROTOCOL_VERSION = 2;
export type Command = 'GET_CONFIGURATION' | 'START_TRANSCRIPTION' |
 'GET_TASK_STATUS' | 'CANCEL_TASK' | 'SAVE_ORIGINAL' | 'GET_CACHED_MARKDOWN' |
 'GET_HISTORY_MARKDOWN' | 'UPDATE_CACHED_MARKDOWN' | 'DELETE_CACHED_MARKDOWN' |
 'GET_HISTORY' | 'DELETE_HISTORY' | 'CLEAR_HISTORY' | 'GET_PENDING_SETTINGS_MIGRATION' |
 'COMPLETE_SETTINGS_MIGRATION';
export interface SafeError { code: string; message: string; retryable: boolean }
export interface Response<T> { id: string; status: 'ok' | 'accepted' | 'error'; data?: T; error?: SafeError }
export interface Configuration { language: Language; protocol_version: number }
export interface Result { markdown: string; title: string; original_markdown?: string; metadata?: Record<string, unknown> }
export interface Task {
	task_id: string; kind: 'transcription'; source_url: string; state: string; progress: number;
	message: string; result: Result | null; error: SafeError | null;
}
export interface MarkdownEntry {
 url: string; title: string; markdown: string; completed_at: string; task_id: string;
 source?: 'cache' | 'file';
}
export interface HistoryItem {
 url: string; title: string; completed_at: string; task_id: string;
 markdown_available: boolean; file_available: boolean;
}
export const terminal = (task: Task): boolean => ['COMPLETED', 'FAILED', 'CANCELLED'].includes(task.state);

export class NativeError extends Error {
 constructor(public code: string, message: string) { super(message); }
}

// Short requests keep browser state separate from the independent engine's lifetime.
export async function request<T>(type: Command, payload: Record<string, unknown> = {}): Promise<T> {
 const id = crypto.randomUUID();
 let response: Response<T>;
 try {
  response = await chrome.runtime.sendMessage({ action: 'videomind', request: { id, type, payload, protocol_version: PROTOCOL_VERSION } });
 } catch {
  throw new NativeError('EXTENSION_BACKGROUND_UNAVAILABLE', 'The Clipper Plus extension background worker did not respond. Reload the extension and try again.');
 }
 if (!response || response.id !== id || !['ok', 'accepted', 'error'].includes(response.status)) {
  throw new NativeError('INVALID_RESPONSE', 'The Clipper Plus engine returned an invalid response.');
 }
 if (response.status === 'error') {
  throw new NativeError(response.error?.code || 'ENGINE_ERROR', response.error?.message || 'Clipper Plus could not complete the request.');
 }
 if (!response.data || typeof response.data !== 'object') throw new NativeError('INVALID_RESPONSE', 'Missing response data.');
 return response.data;
}
