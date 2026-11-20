import assert from 'node:assert/strict';
import { test } from 'node:test';

import { apply } from '../scripts/vista_history_daily_entry.mjs';

test('daily entry saves admitted pixels and records explicit feedback from the real user event', async () => {
  const calls = [];
  const hooks = new Map();
  const png = new Uint8Array([1, 2, 3]);
  const ref = { mediaType: 'image/png', attachmentId: 'sha256:stored' };
  const note = {
    id: 'feedback', source: { kind: 'user' },
    content: [{ type: 'text', text: '评价 R1：我认可这版' }],
  };
  const agent = {
    id: 'session',
    session: {
      seq: 9,
      snapshotEvents: fromSeq => {
        assert.equal(fromSeq, 9);
        return [{ type: 'user/message', seq: 12, data: note }];
      },
    },
  };
  apply({
    on: (name, hook) => hooks.set(name, hook),
    attachments: {
      readImage: async attachment => {
        assert.equal(attachment, ref);
        return { ref, data: png };
      },
    },
    get: name => {
      assert.equal(name, 'vistaHistory');
      return {
        saveImage: async action => {
          calls.push(action);
          return { short_id: 'R1', receipt: '已存 R1 · 初稿' };
        },
        recordUserFeedback: async action => {
          calls.push(action);
          return true;
        },
      };
    },
  }, { sessionId: 'session' });

  const save = {
    id: 'save', source: { kind: 'user' },
    content: [{ type: 'text', text: '保存图片：初稿\n回复收到' }, { type: 'image', attachment: ref }],
  };
  const ignored = { ...save, source: { kind: 'compact-checkpoint' } };
  const ordinary = { ...save, content: [{ type: 'text', text: '下一步用 R1' }] };
  const messages = [save, note, ignored, ordinary];
  const next = () => ({ kind: 'enter', messages });
  const decision = await hooks.get('agent/pre-step')({ agent, messages }, next);
  assert.equal(decision.messages, messages);
  assert.deepEqual(calls, [{
    sessionId: 'session', requestId: 'dsh:session:save', png, label: '初稿', source_agent: 'native-dsh',
  }]);
  await hooks.get('agent/turn-stopping')({ agent });
  await hooks.get('agent/turn-stopping')({ agent });
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], { sessionId: 'session', userSeq: 12, target: 'R1' });
});
