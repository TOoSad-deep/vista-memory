export const inject = ['tools'];
export function apply(ctx) {
  ctx.on('agent/created', ({ agent }) => {
    process.stderr.write(JSON.stringify({
      probe: 'vista-tools',
      sessionId: agent.id,
      tools: ctx.tools.schemas(agent).map(tool => tool.name),
    }) + '\n');
  });
}
