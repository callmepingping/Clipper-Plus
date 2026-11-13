import { PROTOCOL_VERSION, type Command } from './protocol';

const commands: Command[] = ['GET_CONFIGURATION', 'START_TRANSCRIPTION',
 'GET_TASK_STATUS', 'CANCEL_TASK', 'SAVE_ORIGINAL', 'GET_CACHED_MARKDOWN',
 'GET_HISTORY_MARKDOWN', 'UPDATE_CACHED_MARKDOWN', 'DELETE_CACHED_MARKDOWN',
 'GET_HISTORY', 'DELETE_HISTORY', 'CLEAR_HISTORY', 'GET_PENDING_SETTINGS_MIGRATION',
 'COMPLETE_SETTINGS_MIGRATION'];

function interfaceLanguage(): 'en' | 'zh-CN' | 'ja' {
 const language = chrome.i18n.getUILanguage().toLowerCase();
 if (language.startsWith('zh')) return 'zh-CN';
 if (language.startsWith('ja')) return 'ja';
 return 'en';
}

export function installVideoMindBridge(): void {
 chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.action !== 'videomind') return;
  // Content scripts share the runtime ID; only private extension pages may invoke native commands.
  const ownOrigin = chrome.runtime.getURL('');
  const req = message.request;
  const safeCommand = req && typeof req.type === 'string' && commands.includes(req.type) ? req.type : 'unknown';
  const safeVersion = req?.protocol_version === undefined ? 'unspecified'
   : typeof req.protocol_version === 'number' && Number.isSafeInteger(req.protocol_version) ? req.protocol_version : 'invalid';
  const reject = (code: string, reason: string, messageText: string) => {
   // Never log request bodies, URLs, task IDs, or other user data.
   console.error('[Clipper Plus] Native request rejected', {
    reason, command: safeCommand, protocol_version: safeVersion,
   });
   reply({ id: typeof req?.id === 'string' ? req.id : '', status: 'error',
    error: { code, message: messageText, retryable: false } });
  };
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(ownOrigin)) {
   reject('REQUEST_ORIGIN_DENIED', 'extension_origin', 'This request did not come from a Clipper Plus page. Reload the extension and try again.');
   return;
  }
  if (!req || typeof req !== 'object' || typeof req.id !== 'string' || !req.id || req.id.length > 128) {
   reject('INVALID_REQUEST_SCHEMA', 'request_id', 'The extension sent an invalid request ID. Reload the extension and try again.');
   return;
  }
  if (req.protocol_version !== undefined && req.protocol_version !== PROTOCOL_VERSION) {
   reject('PROTOCOL_VERSION_MISMATCH', 'protocol_version', 'The extension and local engine use different protocol versions. Reload the extension and reconnect.');
   return;
  }
  if (!commands.includes(req.type)) {
   reject('UNKNOWN_COMMAND', 'command', 'The extension requested a command this background worker does not support. Reload the extension.');
   return;
  }
  if (!req.payload || typeof req.payload !== 'object' || Array.isArray(req.payload)) {
   reject('INVALID_REQUEST_SCHEMA', 'payload', 'The extension sent an invalid request payload. Reload the extension and try again.');
   return;
  }
  if (new TextEncoder().encode(JSON.stringify(req)).length > 900000) {
   reply({ id: req.id, status: 'error', error: { code: 'MESSAGE_TOO_LARGE', message: 'This document is too large for Native Messaging.', retryable: false } });
   return;
  }
  const forwarded = req.type === 'GET_CACHED_MARKDOWN'
   ? { ...req, payload: { ...req.payload, ui_language: interfaceLanguage() } }
   : req;
  chrome.runtime.sendNativeMessage('com.videomind.host', forwarded, response => {
   const failed = chrome.runtime.lastError;
   if (failed) {
    const detail = (failed.message || '').toLowerCase();
    const code = detail.includes('host not found') || detail.includes('not registered')
     ? 'NATIVE_HOST_NOT_REGISTERED'
     : detail.includes('host has exited') || detail.includes('failed to start')
      ? 'NATIVE_HOST_FAILED_TO_START' : 'NATIVE_MESSAGING_FAILED';
    console.error('[Clipper Plus] Native host call failed', { code, command: safeCommand, protocol_version: PROTOCOL_VERSION });
    reply({ id: req.id, status: 'error', error: {
     code, message: code === 'NATIVE_HOST_NOT_REGISTERED'
      ? 'Chrome cannot find the Clipper Plus Native Messaging Host. Check its registration.'
      : code === 'NATIVE_HOST_FAILED_TO_START'
       ? 'The Clipper Plus Native Messaging Host could not start. Check its executable path and permissions.'
       : 'Chrome could not communicate with the Clipper Plus Native Messaging Host.', retryable: true,
    } });
   } else reply(response);
  });
  return true;
 });
}
