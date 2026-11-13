import type { Task } from './protocol';

export interface Documents { original: string; title: string; taskId?: string; sourceUrl?: string }
export function applyTask(documents: Documents, task: Task): Documents {
	if (task.state !== 'COMPLETED' || !task.result) return documents;
	if (task.kind === 'transcription' && 'markdown' in task.result) return {
		original: task.result.markdown, title: task.result.title, taskId: task.task_id,
  sourceUrl: task.source_url,
 };
 return documents;
}
