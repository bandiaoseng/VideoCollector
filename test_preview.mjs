import test from 'node:test';
import assert from 'node:assert/strict';
import {createDestinationPreview} from './web/preview.mjs';

function fixture() {
  const state = {id: 'first', note: '点评 #二创'}, renders = [], requests = [], timers = new Map();
  let nextTimer = 0;
  const controller = createDestinationPreview({
    getDraftId: () => state.id, getNote: () => state.note,
    show: (text, adjusted = false) => renders.push({text, adjusted}),
    request: note => new Promise((resolve, reject) => requests.push({note, resolve, reject})),
    schedule: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    unschedule: id => timers.delete(id)
  });
  function send() { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()); }
  return {state, renders, requests, controller, send};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('debounces typing and displays the authoritative filename without transforming it', async () => {
  const f = fixture();
  f.controller.update();
  f.state.note = 'CON #二创'; f.controller.update(); f.send();
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].note, 'CON #二创');
  f.requests[0].resolve({category: '二创', filename: '_CON.mp4', adjusted: true}); await settle();
  assert.deepEqual(f.renders.at(-1), {text: '二创 / _CON.mp4', adjusted: true});
});

test('late result for an older note cannot overwrite a newer result', async () => {
  const f = fixture(); f.controller.update(); f.send();
  f.state.note = '新点评 #二创'; f.controller.update(); f.send();
  f.requests[1].resolve({category: '二创', filename: '新点评.mp4'}); await settle();
  f.requests[0].resolve({category: '二创', filename: '旧点评.mp4'}); await settle();
  assert.equal(f.renders.at(-1).text, '二创 / 新点评.mp4');
});

test('switching videos rejects an old result even if the note is identical', async () => {
  const f = fixture(); f.controller.update(); f.send();
  f.state.id = 'second'; f.controller.update();
  f.requests[0].resolve({category: '二创', filename: '旧视频.mp4'}); await settle();
  assert.equal(f.renders.at(-1).text, '正在确认文件名…');
});

test('reset and blank input invalidate both pending requests and timers', async () => {
  const f = fixture(); f.controller.update(); f.send();
  f.controller.cancel();
  const previousCount = f.renders.length;
  f.requests[0].resolve({category: '二创', filename: '旧点评.mp4'}); await settle();
  assert.equal(f.renders.length, previousCount);
  f.controller.update(); f.state.note = ' '; f.controller.update(); f.send();
  assert.equal(f.requests.length, 1);
  assert.equal(f.renders.at(-1).text, '写好点评与 #类目后显示');
});

test('current error replaces old destination, while an outdated error is ignored', async () => {
  const f = fixture(); f.controller.update(); f.send();
  f.state.note = '另一句点评 #二创'; f.controller.update(); f.send();
  f.requests[1].reject(new Error('暂时无法确认文件名')); await settle();
  assert.deepEqual(f.renders.at(-1), {text: '暂时无法确认文件名', adjusted: false});
  f.requests[0].reject(new Error('旧错误')); await settle();
  assert.equal(f.renders.at(-1).text, '暂时无法确认文件名');
});
