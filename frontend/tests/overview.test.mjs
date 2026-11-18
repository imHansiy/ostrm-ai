import {test} from 'node:test'
import assert from 'node:assert/strict'
import {dashboard} from '../app/utils/ai-overview.mjs'
const base=()=>({videos:{done:2106,failed:4657},tasks:{},directories:{queued:39,running:1},scopes:{included:{videos:{done:2106,failed:1525},tasks:{},directories:{queued:39,running:1}},deferred:{videos:{failed:3132},tasks:{},directories:{}}},activity:{active:[]},recovery:{state:'running',exclude_roots:['/百度'],total_tasks:1566,task_cursor:360}})
test('empty video queue does not imply complete when directories block batches',()=>{
 const s=dashboard(base());assert.equal(s.code,'directories');assert.equal(s.remaining,1525);assert.equal(s.queue,0);assert.match(s.detail,/等待目录检查结束/);assert.equal(s.deferred,3132)
})
test('scope switch partitions catalog results rather than recovery cursor',()=>{
 assert.equal(dashboard(base()).total,3631);assert.equal(dashboard(base(),true).total,6763);assert.equal(dashboard(base(),true).remaining,4657)
})
test('stale snapshot takes priority over apparent processing',()=>{
 const d=base();d.activity.active=[{id:1,kind:'video',path:'/other/x'}];assert.equal(dashboard(d,false,true).code,'stale')
})
test('deferred activities do not appear in current scope',()=>{
 const d=base();d.activity.active=[{id:1,kind:'video',path:'/百度/x'}];assert.equal(dashboard(d).active.length,0);assert.equal(dashboard(d,true).code,'processing')
})
test('queue work is distinguishable from inference and pending recovery',()=>{
 const d={videos:{failed:2},tasks:{queued:1},activity:{active:[]}};assert.equal(dashboard(d).code,'queued');d.tasks={};d.recovery={state:'running',total_tasks:20,task_cursor:3};assert.equal(dashboard(d).code,'batch');assert.equal(dashboard(d).future,17)
})
test('paused and unresolved are never labelled complete',()=>{
 assert.equal(dashboard({videos:{failed:2},scans:{paused:1}}).code,'paused');assert.equal(dashboard({videos:{failed:2}}).code,'unresolved')
})
test('no data is loading, known empty catalog is idle, known success is qualified complete',()=>{
 assert.equal(dashboard({}).code,'loading');assert.equal(dashboard({videos:{}}).code,'idle');assert.equal(dashboard({videos:{done:2}}).code,'complete')
})

test('already queued videos are not presented as blocked by directory discovery',()=>{
 const d=base();d.scopes.included.tasks={queued:2};assert.equal(dashboard(d).code,'queued')
})
