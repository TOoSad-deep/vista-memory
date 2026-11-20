import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';

import { imageDigests, lostShortIds, resolveReplyCandidates } from '../scripts/vista_history_dsh.mjs';

test('native offload drops an occurrence while its underlying event retains the reference', () => {
  const data = Buffer.from('fixture pixels').toString('base64');
  const hash = createHash('sha256').update('fixture pixels').digest('hex');
  const message = { content: [{ type: 'image', data, offloaded: true }] };
  assert.deepEqual([...imageDigests(message)], []);
  assert.deepEqual([...imageDigests(message, { includeOffloaded: true })], [hash]);
});

test('only archived records losing both their image and the last usable clue need delivery', () => {
  const rows = [
    { short_id: 'R1', sha256: 'outside-history' },
    { short_id: 'R2', sha256: 'retained-image' },
    { short_id: 'R3', sha256: 'retained-receipt' },
    { short_id: 'R4', sha256: 'group-coverage' },
    { short_id: 'R5', sha256: 'lost-image-and-clue' },
  ];
  const lost = lostShortIds(rows,
    new Set(['R2', 'R3', 'R4', 'R5']), new Set(['retained-image']),
    new Set(['R3']), new Set(['R4']));
  assert.deepEqual(lost, ['R5']);
});

test('a verified reply can be disambiguated by an explicit short ID', () => {
  const sha256 = 'a'.repeat(64);
  const attachmentId = `sha256:${sha256}`;
  const image = { type: 'image', attachment: { attachmentId } };
  const message = { content: [image] };
  const rows = [
    { short_id: 'R2', sha256 },
    { short_id: 'R3', sha256 },
    { short_id: 'R4', sha256: 'b'.repeat(64) },
  ];
  assert.deepEqual(resolveReplyCandidates(message, attachmentId, rows, 'R2'), ['R2']);
  assert.deepEqual(resolveReplyCandidates(message, attachmentId, rows, 'R4'), []);
  assert.deepEqual(resolveReplyCandidates(message, attachmentId, rows), ['R2', 'R3']);
  assert.deepEqual(resolveReplyCandidates(message, undefined, rows, 'R2'), []);
  assert.deepEqual(resolveReplyCandidates({ content: [image, image] }, attachmentId, rows, 'R2'), []);
});
