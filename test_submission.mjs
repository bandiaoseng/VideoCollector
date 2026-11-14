// Execute the actual page module against a tiny DOM and an offline API double.
// These are browser-logic tests, not Android or real cloud acceptance tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setImmediate as settle} from 'node:timers/promises';

let instance = 0;
class Element {
  constructor() {
    this.children=[]; this.value=''; this.hidden=false; this.disabled=false; this.textContent='';
    this.classes=new Set();
    this.classList={toggle:(key,on)=>on?this.classes.add(key):this.classes.delete(key)};
  }
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(name,callback){this['on'+name]=callback;}
  scrollIntoView(){}
  focus(){}
}
const text = node => node.textContent + node.children.map(text).join(' ');
const event = () => ({preventDefault(){}, submitter:new Element()});

async function page() {
  const elements=new Map(), cache=new Map(), drafts=new Map(), tasks=new Map(), submissions=[], reviews=[], parses=[];
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const controls={failRefresh:false, failAfterCreate:false, failAfterSubmit:false, loseSubmitReply:false, holdList:false};
  const response=(data,status=200)=>({ok:status<400,status,json:async()=>structuredClone(data)});
  globalThis.document={getElementById:get, createElement:()=>new Element(),hidden:false};
  globalThis.window={addEventListener(){},confirm(){return !!controls.confirmReview;}};
  globalThis.localStorage={getItem:key=>cache.get(key)??null,setItem:(key,value)=>cache.set(key,value),removeItem:key=>cache.delete(key),get length(){return cache.size;},key:index=>[...cache.keys()][index]??null};
  globalThis.location={search:''}; globalThis.history={replaceState(){}};
  Object.defineProperty(globalThis,'navigator',{value:{},configurable:true});
  globalThis.setInterval=()=>0; globalThis.setTimeout=()=>0; globalThis.clearTimeout=()=>{};
  globalThis.fetch=async(path,options)=>{
    const method=options?.method||'GET', body=options?.body?JSON.parse(options.body):null;
    if(path==='/api/status') {
      if(controls.failRefresh)throw new Error('offline');
      return response({categories:['二创'],worker_online:true,quark_ready:true,platforms:{douyin:true,wechat:false}});
    }
    if(path==='/api/drafts'&&method==='GET') {
      const snapshot=structuredClone([...drafts.values()]);
      if(controls.holdList){
        controls.holdList=false;
        return await new Promise(resolve=>{controls.releaseList=()=>resolve(response({items:snapshot,has_more:false}));});
      }
      return response({items:snapshot,has_more:false});
    }
    if(path==='/api/drafts'&&method==='POST') {
      if(!drafts.has(body.id))drafts.set(body.id,{id:body.id,source:'https://v.douyin.com/offline/',share_text:body.text,
        platform:'douyin',state:'awaiting_note',note:'',version:0,created:1});
      controls.failRefresh=controls.failAfterCreate;
      return response(drafts.get(body.id));
    }
    const saved=path.match(/^\/api\/drafts\/([^/]+)$/);
    if(saved&&method==='PATCH'){
      const draft=drafts.get(saved[1]);
      if(body.version!==draft.version&&body.note!==draft.note)return response({detail:'点评已在其他页面更新',code:'draft_conflict',current:draft},409);
      const updated={...draft,note:body.note,version:draft.version+(body.note!==draft.note?1:0)};
      drafts.set(updated.id,updated);return response(updated);
    }
    const revision=path.match(/^\/api\/drafts\/([^/]+)\/revise$/);
    if(revision){
      let original=drafts.get(revision[1]);
      if(controls.reviseRace){
        original={...original,state:'cancelled'};drafts.set(original.id,original);
        return response({detail:'只有尚未上传的同名冲突记录可以修改',code:'cannot_revise',current:original},409);
      }
      let child=original.revision_id?drafts.get(original.revision_id):null;
      if(!child){
        child={...original,id:crypto.randomUUID(),state:'awaiting_note',task_id:null,filename:null,category:null,version:0,revision_of:original.id,revision_id:null};
        original={...original,state:'superseded',revision_id:child.id};drafts.set(original.id,original);drafts.set(child.id,child);
      }
      if(controls.loseRevisionReply){controls.loseRevisionReply=false;throw new Error('revision response lost');}
      controls.failRefresh=!!controls.failAfterRevision;return response({original,draft:child});
    }
    const cancelled=path.match(/^\/api\/drafts\/([^/]+)\/cancel$/);
    if(cancelled){
      const draft=drafts.get(cancelled[1]);
      if(controls.cancelRace){
        const current={...draft,state:'uploading'};drafts.set(current.id,current);
        return response({detail:'已进入上传阶段，不能保证撤回',code:'cannot_cancel',current},409);
      }
      const current={...draft,state:draft.state==='preparing'?'cancel_requested':'cancelled',version:draft.version+1};
      drafts.set(current.id,current);controls.failRefresh=!!controls.failAfterCancel;return response(current);
    }
    const review=path.match(/^\/api\/drafts\/([^/]+)\/review$/);
    if(review){
      reviews.push(body);
      const draft=drafts.get(review[1]);
      if(body.confirmation){
        const result={...draft,state:'succeeded',review_note:'已确认，没有重复上传'};
        drafts.set(result.id,result);return response(result);
      }
      return response({...draft,review_note:'请核对内容',review_candidate:{full_path:'夸克网盘/二创/测试点评.mp4',filename:'测试点评.mp4',bytes:100},review_confirmation:'a'.repeat(64)});
    }
    const reparse=path.match(/^\/api\/drafts\/([^/]+)\/reparse$/);
    if(reparse){
      parses.push(body);
      const result={...drafts.get(reparse[1]),state:'queued',parse_retry_required:false};
      drafts.set(result.id,result);return response(result);
    }
    const match=path.match(/^\/api\/drafts\/([^/]+)\/submit$/);
    if(match){
      submissions.push({id:match[1],note:body.note});
      const draft=drafts.get(match[1]);
      if(draft.state==='awaiting_note'&&body.version!==draft.version&&body.note!==draft.note)return response({detail:'点评已在其他页面更新',code:'draft_conflict',current:draft},409);
      tasks.set(match[1],body.note);
      const item={...draft,state:controls.submitState||'queued',note:body.note,version:draft.version+(body.note!==draft.note?1:0),task_id:'offline-task',filename:'测试点评.mp4',category:'二创'};
      drafts.set(item.id,item);
      if(controls.loseSubmitReply){controls.loseSubmitReply=false;throw new Error('reply lost after commit');}
      controls.failRefresh=controls.failAfterSubmit;
      return response(item);
    }
    throw new Error('Unexpected offline test request: '+path);
  };
  get('note-panel').hidden=true;
  await import(new URL('./web/app.js?test='+ ++instance,import.meta.url));
  await settle();
  async function share(){get('source').value='https://v.douyin.com/offline/';await get('source-form').onsubmit(event());}
  async function submit(){get('note').value='测试点评 #二创';await get('note-form').onsubmit(event());}
  function button(node,labels){if(labels.includes(node.textContent))return node;return node.children.map(child=>button(child,labels)).find(Boolean);}
  async function cancel(){const action=button(get('items'),['取消收藏','停止并取消']);assert.ok(action);await action.onclick();}
  async function revise(){const action=button(get('items'),['修改点评或类目','查看修改记录']);assert.ok(action);await action.onclick();}
  return {get,cache,drafts,tasks,submissions,reviews,parses,controls,share,submit,cancel,revise,button};
}

