import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchMedia} from './media-fetch.js';
const input=(url='https://dl1.aliyundrive.cloud/video?signature=private',headers={Range:'bytes=0-3'})=>new Request('https://relay.test/media-fetch',{method:'POST',body:JSON.stringify({url,headers})});
test('streams exact bytes with private no-store headers and no credential reflection',async()=>{
  const response=await fetchMedia(input(undefined,{Range:'bytes=0-3',Cookie:'private'}),async(url,options)=>{
    assert.equal(options.cache,'no-store');assert.equal(options.redirect,'manual');assert.equal(options.headers.get('Cookie'),'private');
    return new Response(new Uint8Array([0,1,2,3]),{status:206,headers:{'Content-Range':'bytes 0-3/10','ETag':'"stable"','Set-Cookie':'must-not-leak'}});
  });
  assert.equal(response.status,206);assert.equal(response.headers.get('Cache-Control'),'private, no-store');
  assert.equal(response.headers.get('Set-Cookie'),null);assert.equal(response.headers.get('Content-Length'),'4');
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()),new Uint8Array([0,1,2,3]));
});
test('rejects unknown hosts, credentials, oversized ranges and unauthorized header names before fetch',async()=>{
  for (const [url,headers] of [
    ['https://localhost/video',{Range:'bytes=0-3'}],['https://api.modelbest.cn/v1/models',{Range:'bytes=0-3'}],
    ['https://user:pass@dl1.aliyundrive.cloud/video',{Range:'bytes=0-3'}],
    ['https://dl1.aliyundrive.cloud/video',{Range:'bytes=0-999999999'}],
    ['https://dl1.aliyundrive.cloud/video',{Range:'bytes=0-',Host:'private.internal'}],
  ]) assert.ok((await fetchMedia(input(url,headers),()=>assert.fail('must not fetch'))).status>=400);
});
test('range or encoding mismatch never reaches the caller',async()=>{
  for(const headers of [{'Content-Range':'bytes 1-3/10'},{'Content-Range':'bytes 0-9/10'},{'Content-Range':'bytes 0-3/10','Content-Encoding':'gzip'}]){
    const response=await fetchMedia(input(),async()=>new Response('bad',{status:206,headers}));assert.equal(response.status,502);
  }
});
test('only sanitized capacity rejection is retained from upstream errors',async()=>{
  const response=await fetchMedia(input(),async()=>new Response('private-token RequestDeniedByCallback ExceedMaxConcurrency',{status:403}));
  assert.equal(response.status,403);assert.equal(await response.text(),'RequestDeniedByCallback ExceedMaxConcurrency');
});
test('bounded request parsing rejects excessive JSON',async()=>{
  const request=new Request('https://relay.test/media-fetch',{method:'POST',body:'x'.repeat(65537)});
  assert.equal((await fetchMedia(request,()=>assert.fail('must not fetch'))).status,400);
});
