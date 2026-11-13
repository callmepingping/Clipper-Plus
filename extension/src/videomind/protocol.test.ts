import { describe, it, expect, vi, afterEach } from 'vitest';
import { request } from './protocol';
import { installVideoMindBridge } from './background';

afterEach(() => { vi.unstubAllGlobals(); });
describe('Native protocol', () => {
 it('correlates response IDs and keeps native errors structured', async () => {
  const send = vi.fn(async ({ request: req }) => ({ id: req.id, status: 'ok', data: { language: 'ja' } }));
  vi.stubGlobal('chrome', { runtime: { sendMessage: send } });
  expect(await request('GET_CONFIGURATION')).toEqual({ language: 'ja' });
  send.mockImplementationOnce(async () => ({ id: 'different', status: 'ok', data: { language: 'ja' } }));
  await expect(request('GET_CONFIGURATION')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
  send.mockImplementationOnce(async ({ request: req }) => ({ id: req.id, status: 'error', error: { code: 'VIDEO_DOWNLOAD_FAILED', message: 'Download failed.' } }) as any);
  await expect(request('START_TRANSCRIPTION', { url: 'https://example.com/video' })).rejects.toMatchObject({ code: 'VIDEO_DOWNLOAD_FAILED' });
 });
 it('allows native commands only from private extension pages and enforces size bounds', () => {
  const diagnostic = vi.spyOn(console, 'error').mockImplementation(() => {});
  let listener: Function = () => {};
  const native = vi.fn((_host, req, callback) => callback({ id: req.id, status: 'ok', data: {} }));
  vi.stubGlobal('chrome', { runtime: { id: 'test', getURL: () => 'chrome-extension://test/',
   onMessage: { addListener: (fn: Function) => { listener = fn; } }, sendNativeMessage: native } });
  installVideoMindBridge();
  const reply = vi.fn();
  const message = { action: 'videomind', request: { id: 'id', type: 'GET_CONFIGURATION', payload: {}, protocol_version: 2 } };
  listener(message, { id: 'test', url: 'https://example.com/' }, reply);
  expect(native).not.toHaveBeenCalled();
  expect(reply).toHaveBeenLastCalledWith(expect.objectContaining({
   status: 'error', error: expect.objectContaining({ code: 'REQUEST_ORIGIN_DENIED' }),
  }));
  listener(message, { id: 'test', url: 'chrome-extension://test/popup.html' }, reply);
  expect(native).toHaveBeenCalledTimes(1);
  expect(native.mock.calls[0][1].protocol_version).toBe(2);
  listener({ ...message, request: { ...message.request, type: 'UNKNOWN_COMMAND' } },
   { id: 'test', url: 'chrome-extension://test/popup.html' }, reply);
  expect(reply).toHaveBeenLastCalledWith(expect.objectContaining({
   status: 'error', error: expect.objectContaining({ code: 'UNKNOWN_COMMAND' }),
  }));
  listener({ ...message, request: { ...message.request, protocol_version: 99 } },
   { id: 'test', url: 'chrome-extension://test/popup.html' }, reply);
  expect(reply).toHaveBeenLastCalledWith(expect.objectContaining({
   status: 'error', error: expect.objectContaining({ code: 'PROTOCOL_VERSION_MISMATCH' }),
  }));
  listener({ ...message, request: { ...message.request, payload: { markdown: 'x'.repeat(900001) } } },
   { id: 'test', url: 'chrome-extension://test/popup.html' }, reply);
  expect(native).toHaveBeenCalledTimes(1);
  expect(reply).toHaveBeenLastCalledWith(expect.objectContaining({ error: expect.objectContaining({ code: 'MESSAGE_TOO_LARGE' }) }));
  diagnostic.mockRestore();
 });
});