test('ambiguous parse requires explicit cost confirmation and sends a bound one-shot approval',async()=>{
  const p=await page();await p.share();await p.submit();
  const draft=[...p.drafts.values()][0];
  p.drafts.set(draft.id,{...draft,state:'prepare_failed',parse_retry_required:true,parse_retry_token:'b'.repeat(64)});
  await p.get('refresh').onclick();
  assert.equal(p.button(p.get('items'),['重试']),undefined);
  assert.match(text(p.get('items')),/无法确定是否已扣额度/);
  await p.button(p.get('items'),['重新尝试解析']).onclick();
  assert.equal(p.parses.length,0);
  p.controls.confirmReview=true;
  await p.button(p.get('items'),['重新尝试解析']).onclick();
  assert.deepEqual(p.parses,[{attempt_token:'b'.repeat(64),approved:true}]);
  assert.equal(p.submissions.length,1);
});

test('read-only candidate survives list refresh and confirmation requires explicit acceptance',async()=>{
  const p=await page();await p.share();p.controls.submitState='needs_review';await p.submit();
  await p.button(p.get('items'),['核对夸克结果']).onclick();
  assert.equal(p.reviews.length,1);
  assert.match(text(p.get('items')),/待核对文件/);
  await p.button(p.get('items'),['我已核对，标记完成']).onclick();
  assert.equal(p.reviews.length,1);
  p.controls.confirmReview=true;
  await p.button(p.get('items'),['我已核对，标记完成']).onclick();
  assert.equal(p.reviews.length,2);
  assert.equal(p.reviews[1].confirmation,'a'.repeat(64));
  assert.match(text(p.get('items')),/已存入夸克/);
  assert.equal(p.submissions.length,1);
});

