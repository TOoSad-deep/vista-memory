import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { LiveImageControl } from '../scripts/shader_recall_live_images.mjs';
import { hash } from '../scripts/shader_recall_runtime.mjs';

test('shell copies retain source identity before Python diagnostics', async () => {
  const root = await mkdtemp(join(tmpdir(), 'recall-lineage-'));
  try {
    const input = join(root, 'copied-reference.png');
    const output = join(root, 'diagnostic.png');
    const bytes = Buffer.from('the exact same encoded input');
    await writeFile(input, bytes);
    await mkdir(join(root, '.recall-audit'));
    await writeFile(join(root, '.recall-audit/python-images.jsonl'), [
      { process: 'p1', operation: 'read', path: input },
      { process: 'p1', operation: 'write', path: output },
    ].map(row => JSON.stringify(row)).join('\n') + '\n');
    const control = new LiveImageControl({}, {}, { output: root, workspace: root });
    control.rememberContent(hash(bytes), { kind: 'reference', common: true });
    await control.importPythonLineage();
    assert.equal(control.sources.paths.get(output).common, true);
    assert.equal(control.sources.paths.get(output).unverified, false);
    assert.equal(control.sources.paths.get(output).parents[0].kind, 'reference');
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('a derived old-current picture becomes historical after the candidate changes', () => {
  const control = new LiveImageControl({}, {}, {});
  const derivative = { kind: 'derived', common: true, parents: [{ kind: 'candidate', candidateId: 'a' }] };
  control.currentId = 'a';
  assert.equal(control.commonForRead(derivative), true);
  control.currentId = 'b';
  assert.equal(control.commonForRead(derivative), false);
  assert.equal(control.commonForRead({ kind: 'derived', parents: [{ kind: 'reference', common: true }] }), true);
});

test('unknown copied inputs remain unverified', async () => {
  const root = await mkdtemp(join(tmpdir(), 'recall-unknown-'));
  try {
    await mkdir(join(root, '.recall-audit'));
    const input = join(root, 'unknown.npy');
    const output = join(root, 'view.png');
    await writeFile(input, 'unregistered data');
    await writeFile(join(root, '.recall-audit/python-images.jsonl'), [
      { process: 'p1', operation: 'read', path: input },
      { process: 'p1', operation: 'write', path: output },
    ].map(row => JSON.stringify(row)).join('\n') + '\n');
    const control = new LiveImageControl({}, {}, { output: root, workspace: root });
    await control.importPythonLineage();
    assert.equal(control.sources.paths.get(output).unverified, true);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('image tool projection removes historical pixels before the session result', async () => {
  const root = await mkdtemp(join(tmpdir(), 'recall-projection-'));
  try {
    for (const condition of ['visual', 'text']) {
      const control = new LiveImageControl({}, {}, { output: root, workspace: root, imageCondition: condition });
      control.refreshCatalog = async () => {};
      control.currentId = 'current';
      const path = join(root, 'old.png');
      control.sources.registerPath(path, { kind: 'candidate', candidateId: 'old', record: 'old.json' });
      const content = [
        { type: 'text', text: 'code and effective parameters' },
        { type: 'image', attachment: { attachmentId: 'sha256:original' } },
      ];
      const tool = { name: 'read_image',
        execute: async () => ({ content }),
        output: { render: (_args, value) => value.content, presentationMeta: () => ({ path }) },
        projectContent: (_execution, result) => result.content,
      };
      control.instrumentImageTool(tool);
      const execution = { callId: condition };
      const value = await tool.execute({ file_path: path }, execution);
      const projected = tool.projectContent(execution, { content: value.content });
      assert.deepEqual(projected[0], content[0]);
      assert.equal(projected.filter(block => block.type === 'image').length, condition === 'visual' ? 1 : 0);
      if (condition === 'text') assert.match(projected[1].text, /unavailable/);
    }
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('image tool projection stores only the masked composite attachment in its result', async () => {
  const root = await mkdtemp(join(tmpdir(), 'recall-mask-'));
  try {
    let maskedRegions;
    const ctx = { attachments: {
      async readImage() { return { data: new Uint8Array(Buffer.from('original')), ref: { width: 30, height: 10 } }; },
      async saveImage(input) {
        assert.equal(input.data.toString(), 'masked');
        return { attachmentId: 'sha256:masked' };
      },
    } };
    const runtime = { bridge: { async request(input) {
      assert.equal(Buffer.from(input.data, 'base64').toString(), 'original');
      maskedRegions = input.regions;
      return { data: Buffer.from('masked').toString('base64'), media_type: 'image/png' };
    } } };
    const control = new LiveImageControl(ctx, runtime, { output: root, workspace: root, imageCondition: 'text' });
    control.refreshCatalog = async () => {};
    control.currentId = 'current';
    const path = join(root, 'composite.png');
    control.sources.registerPath(path, { kind: 'composite', candidateId: 'current', width: 30, height: 10, regions: [
      { x: 0, y: 0, width: 10, height: 10, reference: true },
      { x: 10, y: 0, width: 10, height: 10, candidateId: 'current' },
      { x: 20, y: 0, width: 10, height: 10, candidateId: 'old' },
    ] });
    const tool = { name: 'read_image', execute: async () => ({}), output: {
      render: () => [{ type: 'image', attachment: { attachmentId: 'sha256:original' } }],
      presentationMeta: () => ({ path }),
    } };
    control.instrumentImageTool(tool);
    const execution = { callId: 'mask' };
    await tool.execute({ file_path: path }, execution);
    const projected = tool.projectContent(execution, { content: tool.output.render() });
    assert.equal(projected[0].attachment.attachmentId, 'sha256:masked');
    assert.match(projected[1].text, /unavailable/);
    assert.deepEqual(maskedRegions, [{ x: 20, y: 0, width: 10, height: 10 }]);
    const origin = control.projectedOrigins.get('mask:sha256:masked');
    assert.equal(origin.regions[2].withheld, true);
    assert.equal(origin.regions[1].withheld, undefined);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('MCP deferred attachment projection is prepared once before image filtering', async () => {
  const root = await mkdtemp(join(tmpdir(), 'recall-mcp-project-'));
  try {
    const control = new LiveImageControl({}, {}, { output: root, workspace: root, imageCondition: 'text' });
    control.refreshCatalog = async () => {};
    control.imageOrigin = async () => ({ kind: 'candidate', candidateId: 'old', record: 'old.json', common: false });
    let projections = 0;
    const tool = { name: 'mcp__vista__read_record', execute: async () => ({ code: 'keep this code' }),
      output: { render: () => [{ type: 'text', text: 'fallback' }] },
      projectContent(_execution, result) {
        assert.equal(result.value.code, 'keep this code');
        assert.equal(++projections, 1);
        return [{ type: 'text', text: 'keep this code' },
          { type: 'image', attachment: { attachmentId: 'sha256:old' } }];
      },
    };
    control.instrumentImageTool(tool);
    const execution = { callId: 'mcp' };
    const value = await tool.execute({ record: 'old.json' }, execution);
    const projected = tool.projectContent(execution, { value, content: tool.output.render() });
    assert.equal(projections, 1);
    assert.deepEqual(projected.map(block => block.type), ['text', 'text']);
    assert.equal(projected[0].text, 'keep this code');
    assert.match(projected[1].text, /unavailable/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('request admission reclassifies cached current images after a new render', async () => {
  const root = await mkdtemp(join(tmpdir(), 'recall-admission-'));
  try {
    const attachment = { attachmentId: 'sha256:bytes', name: 'render.png' };
    const ctx = { attachments: {
      imageHostPath: () => join(root, 'cached.png'),
      readImageRequest: async () => ({ data: Buffer.from('bytes'), width: 10, height: 10 }),
    }, llm: { imageRequestTarget: () => ({}) } };
    const control = new LiveImageControl(ctx, {}, {
      output: root, policyPath: join(root, 'policy.json'), imageCondition: 'text',
    });
    const message = { role: 'tool', id: 'message', toolCallId: 'call',
      content: [{ type: 'image', attachment }] };
    control.sources.recordCall('call', 'read_image', {});
    control.sources.recordImageResult('call', message, '/original.png', {
      kind: 'candidate', candidateId: 'a', common: true,
    });
    control.currentId = 'a';
    await control.admit({ messages: [message] });
    assert.equal(JSON.parse(await readFile(join(root, 'policy.json'))).bindings[0].common, true);
    control.currentId = 'b';
    await control.admit({ messages: [message] });
    assert.equal(JSON.parse(await readFile(join(root, 'policy.json'))).bindings[0].common, false);
  } finally { await rm(root, { recursive: true, force: true }); }
});
