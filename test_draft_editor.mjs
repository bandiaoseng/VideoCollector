import test from 'node:test';
import assert from 'node:assert/strict';
import {NoteEditor,DraftCache} from './web/draft_editor.mjs';

function memory(){
  const values=new Map();
  return {get:key=>values.get(key)??null,set:(key,value)=>values.set(key,value),remove:key=>values.delete(key),keys:()=>[...values.keys()]};
}

test('offline copies from separate tabs survive writes and acknowledgements in the other tab',()=>{
  const storage=memory(),first=new DraftCache(storage,'first'),second=new DraftCache(storage,'second');
  const base={id:'video',note:'已保存',version:1};
  const a=new NoteEditor(base),b=new NoteEditor(base);a.note='第一页离线文字';b.note='第二页离线文字';
  first.save(a);second.save(b);
  assert.deepEqual(new Set(first.copies('video').map(c=>c.note)),new Set([a.note,b.note]));
  first.clear(a);
  assert.equal(second.current('video').note,b.note);
  assert.ok(second.copies('video').some(copy=>copy.owner==='second'&&copy.note===b.note));
});

test('old unversioned cache is preserved and requires a choice when server text differs',()=>{
  const storage=memory();storage.set('collector.note.video','升级前离线点评');
  const cache=new DraftCache(storage,'new-page'),server={id:'video',note:'服务器较新点评',version:3};
  const editor=new NoteEditor(server,cache.current('video'));
  assert.equal(editor.conflict,server);assert.equal(editor.note,'升级前离线点评');
  cache.save(editor);
  assert.ok(cache.copies('video').some(copy=>copy.owner==='legacy'&&copy.note==='升级前离线点评'));
  editor.resolve(false);assert.equal(editor.note,server.note);assert.equal(editor.version,3);
});

test('an old save acknowledgement cannot dismiss a newer remote edit conflict',()=>{
  const editor=new NoteEditor({id:'video',version:0,note:''});editor.note='本机最新输入';
  editor.observe({id:'video',version:2,note:'另一页更晚的输入'});
  editor.observe({id:'video',version:1,note:'旧列表返回的文字'});
  editor.accept({id:'video',version:1,note:'本机较早输入'});
  assert.equal(editor.conflict.version,2);assert.equal(editor.note,'本机最新输入');
  editor.resolve(true);assert.equal(editor.version,2);assert.equal(editor.note,'本机最新输入');
});

test('a draft cancelled in another page is closed even without a queued task identifier',()=>{
  const editor=new NoteEditor({id:'video',version:0,note:'',state:'awaiting_note'});editor.note='未保存的点评';
  editor.observe({id:'video',version:1,note:'',state:'cancelled',task_id:null});
  assert.equal(editor.conflict.state,'cancelled');
  assert.throws(()=>editor.resolve(true),/已经提交或取消/);
  assert.equal(editor.note,'未保存的点评');
});
