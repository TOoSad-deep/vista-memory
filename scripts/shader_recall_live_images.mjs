import { randomUUID } from 'node:crypto';
import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { ImageProvenance, imageUnavailable } from './shader_recall_image_control.mjs';
import { hash, writeJson } from './shader_recall_runtime.mjs';

export class LiveImageControl {
  constructor(ctx, runtime, config) {
    this.ctx = ctx;
    this.runtime = runtime;
    this.config = config;
    this.sources = new ImageProvenance();
    this.queue = Promise.resolve();
    this.failure = null;
    this.modelCalls = 0;
    this.lineageRead = 0;
    this.processInputs = new Map();
    this.contentOrigins = new Map();
    this.projectedOrigins = new Map();
  }

  rememberContent(sha256, origin) {
    const origins = this.contentOrigins.get(sha256) || new Map();
    const identity = origin.record || origin.candidateId || origin.experimentId || origin.kind;
    origins.set(identity, structuredClone(origin));
    this.contentOrigins.set(sha256, origins);
  }

  commonForRead(origin) {
    if (origin.kind === 'reference' || origin.kind === 'metadata') return true;
    if (origin.regions) return origin.regions.every(region =>
      region.withheld || region.reference || region.candidateId === this.currentId);
    if (origin.kind === 'derived') return origin.parents.every(parent => this.commonForRead(parent));
    if (origin.kind === 'equivalent-content') return origin.alternatives.some(parent => this.commonForRead(parent));
    if (origin.candidateId) return origin.candidateId === this.currentId;
    if (origin.experimentId) return origin.experimentId === this.latestExperimentId;
    return Boolean(origin.common);
  }

