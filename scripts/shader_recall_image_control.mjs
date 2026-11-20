import { createHash } from 'node:crypto';

export const imageUnavailable = 'Historical image unavailable in this condition.';
const digest = data => createHash('sha256').update(data).digest('hex');

function imageValue(block, files) {
  if (block.type === 'image' && block.source?.type === 'base64') {
    return { data: Buffer.from(block.source.data, 'base64'), mediaType: block.source.media_type };
  }
  if (block.type === 'image' && block.source?.type === 'file') {
    const file = files.get(block.source.file_id);
    if (!file) throw new Error('Outbound image file has no audited upload');
    return file;
  }
  const url = block.type === 'input_image' ? block.image_url :
    block.type === 'image_url' ? block.image_url?.url : undefined;
  if (typeof url !== 'string') return null;
  const match = /^data:(image\/[^;]+);base64,([A-Za-z0-9+/=\s]+)$/.exec(url);
  if (!match) throw new Error('Outbound image URL has no audited inline content');
  return { data: Buffer.from(match[2], 'base64'), mediaType: match[1] };
}

function textReplacement(block) {
  return { type: block.type === 'input_image' ? 'input_text' : 'text', text: imageUnavailable };
}

function maskedReplacement(block, masked) {
  if (block.type === 'image') return {
    type: 'image', source: { type: 'base64', media_type: masked.mediaType, data: masked.data.toString('base64') },
  };
  const url = `data:${masked.mediaType};base64,${masked.data.toString('base64')}`;
  if (block.type === 'input_image') return { ...block, image_url: url };
  return { ...block, image_url: { ...block.image_url, url } };
}

/** Project an immutable serialized request using the admitted image order.
 * Each binding comes from the actual attachment request variant and carries
 * its source provenance. Equal image bytes can have different source roles.
 */
export async function controlImagePayload(body, {
  condition, bindings, files = new Map(), maskImage,
}) {
  if (!['text', 'visual'].includes(condition)) throw new Error('Unknown historical-image condition');
  const audit = [];
  const maskedImages = new WeakSet();
  let nextImage = 0;
  async function visit(value, location) {
    if (Array.isArray(value)) {
      const result = [];
      for (let i = 0; i < value.length; i++) {
        const projected = await visit(value[i], `${location}[${i}]`);
        result.push(projected);
        if (projected && typeof projected === 'object' && maskedImages.has(projected)) result.push(textReplacement(value[i]));
      }
      return result;
    }
    if (!value || typeof value !== 'object') return value;
    const image = imageValue(value, files);
    if (image) {
      const binding = bindings[nextImage++];
      if (!binding || !binding.origin) throw new Error(`Outbound image lacks provenance at ${location}`);
      const sha256 = digest(image.data);
      if (binding.sha256 !== sha256) throw new Error(`Outbound image differs from admitted attachment at ${location}`);
      const row = { location, source: binding.origin, sha256, disposition: 'visible' };
      audit.push(row);
      if (condition === 'visual' || binding.common) return structuredClone(value);
      if (binding.regions) {
        const hidden = binding.regions.filter(region => !region.common);
        if (hidden.length === 0) return structuredClone(value);
        if (!maskImage) throw new Error('Composite historical image requires a verified masker');
        const masked = await maskImage(image, hidden);
        if (!Buffer.isBuffer(masked.data) || !masked.mediaType?.startsWith('image/')) throw new Error('Invalid masked image result');
        row.disposition = 'masked';
        row.maskedRegions = hidden;
        row.outputSha256 = digest(masked.data);
        const replacement = maskedReplacement(value, masked);
        maskedImages.add(replacement);
        return replacement;
      }
      row.disposition = 'withheld';
      return textReplacement(value);
    }
    const result = {};
    for (const [key, child] of Object.entries(value)) result[key] = await visit(child, `${location}.${key}`);
    return result;
  }
  const projected = await visit(body, 'request');
  if (nextImage !== bindings.length) throw new Error('Serialized image count differs from admitted image bindings');
  return { body: projected, audit };
}

/** Bind tool-result images to their actual source, including normalized
 * attachment aliases. Resolve provenance by call or path before byte hashes.
 */
export class ImageProvenance {
  constructor() {
    this.paths = new Map();
    this.attachments = new Map();
    this.calls = new Map();
  }

  registerPath(path, origin) { this.paths.set(path, structuredClone(origin)); }

  recordCall(callId, name, args) {
    this.calls.set(callId, { name, args: structuredClone(args) });
  }

  recordImageResult(callId, message, path, recordOrigin) {
    const origin = recordOrigin || this.paths.get(path);
    if (!origin) throw new Error('Tool image source is not registered');
    for (const block of message.content) {
      if (block.type === 'image') this.attachments.set(`${callId}:${block.attachment.attachmentId}`, structuredClone(origin));
    }
  }

  forAttachment(message, attachment) {
    const key = `${message.toolCallId}:${attachment.attachmentId}`;
    const found = this.attachments.get(key);
    if (!found) throw new Error('Attachment has no source-bound tool result');
    return structuredClone(found);
  }
}
