import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { controlImagePayload, imageUnavailable, ImageProvenance } from '../scripts/shader_recall_image_control.mjs';

const bytes = Buffer.from('fixture-image-bytes');
const sha256 = createHash('sha256').update(bytes).digest('hex');
const nativeImage = () => ({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: bytes.toString('base64') } });
const binding = (origin, common) => ({ sha256, origin, common });

test('text condition controls prior tool images while retaining reference, code and metadata', async () => {
  const body = { model: 'deepseek-flash', messages: [
    { role: 'user', content: [nativeImage()] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'old', content: [
      { type: 'text', text: '{"record":"old.json"}\nvoid main(){}' }, nativeImage(),
    ] }] },
  ] };
  const frozen = JSON.stringify(body);
  const result = await controlImagePayload(body, {
    condition: 'text', bindings: [binding('reference', true), binding('old.json', false)],
  });
  assert.deepEqual(result.body.messages[0].content, [nativeImage()]);
  assert.deepEqual(result.body.messages[1].content[0].content[1], { type: 'text', text: imageUnavailable });
  assert.equal(result.body.messages[1].content[0].content[0].text, '{"record":"old.json"}\nvoid main(){}');
  assert.equal(JSON.stringify(body), frozen);
  assert.deepEqual(result.audit.map(row => row.disposition), ['visible', 'withheld']);
});

test('equal bytes keep distinct reference and historical provenance', async () => {
  const body = { messages: [{ content: [nativeImage(), nativeImage()] }] };
  const result = await controlImagePayload(body, {
    condition: 'text', bindings: [binding('reference', true), binding('history', false)],
  });
  assert.equal(result.body.messages[0].content[0].type, 'image');
  assert.equal(result.body.messages[0].content[1].type, 'text');
});

test('visual condition preserves native and OpenAI images with validated ordering', async () => {
  const openai = { type: 'input_image', image_url: `data:image/png;base64,${bytes.toString('base64')}` };
  const body = { input: [{ content: [openai, nativeImage()] }] };
  const result = await controlImagePayload(body, {
    condition: 'visual', bindings: [binding('first', false), binding('second', false)],
  });
  assert.deepEqual(result.body, body);
});

test('file-backed images are verified against the audited upload', async () => {
  const body = { messages: [{ content: [{ type: 'image', source: { type: 'file', file_id: 'file-1' } }] }] };
  const result = await controlImagePayload(body, {
    condition: 'text', bindings: [binding('history', false)],
    files: new Map([['file-1', { data: bytes, mediaType: 'image/png' }]]),
  });
  assert.equal(result.body.messages[0].content[0].text, imageUnavailable);
  await assert.rejects(controlImagePayload(body, { condition: 'text', bindings: [binding('history', false)] }), /audited upload/);
});

test('unknown images and changed variants cannot enter an eligible request', async () => {
  const body = { messages: [{ content: [nativeImage()] }] };
  await assert.rejects(controlImagePayload(body, { condition: 'text', bindings: [] }), /lacks provenance/);
  await assert.rejects(controlImagePayload(body, {
    condition: 'visual', bindings: [{ ...binding('history', false), sha256: 'wrong' }],
  }), /differs from admitted/);
  await assert.rejects(controlImagePayload({ messages: [] }, {
    condition: 'text', bindings: [binding('history', false)],
  }), /image count differs/);
});

test('composite control masks only the historical region and requires a masker', async () => {
  const body = { messages: [{ content: [nativeImage()] }] };
  const regions = [
    { x: 0, y: 0, width: 10, height: 10, common: true },
    { x: 10, y: 0, width: 10, height: 10, common: false },
  ];
  const bindings = [{ ...binding('comparison', false), regions }];
  await assert.rejects(controlImagePayload(body, { condition: 'text', bindings }), /verified masker/);
  const result = await controlImagePayload(body, { condition: 'text', bindings,
    maskImage: async (_image, hidden) => {
      assert.deepEqual(hidden, [regions[1]]);
      return { data: Buffer.from('masked'), mediaType: 'image/png' };
    },
  });
  assert.equal(result.audit[0].disposition, 'masked');
  assert.equal(result.body.messages[0].content[0].source.data, Buffer.from('masked').toString('base64'));
  assert.deepEqual(result.body.messages[0].content[1], { type: 'text', text: imageUnavailable });
});

test('source-bound normalized aliases survive repeated requests without cross-call leakage', () => {
  const sources = new ImageProvenance();
  sources.registerPath('/current.png', { kind: 'candidate', id: 'current', common: true });
  sources.registerPath('/old.png', { kind: 'candidate', id: 'old', common: false });
  const image = { type: 'image', attachment: { attachmentId: 'sha256:same-bytes' } };
  sources.recordImageResult('call-a', { content: [image] }, '/current.png');
  sources.recordImageResult('call-b', { content: [image] }, '/old.png');
  assert.equal(sources.forAttachment({ toolCallId: 'call-a' }, image.attachment).id, 'current');
  assert.equal(sources.forAttachment({ toolCallId: 'call-b' }, image.attachment).id, 'old');
  assert.equal(sources.forAttachment({ toolCallId: 'call-b' }, image.attachment).id, 'old');
  assert.throws(() => sources.forAttachment({ toolCallId: 'other' }, image.attachment), /source-bound/);
});
