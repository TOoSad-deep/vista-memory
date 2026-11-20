import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectBody, inspectTrigger } from '../scripts/dsh_request_audit.mjs';

test('DS account file references retain uploaded PNG identity and source role', () => {
  const uploaded = new Map([['file-1', { sha256: 'source-image', mimeType: 'image/png', bytes: 100, width: 96, height: 96 }]]);
  const body = { model: 'deepseek-flash', messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'call', content: [
    { type: 'image', source: { type: 'file', file_id: 'file-1' } },
  ] }] }] };
  const result = inspectBody(body, [], uploaded);
  assert.equal(result.images.length, 1);
  assert.equal(result.images[0].sha256, 'source-image');
  assert.equal(result.images[0].fileId, 'file-1');
  assert.equal(result.images[0].originRole, 'user');
  assert.equal(result.images[0].width, 96);
});

test('missing upload correspondence remains an explicit evidence gap', () => {
  const result = inspectBody({ messages: [{ content: [
    { type: 'image', source: { type: 'file', file_id: 'unknown' } },
  ] }] }, []);
  assert.equal(result.images[0].unresolvedFile, true);
  assert.equal(result.images[0].sha256, undefined);
});

test('OpenAI data URI and code checks preserve their existing semantics', () => {
  const result = inspectBody({ model: 'test', input: [{ type: 'function_call_output', output: [
    { type: 'input_text', text: 'void main(){}' },
    { type: 'input_image', image_url: 'data:image/png;base64,YWJj' },
  ] }] }, ['void main(){}']);
  assert.equal(result.images.length, 1);
  assert.equal(result.images[0].bytes, 3);
  assert.equal(result.images[0].originType, 'function_call_output');
  assert.equal(result.includedCodeSha256.length, 1);
});

test('focused trace binds exact full source and PNG to their individual wire tool result', () => {
  const body = { messages: [{ role: 'user', content: [
    { type: 'text', text: 'complete shader' },
    { type: 'tool_result', tool_use_id: 'record-read', content: [
      { type: 'text', text: 'metadata\ncomplete shader' },
      { type: 'image', source: { type: 'file', file_id: 'upload' } },
    ] },
  ] }] };
  const result = inspectBody(body, ['complete shader'], new Map([['upload', { sha256: 'png' }]]), [], true);
  assert.deepEqual(result.codeSources.map(row => row.toolCallId), [undefined, 'record-read']);
  assert.equal(result.images[0].toolCallId, 'record-read');
});

test('business feedback stays separate from host cues and local file evidence', () => {
  const expected = {feedback:'正常业务反馈', policy:'author rules', cue:'host instruction',
    candidate:'old', record:'old.json', png:'png', glsl:'glsl', code:'first\nlast\n',
    render:{width:32,height:20,time:0,uniforms:{shine:1}}};
  const metadata = {id:'old', imageHash:'png', sourceHash:'glsl', width:32, height:20, time:0,
    params:[{name:'shine', default:1}]};
  const body = {system:'author rules', messages:[{role:'user',content:[
    {type:'text',text:'host instruction'}, {type:'text',text:'正常业务反馈'},
    {type:'tool_result',tool_use_id:'code',content:[{type:'text',text:'<content>\n1: first\n2: last\n</content>'}]},
    {type:'tool_result',tool_use_id:'params',content:[{type:'text',text:JSON.stringify(metadata)}]},
    {type:'tool_result',tool_use_id:'wrong',content:[{type:'text',text:JSON.stringify({...metadata,id:'other'})}]},
  ]}]};
  const rows=inspectTrigger(body, expected);
  for (const kind of ['host-cue','literal-feedback','author-policy']) assert.ok(rows.some(r=>r.kinds.includes(kind)));
  assert.deepEqual(rows.filter(r=>r.callId).map(r=>[r.callId,r.kinds]),
    [['code',['full-target-glsl']],['params',['target-render-parameters']]]);
  const receipts = inspectTrigger(body, {...expected, fingerprintToolText:true})
    .filter(r=>r.kinds.includes('tool-result-text'));
  assert.deepEqual(receipts.map(r=>r.callId), ['code','params','wrong']);
  assert.deepEqual(receipts.at(-1).kinds, ['tool-result-text']);
  assert.match(receipts.at(-1).textSha256, /^[a-f0-9]{64}$/);
});

test('candidate tool parameter evidence requires the correct version and effective uniforms', () => {
  const expected = { feedback:'feedback', policy:'policy', code:'full source', candidate:'v5',
    record:'r5', png:'png5', glsl:'glsl5', render:{width:32,height:16,time:0,uniforms:{shine:1}} };
  const value = { candidateId:'v5', via:'vista', record:'r5', imageHash:'png5',
    sourceHash:'glsl5', render:expected.render };
  function detected(metadata) {
    const body = {messages:[{role:'tool',tool_call_id:'selected',content:JSON.stringify(metadata)}]};
    return inspectTrigger(body, expected).some(row => row.callId === 'selected' && row.kinds.includes('target-render-parameters'));
  }
  assert.ok(detected(value));
  assert.ok(detected({...value, via:'candidate-files', record:undefined}));
  assert.ok(!detected({...value, candidateId:'v4'}));
  assert.ok(!detected({...value, record:'r4'}));
  assert.ok(!detected({...value, render:{...expected.render, uniforms:{shine:.2}}}));
});