test('accepted link opens its note editor even if the next refresh fails',async()=>{
  const p=await page();p.controls.failAfterCreate=true;
  await p.share();
  assert.equal(p.get('note-panel').hidden,false);
  assert.match(p.get('message').textContent,/视频链接已收到/);
  assert.match(p.get('message').textContent,/不用重复提交/);
  assert.equal(p.cache.has('collector.pending-share'),false);
  assert.equal(p.drafts.size,1);
  p.controls.failRefresh=false;
  await p.submit();
  assert.equal(p.tasks.size,1);
});

test('accepted submission stays accepted and visible when list refresh fails',async()=>{
  const p=await page();await p.share();p.controls.failAfterSubmit=true;
  await p.submit();
  const id=[...p.drafts.keys()][0];
  assert.match(p.get('message').textContent,/已提交/);
  assert.match(p.get('message').textContent,/不用重复提交/);
  assert.equal(p.get('message').classes.has('error'),false);
  assert.equal(p.get('note-panel').hidden,true);
  assert.equal(p.cache.has('collector.note.'+id),false);
  assert.match(text(p.get('items')),/已提交 · 等待处理/);
  await p.get('refresh').onclick();
  assert.match(p.get('message').textContent,/已提交的任务会保留/);
  assert.equal(p.submissions.length,1);
});

test('lost submit reply preserves the note and retries the same draft identity',async()=>{
  const p=await page();await p.share();p.controls.loseSubmitReply=true;
  await p.submit();
  const id=[...p.drafts.keys()][0];
  assert.equal(JSON.parse(p.cache.get('collector.note.'+id)).note,'测试点评 #二创');
  assert.equal(p.get('note-panel').hidden,false);
  assert.equal(p.get('message').classes.has('error'),true);
  await p.submit();
  assert.deepEqual(p.submissions.map(row=>row.id),[id,id]);
  assert.equal(p.tasks.size,1);
  assert.equal(p.cache.has('collector.note.'+id),false);
});

test('a list request started before acceptance cannot restore an outdated draft',async()=>{
  const p=await page();await p.share();p.controls.holdList=true;
  const staleRefresh=p.get('refresh').onclick();await settle();
  assert.equal(typeof p.controls.releaseList,'function');
  await p.submit();
  p.controls.releaseList();await staleRefresh;
  assert.match(text(p.get('items')),/已提交 · 等待处理/);
  assert.doesNotMatch(text(p.get('items')),/补充点评 →/);
});

test('a repeated submission already awaiting review is not described as automatically continuing',async()=>{
  const p=await page();await p.share();p.controls.submitState='needs_review';
  await p.submit();
  assert.match(p.get('message').textContent,/上传结果仍待核对/);
  assert.doesNotMatch(p.get('message').textContent,/会继续下载/);
  assert.match(text(p.get('items')),/上传结果待核对/);
});

test('another page changing a note blocks stale autosave and submit until an explicit choice',async()=>{
  const p=await page();await p.share();const id=[...p.drafts.keys()][0];
  p.drafts.set(id,{...p.drafts.get(id),note:'其他页面的新点评 #二创',version:1});
  p.get('note').value='我的本机点评 #二创';p.get('note').oninput();await settle();
  assert.equal(p.drafts.get(id).note,'其他页面的新点评 #二创');
  assert.equal(p.get('note-conflict').hidden,false);
  assert.equal(p.get('note').value,'我的本机点评 #二创');
  assert.equal(p.get('server-note').textContent,'其他页面的新点评 #二创');
  assert.equal(p.get('submit-note').disabled,true);
  await p.get('note-form').onsubmit(event());assert.equal(p.tasks.size,0);
  p.get('keep-local').onclick();await settle();
  assert.equal(p.drafts.get(id).note,'我的本机点评 #二创');
  assert.equal(p.drafts.get(id).version,2);
  assert.equal(p.get('note-conflict').hidden,true);
  await p.get('note-form').onsubmit(event());assert.equal(p.tasks.get(id),'我的本机点评 #二创');
});

