// Controlled fixture events for native DSH. The adapter is the component under test.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export const inject = ['attachments'];

export async function apply(ctx, config) {
  const runtime = createRequire(config.runtimePackage);
  const { createUserMessage } = await import(pathToFileURL(runtime.resolve('@deepseek-ai/dsh-llm')).href);
  const phase = () => readFileSync(config.phasePath, 'utf8').trim();
  const bytes = readFileSync(config.targetPath);
  const fixture = { kind: 'workspace-history-data', form: 'snapshot', sections: [] };
  const ordinary = text => createUserMessage({ content: [{ type: 'text', text }], source: fixture });
  let archive;
  let attachment;
  let receipt;
  const applied = new Set();

  function replace(session, node, text) {
    session.append('user/message', ordinary(text), {
      surfaceOp: { op: 'replace', startSeq: node.seq, endSeq: node.seq },
      sourceEventSeqs: [node.seq],
    });
  }

  ctx.on('agent/pre-step', async ({ agent }, next) => {
    if (agent.id !== config.sessionId || applied.has(phase())) return next();
    const session = agent.session;
    const current = phase();
    applied.add(current);
    if (!archive) {
      [attachment] = await ctx.attachments.saveImages([{
        data: new Uint8Array(bytes), mediaType: 'image/png', name: 'archived.png',
      }]);
      archive = session.append('user/message', createUserMessage({
        content: [
          { type: 'image', attachment },
          { type: 'text', text: config.historyText },
        ],
        source: fixture,
      }), { surfaceOp: 'append' });
      if (config.save) {
        const result = await ctx.get('vistaHistory').saveImage({
          sessionId: agent.id, requestId: 'native-sdk-save', png: bytes,
          label: config.label, subtask_id: 'archive-set', subtask_label: '早期留白',
        });
        if (result.short_id !== 'R2' || result.receipt !== config.receipt) {
          throw new Error('Unexpected native save receipt');
        }
      } else {
        receipt = session.append('user/message', ordinary(config.receipt), { surfaceOp: 'append' });
      }
    }
    if (current === 'image-only') {
      session.append('image/offload', { targets: [{ seq: archive.seq, imageIndexes: [0] }] });
    }
    if (current === 'feedback') {
      const range = session.surface.nodes.filter(seq => seq >= archive.seq);
      const end = range.findLastIndex(seq => session.eventAt(seq).type === 'assistant/message');
      if (end < 0) throw new Error('Missing prelude checkpoint');
      const sourceEventSeqs = range.slice(0, end + 1);
      session.append('user/message', ordinary('同一组图已经做过早期版本，现在继续改当前版本。'), {
        surfaceOp: { op: 'replace', startSeq: archive.seq, endSeq: sourceEventSeqs.at(-1) },
        sourceEventSeqs,
      });
    }
    if (['last-clue', 'simultaneous', 'native-offload'].includes(current)) {
      const receiptNode = receipt ?? session.surface.nodes.map(seq => session.eventAt(seq)).find(item =>
        item.type === 'user/message' && item.data.content.some(block => block.text === config.receipt));
      if (!receiptNode) throw new Error('Missing receipt in controlled checkpoint');
      if (current === 'simultaneous') replace(session, archive, '早期版本已归档。');
      replace(session, receiptNode, '继续制作同一组图。');
    }
    if (current === 'directory-lost') {
      const directory = session.surface.nodes.map(seq => session.eventAt(seq)).find(item =>
        item.type === 'user/message' && item.data.source?.sections
          ?.some(section => section.name === 'vista-image-catalog'));
      if (!directory) throw new Error('Missing previously delivered directory');
      replace(session, directory, '继续制作同一组图。');
    }
    return next();
  });

  ctx.on('agent/turn-stopping', async ({ agent }) => {
    const current = phase();
    if (agent.id !== config.sessionId || !current.startsWith('note-')) return;
    const event = agent.session.snapshotEvents().findLast(item =>
      item.type === 'user/message' && item.data.source?.kind === 'user');
    if (!event) throw new Error('Missing SDK user feedback event');
    const data = { sessionId: agent.id, userSeq: event.seq };
    if (current === 'note-reply') {
      data.replyToSeq = archive.seq;
      data.attachmentId = attachment.attachmentId;
    }
    const service = ctx.get('vistaHistory');
    await service.recordUserFeedback(data);
    if (current === 'note-explicit') await service.recordUserFeedback(data);
  });
}
