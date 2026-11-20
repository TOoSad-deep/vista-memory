import { appendFile, mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { installBusinessResolution, packagedModule } from './shader_recall_deps.mjs';
import { cloneVista, createBusiness, defaultBusinessRoot, fileManifest, hash, restoreCheckpoint, writeJson } from './shader_recall_runtime.mjs';
import { LiveImageControl } from './shader_recall_live_images.mjs';

export const inject = ['agents', 'tools', 'systemPrompt', 'attachments', 'llm'];

// Cover task-local scratch shaders and selection restoring a different working source.
// Save observations outside the author workspace; the existing primary-file audit stays active.
async function traceWorkspaceShaders(tools, workspace, output) {
  const archive = join(output, 'workspace-source-snapshots');
  await mkdir(archive, { recursive: true });
  async function snapshot(directory, files = {}) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await snapshot(path, files);
      else if (entry.isFile() && /\.(?:frag|glsl|vert|fs|fsh)$/i.test(entry.name)) {
        const bytes = await readFile(path);
        const digest = hash(bytes);
        files[path] = digest;
        await writeFile(join(archive, digest), bytes);
      }
    }
    return files;
  }
  const names = ['bash', 'write', 'edit', 'apply_patch', 'select_shader_candidate', 'render_shader', 'experiment_shader'];
  for (const tool of tools.filter(t => names.includes(t.name))) {
    const execute = tool.execute;
    tool.execute = async function (args, execution) {
      const before = await snapshot(workspace);
      try { return await execute.call(this, args, execution); }
      finally {
        const after = await snapshot(workspace);
        const changes = [...new Set([...Object.keys(before), ...Object.keys(after)])]
          .filter(path => before[path] !== after[path])
          .map(path => ({ path, before: before[path] || null, after: after[path] || null }));
        await appendFile(join(output, 'workspace-shader-edits.jsonl'), JSON.stringify({
          callId: execution.callId, changes,
        }) + '\n');
      }
    };
  }
}

