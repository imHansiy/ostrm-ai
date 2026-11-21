const MAX_RANGE = 8 * 1024 ** 2;
export function isMediaHost(host) {
  return /^(dl[0-9]+\.aliyundrive\.cloud|dl-pc-[a-z0-9-]+\.drive\.quark\.cn|dl-pc-[a-z0-9-]+\.pds\.uc\.cn)$/.test(host || '');
}
const error = (status, message) => new Response(message, {status, headers: {'Cache-Control':'private, no-store'}});
function rejectionKind(text) {
  // Only fixed categories cross the private relay; never reflect signed URLs,
  // request IDs, credentials, or provider messages.
  if (/RequestDeniedByCallback/.test(text) && /ExceedMaxConcurrency/.test(text)) return 'concurrency';
  if (/RequestExpired|ExpiredToken|RequestHasExpired|request (?:has )?expired/i.test(text)) return 'expired';
  if (/RequestTimeTooSkewed/i.test(text)) return 'clock';
  if (/SignatureDoesNotMatch|InvalidSignature/i.test(text)) return 'signature';
  if (/referer/i.test(text)) return 'referer';
  if (/InvalidAccessKeyId|SecurityTokenExpired|InvalidSecurityToken/i.test(text)) return 'credential';
  if (/RequestDeniedByCallback/i.test(text)) return 'callback';
  if (/AccessDenied/i.test(text)) return 'access';
  if (/<!doctype|<html/i.test(text)) return 'html-denial';
  return 'denied';
}
async function boundedBytes(body) {
  if (!body) throw new Error('missing body');
  const reader=body.getReader(), chunks=[];
  let length=0;
  for (;;) {
    const {done,value}=await reader.read();
    if (done) break;
    length+=value.byteLength;
    if (length>65536) { await reader.cancel();throw new Error('large body'); }
    chunks.push(value);
  }
  const bytes=new Uint8Array(length);let offset=0;
  for (const chunk of chunks) {bytes.set(chunk,offset);offset+=chunk.byteLength;}
  return bytes;
}
export async function fetchMedia(request, transport=fetch) {
  if (request.method!=='POST') return error(405,'POST required');
  let body,url,headers;
  try {
    body=JSON.parse(new TextDecoder().decode(await boundedBytes(request.body)));url=new URL(body.url);
    if (url.protocol!=='https:' || url.username || url.password || url.port || url.hash || !isMediaHost(url.hostname)) return error(403,'Media target forbidden');
    headers=new Headers();
    const allowed=new Set(['range','if-range','if-match','cookie','authorization','user-agent','referer']);
    for (const [name,value] of Object.entries(body.headers||{})) {
      if (!allowed.has(name.toLowerCase()) || typeof value!=='string' || value.length>32768 || /[\r\n]/.test(value)) return error(400,'Invalid media headers');
      headers.set(name,value);
    }
  } catch {return error(400,'Invalid bounded media request');}
  const range=/^bytes=(\d+)-(\d+)$/.exec(headers.get('Range')||'');
  if (!range) return error(400,'Finite byte range required');
  const start=Number(range[1]),end=Number(range[2]);
  if (!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<0||end<start||end-start+1>MAX_RANGE) return error(400,'Media range exceeds limit');
  headers.set('Accept-Encoding','identity');
  let upstream;
  try {
    upstream=await transport(url.toString(),{method:'GET',headers,redirect:'manual',cache:'no-store',signal:AbortSignal.timeout(120000)});
  } catch {return error(502,'Media upstream unavailable');}
  if (upstream.status!==206) {
    if (upstream.status===403) {
      try {
        const text=new TextDecoder().decode(await boundedBytes(upstream.body));
        const kind=rejectionKind(text);
        const response=error(403,kind==='concurrency'?'RequestDeniedByCallback ExceedMaxConcurrency':'Media upstream rejected range: '+kind);
        response.headers.set('X-Media-Error-Kind',kind);
        return response;
      } catch {} // Oversized/invalid errors stay generic and bounded.
    } else await upstream.body?.cancel();
    return error(upstream.status>=400&&upstream.status<=599?upstream.status:502,'Media upstream did not accept range');
  }
  const actual=/^bytes (\d+)-(\d+)\/(\d+)$/.exec(upstream.headers.get('Content-Range')||'');
  const encoding=upstream.headers.get('Content-Encoding');
  if (!actual || Number(actual[1])!==start || Number(actual[2])>end || Number(actual[2])<start || Number(actual[3])<=Number(actual[2]) || (encoding&&encoding!=='identity')) {
    await upstream.body?.cancel();return error(502,'Invalid upstream media range');
  }
  const outgoing=new Headers({'Cache-Control':'private, no-store'});
  for (const name of ['Content-Type','Content-Range','ETag','Last-Modified']) {
    if (upstream.headers.has(name)) outgoing.set(name,upstream.headers.get(name));
  }
  outgoing.set('Content-Length',String(Number(actual[2])-start+1));outgoing.set('Accept-Ranges','bytes');
  return new Response(upstream.body,{status:206,headers:outgoing});
}
