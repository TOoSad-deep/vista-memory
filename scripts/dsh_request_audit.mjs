import { createHash } from 'node:crypto';
import { appendFileSync, readFileSync } from 'node:fs';
import { controlImagePayload } from './shader_recall_image_control.mjs';
import { VistaBridge } from './shader_recall_runtime.mjs';

export const inject = ['tools'];

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function inspectBody(body, expectedCodes, uploadedImages = new Map(), expectedMetadata = [], traceSources = false) {
  const images = [];
  const matchedCodes = new Set();
  const matchedMetadata = new Set();
  const codeSources = [];

  function visit(value, location, toolCallId) {
    if (typeof value === 'string') {
      const match = /^data:(image\/[^;]+);base64,([A-Za-z0-9+/=\s]+)$/.exec(value);
      if (match) {
        const bytes = Buffer.from(match[2], 'base64');
        const image = { location, mimeType: match[1], bytes: bytes.length, sha256: sha256(bytes) };
        const inputIndex = /^input\[(\d+)\]/.exec(location);
        const parent = inputIndex && (body.input ?? body.messages)?.[Number(inputIndex[1])];
        if (parent) {
          image.originType = parent.type;
          image.originRole = parent.role;
        }
        if (bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) && bytes.length >= 24) {
          image.width = bytes.readUInt32BE(16);
          image.height = bytes.readUInt32BE(20);
        }
        if (traceSources && toolCallId) image.toolCallId = toolCallId;
        images.push(image);
      }
      for (const code of expectedCodes) {
        if (code && value.includes(code)) {
          const digest = sha256(code);
          matchedCodes.add(digest);
          if (traceSources) codeSources.push({ sha256: digest, location, toolCallId });
        }
      }
      for (const metadata of expectedMetadata) {
        if (metadata && value.includes(metadata)) matchedMetadata.add(sha256(metadata));
      }
    } else if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, `${location}[${index}]`, toolCallId));
    } else if (value && typeof value === 'object') {
      if (value.type === 'tool_result') toolCallId = value.tool_use_id;
      if (value.type === 'function_call_output') toolCallId = value.call_id;
      if (value.role === 'tool') toolCallId = value.tool_call_id;
      if (value.type === 'image' && value.source?.type === 'file') {
        const fileId = value.source.file_id;
        const upload = uploadedImages.get(fileId);
        const image = { location, fileId, ...(upload || { unresolvedFile: true }) };
        const index = /^input\[(\d+)\]/.exec(location);
        const parent = index && (body.input ?? body.messages)?.[Number(index[1])];
        if (parent) { image.originType = parent.type; image.originRole = parent.role; }
        if (traceSources && toolCallId) image.toolCallId = toolCallId;
        images.push(image);
      }
      if (value.type === 'image' && value.source?.type === 'base64') {
        const bytes = Buffer.from(value.source.data, 'base64');
        const image = { location, mimeType: value.source.media_type, bytes: bytes.length, sha256: sha256(bytes) };
        const index = /^input\[(\d+)\]/.exec(location);
        const parent = index && (body.input ?? body.messages)?.[Number(index[1])];
        if (parent) { image.originType = parent.type; image.originRole = parent.role; }
        if (bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
          image.width = bytes.readUInt32BE(16); image.height = bytes.readUInt32BE(20);
        }
        if (traceSources && toolCallId) image.toolCallId = toolCallId;
        images.push(image);
      }
      for (const [key, item] of Object.entries(value)) visit(item, `${location}.${key}`, toolCallId);
    }
  }

  visit(body.input ?? body.messages ?? [], 'input');
  return {
    model: body.model,
    tools: (body.tools ?? []).map(tool => tool.name ?? tool.function?.name),
    images,
    includedCodeSha256: [...matchedCodes],
    ...(traceSources ? { codeSources } : {}),
    includedMetadataSha256: [...matchedMetadata],
    maxOutputTokens: body.max_output_tokens ?? body.max_tokens ?? body.max_completion_tokens,
    reasoningEffort: body.reasoning?.effort ?? body.reasoning_effort,
    configurationReasoningEfforts: (body.input ?? [])
      .filter(item => item.type === 'configuration_update')
      .map(item => item.reasoning?.effort),
  };
}

