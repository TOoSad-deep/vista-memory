import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { createBusiness, fileManifest, hash, writeJson } from './shader_recall_runtime.mjs';

const { values } = parseArgs({ options: { output: { type: 'string' }, inputs: { type: 'string' }, phase: { type: 'string', default: 'calibration' }, 'case-id': { type: 'string' } } });
if (!values.output || !values.inputs) throw new Error('--output and --inputs are required');
const output = resolve(values.output);
await mkdir(output, { recursive: false });
const inputs = JSON.parse(await readFile(resolve(values.inputs), 'utf8'));
const cases = inputs.cases.filter(item => item.split === values.phase && (!values['case-id'] || item.id === values['case-id']));
if (!cases.length) throw new Error('No reference images selected for fixture phase');
const variants = {
  'CAL-01': [
    { u_radius: 0.395, u_gloss: 0.25, u_shadow: 0.10 },
    { u_radius: 0.405, u_gloss: 0.72, u_shadow: 0.10 },
    { u_radius: 0.420, u_gloss: 0.84, u_shadow: 0.12 },
    { u_radius: 0.405, u_gloss: 0.12, u_shadow: 0.10 },
    { u_radius: 0.405, u_gloss: 0.92, u_shadow: 0.08 },
    { u_radius: 0.405, u_gloss: 0.20, u_shadow: 0.10 },
  ],
  'CAL-02': [0.02, 0.04, 0.07, 0.035, 0.06, 0.045].map(u_glow => ({ u_glow })),
};
function setDefaults(source, defaults) {
  return source.split('\n').map(line => {
    const name = /^\/\/ param:\s*(\w+)/.exec(line)?.[1];
    if (!name || !(name in defaults)) return line;
    return line.replace(/default:\s*[^|]+/, `default: ${defaults[name]} `);
  }).join('\n');
}

const goals = {
  T01: { component: '继续完善三个形状周围的发光层次与衰减，保持形状和位置。', parameter: 'u_gain', value: 0.95 },
  T02: { component: '继续完善球形能量团的细丝层次，保持整体范围与蓝青色调。', parameter: 'u_energy', value: 0.8 },
  T03: { component: '继续完善彩色波带的细层次与柔和过渡，保持整体走势。', parameter: 'u_bend', value: 0.09 },
  T04: { component: '继续完善红色心形的材质明暗，保持已有轮廓与尺寸。', parameter: 'u_shade', value: 0.65 },
  T05: { component: '继续完善铜色环上的窄高光层次，保持环的布局和宽度。', parameter: 'u_shine', value: 0.9 },
  T06: { component: '继续完善水面反射的细波纹，保持太阳大小和地平线位置。', parameter: 'u_sun', value: 0.13 },
};
const manifest = { kind: `real-glsl-constructed-${values.phase}`, status: 'preparing', cases: [] };
for (const item of cases) {
  const directory = join(output, item.id);
  const originalPrompts = [];
  const sessions = {
    async create(input) { return { sessionId: input.sessionId }; },
    async prompt(input) { originalPrompts.push(input); return { messageId: input.requestId }; },
    async state() { return { active: true, running: false, queue: [] }; },
    async events() { return { records: [], hasMore: false }; },
    async cancel() {},
  };
  const runtime = await createBusiness({
    artifactRoot: join(directory, 'business'), vistaRoot: join(directory, 'vista'), sourceAgent: null,
  }, sessions);
  try {
    const seedPath = join(dirname(resolve(values.inputs)), 'fixtures', item.id, 'seed.frag');
    const seed = await readFile(seedPath, 'utf8');
    const recipe = variants[item.id] ? variants[item.id].map(uniforms => ({ uniforms, changes: [] })) :
      JSON.parse(await readFile(join(dirname(seedPath), 'variants.json'), 'utf8'));
    const task = await runtime.service.create({
      requestId: 'fixture-task', reference: (await readFile(item.target_original)).toString('base64'),
      requirements: '根据参考图继续完善当前 Shader，保留已接近目标的局部效果。', seedSource: seed,
    });
    const reference = await runtime.bridge.request({
      action: 'save', root: runtime.vistaRoot,
      image_path: join(task.directory, 'reference.png'), label: 'reference', source_agent: null,
    });
    const steps = [];
    for (const [index, variant] of recipe.entries()) {
      let source = variant.source_path ? await readFile(join(dirname(seedPath), variant.source_path), 'utf8') : setDefaults(seed, variant.uniforms);
      for (const change of variant.changes) {
        if (source.split(change.from).length !== 2) throw new Error('Shader variant replacement is ambiguous');
        source = source.replace(change.from, change.to);
      }
      await writeFile(join(task.workspace, 'shader.frag'), source);
      const candidate = await runtime.service.mutate(task.id, 'render');
      const match = await runtime.service.worker({
        action: 'compare', reference: join(task.directory, 'reference.png'), candidate: candidate.imagePath,
      });
      steps.push({ step: index + 1, candidateId: candidate.id, record: candidate.vista.record,
        sourceHash: candidate.sourceHash, imageHash: candidate.imageHash,
        uniforms: runtime.mapping[candidate.id].render.uniforms,
        meanAbsoluteError: match.meanAbsoluteError });
    }
    let provenance;
    try { provenance = JSON.parse(await readFile(join(dirname(seedPath), 'source-provenance.json'), 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const metadata = {
      id: item.id, split: values.phase,
      source: provenance ? 'existing-trajectory-replay-with-constructed-regression' : 'constructed-procedural-glsl-history',
      provenance,
      originalReferenceSha256: item.sha256, normalizedReferenceSha256: hash(await readFile(join(task.directory, 'reference.png'))),
      businessDirectory: task.directory, taskId: task.id, vistaRoot: runtime.vistaRoot,
      currentCandidateId: steps.at(-1).candidateId, referenceRecord: reference.record,
      mapping: runtime.mapping, steps,
      baseTask: await runtime.service.read(task.id),
      vistaManifest: await fileManifest(runtime.vistaRoot),
    };
    await writeJson(join(directory, 'checkpoint.json'), metadata);
    const point = { id: item.id, checkpoint: join(directory, 'checkpoint.json'), source: metadata.source,
      split: values.phase, task_id: item.id, purpose: item.id === 'CAL-02' ? 'information-sufficient' : 'opportunity',
      provided_records: [steps[item.id === 'CAL-02' ? 0 : 4].record] };
    if (values.phase === 'diagnostic') {
      const goal = goals[item.id];
      if (!goal) throw new Error('Diagnostic task has no frozen goal');
      manifest.cases.push({ ...point, id: `${item.id}-history`, requirements: goal.component });
      manifest.cases.push({ ...point, id: `${item.id}-current`, purpose: 'information-sufficient',
        provided_records: [steps[0].record],
        requirements: `将当前 Shader 中 ${goal.parameter} 的默认值设为 ${goal.value}，保持其他参数和程序逻辑，渲染确认后提交。`,
        objective: { type: 'single-default-change', parameter: goal.parameter, value: goal.value } });
    } else manifest.cases.push(point);
    await writeJson(join(output, 'manifest.json'), manifest);
    console.log(JSON.stringify({ case: item.id, candidates: steps.length, current: metadata.currentCandidateId }));
  } finally { await runtime.service.dispose(); }
}
manifest.status = 'prepared';
await writeJson(join(output, 'manifest.json'), manifest);
