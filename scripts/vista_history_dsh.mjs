import { AsyncLocalStorage } from 'node:async_hooks';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { inspectBody } from './dsh_request_audit.mjs';

export const inject = ['attachments', 'llm', 'sessions', 'tools'];

const compactKind = 'compact-checkpoint';
const catalogSource = 'vista-image-catalog';
const receiptSource = 'vista-image-receipt';
const shortId = /^R[1-9][0-9]*$/;
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const defaultRuntime = '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/package.json';
const bridgePath = fileURLToPath(new URL('./vista_history_dsh_bridge.py', import.meta.url));
const packagePath = name => `@deepseek-ai/${name}`;

function openBridge(config) {
  const proc = spawn(config.python, [bridgePath], { stdio: ['pipe', 'pipe', 'inherit'] });
  const input = createInterface({ input: proc.stdout });
  const pending = new Map();
  let nextId = 0;
  let failure;
  function fail(error) {
    failure = error;
    for (const entry of pending.values()) entry.reject(error);
    pending.clear();
  }
  proc.on('error', fail);
  proc.stdin.on('error', fail);
  proc.on('exit', code => fail(new Error(`VISTA 目录进程退出（${code}）`)));
  input.on('line', line => {
    let response;
    try {
      response = JSON.parse(line);
    } catch {
      fail(new Error('VISTA 目录进程返回无效数据'));
      proc.kill();
      return;
    }
    const entry = pending.get(response.id);
    if (!entry) return;
    pending.delete(response.id);
    if (response.error) {
      const error = new Error(response.error.message);
      error.name = response.error.type;
      entry.reject(error);
    } else {
      entry.resolve(response.result);
    }
  });
  proc.stdin.write(JSON.stringify(config) + '\n');
  return {
    request(action, args = {}) {
      if (failure) return Promise.reject(failure);
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        proc.stdin.write(JSON.stringify({ id, action, args }) + '\n');
      });
    },
    close() {
      input.close();
      proc.stdin.end();
      proc.kill();
    },
  };
}

export function imageDigests(message, { includeOffloaded = false } = {}) {
  return new Set((message?.content ?? []).flatMap(block => {
    if (block.type !== 'image' || (!includeOffloaded && block.offloaded)) return [];
    const hash = /^sha256:([a-f0-9]{64})$/.exec(block.attachment?.attachmentId)?.[1];
    if (hash) return [hash];
    return block.data ? [digest(Buffer.from(block.data, 'base64'))] : [];
  }));
}

export function resolveReplyCandidates(message, attachmentId, rows, target) {
  const images = message?.content.filter(block => block.type === 'image') ?? [];
  if (images.length !== 1 || !attachmentId || images[0].attachment?.attachmentId !== attachmentId) return [];
  const [hash] = imageDigests({ content: images }, { includeOffloaded: true });
  if (!hash) return [];
  return rows.filter(row => row.sha256 === hash && (target === undefined || row.short_id === target))
    .map(row => row.short_id);
}

function rawMessage(event) {
  if (event?.type === 'user/message') return event.data;
  if (event?.type === 'tool/result') return event.data.message;
  return undefined;
}

function words(message) {
  return (message?.content ?? []).filter(block => block.type === 'text').map(block => block.text);
}

function hostSection(message, section) {
  return message.source?.kind === 'workspace-history-data'
    && message.source?.sections?.some(item => item.name === section);
}

function receiptIds(message, rows) {
  if (message?.role !== 'user' && message?.role !== 'tool') return [];
  const text = words(message).flatMap(item => item.split('\n'));
  return rows.filter(row => row.label.trim() && text.includes(row.receipt)).map(row => row.short_id);
}

export function lostShortIds(rows, known, visibleDigests, receiptShortIds, covered) {
  return rows.filter(row => known.has(row.short_id) && !visibleDigests.has(row.sha256)
    && !receiptShortIds.has(row.short_id) && !covered.has(row.short_id))
    .map(row => row.short_id);
}

function iterText(value, path, visit) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => iterText(item, `${path}[${index}]`, visit));
  } else if (value !== null && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) iterText(item, `${path}.${key}`, visit);
  } else if (typeof value === 'string') {
    visit(value, path);
  }
}

