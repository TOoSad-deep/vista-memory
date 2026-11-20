import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { createBusiness, defaultBusinessRoot } from '../scripts/shader_recall_runtime.mjs';

const run = promisify(execFile);
const python = fileURLToPath(new URL('../.venv/bin/python', import.meta.url));
const helper = fileURLToPath(new URL('./helpers/shader_recall_registration.py', import.meta.url));
const businessRoot = process.env.VISTA_BUSINESS_ROOT || defaultBusinessRoot;
const { ShaderService } = await import(pathToFileURL(join(businessRoot, 'src/service.js')).href);
const taskId = 'registration-test';
const reference = 'iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAFElEQVR4nGMUUTZiAAMmCMXAwAAAB4AAbRUbCJwAAAAASUVORK5CYII=';
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAFElEQVR4nGPcEiXHAAZMEIqBgQEAFeoBMOzQseAAAAAASUVORK5CYII=';
const source = [
  '#version 300 es',
  '// == ShaderGen params ==',
  '// param: u_shine | type: float | min: 0 | max: 1 | default: 0.625 | label: 高光',
  '// param: u_offset | type: float | min: -1 | max: 1 | default: 0 | label: Offset',
  '// == end params ==',
  'precision highp float;',
  'uniform float u_shine, u_offset;',
  'out vec4 color;',
  'void main() { color = vec4(u_shine, u_offset, 0.0, 1.0); }',
  '',
].join('\r\n');
const render = { width: 3, height: 2, time: 0, uniforms: { u_shine: 0.625, u_offset: 0 } };
const params = Object.entries(render.uniforms).map(([name, value]) => ({ name, type: 'float', default: value }));
const sessions = {
  async create({ sessionId }) { return { sessionId }; },
  async prompt({ requestId }) { return { messageId: requestId }; },
  async events() { return { records: [], hasMore: false }; },
};

async function readRecords(vistaRoot) {
  const { stdout } = await run(python, [helper, 'read', vistaRoot]);
  return JSON.parse(stdout);
}

async function persistedMapping(path) {
  try { return JSON.parse(await readFile(path, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; return {}; }
}

async function assertCandidate(service, candidateId) {
  const task = await service.read(taskId);
  assert.equal(task.failedRenderCount, 0);
  assert.deepEqual(task.renderFailures, []);
  assert.deepEqual(task.candidates.map(row => row.id), [candidateId]);
  assert.deepEqual(await readdir(join(service.directory(taskId), 'candidates')), [candidateId]);
  const candidate = await service.candidate(task, candidateId);
  assert.equal(candidate.image, 'data:image/png;base64,' + png);
  assert.equal(candidate.source, source);
  assert.deepEqual(candidate.params, params);
  return candidate;
}

async function assertRegistered(runtime, candidateId, originalRecord) {
  const candidate = await assertCandidate(runtime.service, candidateId);
  const mapping = await persistedMapping(runtime.mappingPath);
  assert.deepEqual(Object.keys(mapping), [candidateId]);
  assert.deepEqual(runtime.mapping, mapping);
  const entry = mapping[candidateId];
  if (originalRecord !== undefined) assert.equal(entry.record, originalRecord);
  assert.equal(await readFile(entry.sourcePath, 'utf8'), candidate.source);
  assert.equal((await readFile(entry.imagePath)).toString('base64'), png);
  assert.deepEqual(entry.render, render);
  assert.equal(entry.imageHash, candidate.imageHash);
  assert.deepEqual(await readRecords(runtime.vistaRoot), [{
    record: entry.record, png: candidate.image.split(',')[1], source: candidate.source, render,
  }]);
  return entry.record;
}

async function faultyPython(root, stage, log) {
  const wrapper = join(root, 'python-with-storage-fault');
  const args = [python, helper, 'inject', stage, log].map(value => "'" + value.replaceAll("'", "'\\''") + "'");
  await writeFile(wrapper, '#!/bin/sh\nexec ' + args.join(' ') + ' "$@"\n', { mode: 0o700 });
  return wrapper;
}

for (const stage of ['save', 'map']) {
  test('a failed ' + stage + ' commit recovers the published candidate across two cold starts', async t => {
    const root = await mkdtemp(join(tmpdir(), 'vista-registration-'));
    const log = join(root, 'fault.log');
    const config = { businessRoot, artifactRoot: join(root, 'business'), vistaRoot: join(root, 'vista') };
    let renders = 0;
    t.mock.method(ShaderService.prototype, 'worker', async function (input) {
      if (input.action === 'normalize') return { png: input.data, width: 3, height: 2, originalSize: [3, 2] };
      assert.equal(input.action, 'render');
      assert.equal(++renders, 1, 'recovery must reuse the published artifacts');
      return { png, source: input.source, params };
    });
    let runtime;
    try {
      runtime = await createBusiness({ ...config, python: await faultyPython(root, stage, log) }, sessions);
      const task = await runtime.service.create({ requestId: taskId, reference, requirements: '铜色高光', seedSource: source });
      const published = await runtime.service.mutate(task.id, 'render');
      const candidateId = published.id;
      assert.ok(candidateId);
      assert.equal(published.vistaRegistration.status, 'pending');
      assert.match(await readFile(log, 'utf8'), stage === 'save' ? /os\.link record-.*\.json/ : /os\.rename vista-records\.json/);
      await assertCandidate(runtime.service, candidateId);
      assert.deepEqual(runtime.mapping, {});
      assert.deepEqual(await persistedMapping(runtime.mappingPath), {});
      const before = await readRecords(config.vistaRoot);
      assert.equal(before.length, stage === 'save' ? 0 : 1);
      let record = before[0]?.record;
      if (stage === 'map') assert.deepEqual(before[0], { record, png, source, render });
      await writeFile(join(task.workspace, 'shader.frag'), '// later, unpublished edit\n');

      for (let restart = 0; restart < 2; restart++) {
        await runtime.service.dispose();
        runtime = undefined;
        runtime = await createBusiness(config, sessions);
        record = await assertRegistered(runtime, candidateId, record);
        await runtime.reconcile();
        await assertRegistered(runtime, candidateId, record);
      }
      assert.equal(renders, 1);
    } finally {
      await runtime?.service.dispose();
      await rm(root, { recursive: true, force: true });
    }
  });
}
