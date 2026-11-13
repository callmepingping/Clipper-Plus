import { describe, it, expect } from 'vitest';
import { applyTask } from './workflow';
import type { Task } from './protocol';

describe('VideoMind workflow decisions', () => {
 it('applies only completed transcription results and preserves documents on failure', () => {
  const before = { original: 'old', title: 'Old' };
  const task: Task = { task_id: 'b', kind: 'transcription', source_url: 'https://example.com/video', state: 'FAILED', progress: 0, message: '', error: null,
   result: { markdown: 'new', title: 'New' } };
  expect(applyTask(before, task)).toBe(before);
  expect(applyTask(before, { ...task, state: 'COMPLETED' })).toEqual({ original: 'new', title: 'New', taskId: 'b', sourceUrl: task.source_url });
 });
});
