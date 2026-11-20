// Seed the same controlled, already-trimmed checkpoint in each scored session.
// The archived PNG is present in the DSH event log and absent from the surface.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

export const inject = ['attachments'];

export async function apply(ctx, config) {
  const runtime = createRequire(config.runtimePackage);
  const { createUserMessage } = await import(pathToFileURL(runtime.resolve('@deepseek-ai/dsh-llm')).href);
  let seeded = false;
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    if (!seeded && agent.id === config.sessionId) {
      seeded = true;
      const [attachment] = await ctx.attachments.saveImages([{
        data: new Uint8Array(readFileSync(config.image)), mediaType: 'image/png', name: 'archived.png',
      }]);
      const session = agent.session;
      const user = content => createUserMessage({ content, source: { kind: 'user' } });
      const old = session.append('user/message', user([
        { type: 'image', attachment },
      ]), { surfaceOp: 'append' });
      const receipt = session.append('user/message', user([
        { type: 'text', text: config.receipt },
      ]), { surfaceOp: 'append' });
      session.append('user/message', user([{ type: 'text', text: config.summary }]), {
        surfaceOp: { op: 'replace', startSeq: old.seq, endSeq: receipt.seq },
        sourceEventSeqs: [old.seq, receipt.seq],
      });
    }
    return next();
  });
}