// Observe provider payloads after image admission, including the numbered file
// view used by the author's ordinary read tool. Store hashes and locations only.
export function inspectTrigger(body, expected) {
  const matches = [];
  function visit(value, location, callId) {
    if (Array.isArray(value)) {
      value.forEach((v, i) => visit(v, `${location}[${i}]`, callId));
    } else if (value && typeof value === 'object') {
      if (value.type === 'tool_result') callId = value.tool_use_id;
      if (value.type === 'function_call_output') callId = value.call_id;
      if (value.role === 'tool') callId = value.tool_call_id;
      for (const [k, v] of Object.entries(value)) visit(v, `${location}.${k}`, callId);
    } else if (typeof value === 'string') {
      const kinds = [];
      if (value === expected.feedback) kinds.push('literal-feedback');
      if (expected.cue && value.includes(expected.cue)) kinds.push('host-cue');
      if (value.includes(expected.policy)) kinds.push('author-policy');
      if (value.includes('Execution budget for this task. Model requests remaining')) kinds.push('test-budget');
      if (callId) {
        const display = /<content>\s*\n([\s\S]*?)(?:<\/content>|$)/.exec(value);
        const text = display ? display[1].split('\n').filter(l => /^\s*\d+:/.test(l))
          .map(l => l.replace(/^\s*\d+: ?/, '')).join('\n') : value;
        if ((text + '\n').includes(expected.code)) kinds.push('full-target-glsl');
        const candidates = [];
        try {
          const obj = JSON.parse(text.trim());
          if (obj && typeof obj === 'object') candidates.push(obj, ...(Array.isArray(obj.candidates) ? obj.candidates : []));
        } catch { /* An MCP result starts with one JSON metadata line followed by its GLSL. */ }
        try { candidates.push(JSON.parse(text.trim().split('\n')[0])); } catch { /* Plain source or prose. */ }
        const render = expected.render;
        if (candidates.some(obj => {
          if (!obj || typeof obj !== 'object') return false;
          if ('candidateId' in obj && (obj.via === 'vista' || obj.via === 'candidate-files')) {
            return obj.candidateId === expected.candidate && obj.imageHash === expected.png &&
              obj.sourceHash === expected.glsl && (obj.via !== 'vista' || obj.record === expected.record) &&
              obj.render && ['time', 'width', 'height'].every(k => obj.render[k] === render[k]) &&
              obj.render.uniforms && Object.keys(obj.render.uniforms).length === Object.keys(render.uniforms).length &&
              Object.entries(render.uniforms).every(([k,v]) => obj.render.uniforms[k] === v);
          }
          const vista = obj.record === expected.record;
          if (!vista && (obj.id !== expected.candidate || obj.imageHash !== expected.png || obj.sourceHash !== expected.glsl)) return false;
          const fields = vista ? obj.render : obj;
          if (!fields || !['time', 'width', 'height'].every(k => fields[k] === render[k])) return false;
          const uniforms = vista ? fields.uniforms : Object.fromEntries((Array.isArray(obj.params) ? obj.params : []).map(p => [p.name, p.default]));
          return uniforms && Object.keys(uniforms).length === Object.keys(render.uniforms).length
            && Object.entries(render.uniforms).every(([k,v]) => uniforms[k] === v);
        })) kinds.push('target-render-parameters');
      }
      // A shell result can contain multiple records or a partial metadata view.
      // Retain its exact wire fingerprint even if no structured matcher fired.
      if (expected.fingerprintToolText && callId && /\.(text|output|content)$/.test(location)) {
        kinds.push('tool-result-text');
      }
      if (kinds.length) matches.push({ kinds, location, callId, textSha256: sha256(value) });
    }
  }
  visit(body.input ?? body.messages ?? [], 'input');
  if (body.system) visit(body.system, 'system');
  if (body.instructions) visit(body.instructions, 'instructions');
  return matches;
}

