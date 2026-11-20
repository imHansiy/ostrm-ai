import { connect } from 'cloudflare:sockets';
import { timingSafeEqual } from 'node:crypto';
import { fetchMedia, isMediaHost } from './media-fetch.js';

const targets = Object.freeze({
  'openapi.alipan.com': '/adrive/v1.0/user/getDriveInfo',
  'drive.quark.cn': '/1/clouddrive/config?pr=ucpro&fr=pc',
  'pc-api.uc.cn': '/1/clouddrive/config?pr=UCBrowser&fr=pc',
  'api.modelbest.cn': '/v1/models',
});
function authorized(request, secret) {
  if (!secret || secret.length < 32) return false;
  const expected = Buffer.from(`Bearer ${secret}`);
  const actual = Buffer.from(request.headers.get('Authorization') || '');
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}
export default {
  async fetch(request, env, ctx) {
    if (!authorized(request, env.RELAY_SECRET)) return new Response('Unauthorized', {status: 401});
    const url = new URL(request.url);
    if (url.pathname === '/health') return Response.json({ok: true, version: 3});
    if (url.pathname === '/media-fetch') return fetchMedia(request);
    const host = url.searchParams.get('host');
    const media = isMediaHost(host);
    const model = host === 'api.modelbest.cn';
    const flow = url.searchParams.get('flow') === 'ack';
    const uploadFlow = url.searchParams.get('upload') === 'ack';
    if (!Object.hasOwn(targets, host) && !media) return new Response('Forbidden target', {status: 403});
    if (media && (url.pathname !== '/connect' || !flow)) return new Response('Flow control required', {status: 403});
    if (model && url.pathname === '/connect' && (!flow || !uploadFlow)) return new Response('Bidirectional flow control required', {status: 403});
    if (url.pathname === '/tcp-probe') {
      let sock;
      try {
        sock=connect({hostname:host,port:443},{secureTransport:'on'});
        const result=await Promise.race([(async()=>{
          await sock.opened;
          const w=sock.writable.getWriter();
          await w.write(new TextEncoder().encode(`GET ${targets[host]} HTTP/1.1\r\nHost: ${host}\r\nConnection: close\r\n\r\n`));
          const r=sock.readable.getReader();const chunk=await r.read();
          return new TextDecoder().decode(chunk.value).split('\r\n')[0].slice(0,100);
        })(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('timeout')),12000))]);
        return Response.json({host,result});
      } catch {return Response.json({host,error:'tcp_tls_failed'},{status:502});}
      finally {if(sock) await sock.close().catch(()=>{});}
    }
    if (url.pathname === '/probe') {
      const started = Date.now();
      try {
        const response = await fetch(`https://${host}${targets[host]}`, {
          method: host === 'openapi.alipan.com' ? 'POST' : 'GET',
          redirect: 'manual', signal: AbortSignal.timeout(12000),
          headers: {'Content-Type': 'application/json'},
          ...(host === 'openapi.alipan.com' ? {body: '{}'} : {}),
        });
        await response.body?.cancel();
        return Response.json({host, status: response.status, elapsed_ms: Date.now()-started});
      } catch { return Response.json({host, error: 'upstream_unreachable', elapsed_ms: Date.now()-started}, {status: 502}); }
    }
    if (url.pathname !== '/connect' || request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') return new Response('Not found', {status: 404});
    let socket;
    try {
      socket = connect({hostname: host, port: 443}, {secureTransport: 'off'});
      await Promise.race([socket.opened, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 10000))]);
    } catch {
      if (socket) await socket.close().catch(() => {});
      return new Response('Upstream connect failed', {status: 502});
    }
    const [client, server] = Object.values(new WebSocketPair());
    const writer = socket.writable.getWriter();
    let stopped = false, incoming = 0, outgoing = 0, pending = 0, chain = Promise.resolve(), ack = null, inflight = 0;
    const unacked = [];
    const close = () => {
      if (stopped) return;
      stopped = true;
      clearTimeout(timer);
      if (ack) { ack(); ack = null; }
      try { server.close(1000); } catch {}
      void socket.close().catch(() => {});
    };
    const timer = setTimeout(close, model ? 600000 : (media ? 1800000 : 120000));
    server.binaryType = 'arraybuffer';
    server.accept();
    server.addEventListener('close', close);
    server.addEventListener('error', close);
    server.addEventListener('message', event => {
      if (flow && event.data === 'ack' && unacked.length > 0) {
        inflight-=unacked.shift();
        if (ack) { const resolve=ack; ack=null; resolve(); }
        return;
      }
      if (stopped || typeof event.data === 'string') { close(); return; }
      const bytes = new Uint8Array(event.data);
      incoming += bytes.byteLength;
      pending += bytes.byteLength;
      if (incoming > (model ? 64*1024*1024 : 8*1024*1024) || pending > 1024*1024) { close(); return; }
      chain = chain.then(async () => {
        if (!stopped) await writer.write(bytes);
        pending -= bytes.byteLength;
        if (uploadFlow && !stopped) server.send('uack');
      }).catch(close);
    });
    ctx.waitUntil((async () => {
      try {
        const reader = socket.readable.getReader();
        while (!stopped) {
          const {done, value} = await reader.read();
          if (done) break;
          outgoing += value.byteLength;
          if (outgoing > (media ? 3*1024**3 : 16*1024*1024)) break;
          for (let offset=0; offset<value.byteLength && !stopped; offset+=32768) {
            const chunk=value.subarray(offset,offset+32768);
            if (flow) {
              while ((inflight >= 2*1024*1024 || unacked.length >= 1024) && !stopped) await new Promise(resolve => { ack=resolve; });
              if (stopped) break;
              unacked.push(chunk.byteLength);
              inflight+=chunk.byteLength;
              server.send(chunk);
            } else server.send(chunk);
          }
        }
        // A short file may fit entirely in the credit window. Keep the
        // WebSocket alive until its final bytes have reached the client.
        while (flow && unacked.length > 0 && !stopped) await new Promise(resolve => { ack=resolve; });
      } catch {} finally { close(); }
    })());
    void socket.closed.catch(close);
    return new Response(null, {status: 101, webSocket: client});
  },
};
