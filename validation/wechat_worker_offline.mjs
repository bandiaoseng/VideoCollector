// Offline contract probe of pinned upstream source. No live network or credentials.
// Run: node --experimental-vm-modules validation/wechat_worker_offline.mjs
import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {createHash, webcrypto} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.join(root, 'upstream/wx_channels_download');
const commit = '124f044235bc706fe796c043a14ff442a150ae5d';
assert.equal(execFileSync('git', ['-C', repo, 'rev-parse', 'HEAD'], {encoding:'utf8'}).trim(), commit);
const source = await readFile(path.join(repo, 'internal/workers/sph/worker.js'), 'utf8');
const sourceHash = createHash('sha256').update(source).digest('hex');
// Fixed test values only. The user's real share is deliberately never submitted.
const access = 'offline-test-access';
const cookie = 'offline-test-cookie';
const share = 'https://weixin.qq.com/sph/OFFLINE_FIXTURE_ONLY';
const feed = {errCode:0, data:{feedInfo:{id:'offline-video', videoUrl:'https://media.example.invalid/fixture.mp4'}}};
const parsed = {data:{wx_export_id:'offline-video', playable_url:'https://preview.example.invalid/feed?token=fixture-token&eid=fixture-export'}};

async function invoke({env={ACCESS_CREDENTIAL:access, COOKIE:cookie}, authorization=access,
                       body={url:share}, parseBody=parsed, parseStatus=200, feedBody=feed}={}) {
  const calls=[];
  const context=vm.createContext({Request, Response, URL, URLSearchParams, TextEncoder, TextDecoder,
    Uint8Array, atob, btoa, crypto:webcrypto, console:{log(){}},
    // The real fetch is never injected. All upstream fetches are answered in memory.
    fetch:async (target, options)=>{
      const url=new URL(target);
      const input=JSON.parse(options.body);
      calls.push({host:url.hostname, path:url.pathname, body:input, cookie:options.headers.cookie});
      if(url.hostname==='yuanbao.tencent.com' && url.pathname==='/api/weixin/get_parse_result') {
        return Response.json(parseBody,{status:parseStatus});
      }
      if(url.hostname==='channels.weixin.qq.com' && url.pathname==='/finder-preview/api/feed/get_feed_info') {
        return Response.json(feedBody);
      }
      throw new Error('Unexpected fetch target in offline fixture');
    }});
  const module=new vm.SourceTextModule(source,{context,identifier:'pinned-sph-worker'});
  await module.link(specifier=>{
    // Only static UI assets are substituted; API source is not rewritten.
    assert.ok(['./index.html','./icon.js'].includes(specifier));
    return new vm.SyntheticModule(['default'],function(){this.setExport('default','');},{context});
  });
  await module.evaluate({timeout:2000});
  const request=new Request('https://collector.example.invalid/api/fetch_video_profile',{
    method:'POST',headers:{Authorization:'Bearer '+authorization,'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const response=await module.namespace.default.fetch(request,env,{});
  return {status:response.status,body:await response.json(),calls};
}

const tests=[];
async function check(name,fn){await fn();tests.push({name,result:'passed'});}
await check('missing_access_configuration_blocks_before_fetch',async()=>{
  const r=await invoke({env:{COOKIE:cookie}});assert.equal(r.status,503);assert.equal(r.calls.length,0);
});
await check('wrong_access_credential_blocks_before_fetch',async()=>{
  const r=await invoke({authorization:'wrong'});assert.equal(r.status,401);assert.equal(r.calls.length,0);
});
await check('missing_share_rejected_without_fetch',async()=>{
  const r=await invoke({body:{}});assert.equal(r.status,400);assert.equal(r.calls.length,0);
});
await check('fixture_metadata_resolves_without_desktop_callbacks',async()=>{
  const r=await invoke();assert.equal(r.status,200);assert.deepEqual(r.body,feed);
  assert.equal(r.calls.length,2);assert.equal(r.calls[0].body.url,share);
  assert.equal(r.calls[0].cookie,cookie);assert.equal(r.calls[1].cookie,undefined);
  assert.deepEqual(r.calls[1].body,{baseReq:{generalToken:'fixture-token'},exportId:'fixture-export'});
});
await check('upstream_http_rejection_is_not_success',async()=>{
  const r=await invoke({parseStatus:401});assert.equal(r.status,500);assert.equal(r.calls.length,1);
});
await check('missing_parse_identity_stops_before_feed_lookup',async()=>{
  const r=await invoke({parseBody:{data:{}}});assert.equal(r.status,500);assert.equal(r.calls.length,1);
});
await check('unavailable_video_has_explicit_failure',async()=>{
  const r=await invoke({feedBody:{errCode:0,data:{errMsg:{type:1,title:'<b>Unavailable</b>',content:'Fixture only'}}}});
  assert.equal(r.status,422);assert.equal(r.body.code,'FEED_INFO_UNAVAILABLE');
});
await check('feed_api_error_is_not_success',async()=>{
  const r=await invoke({feedBody:{errCode:100,errMsg:'fixture failure'}});assert.equal(r.status,500);
});

// These are observed weaknesses, not successful business acceptance tests.
const missingToken=await invoke({parseBody:{data:{wx_export_id:'fixture',playable_url:'invalid'}}});
const emptyFeed=await invoke({feedBody:{errCode:0}});
const missingCookie=await invoke({env:{ACCESS_CREDENTIAL:access},parseStatus:401});
const findings={
  missing_playable_token_still_attempts_feed_fetch:missingToken.calls.length===2 && missingToken.calls[1].body.baseReq.generalToken==='',
  empty_feed_can_return_http_200:emptyFeed.status===200 && !emptyFeed.body.data,
  missing_yuanbao_cookie_not_checked_locally:missingCookie.calls.length===1 && missingCookie.calls[0].cookie===undefined,
};
const report={scope:'Pinned upstream metadata API tested with synthetic in-memory responses only; no real video, login, upload, or network verification.',
  commit,source_sha256:sourceHash,node:process.version,live_network_requests:0,tests,findings,
  real_video_download_verified:false,production_selected:false};
await writeFile(path.join(root,'wechat_offline_contract_result.json'),JSON.stringify(report,null,2),'utf8');
console.log(JSON.stringify({offline_tests_passed:tests.length,findings,live_network_requests:0,real_video_download_verified:false}));
