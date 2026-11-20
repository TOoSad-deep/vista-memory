import { createHash, randomUUID } from 'node:crypto';
import { cp, mkdir, readFile, readdir, rename, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const defaultBusinessRoot = resolve(projectRoot, '..', 'dsh-shader-11-13');
export const hash = value => createHash('sha256').update(value).digest('hex');

export async function writeJson(path, value) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${randomUUID()}.tmp`;
  await writeFile(temporary, JSON.stringify(value, null, 2) + '\n', { flag: 'wx' });
  await rename(temporary, path);
}

export class VistaBridge {
  constructor(python = join(projectRoot, '.venv', 'bin', 'python'), businessRoot = defaultBusinessRoot) {
    this.python = python;
    this.businessRoot = businessRoot;
  }
  async request(input) {
    const masking = input.action === 'mask';
    const channel = masking ? 'masker' : 'storage';
    this[channel] ??= import(pathToFileURL(join(this.businessRoot, 'src', 'vista-bridge.js')).href)
      .then(({ VistaBridge: Bridge }) => new Bridge(this.python, masking
        ? join(projectRoot, 'scripts', 'shader_recall_bridge.py') : join(this.businessRoot, 'runtime', 'vista_bridge.py')));
    return (await this[channel]).request(input);
  }
  async close() {
    for (const channel of [this.masker, this.storage]) if (channel) await (await channel).close();
  }
}

export async function fileManifest(root) {
  const files = {};
  async function visit(directory, prefix) {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await visit(path, relative);
      else if (entry.isFile()) files[relative] = hash(await readFile(path));
      else throw new Error(`Fixture contains a non-regular entry: ${relative}`);
    }
  }
  await visit(root, '');
  return files;
}

export async function cloneVista(template, destination) {
  const before = await fileManifest(template);
  await mkdir(dirname(destination), { recursive: true });
  await mkdir(destination);
  for (const entry of await readdir(template)) {
    await cp(join(template, entry), join(destination, entry), { recursive: true, force: false, errorOnExist: true });
  }
  const copied = await fileManifest(destination);
  if (JSON.stringify(before) !== JSON.stringify(copied)) throw new Error('VISTA fixture clone differs');
  return before;
}

export async function createBusiness(config, sessions) {
  const businessRoot = resolve(config.businessRoot || defaultBusinessRoot);
  const { ShaderService } = await import(pathToFileURL(join(businessRoot, 'src', 'service.js')).href);
  const vistaRoot = resolve(config.vistaRoot);
  const bridge = new VistaBridge(config.python, businessRoot);
  const service = new ShaderService(sessions, {
    artifactRoot: resolve(config.artifactRoot), vistaRoot, vistaBridge: bridge, vistaSourceAgent: config.sourceAgent,
    python: config.renderPython || join(businessRoot, '.venv', 'bin', 'python'),
  });
  await mkdir(vistaRoot, { recursive: true });
  await mkdir(service.root, { recursive: true });
  const registration = service.vistaRegistration;
  try {
    if (config.mapping) await registration.commitMappings(config.mapping);
    const recovery = await registration.reconcile();
    return { service, bridge, vistaRoot, businessRoot, ...registration, recovery };
  } catch (error) {
    await service.dispose();
    throw error;
  }
}

export async function restoreCheckpoint(runtime, task, checkpoint) {
  const { service, businessRoot } = runtime;
  const restoredMapping = {};
  if (task.referenceHash !== checkpoint.normalizedReferenceSha256) throw new Error('Checkpoint reference differs');
  const destination = service.directory(task.id);
  await cp(join(checkpoint.businessDirectory, 'candidates'), join(destination, 'candidates'), {
    recursive: true, force: false, errorOnExist: true,
  });
  task.candidates = structuredClone(checkpoint.baseTask.candidates);
  task.candidates.forEach((candidate, index) => { candidate.roundIndex = 0; candidate.attempt = index + 1; });
  const current = task.candidates.find(candidate => candidate.id === checkpoint.currentCandidateId);
  if (!current) throw new Error('Checkpoint current candidate is absent');
  task.selectedCandidateId = current.id;
  task.seedCandidateId = current.id;
  const authorRound = { ...task.workflow.rounds[0], index: 1, baselineId: current.id, experimentBestId: current.id };
  task.workflow.rounds = [{
    index: 0, authorSessionId: checkpoint.baseTask.sessionId,
    baselineId: null, candidateIds: task.candidates.map(candidate => candidate.id),
    submittedId: current.id, status: 'submitted',
  }, authorRound];
  for (const candidate of task.candidates) {
    const folder = join(destination, 'candidates', candidate.id);
    const bytes = await readFile(join(folder, 'render.png'));
    const source = await readFile(join(folder, 'shader.frag'), 'utf8');
    if (hash(bytes) !== candidate.imageHash || hash(source) !== candidate.sourceHash) throw new Error('Restored candidate differs from fixture');
    await writeJson(join(folder, 'candidate.json'), candidate);
    const entry = checkpoint.mapping[candidate.id];
    if (!entry) throw new Error('Candidate has no VISTA fixture mapping');
    restoredMapping[candidate.id] = { ...entry, imagePath: join(folder, 'render.png'), sourcePath: join(folder, 'shader.frag') };
  }
  await service.candidateStore.restore(task, await service.candidate(task, current.id));
  await runtime.commitMappings(restoredMapping);
  await service.save(task);
  const { writeHistory } = await import(pathToFileURL(join(businessRoot, 'src', 'history.js')).href);
  task.history = await writeHistory(service, task);
  await service.save(task);
  const { freshAuthorPrompt } = await import(pathToFileURL(join(businessRoot, 'src', 'prompts', 'author.js')).href);
  return {
    task,
    prompt: freshAuthorPrompt(task, task, service.workspace(task.id), destination, ''),
    source: await readFile(join(service.workspace(task.id), 'shader.frag'), 'utf8'),
    currentImage: join(destination, 'best.png'),
  };
}

// The renderer owns the pixel output. Fixture sources are procedural GLSL.
export function fixtureSource({ radius = 0.24, glow = 0.08, hue = 0.58, offset = 0, bars = 0, gap = 0.12 } = {}) {
  return `#version 300 es
// == ShaderGen params ==
// == end params ==
precision highp float;
uniform vec2 u_resolution;
out vec4 color;
const float radius = ${Number(radius).toFixed(5)};
const float glow = ${Number(glow).toFixed(5)};
const float hue = ${Number(hue).toFixed(5)};
const float offsetX = ${Number(offset).toFixed(5)};
const float stripeMix = ${Number(bars).toFixed(5)};
const float gap = ${Number(gap).toFixed(5)};
void main() {
  vec2 p = (gl_FragCoord.xy - 0.5 * u_resolution) / u_resolution.y;
  p.x -= offsetX;
  float d = length(p) - radius;
  float body = 1.0 - smoothstep(-0.008, 0.008, d);
  float halo = exp(-max(d, 0.0) / max(glow, 0.0001));
  vec3 tint = 0.5 + 0.5 * cos(6.2831853 * (hue + vec3(0.0, 0.33, 0.67)));
  float stripe = 1.0 - smoothstep(0.014, 0.022, abs(mod(p.x + 0.5 * gap, gap) - 0.5 * gap));
  float shape = mix(body, stripe * body, stripeMix);
  vec3 rgb = vec3(0.025, 0.035, 0.065) + tint * (0.72 * shape + 0.22 * halo);
  color = vec4(rgb, 1.0);
}
`;
}
