// Opt-in VISTA actions on ordinary native DSH user prompts.
import { appendFileSync } from 'node:fs';

export const inject = ['attachments'];

export function apply(ctx, config) {
  if (!config.sessionId) throw new Error('VISTA 图片操作需要绑定一个 DSH 会话');
  const feedback = new Map();
  const audit = action => {
    if (config.auditPath) {
      appendFileSync(config.auditPath, `${JSON.stringify({ session_id: config.sessionId, ...action })}\n`);
    }
  };
  function vista() {
    const service = ctx.get('vistaHistory');
    if (!service) throw new Error('当前会话需要加载可写的 VISTA 插件');
    return service;
  }

  // Install before VISTA's pre-step hook so its receipt joins the same request.
  ctx.on('agent/pre-step', async ({ agent, messages, signal }, next) => {
    if (agent.id !== config.sessionId) return next();
    for (const message of messages) {
      if (message.source?.kind !== 'user') continue;
      const firstLine = message.content.find(block => block.type === 'text')?.text.split(/\r?\n/, 1)[0].trim() ?? '';
      const save = /^保存图片\s*[:：]\s*(.+)$/u.exec(firstLine);
      if (save) {
        const images = message.content.filter(block => block.type === 'image');
        if (images.length !== 1 || images[0].attachment?.mediaType !== 'image/png') {
          throw new Error('保存图片时请随消息附上恰好一张 PNG');
        }
        const { ref, data } = await ctx.attachments.readImage(images[0].attachment, signal);
        const requestId = `dsh:${agent.id}:${message.id}`;
        const result = await vista().saveImage({
          sessionId: agent.id, requestId, png: data, label: save[1], source_agent: 'native-dsh',
          ...(config.subtaskId === undefined ? {} : { subtask_id: config.subtaskId }),
          ...(config.subtaskLabel === undefined ? {} : { subtask_label: config.subtaskLabel }),
        });
        audit({ kind: 'save', message_id: message.id, request_id: requestId, attachment_id: ref.attachmentId, ...result });
      }
      const note = /^评价\s+(R[1-9][0-9]*)\s*[:：]\s*\S/u.exec(firstLine);
      if (note) feedback.set(message.id, { target: note[1], fromSeq: agent.session.seq });
    }
    return next();
  });

  // The standard step appends the actual user message, assigning its sequence.
  ctx.on('agent/turn-stopping', async ({ agent }) => {
    if (agent.id !== config.sessionId || !feedback.size) return;
    const firstSeq = Math.min(...[...feedback.values()].map(item => item.fromSeq));
    for (const event of agent.session.snapshotEvents(firstSeq)) {
      if (event.type !== 'user/message' || event.data.source?.kind !== 'user') continue;
      const note = feedback.get(event.data.id);
      if (!note) continue;
      const written = await vista().recordUserFeedback({
        sessionId: agent.id, userSeq: event.seq, target: note.target,
      });
      feedback.delete(event.data.id);
      audit({ kind: 'feedback', message_id: event.data.id, user_seq: event.seq, target: note.target, written });
    }
  });
}