test('another page submitting preserves this page text without allowing a second task',async()=>{
  const p=await page();await p.share();const id=[...p.drafts.keys()][0];
  p.get('note').value='本机还没提交的点评 #二创';p.get('note').oninput();await settle();
  p.drafts.set(id,{...p.drafts.get(id),note:'另一页提交的点评 #二创',version:2,state:'queued',task_id:'foreign-task'});
  await p.get('refresh').onclick();
  assert.equal(p.get('note-panel').hidden,false);
  assert.equal(p.get('note').value,'本机还没提交的点评 #二创');
  assert.equal(p.get('keep-local').disabled,true);
  assert.equal(p.get('submit-note').disabled,true);
  await p.get('note-form').onsubmit(event());assert.equal(p.tasks.size,0);
  p.get('use-server').onclick();assert.equal(p.get('note-panel').hidden,true);
});

test('cancelled draft retains unsaved local text even when the following refresh fails',async()=>{
  const p=await page();await p.share();p.controls.failAfterCancel=true;
  p.get('note').value='来不及保存的点评 #二创';await p.cancel();
  assert.match(p.get('message').textContent,/已取消，不会继续上传/);
  assert.match(text(p.get('items')),/本机暂存点评：来不及保存的点评/);
  assert.equal(p.get('note-panel').hidden,true);
  await p.get('note-form').onsubmit(event());assert.equal(p.tasks.size,0);
});

test('downloading cancellation remains pending until the worker confirms it has stopped',async()=>{
  const p=await page();await p.share();p.controls.submitState='preparing';await p.submit();
  await p.cancel();
  assert.match(text(p.get('items')),/正在取消/);
  assert.match(p.get('message').textContent,/等待下载停止/);
  assert.doesNotMatch(p.get('message').textContent,/收藏已取消/);
  const id=[...p.drafts.keys()][0];p.drafts.set(id,{...p.drafts.get(id),state:'cancelled'});
  await p.get('refresh').onclick();assert.match(text(p.get('items')),/已取消/);
});

test('losing the race to upload displays the actual uploading state without claiming cancellation',async()=>{
  const p=await page();await p.share();await p.submit();p.controls.cancelRace=true;
  await p.cancel();
  assert.match(p.get('message').textContent,/不能保证撤回/);
  assert.match(text(p.get('items')),/正在上传夸克/);
  assert.doesNotMatch(text(p.get('items')),/已取消/);
});

test('conflicts and incomplete destination checks do not promise a background upload',async()=>{
  for(const state of ['name_conflict','destination_unchecked']){
    const p=await page();await p.share();p.controls.submitState=state;await p.submit();
    assert.match(p.get('message').textContent,state==='name_conflict'?/修改点评或类目/:/尚未上传/);
    assert.doesNotMatch(p.get('message').textContent,/后台会继续/);
    const buttons=text(p.get('items'));
    assert.match(buttons,/取消收藏/);
    if(state==='name_conflict')assert.doesNotMatch(buttons,/重试/);
    else assert.match(buttons,/重试/);
    await p.cancel();assert.match(text(p.get('items')),/已取消/);
  }
});

test('revision keeps the link and note editable even if its following list refresh fails',async()=>{
  const p=await page();await p.share();p.controls.submitState='name_conflict';await p.submit();
  p.controls.failAfterRevision=true;await p.revise();
  assert.equal(p.drafts.size,2);assert.equal(p.tasks.size,1);
  assert.equal(p.get('note-panel').hidden,false);assert.equal(p.get('note').value,'测试点评 #二创');
  assert.match(p.get('message').textContent,/原链接和点评已带过来/);
  assert.match(text(p.get('items')),/已有修改记录/);
  p.controls.failRefresh=false;p.controls.submitState='queued';
  p.get('note').value='改过的点评 #二创';await p.get('note-form').onsubmit(event());
  assert.equal(p.tasks.size,2);assert.notEqual(p.submissions[0].id,p.submissions[1].id);
  assert.equal([...p.drafts.values()].find(d=>d.state==='superseded').note,'测试点评 #二创');
});

test('lost revision response retries the same child and never submits it automatically',async()=>{
  const p=await page();await p.share();p.controls.submitState='name_conflict';await p.submit();
  p.controls.loseRevisionReply=true;await p.revise();
  assert.equal(p.get('note-panel').hidden,true);assert.equal(p.drafts.size,2);
  await p.revise();assert.equal(p.drafts.size,2);assert.equal(p.submissions.length,1);
  assert.equal(p.get('note-panel').hidden,false);
});

test('a cancelled original rejects a late revision without opening a new editor',async()=>{
  const p=await page();await p.share();p.controls.submitState='name_conflict';await p.submit();
  p.controls.reviseRace=true;await p.revise();
  assert.equal(p.drafts.size,1);assert.equal(p.get('note-panel').hidden,true);
  assert.match(text(p.get('items')),/已取消/);
  assert.match(p.get('message').textContent,/只有尚未上传/);
});
