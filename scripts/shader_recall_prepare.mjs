import assert from 'node:assert/strict';
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { cloneVista, createBusiness, fileManifest, fixtureSource, hash, VistaBridge, writeJson } from './shader_recall_runtime.mjs';

const { values } = parseArgs({ options: {
  output: { type: 'string' },
  'business-root': { type: 'string' },
  'health-only': { type: 'boolean', default: false },
  'recover-only': { type: 'boolean', default: false },
} });
if (!values.output) throw new Error('--output is required');
const output = resolve(values.output);
const recover = values['recover-only'];
if (recover) {
  if (values['health-only']) throw new Error('Choose either --health-only or --recover-only');
  for (const folder of ['business', 'vista']) {
    if (!(await stat(join(output, folder))).isDirectory()) throw new Error(`Expected existing ${folder} directory`);
  }
} else {
  await mkdir(output, { recursive: true });
}
const prompts = [];
const sessions = {
  async create(input) { return { sessionId: input.sessionId }; },
  async prompt(input) { prompts.push(input); return { messageId: `prepared-${prompts.length}` }; },
  async state() { return { active: true, running: false, queue: [] }; },
  async events() { return { records: [], hasMore: false }; },
  async cancel() {},
};
const runtime = await createBusiness({
  artifactRoot: join(output, 'business'), vistaRoot: join(output, 'vista'),
  businessRoot: values['business-root'],
}, sessions);
const report = {
  kind: recover ? 'registration-recovery' : 'backend-preflight', startedAt: new Date().toISOString(),
  sessionMode: 'preparation-controller-without-model', checks: [], status: 'running',
};
const check = (name, value) => { assert.ok(value, name); report.checks.push({ name, passed: true }); };
try {
  if (recover) {
    report.recovery = runtime.recovery;
    check('All published candidates and experiments have VISTA mappings', !report.recovery.pending.length);
  } else {
    report.renderer = await runtime.service.worker({ action: 'health' });
    check('Real business worker exposes WebGL2', report.renderer.webgl2);
  }
  if (!recover && !values['health-only']) {
    const target = await runtime.service.worker({ action: 'render', source: fixtureSource(), width: 192, height: 160 });
    const task = await runtime.service.create({
      requestId: 'backend-check', reference: target.png, requirements: '复现参考图的发光圆形。',
      seedSource: fixtureSource({ glow: 0.025 }),
    });
    check('Actual business task created and author binding persisted', (await runtime.service.forSession(task.sessionId)) === task.id);
    check('Initial business prompt captured for later live Session verification', prompts.length === 1);
    const referencePath = join(runtime.service.directory(task.id), 'reference.png');
    const reference = await runtime.bridge.request({ action: 'save', root: runtime.vistaRoot,
      image_path: referencePath, label: 'reference', source_agent: 'intake' });
    const first = await runtime.service.mutate(task.id, 'render');
    await writeFile(join(task.workspace, 'shader.frag'), fixtureSource({ glow: 0.09 }));
    const second = await runtime.service.mutate(task.id, 'render');
    check('Two real candidates automatically registered', first.vista.record !== second.vista.record);
    check('Changed GLSL has changed image and source hashes', first.imageHash !== second.imageHash && first.sourceHash !== second.sourceHash);
    const records = await runtime.bridge.request({ action: 'list', root: runtime.vistaRoot });
    check('Reference and two candidates discoverable', records.records.length === 3 && records.records.some(r => r.record === reference.record));
    for (const candidate of [first, second]) {
      const data = JSON.parse(await readFile(join(runtime.vistaRoot, 'json', candidate.vista.record), 'utf8'));
      check(`PNG matches candidate ${candidate.id}`, hash(await readFile(join(runtime.vistaRoot, data.png))) === candidate.imageHash);
      check(`GLSL matches candidate ${candidate.id}`, hash(await readFile(join(runtime.vistaRoot, data.glsl))) === candidate.sourceHash);
      check(`Render inputs saved for ${candidate.id}`, data.render.time === 0 && data.render.width === 192 && data.render.height === 160);
    }
    await writeFile(join(task.workspace, 'shader.frag'), '#version 300 es\nnot valid GLSL');
    await assert.rejects(runtime.service.mutate(task.id, 'render'));
    check('Failed render does not create an image record', (await runtime.bridge.request({ action: 'list', root: runtime.vistaRoot })).records.length === 3);
    await writeFile(join(task.workspace, 'shader.frag'), fixtureSource({ glow: 0.07 }));
    const recovered = await runtime.service.mutate(task.id, 'render');
    check('Rendering and registration recover after compilation failure', Boolean(recovered.vista.record));
    const before = await fileManifest(runtime.vistaRoot);
    const clonedRoot = join(output, 'isolated-copy', 'vista');
    await cloneVista(runtime.vistaRoot, clonedRoot);
    const cloneBridge = new VistaBridge();
    try {
      await cloneBridge.request({ action: 'save', root: clonedRoot, image_path: referencePath, label: 'copy-only' });
      check('Append in clone preserves frozen template bytes', JSON.stringify(await fileManifest(runtime.vistaRoot)) === JSON.stringify(before));
      check('Clone append is isolated', (await cloneBridge.request({ action: 'list', root: clonedRoot })).records.length === 5);
    } finally { await cloneBridge.close(); }
    await assert.rejects(cloneVista(runtime.vistaRoot, clonedRoot), { code: 'EEXIST' });
    check('Existing run destination cannot be reused', true);
    report.taskId = task.id;
    report.recordCount = 4;
    report.initialPromptPath = join(output, 'prepared-prompt.json');
    await writeJson(report.initialPromptPath, prompts[0]);
  }
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = { type: error.name, message: error.message };
  process.exitCode = 1;
} finally {
  await runtime.service.dispose();
  report.finishedAt = new Date().toISOString();
  await writeJson(join(output, `${report.kind}.json`), report);
}
console.log(JSON.stringify({ status: report.status, checks: report.checks.length, output, error: report.error }));
