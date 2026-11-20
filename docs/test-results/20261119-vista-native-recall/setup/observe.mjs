// Local fixture boundary and transport observer. No additional agent tools.
import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const runtime = createRequire('/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/package.json');
export async function apply(ctx, cfg) {
  const { createUserMessage } = await import(pathToFileURL(runtime.resolve('@deepseek-ai/dsh-llm')).href);
  const log = event => appendFileSync(cfg.log, JSON.stringify({time: new Date().toISOString(), ...event}) + '\n');
  let replaced = false;
  ctx.on('agent/pre-step', ({agent}, next) => {
    if (!replaced && existsSync(cfg.marker)) {
      const session = agent.session;
      const events = session.surface.nodes.map(seq => session.eventAt(seq));
      const start = events.find(e => e.type === 'user/message' && e.data.content.some(b => b.type === 'image'));
      const end = events.findLast(e => e.type === 'assistant/message');
      if (!start || !end || end.seq <= start.seq) throw new Error('Missing completed, image-bearing history');
      const receipt = session.append('user/message', createUserMessage({
        content: [{type:'text', text:cfg.summary}], source:{kind:'user'},
      }), {surfaceOp:{op:'replace', startSeq:start.seq, endSeq:end.seq},
          sourceEventSeqs:events.filter(e => start.seq <= e.seq && e.seq <= end.seq).map(e => e.seq)});
      replaced = true;
      log({kind:'history-replaced', session:session.id, start:start.seq, end:end.seq, seq:receipt.seq, summary:cfg.summary});
    }
    return next();
  });
  const original = globalThis.fetch;
  const uploads = new Map();
  if (existsSync(cfg.log)) {
    for (const line of readFileSync(cfg.log,'utf8').trim().split('\n').filter(Boolean)) {
      const prior = JSON.parse(line);
      if (prior.kind === 'upload' && prior.status === 200) uploads.set(prior.file, prior.sha256);
    }
  }
  let prelude = 0;
  let scored = 0;
  globalThis.fetch = async function(input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.pathname.endsWith('/models')) {
      const response = await original.call(this, input, init);
      const payload = await response.clone().json();
      const matches = [];
      function visit(value) {
        if (value && typeof value === 'object') {
          if (Object.values(value).includes('deepseek-flash')) {
            matches.push(Object.fromEntries(Object.entries(value).filter(([k,v]) => typeof v==='string' && /^(id|name|display_name|displayName|model)$/.test(k))));
          }
          Object.values(value).forEach(visit);
        }
      }
      visit(payload); log({kind:'model-list', status:response.status, matches});
      return response;
    }
    if (init?.body instanceof FormData) {
      const blobs = [...init.body.values()].filter(v => v instanceof Blob && v.type.startsWith('image/'));
      if (blobs.length !== 1 || !url.pathname.endsWith('/files')) throw new Error('Unexpected upload');
      const bytes = Buffer.from(await blobs[0].arrayBuffer());
      const response = await original.call(this, input, init);
      const receipt = await response.clone().json();
      const id = receipt.id ?? receipt.file_id;
      if (!response.ok || !id) throw new Error('Upload has no success receipt');
      uploads.set(id, sha(bytes));
      log({kind:'upload', file:id, sha256:sha(bytes), bytes:bytes.length, mime:blobs[0].type, status:response.status});
      return response;
    }
    if (!url.pathname.endsWith('/messages')) return original.call(this, input, init);
    const body = JSON.parse(init.body);
    const test = existsSync(cfg.marker);
    const n = test ? ++scored : ++prelude;
    const tools = (body.tools || []).map(t => t.name).sort();
    if (body.model !== 'deepseek-flash' || tools.join(',') !== 'mcp__vista__list_records,mcp__vista__read_record'
        || Object.hasOwn(body,'dsh_session_log') || n > (test ? 8 : 3)) throw new Error('Unexpected route, tools, telemetry or budget');
    const images = [];
    function scan(value, path, call) {
      if (!value || typeof value !== 'object') return;
      if (value.type === 'tool_result') call = value.tool_use_id;
      if (value.type === 'image') {
        const src = value.source;
        const digest = src?.type === 'file' ? uploads.get(src.file_id) : src?.type === 'base64' ? sha(Buffer.from(src.data,'base64')) : undefined;
        if (!digest) throw new Error('Unresolved wire image');
        images.push({path, call, file:src.file_id, sha256:digest});
      }
      for (const [key,item] of Object.entries(value)) scan(item, path+'.'+key, call);
    }
    scan(body,'body');
    if (!test && n === 1 && (images.length!==1 || images[0].sha256!==cfg.digest)) throw new Error('Archive image absent');
    if (test && n === 1) {
      const wire = JSON.stringify(body);
      if (!replaced || images.length || [...uploads.keys()].some(id => wire.includes(id)) ||
          wire.includes(cfg.digest) || /record-[0-9a-f]{32}|朝下|向下|下方|底部|朝上|向上|上方|顶部|朝左|向左|左侧|朝右|向右|右侧/.test(wire) ||
          !wire.includes(cfg.question) || !wire.includes(cfg.summary)) throw new Error('Initial scored request failed fixture checks');
    }
    const sanitized = JSON.parse(JSON.stringify(body,(k,v) => k === 'data' && typeof v === 'string' && v.length > 256 ? '[binary omitted]' : v));
    log({kind:'request', phase:test?'question':'history', n, model:body.model, tools, images, body:sanitized});
    const response = await original.call(this,input,init);
    log({kind:'response', phase:test?'question':'history', n, status:response.status});
    return response;
  };
  ctx.effect(() => () => { globalThis.fetch = original; });
}