export function apply(ctx, config) {
  JSON.parse(readFileSync(config.expectedCodesPath, 'utf8'));
  const originalFetch = globalThis.fetch;
  let requestId = 0;
  let activeSession;
  const uploadedImages = new Map();
  const uploadedFiles = new Map();
  let modelRequests = 0;
  let masker;
  const write = event => appendFileSync(config.outputPath, JSON.stringify({ time: new Date().toISOString(), ...event }) + '\n');

  ctx.on('agent/created', ({ agent }) => {
    activeSession = agent.id;
    write({ type: 'tools', sessionId: agent.id, tools: ctx.tools.schemas(agent).map(tool => tool.name) });
  });

  const auditedFetch = async function (input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    const currentRequest = ++requestId;
    let body;
    if (typeof init?.body === 'string') body = init.body;
    else if (input instanceof Request) body = await input.clone().text();

    let audit = { type: 'request', requestId: currentRequest, sessionId: activeSession, pathname: url.pathname };
    let projectedInit = init;
    const upload = [];
    const uploadData = [];
    if (init?.body instanceof FormData) {
      for (const value of init.body.values()) {
        if (!(value instanceof Blob) || !value.type.startsWith('image/')) continue;
        const bytes = Buffer.from(await value.arrayBuffer());
        const metadata = { mimeType: value.type, bytes: bytes.length, sha256: sha256(bytes) };
        if (bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
          metadata.width = bytes.readUInt32BE(16); metadata.height = bytes.readUInt32BE(20);
        }
        upload.push(metadata);
        uploadData.push({ data: bytes, mediaType: value.type });
      }
    }
    if (body) {
      try {
        let parsed = JSON.parse(body);
        let projection;
        if (parsed.model && !config.policyPath && config.maxModelRequests && ++modelRequests > config.maxModelRequests) {
          throw new Error('Model request budget exceeded');
        }
        if (parsed.model && config.policyPath) {
          const policy = JSON.parse(readFileSync(config.policyPath, 'utf8'));
          if (++modelRequests > policy.maxModelRequests) throw new Error('Model request budget exceeded');
          projection = await controlImagePayload(parsed, {
            condition: policy.condition, bindings: policy.bindings, files: uploadedFiles,
            maskImage: async (image, regions) => {
              masker ||= new VistaBridge(config.python);
              const result = await masker.request({ action: 'mask', data: image.data.toString('base64'), regions });
              return { data: Buffer.from(result.data, 'base64'), mediaType: result.media_type };
            },
          });
          parsed = projection.body;
          if (projection.audit.some(row => row.disposition !== 'visible')) projectedInit = { ...init, body: JSON.stringify(parsed) };
        }
        const metadata = config.expectedMetadataPath ? JSON.parse(readFileSync(config.expectedMetadataPath, 'utf8')) : [];
        const codes = JSON.parse(readFileSync(config.expectedCodesPath, 'utf8'));
        audit = { ...audit, ...inspectBody(parsed, codes, uploadedImages, metadata, config.traceSources) };
        if (parsed.model && config.triggerEvidencePath) {
          audit.triggerEvidence = inspectTrigger(parsed, JSON.parse(readFileSync(config.triggerEvidencePath, 'utf8')));
        }
        if (projection) {
          audit.imageControl = projection.audit;
          const visible = projection.audit.filter(row => row.disposition !== 'withheld');
          audit.images.forEach((entry, index) => { entry.source = visible[index].source; });
        }
      } catch (error) {
        if (config.policyPath || error.message === 'Model request budget exceeded') {
          write({ type: 'image-control-error', requestId: currentRequest, errorType: error.name, message: error.message });
          throw error;
        }
        audit.bodyParseFailed = true;
      }
    }
    write(audit);

    try {
      const response = await originalFetch.call(this, input, projectedInit);
      if (upload.length) {
        const receipt = await response.clone().json();
        const fileId = receipt.id ?? receipt.file_id;
        if (response.ok && typeof fileId === 'string' && upload.length === 1) {
          uploadedImages.set(fileId, upload[0]);
          uploadedFiles.set(fileId, uploadData[0]);
          write({ type: 'image-upload', requestId: currentRequest, fileId, ...upload[0] });
        } else write({ type: 'unresolved-image-upload', requestId: currentRequest, status: response.status });
      }
      write({ type: 'response', requestId: currentRequest, status: response.status });
      return response;
    } catch (error) {
      write({ type: 'transport-error', requestId: currentRequest, errorType: error?.name ?? 'Error' });
      throw error;
    }
  };
  globalThis.fetch = auditedFetch;

  ctx.on('dispose', () => {
    if (globalThis.fetch === auditedFetch) globalThis.fetch = originalFetch;
  });
  ctx.effect(() => async () => { await masker?.close(); });
}
