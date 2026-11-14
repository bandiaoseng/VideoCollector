'use strict';
import {createDestinationPreview} from './preview.mjs';
import {NoteEditor, DraftCache, noteClosed} from './draft_editor.mjs';
const $ = id => document.getElementById(id);
const labels = {awaiting_note:'待补点评',queued:'已提交 · 等待处理',preparing:'正在下载',uploading:'正在上传夸克',succeeded:'已存入夸克',prepare_failed:'下载未完成',setup_required:'需要配置服务',needs_review:'上传结果待核对',cancel_requested:'正在取消',cancelled:'已取消'};
const hints = {prepare_failed:'下载没有完成，点评已保留。可稍后重试。',setup_required:'服务暂未就绪，点评已保留。',needs_review:'上传结果不确定，请先核对夸克，避免重复上传。',cancel_requested:'已请求取消，正在等待下载停止；不会再启动上传。',cancelled:'这条收藏已取消，不会继续上传。原记录保留。'};
Object.assign(labels,{name_conflict:'同名冲突 · 尚未上传',destination_unchecked:'目录未查清 · 尚未上传',superseded:'已有修改记录'});
Object.assign(hints,{name_conflict:'目标目录已有同名文件或待核对的同名上传。请点“修改点评或类目”，修改后再提交，无需重新分享链接。',destination_unchecked:'目标文件夹暂时没查完整，视频和点评已保留，尚未上传。可稍后重试。',superseded:'这份原记录已停止，原点评保留。请在修改记录中继续。'});
let settings, selected=null, entries=[], offset=0, pendingSource=null, busy=false;
const reviewCandidates=new Map(), reviewing=new Set();
const reparsing=new Set();
let acceptedVersion=0;
let saveChain=Promise.resolve();
const storage={get(k){try{return localStorage.getItem(k);}catch{return null;}},set(k,v){try{localStorage.setItem(k,v);}catch{}},remove(k){try{localStorage.removeItem(k);}catch{}},keys(){try{return Array.from({length:localStorage.length},(_,i)=>localStorage.key(i));}catch{return [];}}};
const draftCache=new DraftCache(storage,crypto.randomUUID()), editors=new Map();
const pendingKey='collector.pending-share';
function showMessage(text,error=false){$('message').textContent=text;$('message').classList.toggle('error',error);$('message').hidden=!text;if(error&&text)$('message').scrollIntoView({behavior:'smooth',block:'center'});}
async function api(path,method='GET',body){
  let response;
  try{response=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-Collector-Request':'1'},body:body===undefined?undefined:JSON.stringify(body)});}catch{throw new Error('网络暂时不可用，内容已留在这台设备上，请稍后重试。');}
  const data=await response.json();
  if(!response.ok){if(response.status===401){$('login-panel').hidden=false;$('workspace').hidden=true;$('logout').hidden=true;}const error=new Error(typeof data.detail==='string'?data.detail:'提交内容格式不正确，请检查后重试。');error.code=data.code;error.current=data.current;throw error;}
  return data;
}
function platform(item){return item.platform==='douyin'?'抖音':'视频号';}
function caption(item){return item.share_text.replace(/https?:\/\/\S+/g,'').trim().slice(0,100)||'已收到视频链接';}
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function renderEntries(){
  $('items').replaceChildren();$('empty').hidden=entries.length>0;
  for(const item of entries){
    const card=el('article','entry'+(selected?.id===item.id?' active':''));
    const head=el('div','entry-head');head.append(el('span','platform',platform(item)),el('span','badge'+(item.state==='succeeded'?' success':hints[item.state]?' problem':''),labels[item.state]||'处理中'));
    card.append(head,el('h3','',item.filename||item.note||caption(item)));
    if(item.category)card.append(el('p','',`夸克 / ${item.category}`));
    if(item.revision_of)card.append(el('p','','这是原收藏的修改记录，视频链接已保留。'));
    if(item.parse_retry_required)card.append(el('p','','上次解析结果不明，无法确定是否已扣额度。只有明确确认后才会重新请求一次，程序不会充值。'));
    else if(hints[item.state])card.append(el('p','',hints[item.state]));
    if(item.review_note)card.append(el('p','',item.review_note));
    if(item.notice)card.append(el('p','notice',item.notice));
    if(['cancelled','cancel_requested'].includes(item.state)){
      const seen=new Set([item.note]);
      for(const copy of draftCache.copies(item.id)){if(!seen.has(copy.note)){seen.add(copy.note);card.append(el('p','notice','本机暂存点评：'+copy.note));}}
    }
    const foot=el('div','entry-foot');const time=el('time','',new Date(item.created*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}));foot.append(time);
    if(item.state==='awaiting_note'){const edit=el('button','text-button','补充点评 →');edit.disabled=busy;edit.onclick=()=>choose(item);foot.append(edit);}
    if(item.state==='name_conflict'||item.revision_id){
      const revise=el('button','text-button',item.revision_id?'查看修改记录':'修改点评或类目');revise.disabled=busy;
      revise.onclick=async()=>{revise.disabled=true;try{
        const result=await api(`/drafts/${item.id}/revise`,'POST',{});
        rememberAccepted(result.original);rememberAccepted(result.draft);
        if(result.draft.state==='awaiting_note')choose(result.draft);
        await refreshAfterAcceptance(result.draft.state==='awaiting_note'?'原链接和点评已带过来。修改点评或 #类目 后，再点提交。':submissionMessage(result.draft));
      }catch(e){if(e.current)rememberAccepted(e.current);showMessage(e.message,true);}finally{revise.disabled=false;}};
      foot.append(revise);
    }
    if(['awaiting_note','queued','preparing','prepare_failed','setup_required','name_conflict','destination_unchecked'].includes(item.state)){
      const cancel=el('button','text-button',item.state==='preparing'?'停止并取消':'取消收藏');cancel.disabled=busy;
      cancel.onclick=async()=>{cancel.disabled=true;try{
        if(selected?.id===item.id){const editor=editors.get(item.id);editor.note=$('note').value;draftCache.save(editor);}
        const result=await api(`/drafts/${item.id}/cancel`,'POST',{});rememberAccepted(result);
        if(selected?.id===item.id)resetComposer();
        await refreshAfterAcceptance(result.state==='cancel_requested'?'已请求取消，正在等待下载停止；不会再启动上传。':'这条收藏已取消，不会继续上传。');
      }catch(e){if(e.current)rememberAccepted(e.current);showMessage(e.message,true);}finally{cancel.disabled=false;}};
      foot.append(cancel);
    }
    if(['prepare_failed','setup_required','destination_unchecked'].includes(item.state)&&!item.parse_retry_required){const retry=el('button','text-button','重试');retry.onclick=async()=>{retry.disabled=true;try{const result=await api(`/drafts/${item.id}/retry`,'POST',{});rememberAccepted(result);await refreshAfterAcceptance('已收到重试请求，可在列表中查看进度。');}catch(e){showMessage(e.message,true);}finally{retry.disabled=false;}};foot.append(retry);}
    if(item.state==='prepare_failed'&&item.parse_retry_required&&item.parse_retry_token){
      const reparse=el('button','text-button','重新尝试解析');
      reparse.disabled=reparsing.has(item.id);
      reparse.onclick=async()=>{
        if(reparsing.has(item.id)||!window.confirm('上次没有收到解析回复，无法确定是否已经消耗额度。重新尝试可能再消耗一次解析额度，程序不会充值。是否确认只重新请求一次？'))return;
        reparsing.add(item.id);renderEntries();
        try{const result=await api(`/drafts/${item.id}/reparse`,'POST',{attempt_token:item.parse_retry_token,approved:true});rememberAccepted(result);await refreshAfterAcceptance('这次解析批准已记录或处理，请查看任务状态。无需重复提交。');}
        catch(e){showMessage(e.message,true);}
        finally{reparsing.delete(item.id);renderEntries();}
      };
      foot.append(reparse);
    }
    if(item.state==='needs_review'){
      const candidate=reviewCandidates.get(item.id);
      if(candidate)card.append(el('p','',`待核对文件：${candidate.review_candidate.full_path}`));
      const performReview=async confirmation=>{
        if(reviewing.has(item.id))return;
        reviewing.add(item.id);renderEntries();
        try{
        const result=await api(`/drafts/${item.id}/review`,'POST',confirmation?{confirmation}:{});
        if(result.review_candidate&&result.review_confirmation)reviewCandidates.set(item.id,result);
        else reviewCandidates.delete(item.id);
        rememberAccepted(result);
        await refreshAfterAcceptance(result.review_note||'暂时无法确认，请保留这条任务。');
      }catch(e){reviewCandidates.delete(item.id);showMessage(e.message,true);}
      finally{reviewing.delete(item.id);renderEntries();}};
      const review=el('button','text-button','核对夸克结果');
      review.disabled=reviewing.has(item.id);
      review.onclick=()=>performReview();
      foot.append(review);
      if(candidate){
        const confirm=el('button','text-button','我已核对，标记完成');
        confirm.disabled=reviewing.has(item.id);
        confirm.onclick=()=>{
          if(window.confirm(`请先在夸克播放并确认内容和声音完整：\n${candidate.review_candidate.full_path}\n\n网盘未提供完整校验信息。只有你已确认内容对应，才标记完成；不会重新上传。是否确认？`))
            return performReview(candidate.review_confirmation);
        };
        foot.append(confirm);
      }
    }
    card.append(foot);$('items').append(card);
  }
}
const destinationPreview=createDestinationPreview({
  getDraftId:()=>selected?.id,
  getNote:()=>$('note').value,
  request:note=>api('/preview','POST',{note}),
  show:(text,adjusted=false)=>{
    $('destination').textContent=text;
    $('filename-notice').textContent=adjusted?'文件名已做必要调整，完整点评会保留。':'';
  }
});
function preview(){destinationPreview.update();}
function renderEditor(editor){
  if(selected?.id!==editor.id)return;
  $('note-conflict').hidden=!editor.conflict;
  $('server-note').textContent=editor.conflict?.note||'（空点评）';
  $('keep-local').disabled=busy||noteClosed(editor.conflict);
  $('use-server').disabled=busy;
  $('submit-note').disabled=busy||!!editor.conflict;
  if(editor.conflict)$('save-status').textContent=noteClosed(editor.conflict)?'其他页面已提交或取消这条视频；上方点评仍暂存于本机，不会替换已有记录。':'另一页面的点评已更新。上方是你正在编辑的内容，请比较后选择。';
}
function saved(editor,result){
  editor.accept(result);
  if(editor.note===editor.baseNote&&!editor.conflict)draftCache.clear(editor);else draftCache.save(editor);
  renderEditor(editor);
}
function noteError(editor,error){
  if(error.code==='draft_conflict'&&error.current)editor.observe(error.current);
  else if(error.code==='note_saved'&&error.current)saved(editor,error.current);
  draftCache.save(editor);
  if(selected?.id===editor.id)$('save-status').textContent='点评仍暂存在本机；联网后可以继续保存。';
  renderEditor(editor);
}
function choose(item){
  if(busy)return;
  selected=item;$('source-panel').hidden=true;$('note-panel').hidden=false;
  $('selected-platform').textContent=platform(item);$('selected-caption').textContent=caption(item);$('selected-url').textContent=item.source;
  let editor=editors.get(item.id);
  if(!editor){editor=new NoteEditor(item,draftCache.current(item.id));editors.set(item.id,editor);}else editor.observe(item);
  if(editor.conflict)draftCache.save(editor);
  $('note').value=editor.note;
  $('save-status').textContent=editor.note!==item.note?'有本机暂存的点评；确认内容后可继续编辑或提交。':'点评会自动保存为草稿。';
  $('local-copies').replaceChildren();
  const seen=new Set([editor.note,item.note]);
  for(const copy of draftCache.copies(item.id)){
    if(seen.has(copy.note))continue;seen.add(copy.note);
    const button=el('button','secondary','查看另一份本机暂存：'+copy.note.slice(0,60));button.type='button';
    button.onclick=()=>{if(busy)return;editor.note=copy.note;editor.conflict=editor.server;draftCache.save(editor);$('note').value=editor.note;renderEditor(editor);preview();};
    $('local-copies').append(button);
  }
  renderEditor(editor);
  preview();renderEntries();$('note-panel').scrollIntoView({behavior:'smooth',block:'start'});$('note').focus({preventScroll:true});
}
function saveNote(){
  if(!selected||busy)return;
  const id=selected.id,note=$('note').value,editor=editors.get(id);
  editor.note=note;draftCache.save(editor);$('save-status').textContent='正在保存点评…';preview();renderEditor(editor);
  saveChain=saveChain.catch(()=>{}).then(async()=>{
    if(editor.conflict||editor.note!==note)return;
    try{const result=await api(`/drafts/${id}`,'PATCH',{note,version:editor.version});saved(editor,result);if(selected?.id===id&&$('note').value===note&&!editor.conflict)$('save-status').textContent='点评草稿已保存';}
    catch(e){noteError(editor,e);}
  });
}
async function refresh(){
  const version=acceptedVersion;
  settings=await api('/status');
  const blockers=[];
  if(!settings.worker_online)blockers.push('后台暂未运行');
  if(!settings.quark_ready)blockers.push('夸克上传服务尚未就绪');
  if(settings.categories_ready===false)blockers.push('收藏类目尚未绑定');
  if(settings.storage_ready===false)blockers.push(settings.storage_problem||'后台磁盘空间不足');
  const platforms=['douyin','wechat'].map(key=>{
    const name=key==='douyin'?'抖音':'视频号';
    return settings.platforms[key]?name+'下载已配置':name+'：'+(settings.platform_problems?.[key]||'下载服务尚未接通');
  }).join('；');
  $('service-status').textContent=blockers.length?blockers.join('；')+'。链接和点评可先保存为草稿。':platforms+'。提交成功后可关闭页面。';
  $('categories').replaceChildren(...settings.categories.map(category=>{const chip=el('button','chip','#'+category);chip.type='button';chip.onclick=()=>{$('note').value=$('note').value.replace(/(^|\s)[#＃][^\s#＃]+/g,' ').trim()+' #'+category;saveNote();$('note').focus();};return chip;}));
  const result=await api('/drafts');
  if(version!==acceptedVersion)return; // An earlier read cannot erase a later acceptance.
  entries=[...result.items,...entries.filter(old=>!result.items.some(item=>item.id===old.id))];
  offset=Math.max(result.items.length,offset);if(offset<=50)$('more').hidden=!result.has_more;
  renderEntries();
  if(selected){
    const current=entries.find(item=>item.id===selected.id),editor=editors.get(selected.id);
    if(current){editor.observe(current);if(editor.conflict)draftCache.save(editor);$('note').value=editor.note;renderEditor(editor);
      if(current.state!=='awaiting_note'&&!editor.conflict){draftCache.clear(editor);resetComposer();}
    }
  }
}
function resetComposer(){destinationPreview.cancel();selected=null;$('source-panel').hidden=false;$('note-panel').hidden=true;$('source').value='';renderEntries();}
function rememberAccepted(item){
  acceptedVersion++;
  entries=[item,...entries.filter(old=>old.id!==item.id)];
  renderEntries();
}
async function refreshAfterAcceptance(message){
  showMessage(message);
  try{await refresh();}catch{
    showMessage(message+' 列表暂时无法更新，稍后刷新即可，不用重复提交。');
  }
}
function submissionMessage(item){
  if(['cancelled','cancel_requested','name_conflict','destination_unchecked','superseded'].includes(item.state))return hints[item.state];
  if(item.state==='succeeded')return '这条视频已经存入夸克。';
  if(item.state==='needs_review')return '提交已收到，上传结果仍待核对。请保留这条任务，不用重复提交。';
  if(item.state==='prepare_failed')return '提交已收到，下载暂未完成。点评已保留，可在列表中重试。';
  if(item.state==='setup_required')return '提交已收到，处理服务尚未就绪。点评已保留，请稍后查看进度。';
  return '已提交，可以关闭页面。后台会继续下载并存入夸克。';
}
async function acceptSource(text){
  if(!pendingSource||pendingSource.text!==text)pendingSource={id:crypto.randomUUID(),text};
  storage.set(pendingKey,JSON.stringify(pendingSource));
  const item=await api('/drafts','POST',pendingSource);storage.remove(pendingKey);pendingSource=null;
  rememberAccepted(item);
  if(item.state==='awaiting_note'){
    choose(item);await refreshAfterAcceptance('视频链接已收到，接着写点评。');
  }else{
    resetComposer();await refreshAfterAcceptance('这条提交已收到，可在最近收藏中查看进度。');
  }
}
async function start(){
  await refresh();$('login-panel').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;
  if(pendingSource){$('source').value=pendingSource.text;await acceptSource(pendingSource.text);}
}
$('source-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await acceptSource($('source').value);}catch(e){showMessage(e.message,true);}finally{button.disabled=false;}};
$('login-form').onsubmit=async event=>{event.preventDefault();event.submitter.disabled=true;try{await api('/login','POST',{token:$('token').value});$('token').value='';showMessage('');await start();}catch(e){showMessage(e.message,true);}finally{event.submitter.disabled=false;}};
$('note').addEventListener('input',saveNote);
$('keep-local').onclick=()=>{
  if(!selected||busy)return;const editor=editors.get(selected.id);editor.resolve(true);renderEditor(editor);saveNote();
};
$('use-server').onclick=()=>{
  if(!selected||busy)return;const editor=editors.get(selected.id),remote=editor.conflict;
  editor.resolve(false);draftCache.clear(editor);$('note').value=editor.note;renderEditor(editor);preview();
  $('save-status').textContent='已使用保存过的点评。';
  if(noteClosed(remote)){rememberAccepted(remote);resetComposer();}
};
$('note-form').onsubmit=async event=>{
  event.preventDefault();if(!selected||busy)return;
  const id=selected.id,note=$('note').value,editor=editors.get(id);
  editor.note=note;draftCache.save(editor);if(editor.conflict){renderEditor(editor);return;}busy=true;
  $('note').disabled=true;$('submit-note').disabled=true;$('another').disabled=true;
  try{
    await saveChain;
    if(editor.conflict){renderEditor(editor);return;}
    const result=await api(`/drafts/${id}/submit`,'POST',{note,version:editor.version});
    saved(editor,result);draftCache.clear(editor);rememberAccepted(result);resetComposer();
    await refreshAfterAcceptance(submissionMessage(result));
  }
  catch(e){noteError(editor,e);showMessage(e.message,true);}
  finally{busy=false;$('note').disabled=false;$('submit-note').disabled=false;$('another').disabled=false;renderEntries();renderEditor(editor);}
};
$('another').onclick=()=>{if(!busy)resetComposer();};
$('refresh').onclick=()=>refresh().catch(()=>showMessage('列表暂时无法更新，请稍后刷新。已提交的任务会保留。',true));
$('logout').onclick=async()=>{try{await api('/logout','POST',{});$('workspace').hidden=true;$('login-panel').hidden=false;$('logout').hidden=true;showMessage('已退出登录');}catch(e){showMessage(e.message,true);}};
$('more').onclick=async()=>{try{const result=await api('/drafts?offset='+offset);entries.push(...result.items.filter(item=>!entries.some(old=>old.id===item.id)));offset+=result.items.length;$('more').hidden=!result.has_more;renderEntries();}catch(e){showMessage(e.message,true);}};
try{pendingSource=JSON.parse(storage.get(pendingKey)||'null');}catch{}
const params=new URLSearchParams(location.search);
const shared=['title','text','url'].map(key=>params.get(key)||'').filter(Boolean).join('\n');
if(shared){pendingSource={id:crypto.randomUUID(),text:shared};storage.set(pendingKey,JSON.stringify(pendingSource));history.replaceState(null,'','/');}
start().catch(e=>{if(!$('login-panel').hidden)return;showMessage(e.message,true);});
setInterval(()=>{if(!$('workspace').hidden&&!document.hidden&&!busy)refresh().catch(()=>showMessage('列表暂时无法更新，请稍后刷新。已提交的任务会保留。',true));},5000);
let installPrompt;
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;$('install').hidden=false;});
$('install').onclick=async()=>{if(installPrompt){await installPrompt.prompt();installPrompt=null;$('install').hidden=true;}};
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