  async copiedContentOrigin(path) {
    let bytes;
    try { bytes = await readFile(path); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
    const origins = [...(this.contentOrigins.get(hash(bytes))?.values() || [])];
    if (origins.length === 1) return structuredClone(origins[0]);
    if (origins.length > 1) return {
      kind: 'equivalent-content', alternatives: origins,
      common: origins.some(origin => origin.kind === 'reference' || origin.candidateId === this.currentId || origin.common),
    };
    return null;
  }

  async importPythonLineage() {
    const path = join(this.config.workspace, '.recall-audit', 'python-images.jsonl');
    let text;
    try { text = await readFile(path, 'utf8'); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
    const lines = text.trim() ? text.trim().split('\n') : [];
    for (const line of lines.slice(this.lineageRead)) {
      const entry = JSON.parse(line);
      const inputs = this.processInputs.get(entry.process) || new Map();
      this.processInputs.set(entry.process, inputs);
      if (entry.operation === 'read') {
        const origin = this.sources.paths.get(entry.path) || await this.copiedContentOrigin(entry.path);
        if (origin) this.sources.registerPath(entry.path, origin);
        inputs.set(entry.path, origin || { kind: 'unknown', path: entry.path });
      } else {
        const parents = [...inputs.values()].filter(origin => origin.kind !== 'metadata');
        const common = parents.every(origin => this.commonForRead(origin));
        const hasCommon = parents.some(origin => this.commonForRead(origin));
        this.sources.registerPath(entry.path, {
          kind: 'derived', path: entry.path, common, parents,
          unverified: parents.some(origin => origin.kind === 'unknown' || origin.unverified),
          needsRegionAnnotation: !common && hasCommon,
        });
      }
    }
    this.lineageRead = lines.length;
  }

  async instrumentAuthorTools(agent) {
    const auditDirectory = join(this.config.workspace, '.recall-audit');
    await mkdir(auditDirectory, { recursive: true });
    await mkdir(join(this.config.output, 'source-edits'), { recursive: true });
    const pythonPath = join(this.config.projectRoot, 'scripts', 'shader_recall_python');
    const auditPath = join(auditDirectory, 'python-images.jsonl');
    const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
    const control = this;
    for (const tool of this.ctx.tools.definitions(agent)) {
      if (['read_image', 'mcp__vista__read_record', 'recall_shader_candidate'].includes(tool.name)) {
        this.instrumentImageTool(tool);
      }
      if (!['bash', 'write', 'edit', 'apply_patch'].includes(tool.name)) continue;
      const execute = tool.execute;
      tool.execute = async function (args, execution) {
        const path = join(control.runtime.service.workspace(control.taskId), 'shader.frag');
        const before = await control.readSource(path);
        let actualArgs = args;
        if (tool.name === 'bash') {
          const command = [
            `export PYTHONPATH=${quote(pythonPath)}\${PYTHONPATH:+":$PYTHONPATH"}`,
            `export DSH_RECALL_FS_AUDIT=${quote(auditPath)}`,
            `export DSH_RECALL_FS_AUDIT_ROOT=${quote(control.config.output)}`,
            args.command,
          ].join('\n');
          actualArgs = { ...args, command };
        }
        try { return await execute.call(this, actualArgs, execution); }
        finally {
          try {
            const after = await control.readSource(path);
            const row = { callId: execution.callId, name: tool.name, time: new Date().toISOString(),
              beforeHash: before && hash(before), afterHash: after && hash(after), path };
            row.changed = row.beforeHash !== row.afterHash;
            if (row.changed) {
              const id = randomUUID();
              if (before) {
                row.beforePath = join(control.config.output, 'source-edits', `${id}-before.frag`);
                await writeFile(row.beforePath, before);
              }
              if (after) {
                row.afterPath = join(control.config.output, 'source-edits', `${id}-after.frag`);
                await writeFile(row.afterPath, after);
              }
            }
            await appendFile(join(control.config.output, 'source-edits.jsonl'), JSON.stringify(row) + '\n');
            if (tool.name === 'bash') await control.importPythonLineage();
          } catch (error) { control.failure = error; }
        }
      };
    }
  }

  async imageOrigin(name, args, path, value) {
    if (name === 'recall_shader_candidate' && value) {
      const origin = {kind:'candidate', candidateId:value.candidateId, taskId:value.taskId,
        channel:name, path:join(this.runtime.service.directory(value.taskId), 'candidates', value.candidateId, 'render.png')};
      if (value.via === 'vista') origin.record = value.record;
      return {...origin, common:this.commonForRead(origin)};
    }
    if (name === 'mcp__vista__read_record') {
      const record = JSON.parse(await readFile(join(this.runtime.vistaRoot, 'json', args.record), 'utf8'));
      path = join(this.runtime.vistaRoot, record.png);
      if (record.glsl === null) return { kind: 'reference', common: true, record: args.record, path };
    }
    path ||= args.file_path && resolve(this.config.workspace, args.file_path);
    const origin = this.sources.paths.get(path) || await this.copiedContentOrigin(path);
    if (!origin) throw new Error(`Unclassified image source from ${name}`);
    if (origin.unverified) throw new Error('Derived image has unverified pixel inputs');
    const result = { ...structuredClone(origin), channel: name, path };
    result.common = this.commonForRead(result);
    if (result.kind === 'derived') {
      result.needsRegionAnnotation = !result.common && result.parents.some(parent => this.commonForRead(parent));
    }
    if (result.needsRegionAnnotation && this.config.imageCondition === 'text') {
      throw new Error('Mixed derived image requires region provenance');
    }
    if (result.regions) result.regions = result.regions.map(region => ({ ...region,
      common: Boolean(region.withheld || region.reference || region.candidateId === this.currentId),
    }));
    return result;
  }

  instrumentImageTool(tool) {
    const execute = tool.execute;
    const project = tool.projectContent;
    const prepared = new Map();
    const control = this;
    tool.execute = async function (args, execution) {
      const value = await execute.call(this, args, execution);
      try {
        await control.queue;
        await control.refreshCatalog();
        const fallback = tool.output.render(args, value);
        const path = tool.output.presentationMeta?.(args, value)?.path;
        const content = project?.call(this, execution, {
          value, content: fallback, isError: false,
        }) ?? fallback;
        const replacements = new Map();
        for (const block of content) {
          if (block.type !== 'image') continue;
          const origin = await control.imageOrigin(tool.name, args, path, value);
          const attachment = block.attachment;
          let replacement = [block];
          let projectedOrigin = origin;
          let disposition = 'visible';
          if (control.config.imageCondition === 'text' && !origin.common) {
            const hidden = origin.regions?.filter(region => !region.common);
            if (hidden?.length) {
              const image = await control.ctx.attachments.readImage(attachment, execution.signal);
              const regions = hidden.map(region => {
                const x = Math.floor(region.x * image.ref.width / origin.width);
                const y = Math.floor(region.y * image.ref.height / origin.height);
                return { x, y,
                  width: Math.ceil((region.x + region.width) * image.ref.width / origin.width) - x,
                  height: Math.ceil((region.y + region.height) * image.ref.height / origin.height) - y,
                };
              });
              const masked = await control.runtime.bridge.request({
                action: 'mask', data: Buffer.from(image.data).toString('base64'), regions,
              });
              const saved = await control.ctx.attachments.saveImage({
                data: Buffer.from(masked.data, 'base64'), mediaType: masked.media_type || masked.mediaType,
                name: attachment.name,
              });
              projectedOrigin = { ...origin, candidateId: undefined, originalCandidateId: origin.candidateId,
                regions: origin.regions.map(region => ({ ...region, withheld: !region.common || region.withheld })),
              };
              replacement = [{ ...block, attachment: saved }, { type: 'text', text: imageUnavailable }];
              disposition = 'masked';
            } else if (!origin.regions) {
              replacement = [{ type: 'text', text: imageUnavailable }];
              disposition = 'withheld';
            }
          }
          for (const item of replacement) if (item.type === 'image') {
            control.projectedOrigins.set(`${execution.callId}:${item.attachment.attachmentId}`, projectedOrigin);
          }
          replacements.set(attachment.attachmentId, replacement);
          await appendFile(join(control.config.output, 'tool-image-control.jsonl'), JSON.stringify({
            callId: execution.callId, name: tool.name, origin, disposition,
            originalAttachmentId: attachment.attachmentId,
            projectedAttachmentIds: replacement.filter(item => item.type === 'image').map(item => item.attachment.attachmentId),
          }) + '\n');
        }
        prepared.set(execution.callId, { content, replacements });
        return value;
      } catch (error) {
        control.failure = error;
        await writeJson(join(control.config.output, 'image-control-failure.json'), {
          stage: 'tool-output-projection', message: error.message, errorType: error.name,
        });
        throw error;
      }
    };
    tool.projectContent = function (execution, result) {
      const preparation = prepared.get(execution.callId);
      prepared.delete(execution.callId);
      if (result.isError) return result.content;
      const content = preparation?.content ?? result.content;
      const replacements = preparation?.replacements;
      return content.flatMap(block => {
        if (block.type !== 'image') return [block];
        const replacement = replacements?.get(block.attachment.attachmentId);
        if (!replacement) {
          control.failure = new Error('Image tool produced an unprepared attachment');
          throw control.failure;
        }
        return replacement;
      });
    };
  }

  async readSource(path) {
    try { return await readFile(path); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }

  async refreshCatalog() {
    if (!this.taskId) return;
    const { service, mapping, vistaRoot } = this.runtime;
    const task = await service.read(this.taskId);
    this.currentId = task.candidates.at(-1)?.id || this.config.currentCandidateId;
    const latestExperiment = task.experimentLog?.at(-1);
    this.latestExperimentId = latestExperiment &&
      latestExperiment.createdAt > (task.candidates.at(-1)?.createdAt || '') ? latestExperiment.id : null;
    const directory = service.directory(task.id);
    this.sources.registerPath(join(directory, 'reference.png'), { kind: 'reference', common: true });
    this.rememberContent(task.referenceHash, { kind: 'reference', common: true });
    this.sources.registerPath(join(directory, 'reference-original'), { kind: 'reference', common: true });
    this.sources.registerPath(join(directory, 'task.json'), { kind: 'metadata', common: true });
    this.sources.registerPath(join(directory, 'metrics.json'), { kind: 'metadata', common: true });
    if (task.selectedCandidateId) this.sources.registerPath(join(directory, 'best.png'), {
      kind: 'candidate', candidateId: task.selectedCandidateId, record: mapping[task.selectedCandidateId]?.record,
    });
    for (const candidate of task.candidates) {
      const folder = join(directory, 'candidates', candidate.id);
      const origin = { kind: 'candidate', candidateId: candidate.id, record: mapping[candidate.id]?.record };
      this.rememberContent(candidate.imageHash, origin);
      this.sources.registerPath(join(folder, 'render.png'), origin);
      if (candidate.comparison) {
        const layout = JSON.parse(await readFile(join(folder, 'comparison.json'), 'utf8'));
        const composite = {
          kind: 'composite', candidateId: candidate.id,
          width: layout.cellWidth * 3, height: layout.cellHeight + layout.labelHeight,
          regions: [
            { x: 0, y: 0, width: layout.cellWidth, height: layout.cellHeight + layout.labelHeight, reference: true },
            { x: layout.cellWidth, y: 0, width: layout.cellWidth, height: layout.cellHeight + layout.labelHeight, candidateId: layout.candidateId },
            { x: layout.cellWidth * 2, y: 0, width: layout.cellWidth, height: layout.cellHeight + layout.labelHeight, candidateId: layout.bestId },
          ],
        };
        this.sources.registerPath(join(folder, 'comparison.png'), composite);
        this.rememberContent(candidate.comparison.imageHash, composite);
      }
    }
    for (const experiment of task.experimentLog || []) {
      this.sources.registerPath(experiment.imagePath, {
        kind: 'experiment', experimentId: experiment.id, record: mapping[experiment.id]?.record,
      });
    }
    for (const entry of Object.values(mapping)) {
      let record;
      try { record = JSON.parse(await readFile(join(vistaRoot, 'json', entry.record), 'utf8')); }
      catch (error) { if (error.code === 'ENOENT' || error instanceof SyntaxError) continue; throw error; }
      if (typeof record?.png !== 'string') continue;
      this.sources.registerPath(join(vistaRoot, 'json', entry.record), { kind: 'metadata', common: true });
      const canonical = this.sources.paths.get(entry.imagePath);
      if (canonical) this.sources.registerPath(join(vistaRoot, record.png), canonical);
    }
  }

  observe(session, event) {
    if (session.id !== this.sessionId) return;
    const data = event.data || {};
    if (event.type === 'tool/call') {
      let args;
      try { args = typeof data.arguments === 'string' ? JSON.parse(data.arguments) : data.arguments || {}; }
      catch { args = {}; }
      this.sources.recordCall(data.callId, data.name, args);
    }
    if (event.type !== 'tool/result') return;
    this.queue = this.queue.then(async () => {
      await this.refreshCatalog();
      const message = data.message;
      if (message.isError) return;
      const call = this.sources.calls.get(message.toolCallId);
      if (!call) throw new Error('Image audit received an unmatched tool result');
      if (call.name === 'compare_shader_images') {
        const result = JSON.parse(message.content.filter(block => block.type === 'text').map(block => block.text).join('\n'));
        const task = await this.runtime.service.read(this.taskId);
        const width = call.args.region?.width || task.width;
        const height = call.args.region?.height || task.height;
        this.sources.registerPath(result.path, {
          kind: 'composite', width: width * 3, height,
          regions: [
            { x: 0, y: 0, width, height, reference: true },
            { x: width, y: 0, width, height, candidateId: call.args.candidateId },
            { x: width * 2, y: 0, width, height, candidateId: call.args.candidateId },
          ],
        });
      }
      const images = message.content.filter(block => block.type === 'image');
      if (!images.length) return;
      for (const image of images) {
        const key = `${message.toolCallId}:${image.attachment.attachmentId}`;
        const origin = this.projectedOrigins.get(key) || await this.imageOrigin(call.name, call.args, data.meta?.path);
        this.projectedOrigins.delete(key);
        this.sources.recordImageResult(message.toolCallId, { ...message, content: [image] }, origin.path, origin);
        await appendFile(join(this.config.output, 'image-provenance.jsonl'), JSON.stringify({
          eventSeq: event.seq, callId: message.toolCallId, origin,
          attachments: [image.attachment.attachmentId],
        }) + '\n');
      }
    }).catch(error => { this.failure = error; });
  }

  async admit(options) {
    await this.queue;
    if (this.failure) throw this.failure;
    const bindings = [];
    for (const message of options.messages) for (const block of message.content) {
      if (block.type !== 'image' || block.offloaded) continue;
      const attachment = block.attachment;
      let origin;
      if (message.role === 'tool') {
        origin = structuredClone(this.sources.forAttachment(message, attachment));
        origin.common = this.commonForRead(origin);
        if (origin.regions) origin.regions = origin.regions.map(region => ({ ...region,
          common: Boolean(region.withheld || region.reference || region.candidateId === this.currentId),
        }));
      }
      else if (message.role === 'user') {
        const record = this.config.providedImageNames?.[attachment.name];
        const candidateId = record ? Object.keys(this.runtime.mapping).find(
          id => this.runtime.mapping[id].record === record,
        ) : attachment.name === 'current.png' ? this.config.currentCandidateId : undefined;
        origin = {
          kind: attachment.name === 'reference.png' ? 'reference' : 'input',
          name: attachment.name,
          common: Boolean(record || attachment.name === 'reference.png' || candidateId === this.currentId),
          record, candidateId,
        };
      }
      else throw new Error('Image in an unclassified message role');
      const cachedOrigin = structuredClone(origin);
      if (cachedOrigin.kind !== 'reference') cachedOrigin.common = false;
      this.sources.registerPath(this.ctx.attachments.imageHostPath(attachment), cachedOrigin);
      this.rememberContent(attachment.attachmentId.replace(/^sha256:/, ''), cachedOrigin);
      const target = this.ctx.llm.imageRequestTarget(options.provider, options.model, attachment);
      const version = await this.ctx.attachments.readImageRequest(attachment, target, options.signal);
      const binding = {
        sha256: hash(version.data), origin, common: Boolean(origin.common),
        messageId: message.id, toolCallId: message.toolCallId,
      };
      if (origin.regions) {
        binding.regions = origin.regions.map(region => {
          const x = Math.floor(region.x * version.width / origin.width);
          const y = Math.floor(region.y * version.height / origin.height);
          const right = Math.ceil((region.x + region.width) * version.width / origin.width);
          const bottom = Math.ceil((region.y + region.height) * version.height / origin.height);
          return { x, y, width: right - x, height: bottom - y, common: region.common };
        });
      }
      bindings.push(binding);
    }
    await writeJson(this.config.policyPath, {
      condition: this.config.imageCondition || 'visual', bindings,
      maxModelRequests: this.config.maxModelRequests || 12, admission: ++this.modelCalls,
    });
    await appendFile(join(this.config.output, 'image-admissions.jsonl'), JSON.stringify({
      time: new Date().toISOString(), admission: this.modelCalls, purpose: options.purpose,
      bindings,
    }) + '\n');
  }

  install() {
    this.ctx.on('session/event', (session, event) => this.observe(session, event));
    const control = this;
    this.ctx.on('llm/stream', async function* (options, next) {
      try { await control.admit(options); }
      catch (error) {
        await writeJson(join(control.config.output, 'image-control-failure.json'), {
          stage: 'attachment-admission', message: error.message, errorType: error.name,
        });
        throw error;
      }
      yield* next();
    }, { prepend: true });
  }
}