function callsFrom(messages) {
  const calls = [];
  for (const message of messages) {
    for (const block of Array.isArray(message.content) ? message.content : []) {
      if (block.type === 'tool_use') calls.push({ id: block.id, name: block.name, args: block.input });
    }
  }
  return calls;
}

export function apply(ctx, config) {
  if (!config.sessionId || !config.python || !config.vistaRoot
      || (!config.fixedView && (!config.taskId || !config.taskDir))) {
    throw new Error('VISTA 目录需要绑定会话、任务和存储位置');
  }
  const runtime = createRequire(config.runtimePackage ?? defaultRuntime);
  const load = name => import(pathToFileURL(runtime.resolve(packagePath(name))).href);
  return install(ctx, config, load);
}

async function install(ctx, config, load) {
  const { createUserMessage } = await load('dsh-llm');
  const { defineTool } = await load('dsh-tools');
  const bridge = openBridge(config);
  ctx.effect(() => () => bridge.close());

  const state = {
    turn: 0, nextScan: 0, archivedDigests: new Set(), archivedReceipts: new Set(),
    rows: [], visibleSeen: new Set(), pendingReceipts: new Map(), clueTools: new Map(),
    queued: undefined,
  };
  const knownViews = new Map();
  const requestScope = new AsyncLocalStorage();
  const log = event => {
    if (config.auditPath) {
      appendFileSync(config.auditPath, JSON.stringify({
        time: new Date().toISOString(), session_id: config.sessionId, ...event,
      }) + '\n');
    }
  };
  function requireScope(sessionId) {
    if (sessionId !== config.sessionId) throw new Error('会话与 VISTA 任务范围不符');
    const session = ctx.sessions.get(sessionId);
    if (!session) throw new Error('VISTA 任务会话不可用');
    return session;
  }
  const argsForView = () => ({
    selected: config.selected ?? null,
    subtask_id: config.subtaskId ?? null,
    grouped: config.grouped ?? true,
    include_notes: config.includeNotes ?? true,
    budget: config.budget ?? 400,
  });
  function rememberView(view) {
    knownViews.set(view.text, view.view_id);
    return view;
  }
  async function newView() {
    const view = config.frozenViewId
      ? await bridge.request('get_view', { view_id: config.frozenViewId })
      : await bridge.request('view', argsForView());
    return rememberView(view);
  }
  function userData(name, text) {
    return createUserMessage({
      content: [{ type: 'text', text }],
      source: { kind: 'workspace-history-data', form: 'snapshot', sections: [{ name, text }] },
    });
  }

  async function placement(agent, proposed) {
    const session = requireScope(agent.id);
    state.rows = await bridge.request('rows');
    for (const event of session.snapshotEvents(state.nextScan)) {
      if (event.type === 'tool/call' && ['vista_history_read', 'vista_history_expand'].includes(event.data.name)) {
        state.clueTools.set(event.data.callId, event.data);
      }
      const message = rawMessage(event);
      for (const hash of imageDigests(message, { includeOffloaded: true })) state.archivedDigests.add(hash);
      for (const id of receiptIds(message, state.rows)) state.archivedReceipts.add(id);
    }
    state.nextScan = session.seq;
    const visible = [...session.deriveMessages(), ...proposed];
    const digests = new Set(visible.flatMap(message => [...imageDigests(message)]));
    const receipts = new Set(visible.flatMap(message => receiptIds(message, state.rows)));
    const covered = new Set();
    let version;
    for (const message of visible) {
      if (!hostSection(message, catalogSource) && message.source?.kind !== compactKind
          && message.role !== 'tool') continue;
      const tool = !message.isError && state.clueTools.get(message.toolCallId);
      for (const text of words(message)) {
        if (tool?.name === 'vista_history_read') {
          try {
            const metadata = JSON.parse(text);
            const item = state.rows.find(row => row.short_id === metadata.short_id
              && row.record === metadata.record && row.label === metadata.label);
            if (item?.label.trim()) covered.add(item.short_id);
          } catch { /* A code block is not record metadata. */ }
        }
        const expansion = tool?.name === 'vista_history_expand' ? JSON.parse(tool.arguments) : undefined;
        const id = knownViews.get(text) ?? expansion?.view_id
          ?? /^VISTA 目录 · view_id=([A-Za-z0-9_-]{12})(?:\r?\n|$)/.exec(text)?.[1];
        if (!id) continue;
        try {
          // Resolve active snapshots against the bound task, including after SDK resume.
          const view = await bridge.request('get_view', { view_id: id });
          if (!config.fixedView) version ??= await bridge.request('current_version');
          if (version !== undefined && version !== view.version) continue;
          if (view.text === text) {
            rememberView(view);
            for (const short of view.covered_short_ids) covered.add(short);
          } else if (expansion) {
            const page = await bridge.request('expand', expansion);
            if (page.text === text) for (const short of page.short_ids) covered.add(short);
          }
        } catch (error) {
          if (!/视图已失效/.test(error.message)) throw error;
        }
      }
    }
    const known = new Set(state.archivedReceipts);
    for (const row of state.rows) {
      if (state.archivedDigests.has(row.sha256)) known.add(row.short_id);
    }
    const missing = lostShortIds(state.rows, known, digests, receipts, covered);
    const newlyInjected = config.mode === 'auto' && missing.length ? await newView() : undefined;
    if (newlyInjected && !missing.every(id => newlyInjected.covered_short_ids.includes(id))) {
      throw new Error('VISTA 回看目录未覆盖登记图片');
    }
    state.queued = {
      missing, visible_digests: [...digests], retained_receipts: [...receipts],
      compact_checkpoint: visible.findLast(message => message.source?.kind === compactKind)?.id,
      ...(newlyInjected ? { injected_view_id: newlyInjected.view_id, injected: 1 } : { injected: 0 }),
    };
    return newlyInjected && userData(catalogSource, newlyInjected.text);
  }

  function followCheckpoint(session, data) {
    if (!data) return data;
    const event = session.surface.nodes.map(seq => session.eventAt(seq))
      .findLast(item => item.type === 'user/message' && item.data.source?.kind === compactKind);
    if (!event) return data;
    const checkpoint = event.data;
    const previousCard = checkpoint.content.some(block => block.type === 'text'
      && knownViews.has(block.text));
    if (previousCard) return data;
    // DSH can retain a verbatim tail after its summary. Keep ordinary task
    // data in the next content block, in the summary's original position.
    session.append('user/message', createUserMessage({
      content: [...checkpoint.content, ...data.content],
      source: checkpoint.source,
    }), {
      surfaceOp: { op: 'replace', startSeq: event.seq, endSeq: event.seq },
      sourceEventSeqs: [event.seq],
    });
    return undefined;
  }

  ctx.on('agent/pre-step', async ({ agent, turn }, next) => {
    const decision = await next();
    if (agent.id !== config.sessionId || decision.kind !== 'enter') return decision;
    if (state.turn !== turn) {
      state.turn = turn;
      state.visibleSeen.clear();
    }
    const receipts = [...state.pendingReceipts.values()];
    const taskData = followCheckpoint(agent.session, await placement(agent, [...receipts, ...decision.messages]));
    const prefix = taskData ? [taskData, ...receipts] : receipts;
    return prefix.length ? { ...decision, messages: [...prefix, ...decision.messages] } : decision;
  });

  ctx.on('session/event', (session, event) => {
    if (session.id !== config.sessionId || event.type !== 'user/message') return;
    for (const [key, message] of state.pendingReceipts) {
      if (message.id === event.data.id) state.pendingReceipts.delete(key);
    }
  });

  ctx.on('agent/request', async ({ agent }, next) => {
    const request = await next();
    // Native image-offload and overflow recovery retry without another pre-step.
    if (agent.id === config.sessionId && !state.queued) {
      const data = followCheckpoint(agent.session, await placement(agent, []));
      if (data) agent.session.append('user/message', data, { surfaceOp: 'append' });
    }
    return request;
  });

  const string = (description, required = true) => ({ type: 'string', description, ...(required ? { required: true } : {}) });
  function register(name, description, parameters, execute) {
    ctx.tools.register(defineTool({
      name, description, parameters,
      output: { schema: { type: 'json' }, render: (_args, blocks) => blocks },
      async execute(args, event) {
        requireScope(event.agent?.id);
        log({ kind: 'tool', name, call_id: event.callId, args });
        return execute(args, event);
      },
    }));
  }
  const textBlock = text => ({ type: 'text', text });
  register('vista_history_catalog', '查询本任务的图片回看短目录和展开入口。仅在需要找回任务图片时使用。', {},
    async () => [textBlock((await newView()).text)]);
  if (!config.fixedView) register('vista_history_expand', '查看短目录指定分组、其余图片或一张图片的备注，仅返回文字。复制同一视图的编号、入口和页游标。', {
    view_id: string('目录中显示的视图编号'),
    target: string('组入口如 G1，其余图片入口 rest，或短号如 R2'),
    cursor: string('上一页给出的游标', false),
  }, async args => [textBlock((await bridge.request('expand', args)).text)]);
  register('vista_history_read', '用任务内短号回读原图。只在需要核对视觉细节时打开。', {
    short_id: string('图片短号，例如 R2'),
    content: string('可选，image、code 或 both，默认 image', false),
  }, async ({ short_id, content = 'image' }) => {
    if (!['image', 'code', 'both'].includes(content)) throw new Error('请选择 image、code 或 both');
    const item = await bridge.request('read', { short_id, content, turn: state.turn, limit: config.imageLimit ?? 3 });
    const blocks = [textBlock(JSON.stringify(item.metadata))];
    if (item.image) {
      const [attachment] = await ctx.attachments.saveImages([{
        data: new Uint8Array(Buffer.from(item.image, 'base64')), mediaType: 'image/png',
      }]);
      blocks.push({ type: 'image', attachment });
    }
    if (item.code !== null) blocks.push(textBlock(item.code));
    return blocks;
  });

  if (!config.readOnly && !config.fixedView) {
    register('vista_history_observe', '仅在本轮实际看过该图后，把自己的观察写到该图。对比时两张图均须看过。', {
      target: string('观察对象的图片短号'),
      text: string('观察原话'),
      compared_to: string('被比较的图片短号', false),
      short_text: string('不丢限定语的短句', false),
    }, async (args, event) => {
      const targets = [args.target, ...(args.compared_to ? [args.compared_to] : [])];
      for (const id of targets) {
        if (!state.rows.some(row => row.short_id === id && state.visibleSeen.has(row.sha256))) {
          throw new Error(`本轮尚未看见 ${id} 的原图`);
        }
      }
      await bridge.request('observe', { ...args, event_id: event.callId });
      return [textBlock(`已记录 ${args.target} 的观察`)];
    });
    ctx.provide('vistaHistory', {
      async saveImage({ sessionId, requestId, png, ...metadata }) {
        requireScope(sessionId);
        const item = await bridge.request('save', {
          ...metadata, request_id: requestId, png: Buffer.from(png).toString('base64'),
        });
        state.pendingReceipts.set(requestId, userData(receiptSource, item.receipt));
        log({ kind: 'save', request_id: requestId, ...item });
        return item;
      },
      async recordUserFeedback({ sessionId, userSeq, target, replyToSeq, attachmentId, shortText, comparedTo }) {
        const session = requireScope(sessionId);
        const event = session.eventAt(userSeq);
        if (event?.type !== 'user/message' || event.data.source?.kind !== 'user') {
          throw new Error('用户评价需要原生用户消息事件');
        }
        if (target !== undefined && !shortId.test(target)) throw new Error('无效的图片短号');
        let replyShortIds = [];
        if (replyToSeq !== undefined) {
          if (replyToSeq >= userSeq) {
            log({ kind: 'user-feedback', user_seq: userSeq, written: false, reason: 'ambiguous-reply' });
            return false;
          }
          state.rows = await bridge.request('rows');
          replyShortIds = resolveReplyCandidates(
            rawMessage(session.eventAt(replyToSeq)), attachmentId, state.rows, target,
          );
          if (!replyShortIds.length) {
            log({ kind: 'user-feedback', user_seq: userSeq, written: false, reason: 'ambiguous-binding' });
            return false;
          }
        }
        const written = await bridge.request('user_feedback', {
          event_id: event.data.id,
          text: words(event.data).join('\n'),
          target: target ?? null,
          reply_short_ids: replyShortIds,
          short_text: shortText ?? null,
          compared_to: comparedTo ?? null,
        });
        log({ kind: 'user-feedback', user_seq: userSeq, event_id: event.data.id, written });
        return written;
      },
    });
  }

  // Scope out-of-band auditing to the exact DSH model stream. In particular a
  // compaction summarizer or provider catalog request is not a new Agent turn.
  ctx.on('llm/stream', (options, next) => {
    if (options.sessionId !== config.sessionId) return next();
    const pending = options.purpose ? undefined : state.queued;
    if (!options.purpose) state.queued = undefined;
    const details = { turn: state.turn, purpose: options.purpose ?? 'agent', pending };
    return (async function* () {
      const iterator = requestScope.run(details, () => next()[Symbol.asyncIterator]());
      try {
        while (true) {
          const result = await requestScope.run(details, () => iterator.next());
          if (result.done) return;
          if (result.value.type === 'usage') log({ kind: 'usage', ...result.value, n: details.n });
          yield result.value;
        }
      } finally {
        await iterator.return?.();
      }
    })();
  });

  const originalFetch = globalThis.fetch;
  let n = 0;
  const uploads = new Map();
  if (config.auditPath && existsSync(config.auditPath)) {
    for (const line of readFileSync(config.auditPath, 'utf8').split('\n').filter(Boolean)) {
      const event = JSON.parse(line);
      if (event.session_id !== config.sessionId) throw new Error('请求审计日志属于其他会话');
      if (event.kind === 'upload') uploads.set(event.fileId, { sha256: event.sha256 });
      if (event.kind === 'request') n = Math.max(n, event.n);
    }
  }
  const auditedFetch = async function (input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    const details = requestScope.getStore();
    if (!details) return originalFetch.call(this, input, init);
    if (url.pathname.endsWith('/files') && init?.body instanceof FormData) {
      const blobs = [...init.body.values()].filter(item => item instanceof Blob && item.type.startsWith('image/'));
      const response = await originalFetch.call(this, input, init);
      if (response.ok && blobs.length === 1) {
        const data = await response.clone().json();
        const fileId = data.id ?? data.file_id;
        if (typeof fileId === 'string') {
          const sha256 = digest(Buffer.from(await blobs[0].arrayBuffer()));
          uploads.set(fileId, { sha256 });
          log({ kind: 'upload', fileId, sha256, status: response.status });
        }
      }
      return response;
    }
    if (!url.pathname.endsWith('/messages') || typeof init?.body !== 'string') {
      return originalFetch.call(this, input, init);
    }
    const current = ++n;
    details.n = current;
    const phase = config.phasePath && existsSync(config.phasePath) ? readFileSync(config.phasePath, 'utf8').trim() : undefined;
    const body = JSON.parse(init.body);
    const wire = inspectBody(body, [], uploads, [], true);
    const catalogs = [];
    const receipts = [];
    iterText(body.messages ?? [], 'messages', (value, location) => {
      const id = knownViews.get(value);
      if (id) catalogs.push({ text: value, view_id: id, location });
      for (const row of state.rows) {
        if (value === row.receipt) receipts.push({ text: value, short_id: row.short_id, location });
      }
    });
    const truncated = (key, value) => key === 'data' && typeof value === 'string' && value.length > 256 ? '[binary omitted]' : value;
    log({
      kind: 'request', n: current, phase, turn: details.turn, purpose: details.purpose,
      ...wire, catalogs, receipts, tool_calls: callsFrom(body.messages ?? []),
      ...details.pending, injected: catalogs.some(card => card.view_id === details.pending?.injected_view_id) ? details.pending.injected : 0,
      ...(config.captureText ? { body: JSON.parse(JSON.stringify(body, truncated)) } : {}),
    });
    if (config.maxModelRequests && current > config.maxModelRequests) throw new Error('VISTA 模型请求预算已用完');
    try {
      const response = await originalFetch.call(this, input, init);
      if (response.ok && details.purpose === 'agent') {
        for (const image of wire.images) if (image.sha256) state.visibleSeen.add(image.sha256);
      }
      log({ kind: 'response', n: current, status: response.status });
      return response;
    } catch (error) {
      log({ kind: 'transport-error', n: current, error: error.name });
      throw error;
    }
  };
  globalThis.fetch = auditedFetch;
  ctx.effect(() => () => { if (globalThis.fetch === auditedFetch) globalThis.fetch = originalFetch; });
}
