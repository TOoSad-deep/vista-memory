// Wire checks for the two continuations from the archived T05 formal render.
import { appendFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const sha = v => createHash('sha256').update(v).digest('hex');
const stable = v => Array.isArray(v) ? v.map(stable) : v && typeof v === 'object'
  ? Object.fromEntries(Object.keys(v).sort().map(k => [k, stable(v[k])])) : v;

export function apply(ctx, cfg) {
  const prior = globalThis.fetch;
  const uploads = new Map();
  let calls = 0;
  const log = row => appendFileSync(cfg.log, JSON.stringify({time:new Date().toISOString(), ...row})+'\n');
  globalThis.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (init?.body instanceof FormData) {
      const blob = [...init.body.values()].find(v => v instanceof Blob && v.type.startsWith('image/'));
      if (blob) {
        const digest = sha(Buffer.from(await blob.arrayBuffer()));
        const response = cfg.preview ? Response.json({id:'file-'+digest,type:'file',filename:'image.png',mime_type:blob.type,size_bytes:blob.size,
          created_at:new Date().toISOString()}) : await prior(input,init);
        const reply = await response.clone().json();
        if (!response.ok || !(reply.id || reply.file_id)) throw new Error('Image upload failed');
        uploads.set(reply.id || reply.file_id,digest);
        return response;
      }
    }
    if (!url.pathname.endsWith('/messages') || typeof init?.body !== 'string') return prior(input,init);
    if (calls >= 2) { log({type:'budget_stop'}); throw new Error('Post-render model request limit reached'); }
    try {
      const body = JSON.parse(init.body);
      if (body.model !== 'deepseek-flash' || !Array.isArray(body.messages)) throw new Error('Unexpected model transport');
      const messages = body.messages;
      const idx = messages.findIndex(m => Array.isArray(m.content) && m.content.some(b => b.type==='tool_result' && b.tool_use_id===cfg.renderCall));
      if (idx < 0) throw new Error('Missing archived render result');
      const pos = messages[idx].content.findIndex(b=>b.type==='tool_result' && b.tool_use_id===cfg.renderCall);
      const block = messages[idx].content[pos];
      const text = block.content.find(b=>b.type==='text');
      const rendered = JSON.parse(text.text);
      if (rendered.id!==cfg.candidate || sha(text.text)!==cfg.renderDigest) throw new Error('Archived render payload mismatch');
      // Only the first model decision after the result is replayed. Native
      // resume opens a turn; discard its wake-up and context updates on wire.
      const nextAssistant = messages.findIndex((m,i)=>i>idx && m.role==='assistant');
      body.messages=[...messages.slice(0,idx),{...messages[idx],content:messages[idx].content.slice(0,pos+1)},
        ...(nextAssistant<0 ? [] : messages.slice(nextAssistant))];
      // This optional telemetry suffix includes SDK lifecycle events, including
      // the artificial wake marker. Neither arm sends those events to DS.
      delete body.dsh_session_log;
      if (JSON.stringify(body).includes(cfg.wake)) throw new Error('Replay wake marker would reach model');
      const canonical = JSON.parse(JSON.stringify(body));
      const canonicalResult = canonical.messages[idx].content[pos].content.find(b=>b.type==='text');
      const facts=JSON.parse(canonicalResult.text); const receipt=facts.recallComparison;
      if (JSON.stringify(receipt??null)!==JSON.stringify(cfg.expectedReceipt)) throw new Error('Unexpected receipt');
      delete facts.recallComparison; canonicalResult.text=JSON.stringify(facts);
      const images=[];
      function visit(obj) {
        if (!obj || typeof obj!=='object') return;
        if (obj.type==='file' && obj.file_id) {
          const hash=uploads.get(obj.file_id); if(!hash)throw new Error('Unresolved file image');
          images.push(hash);obj.file_id='sha256:'+hash;
        }
        for (const v of Object.values(obj)) visit(v);
      }
      visit(canonical);
      const normalized=JSON.stringify(stable(canonical)).replaceAll(cfg.sessionId,'{SESSION}').replaceAll(cfg.manifestHash,'{MANIFEST}');
      const signature=sha(normalized);
      if (calls===0) {
        if (nextAssistant>=0 || !cfg.images.every(im=>images.includes(im))) throw new Error('First request state differs from cutoff');
        if (cfg.preview) writeFileSync(cfg.previewBody,normalized);
        if (cfg.expectedSignature && cfg.expectedSignature!==signature) throw new Error('First payload differs beyond receipt or transport identity');
      }
      const index=++calls;
      log({type:cfg.preview?'preview':'request', index, signature, renderedCandidate:rendered.id,
        renderToolCallId:cfg.renderCall,renderTextSha256:sha(text.text),receipt:receipt??null, images,
        messages:body.messages.length,tools:body.tools.map(t=>t.name), model:body.model, maxTokens:body.max_tokens});
      if (cfg.preview) return Response.json({type:'error',error:{type:'invalid_request_error',message:'Offline preview complete'}},{status:400});
      const resp=await prior(input,{...init,body:JSON.stringify(body)});
      log({type:'response',index,status:resp.status});
      return resp;
    } catch(error) { log({type:'error',message:error.message}); throw error; }
  };
  ctx.effect(()=>()=>{globalThis.fetch=prior;});
}