// The SDK owns Session creation. Business preparation binds that exact live
// Session before the original Shader author role installs its capabilities.
export async function apply(ctx, config) {
  const output = resolve(config.output);
  const businessRoot = resolve(config.businessRoot || defaultBusinessRoot);
  process.env.VISTA_BUSINESS_ROOT = businessRoot;
  installBusinessResolution();
  await mkdir(output, { recursive: true });
  const checkpoint = config.checkpointPath ? JSON.parse(await readFile(config.checkpointPath, 'utf8')) : null;
  if (checkpoint) {
    const frozen = await fileManifest(checkpoint.vistaRoot);
    if (JSON.stringify(frozen) !== JSON.stringify(checkpoint.vistaManifest)) throw new Error('Frozen VISTA template changed');
    await cloneVista(checkpoint.vistaRoot, config.vistaRoot);
    await writeJson(join(output, 'initial-record-manifest.json'), await fileManifest(config.vistaRoot));
  }
  const log = event => appendFile(join(output, 'adapter-audit.jsonl'), JSON.stringify({ time: new Date().toISOString(), ...event }) + '\n');
  let runtime;
  let businessContext;
  const sessions = {
    async create({ sessionId }) {
      if (!ctx.agents.get(sessionId)) throw new Error('Business binding requires an existing live SDK Agent');
      return { sessionId };
    },
    async prompt(input) {
      const filename = config.businessFeedback && input.requestId === 'trigger-feedback'
        ? 'business-continuation.json' : 'prepared-prompt.json';
      await writeJson(join(output, filename), input);
      return { messageId: input.requestId };
    },
    async selectModel() {},
    async state({ sessionId }) {
      const agent = ctx.agents.get(sessionId);
      return { active: Boolean(agent), running: Boolean(agent?.running), queue: [] };
    },
    async events({ address, maxMessages = 64, beforeSeq }) {
      const session = ctx.agents.get(address.sessionId)?.session;
      if (!session) throw new Error('Session events unavailable');
      const records = [];
      for (let seq = Math.min(beforeSeq ?? session.seq + 1, session.seq + 1) - 1; seq >= 0 && records.length < maxMessages; seq--) {
        const event = session.eventAt(seq);
        if (event) records.push({ event });
      }
      return { records, hasMore: records.length === maxMessages };
    },
    async cancel({ sessionId }) { ctx.agents.get(sessionId)?.cancel?.(); },
  };
  runtime = await createBusiness({
    businessRoot, artifactRoot: join(output, 'business'), vistaRoot: config.vistaRoot || join(output, 'vista'),
    python: config.python,
  }, sessions);
  ctx.provide('shaderWorkbench', runtime.service);
  let renderAttempts = 0;
  if (config.maxRenderAttempts) {
    const mutate = runtime.service.mutate.bind(runtime.service);
    runtime.service.mutate = async (taskId, action, ...args) => {
      const rendering = ['render', 'experiment', 'edit', 'tune'].includes(action);
      if (rendering && ++renderAttempts > config.maxRenderAttempts) throw new Error('Run render-attempt budget exceeded');
      const result = await mutate(taskId, action, ...args);
      if (rendering && !config.nativeAuthor) {
        result.recallBudget = { renderAttempts, limit: config.maxRenderAttempts };
        result.remainingAttempts = Math.min(result.remainingAttempts ?? config.maxRenderAttempts,
          Math.max(0, config.maxRenderAttempts - renderAttempts));
      }
      return result;
    };
  }
  const imageControl = config.imageControl ? new LiveImageControl(ctx, runtime, config) : null;
  imageControl?.install();
  ctx.effect(() => async () => { await runtime.service.dispose(); });
  const roleModule = await import(pathToFileURL(join(businessRoot, 'src', 'session-role.js')).href);
  const { createUserMessage } = await import(packagedModule('@deepseek-ai/dsh-llm'));
  ctx.on('agent/created', async ({ agent }) => {
    if (config.sessionId && agent.session.id !== config.sessionId) throw new Error('Unexpected Session in isolated Shader experiment');
    const sessionId = agent.session.id;
    if (!config.renderedCheckpoint) await runtime.service.saveConversation({
      sessionId, cwd: resolve(config.workspace), taskId: null, jobId: null,
      stage: 'Shader生成', agentPreset: 'shader', role: 'author', sessionCreated: false,
    });
    const referencePath = resolve(config.referencePath);
    const reference = (await readFile(referencePath)).toString('base64');
    const seedSource = config.sourcePath ? await readFile(config.sourcePath, 'utf8') : undefined;
    let task = config.renderedCheckpoint
      ? await runtime.service.view(await runtime.service.read(config.taskId))
      : await runtime.service.create({
      requestId: config.taskId || 'recall-task', sessionId, stage: 'author', reference,
      requirements: config.requirements || '根据参考图完善当前 Shader，保留已经接近参考的局部效果。',
      authorLoop: true, seedSource, maxOptimizationRounds: 0,
    });
    if (checkpoint) {
      const restored = await restoreCheckpoint(runtime, await runtime.service.read(task.id), checkpoint);
      task = await runtime.service.view(restored.task);
      const snapshotPrompt = {
        requestId: 'checkpoint-continuation', sessionId,
        content: [{ type: 'text', text: restored.prompt + '\nCurrent Shader source:\n' + restored.source }],
        images: [
          { path: join(task.directory, 'reference.png'), name: 'reference.png' },
          { path: restored.currentImage, name: 'current.png' },
        ],
      };
      if (config.accessView === 'known') {
        snapshotPrompt.content[0].text += '\nCandidate record references (task data):\n' + JSON.stringify(
          checkpoint.steps.map(step => ({ step: step.step, candidateId: step.candidateId, record: step.record })),
        );
      }
      await writeJson(join(output, 'prepared-prompt.json'), snapshotPrompt);
      if (config.businessFeedback) {
        // Exercise the real continue entry point. Keep the human message verbatim
        // and attribute the host's own wrapper and checkpoint context separately.
        await runtime.service.continue(task.id, 'trigger-feedback', config.businessFeedback);
        const captured = JSON.parse(await readFile(join(output, 'business-continuation.json'), 'utf8'));
        const parts = captured.content[0].text.split(config.businessFeedback);
        if (parts.length !== 2) throw new Error('Business continuation must contain exactly one literal feedback');
        const nativeHostSuffix = parts[1].trim();
        let deliveredSuffix = parts[1];
        if (config.experimentalHostCue) {
          if (!nativeHostSuffix) throw new Error('Missing business trigger to replace');
          deliveredSuffix = '\n' + config.experimentalHostCue
            .replaceAll('{selected}', checkpoint.currentCandidateId)
            .replaceAll('{directory}', task.directory);
        }
        businessContext = [snapshotPrompt.content[0].text, parts[0], deliveredSuffix].join('\n');
        const { visualRecallPolicy } = await import(pathToFileURL(join(businessRoot, 'src/prompts/visual-recall.js')).href);
        await writeJson(join(output, 'loaded-author.json'), {
          pid: process.pid, businessRoot, continuationMethodSha256: hash(runtime.service.continue.toString()),
          policy: visualRecallPolicy, hostSuffix: deliveredSuffix.trim(), feedback: config.businessFeedback,
          ...(config.experimentalHostCue ? { nativeHostSuffix } : {}),
        });
      }
    }
    if (config.maxRenderAttempts && !config.renderedCheckpoint) {
      const active = await runtime.service.read(task.id);
      active.workflow.renderLimit = config.maxRenderAttempts;
      await runtime.service.save(active);
    }
    if (task.status !== 'running' || await runtime.service.forSession(sessionId) !== task.id) throw new Error('Business task restoration failed');
    if (imageControl) {
      imageControl.sessionId = sessionId;
      imageControl.taskId = task.id;
      await imageControl.refreshCatalog();
    }
    const referenceRecord = config.renderedCheckpoint ? { record: config.renderedCheckpoint.referenceRecord } :
      checkpoint ? { record: checkpoint.referenceRecord } : await runtime.bridge.request({
      action: 'save', root: runtime.vistaRoot, image_path: join(task.directory, 'reference.png'),
      label: 'reference', source_agent: 'intake',
    });
    let roleCreated;
    const roleComposer = {
      name: 'vista-real-shader-role',
      inject: ['shaderWorkbench', 'tools', 'systemPrompt'],
      async apply(roleCtx) {
        const proxy = new Proxy(roleCtx, {
          get(target, key) {
            // The SDK Agent is already bound. The original created-handler
            // installs its author persona; the unbound entry persona belongs
            // to the Desktop preset's parent scope, not this Agent scope.
            if (key === 'systemPrompt') return new Proxy(target.systemPrompt, {
              get(prompt, member) {
                if (member === 'section') return section => {
                  if (section.name === 'deployment:persona-prefix') return () => {};
                  return prompt.section(section);
                };
                const value = Reflect.get(prompt, member, prompt);
                return typeof value === 'function' ? value.bind(prompt) : value;
              },
            });
            if (key === 'on') return (event, callback, ...options) => {
              if (event === 'agent/created') { roleCreated = callback; return () => {}; }
              return target.on(event, callback, ...options);
            };
            const value = Reflect.get(target, key, target);
            return typeof value === 'function' ? value.bind(target) : value;
          },
        });
        roleModule.apply(proxy);
        if (!roleCreated) throw new Error('Original role composer did not install an Agent handler');
        await roleCreated({ agent });
      },
    };
    await agent.ctx.plugin(roleComposer);
    await agent.ctx.plugin(await import(packagedModule('@deepseek-ai/dsh-time-context')));
    await agent.ctx.plugin(await import(pathToFileURL(join(businessRoot, 'src', 'budget-plugin.js')).href));
    const mcpModule = await import(packagedModule('@deepseek-ai/dsh-mcp-client'));
    await agent.ctx.plugin(mcpModule, {
      serverName: 'vista', transport: 'stdio', command: config.python,
      args: ['-m', 'vista_memory.mcp', '--root', runtime.vistaRoot],
      cwd: config.projectRoot, failOnStartupError: true, startupTimeoutMs: 30000, toolCallTimeoutMs: 30000,
    });
    const tools = ctx.tools.schemas(agent);
    const names = tools.map(tool => tool.name);
    for (const name of ['read', 'read_image', 'write', 'render_shader', 'assess_shader_candidate', 'submit_shader_candidate', 'mcp__vista__list_records', 'mcp__vista__read_record']) {
      if (!names.includes(name)) throw new Error(`Restored author lacks ${name}`);
    }
    if (imageControl) await imageControl.instrumentAuthorTools(agent);
    if (config.traceWorkspaceShaders) {
      await traceWorkspaceShaders(ctx.tools.definitions(agent), config.workspace, output);
    }
    const memoryCounts = { mcp__vista__list_records: 0, mcp__vista__read_record: 0 };
    for (const tool of ctx.tools.definitions(agent)) {
      if (!(tool.name in memoryCounts)) continue;
      const execute = tool.execute;
      tool.execute = async function (args, execution) {
        const limit = tool.name === 'mcp__vista__list_records' ? config.maxListCalls || 2 : config.maxReadCalls || 8;
        if (++memoryCounts[tool.name] > limit) throw new Error('Run VISTA tool-call budget exceeded');
        return execute.call(this, args, execution);
      };
    }
    let taskStarted;
    agent.ctx.on('agent/pre-step', async (_step, next) => {
      const decision = await next();
      if (decision.kind === 'reject') return decision;
      taskStarted ??= performance.now();
      let requests = 0;
      try {
        const text = await readFile(join(output, 'http-audit.jsonl'), 'utf8');
        requests = text.split('\n').filter(Boolean).map(line => JSON.parse(line)).filter(row => row.type === 'request' && row.model).length;
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      const seconds = Math.max(0, Math.floor((config.timeoutSeconds || 600) - (performance.now() - taskStarted) / 1000));
      const text = `Execution budget for this task. Model requests remaining including this request ${Math.max(0, (config.maxModelRequests || 12) - requests)}. Render attempts remaining ${Math.max(0, (config.maxRenderAttempts || 3) - renderAttempts)}, including experiments and failures. Seconds remaining ${seconds}. VISTA list calls remaining ${Math.max(0, (config.maxListCalls || 2) - memoryCounts.mcp__vista__list_records)}. VISTA read calls remaining ${Math.max(0, (config.maxReadCalls || 8) - memoryCounts.mcp__vista__read_record)}. Each response is limited to 8192 output tokens. Reserve enough requests to render, inspect, assess and submit a complete candidate. Report remaining visual differences honestly.`;
      const messages = [...decision.messages];
      if (businessContext) {
        messages.unshift(createUserMessage({ content: [{ type: 'text', text: businessContext }],
          source: { kind: 'shader-business-context', form: 'snapshot', sections: [{ name: 'continuation', text: businessContext }] } }));
        businessContext = undefined;
      }
      if (config.nativeAuthor) return { ...decision, messages };
      return { ...decision, messages: [...messages, createUserMessage({
        content: [{ type: 'text', text }], source: { kind: 'shader-recall-budget', form: 'snapshot', sections: [{ name: 'shader-recall-budget', text }] },
      })] };
    }, { prepend: true });
    await writeJson(join(output, 'session-restore-check.json'), {
      status: 'capabilities-ready', sessionId, taskId: task.id, role: 'author', mode: 'sdk-original-business-role-composition',
      binding: JSON.parse(await readFile(runtime.service.conversationPath(sessionId), 'utf8')),
      workspace: task.workspace, directory: task.directory, vistaRoot: runtime.vistaRoot,
      referenceRecord, tools, businessRoot,
    });
    const selection = agent.model.current();
    const model = await ctx.llm.resolveModelInfo(selection.provider, selection.model, new AbortController().signal);
    await writeJson(join(output, 'model-capabilities.json'), {
      provider: selection.provider, id: model.id, name: model.name,
      inputModalities: model.inputModalities, contextWindow: model.contextWindow,
      maxTokens: model.maxTokens, reasoning: model.reasoning,
    });
    await log({ type: 'restored', sessionId, taskId: task.id, tools: names });
  });
}
