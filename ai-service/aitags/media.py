"""Bounded HTTP gateway: ffmpeg never receives cloud credentials or arbitrary URLs."""
import base64
import fnmatch
import ipaddress
import json
import math
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlsplit
import httpx
from . import config
from .activity import report, reporter, read_ffmpeg_progress


class MediaError(RuntimeError):
    pass


FORMATS='mov,matroska,webm,avi,mpegts,flv,mpegvideo,asf'
_ALI_CALLBACK_SLOT = threading.BoundedSemaphore(1)


def _ali_callback(url):
    return fnmatch.fnmatchcase((urlsplit(url).hostname or '').lower(), 'dl*.aliyundrive.cloud')


def _callback_wait(delay, deadline, cancelled):
    end = min(deadline, time.monotonic()+delay)
    while time.monotonic() < end:
        if cancelled():
            raise MediaError('媒体读取已停止')
        time.sleep(max(0, min(.1, end-time.monotonic())))
    if cancelled() or time.monotonic() >= deadline:
        raise MediaError('媒体读取已停止或超时')


def _open_media_response(client, method, url, headers, deadline, cancelled):
    for attempt in range(4):
        if cancelled() or time.monotonic() >= deadline:
            raise MediaError('媒体读取已停止或超时')
        response = client.send(client.build_request(method, url, headers=headers), stream=True)
        if not (_ali_callback(url) and response.status_code == 403):
            return response
        # Read a bounded provider error, never persist it or the signed URL.
        body = bytearray()
        try:
            for chunk in response.iter_raw(chunk_size=4096):
                body.extend(chunk[:65536-len(body)])
                if len(body) >= 65536:
                    break
        except BaseException:
            response.close()
            raise
        temporary = b'RequestDeniedByCallback' in body and b'ExceedMaxConcurrency' in body
        if not temporary:
            return response
        response.close()
        if attempt == 3:
            raise MediaError('阿里云盘下载并发暂时受限，本次有限重试已用尽，请稍后恢复任务')
        _callback_wait(.5 * 2**attempt, deadline, cancelled)


def media_headers(url):
    host = (urlsplit(url).hostname or '').lower()
    headers = {'Accept-Encoding': 'identity'}
    if _ali_callback(url):
        # A finished range must not retain a callback connection in the pool.
        headers['Connection'] = 'close'
    # Required by the official Baidu driver for large-file downloads.
    if host == 'baidupcs.com' or host.endswith('.baidupcs.com'):
        headers['User-Agent'] = 'pan.baidu.com'
    return headers


def approve_url(url, openlist_url):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    own = urlsplit(openlist_url)
    if p.scheme not in ('http', 'https') or not host or p.username or p.password:
        raise MediaError('媒体地址协议不受支持')
    if host == own.hostname and p.port == own.port and p.scheme == own.scheme:
        return
    if not any(fnmatch.fnmatchcase(host, pattern) for pattern in config.MEDIA_HOSTS):
        raise MediaError('媒体主机未加入 MEDIA_ALLOWED_HOSTS：' + host)
    for addr in socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80)):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise MediaError('媒体重定向到非公网地址；请通过 OpenList 代理访问')


@contextmanager
def gateway(url, openlist_url, budget, timeout, cancelled=lambda: False, *, send_buffer_bytes=None, request_headers=None):
    approve_url(url, openlist_url)
    nonce = '/' + uuid.uuid4().hex
    remaining = [budget]
    used = [0]
    failures = []
    lock = threading.Lock()
    deadline = time.monotonic() + timeout
    proxy_options = {}
    host = (urlsplit(url).hostname or '').lower()
    if config.MEDIA_PROXY_URL and any(fnmatch.fnmatchcase(host, pattern) for pattern in config.MEDIA_PROXY_HOSTS):
        proxy_options['proxy'] = config.MEDIA_PROXY_URL
    client = httpx.Client(timeout=15, follow_redirects=False, **proxy_options)
    callback = reporter()
    pending = [0, 0.0]
    transfer_lock = threading.Lock()
    active_transfer = [None]

    def flush_network(force=False):
        if callback and pending[0] and (force or time.monotonic() >= pending[1]):
            callback(network_delta=pending[0])
            pending[:] = [0, time.monotonic()+1]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.transfer(True)

        def do_GET(self):
            self.transfer(False)

        def transfer(self, head):
            if self.path != nonce:
                self.send_error(404)
                return
            response = None
            admitted = False
            self.superseded = threading.Event()
            if _ali_callback(url):
                # FFmpeg opens the next Range before closing the old response.
                # End only this video's superseded stream before waiting for
                # the shared provider slot; other gateways keep their stream.
                with transfer_lock:
                    previous = active_transfer[0]
                    active_transfer[0] = self
                    if previous is not None:
                        previous.superseded.set()
                        try:
                            previous.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
            try:
                if send_buffer_bytes is not None:
                    # Cover probes frequently seek away after reading a header.
                    # Limit abandoned read-ahead; full-video transfers keep defaults.
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer_bytes)
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if time.monotonic() >= deadline or cancelled():
                    raise MediaError('媒体读取已停止')
                current = url
                if _ali_callback(url):
                    while not _ALI_CALLBACK_SLOT.acquire(timeout=.1):
                        if self.superseded.is_set():
                            raise OSError('superseded media range')
                        if cancelled() or time.monotonic() >= deadline:
                            raise MediaError('媒体读取已停止或超时')
                        if callback:
                            callback()
                    admitted = True
                for _ in range(6):
                    approve_url(current, openlist_url)
                    headers = media_headers(current)
                    if urlsplit(current)[:2] == urlsplit(url)[:2]:
                        headers.update(request_headers or {})
                    if self.headers.get('Range'):
                        headers['Range'] = self.headers['Range']
                    response = _open_media_response(client, 'HEAD' if head else 'GET', current, headers, deadline,
                                                    lambda: cancelled() or self.superseded.is_set())
                    if response.status_code in (301,302,303,307,308):
                        target = response.headers.get('location', '')
                        response.close()
                        current = urljoin(current, target)
                        continue
                    break
                if response is None or response.status_code not in (200,206):
                    status = response.status_code if response is not None else 502
                    raise MediaError(f'媒体源拒绝读取（HTTP {status}），请检查直链有效期和网盘下载权限')
                self.send_response(response.status_code)
                for key in ('Content-Length','Content-Range','Content-Type'):
                    if key in response.headers:
                        self.send_header(key, response.headers[key])
                if response.status_code == 206 and response.headers.get('Content-Range','').startswith('bytes '):
                    self.send_header('Accept-Ranges', 'bytes')
                elif 'Accept-Ranges' in response.headers:
                    self.send_header('Accept-Ranges',response.headers['Accept-Ranges'])
                self.send_header('Connection', 'close')
                self.end_headers()
                if not head:
                    for chunk in response.iter_raw(chunk_size=32768):
                        if self.superseded.is_set():
                            raise OSError('superseded media range')
                        with lock:
                            if len(chunk) > remaining[0] or time.monotonic() >= deadline or cancelled():
                                failures.append('媒体读取超过单片流量预算或超时，已停止下载')
                                break
                            remaining[0] -= len(chunk)
                            used[0] += len(chunk)
                            pending[0] += len(chunk)
                            flush_network()
                        self.wfile.write(chunk)
            except (httpx.HTTPError, OSError, MediaError) as error:
                if isinstance(error, MediaError) and not self.superseded.is_set():
                    failures.append(str(error))
                elif isinstance(error, httpx.HTTPError) and not self.superseded.is_set():
                    failures.append('媒体源连接或读取失败，请检查网盘/CDN 网络和限速')
                self.close_connection = True
            finally:
                with lock:
                    flush_network(True)
                try:
                    if response:
                        response.close()
                finally:
                    if admitted:
                        _ALI_CALLBACK_SLOT.release()
                    with transfer_lock:
                        if active_transfer[0] is self:
                            active_transfer[0] = None
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            yield f'http://127.0.0.1:{server.server_port}{nonce}', used
        except MediaError:
            if failures:
                raise MediaError(failures[-1]) from None
            raise
    finally:
        server.shutdown()
        server.server_close()
        client.close()
        thread.join(timeout=2)


def run_process(args, timeout, cancelled=lambda: False):
    # Never log command lines: they may contain a short-lived gateway URL.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='aitags-progress-') as directory, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as errors:
        args = list(args)
        program = Path(args[0]).stem.lower()
        tracked = reporter() is not None
        progress = Path(directory)/'progress.txt'
        if tracked and program.startswith('ffmpeg'):
            args[1:1] = ['-progress', str(progress), '-stats_period', '1', '-nostats']
        proc = subprocess.Popen(args, stdout=out, stderr=errors)
        deadline = time.monotonic() + timeout
        next_report = 0

        def pulse():
            if not tracked:
                return
            values = read_ffmpeg_progress(progress)
            output = Path(args[-1])
            if output.is_file():
                values['output_bytes'] = output.stat().st_size
            report(process=program, **values)
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline or cancelled():
                    raise MediaError('媒体解码超时或任务取消')
                if time.monotonic() >= next_report:
                    pulse()
                    next_report = time.monotonic()+1
                time.sleep(.1)
            pulse()
            if proc.returncode:
                errors.seek(0)
                detail = errors.read(8192).decode(errors='replace').lower()
                if 'no space left' in detail or 'disk quota exceeded' in detail:
                    raise MediaError('视频压缩临时磁盘空间或账号配额不足')
                if any(word in detail for word in ('moov atom not found','invalid data found','error reading header')):
                    raise MediaError('视频容器损坏、不完整或文件内容不是有效视频')
                raise MediaError('FFmpeg 无法解码；检查格式、Range 支持、流量上限和媒体域名白名单')
            out.seek(0)
            return out.read(1024 * 1024)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def sample_frames(url, openlist_url, settings, cancelled=lambda: False, request_headers=None):
    started = time.monotonic()
    timeout = settings['media_timeout']
    def left():
        return max(.1, timeout - (time.monotonic() - started))
    with gateway(url, openlist_url, settings['max_network_bytes'], timeout, cancelled, request_headers=request_headers) as (source, used):
        probe = run_process([config.FFPROBE, '-v','error','-protocol_whitelist','http,tcp','-format_whitelist',FORMATS,
                             '-rw_timeout','15000000','-probesize','5000000','-analyzeduration','5000000',
                             '-show_entries','format=duration','-of','json', source], left(), cancelled)
        duration = float(json.loads(probe).get('format', {}).get('duration', 0))
        if duration <= 0 or duration > 86400 * 30:
            raise MediaError('无法确定有效视频时长')
        count = settings['frames']
        timestamps = [(i + .5) * duration / count for i in range(count)]
        images = []
        with TemporaryDirectory(prefix='aitags-') as temp:
            for i, second in enumerate(timestamps):
                path = Path(temp) / f'{i}.jpg'
                run_process([config.FFMPEG,'-v','error','-nostdin','-y','-threads','1',
                             '-protocol_whitelist','http,tcp','-format_whitelist',FORMATS,'-rw_timeout','15000000',
                             '-ss', str(second), '-i', source, '-frames:v','1','-an',
                             '-vf','scale=768:768:force_original_aspect_ratio=decrease',
                             '-threads','1','-q:v','4', str(path)], left(), cancelled)
                if not path.exists() or path.stat().st_size > 2 * 1024 * 1024:
                    raise MediaError('抽帧失败或图片过大')
                images.append({'second': round(second,2), 'data': base64.b64encode(path.read_bytes()).decode()})
        return {'duration': duration, 'frames': images, 'network_bytes': used[0]}


# Leave room for Base64 and JSON under the measured ModelBest 32 MiB request limit.
NATIVE_VIDEO_MAX_BYTES = 23 * 1024**2


def video_duration(source, options, timeout, cancelled):
    probe = run_process([config.FFPROBE, '-v','error',*options,'-select_streams','V:0',
                         '-probesize','5000000','-analyzeduration','5000000',
                         '-show_entries','stream=duration:format=duration','-of','json',source],
                        min(timeout, config.VIDEO_PROBE_TIMEOUT), cancelled)
    data = json.loads(probe)
    streams = data.get('streams', [])
    if not streams:
        raise MediaError('找不到可识别的视频轨道')
    for value in (streams[0].get('duration'), data.get('format', {}).get('duration')):
        if value in (None, 'N/A'):
            continue
        duration = float(value)
        if math.isfinite(duration) and duration > 0:
            return duration
    raise MediaError('无法确定有效视频时长')


def detect_subtitle_crop(source, options, duration, root, remaining, cancelled):
    from .subtitles import crop_filter,recognize_pgm
    candidates = list(dict.fromkeys([config.SUBTITLE_OCR_CROP,'0.1,0.87,0.8,0.07','0.1,0.925,0.8,0.075']))
    scores = {crop:0 for crop in candidates}
    hits = {crop:0 for crop in candidates}
    fractions = (.1,.35,.6,.5,.75,.9,.05,.15,.2,.25,.3,.4,.45,.55,.65,.7,.8,.85,.95)
    report(stage='subtitle_layout')
    for i,fraction in enumerate(fractions):
        image = root/f'layout-{i}.jpg'
        run_process([config.FFMPEG,'-v','error','-nostdin','-y','-threads','1',*options,
                     '-ss',str(duration*fraction),'-i',source,'-frames:v','1','-an',
                     '-vf',"scale=w='min(1920,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease",
                     '-threads','1','-q:v','3',str(image)],min(40,remaining()),cancelled)
        if not 0 < image.stat().st_size <= 2*1024**2:
            raise MediaError('字幕区域探测画面过大或无法读取')
        for crop in candidates:
            pixels = run_process([config.FFMPEG,'-v','error','-nostdin','-threads','1','-i',str(image),
                                  '-vf',crop_filter(crop),'-threads','1','-vcodec','pgm',
                                  '-f','image2pipe','-'],remaining(),cancelled)
            if cancelled():
                raise MediaError('字幕区域探测已取消')
            text = recognize_pgm(pixels,min(12,remaining()))
            count = sum('\u4e00' <= c <= '\u9fff' for c in text)
            if count >= 4:
                hits[crop] += 1
                scores[crop] += min(30,count)
        winner = max(scores,key=scores.get)
        report(processed_entries=i+1, total_entries=len(fractions), advanced=True)
        if i >= 2 and scores[winner] >= 20 and hits[winner] >= 2:
            break
    if scores[winner] < 12 or hits[winner] < 2:
        raise MediaError('字幕位置探测未读取到可靠文字，请手动设置字幕区域并关闭自动探测')
    return max(scores,key=scores.get)


def refresh_context_frames(sample, url, openlist_url, settings, cancelled=lambda:False, request_headers=None):
    """Upgrade only still-image evidence; retain the full MP4 and all extracted captions."""
    from .subtitles import relation_points
    deadline = time.monotonic()+min(settings['media_timeout'],240)
    frames = []
    existing = {f['second']:f for f in sample.get('context_frames',[])}
    options = ['-protocol_whitelist','http,tcp','-format_whitelist',FORMATS,'-rw_timeout','15000000']
    with gateway(url,openlist_url,min(settings['max_network_bytes'],64*1024**2),240,cancelled,request_headers=request_headers) as (source,used):
        with TemporaryDirectory(prefix='aitags-context-') as temp:
            for i,point in enumerate(relation_points(sample['subtitles'],sample['duration'],settings.get('context_frames',5))):
                if point['second'] in existing:
                    frames.append({**existing[point['second']],**point})
                    continue
                output = Path(temp)/f'{i}.jpg'
                run_process([config.FFMPEG,'-v','error','-nostdin','-y','-threads','1',*options,
                             '-ss',str(point['second']),'-i',source,'-frames:v','1','-an',
                             '-vf',"scale=w='min(1920,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease",
                             '-threads','1','-q:v','2',str(output)],min(40,max(.1,deadline-time.monotonic())),cancelled)
                if not 0 < output.stat().st_size <= 2*1024**2:
                    raise MediaError('人物核对画面超过大小限制或提取失败')
                frames.append({**point,'data':base64.b64encode(output.read_bytes()).decode()})
    return {**sample,'context_frames':frames,'context_revision':3,
            'network_bytes':sample.get('network_bytes',0)+used[0]}


def compress_video(url, openlist_url, settings, cancelled=lambda: False, subtitles=False, request_headers=None):
    timeout = settings['media_timeout']
    deadline = time.monotonic() + timeout
    def remaining():
        return max(.1, deadline - time.monotonic())

    def artwork(sample,output):
        # No second cloud-media read: use the local compressed timeline.
        try:
            report(stage='cover')
            cover=output.with_name('poster.jpg')
            run_process([config.FFMPEG,'-v','error','-nostdin','-y','-threads','1',
                '-ss',str(min(sample['duration']/3,30)),'-i',str(output),'-frames:v','1','-an',
                '-threads','1','-q:v','3',str(cover)],min(10,remaining()),cancelled)
            data=cover.read_bytes()
            if 0<len(data)<=1024**2:sample['cover']=base64.b64encode(data).decode()
        except (MediaError,OSError):pass
        return sample

    http_options = ['-protocol_whitelist','http,tcp','-format_whitelist',FORMATS,'-rw_timeout','15000000']
    with gateway(url, openlist_url, settings['max_network_bytes'], timeout, cancelled, request_headers=request_headers) as (source, used):
        report(stage='probe')
        duration = video_duration(source, http_options, remaining(), cancelled)
        report(duration=duration)
        subtitle_index = None
        if subtitles:
            probe = run_process([config.FFPROBE,'-v','error',*http_options,'-select_streams','s',
                                 '-show_entries','stream=index,codec_name:stream_tags=language',
                                 '-of','json',source],remaining(),cancelled)
            streams = json.loads(probe).get('streams',[])
            text_streams = [s for s in streams if s.get('codec_name') in ('subrip','ass','ssa','mov_text','webvtt','text')]
            text_streams.sort(key=lambda s: s.get('tags',{}).get('language','') not in ('chi','zho','zh','zh-CN'))
            if text_streams:
                subtitle_index = text_streams[0]['index']
        fps, edge = (2, 320) if duration > 3600 else (3, 480) if duration > 900 else (6, 640)
        # Reserve container overhead and one MiB of headroom for rate control.
        payload = NATIVE_VIDEO_MAX_BYTES - 1024**2 - math.ceil(duration * fps) * 32
        bitrate = min(1_000_000, int(8 * payload / duration) // 1000 * 1000)
        if bitrate < 1000:
            raise MediaError('视频时长过长，无法在 23 MiB 内保留完整时间线')
        with TemporaryDirectory(prefix='aitags-') as temp:
            output = Path(temp) / 'video.mp4'
            scaled = Path(temp) / 'scaled.mkv'
            filters = f"fps={fps},scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
            encoding = {'codec':'h264','preset':'veryfast','passes':2,'fps':fps,
                        'max_edge':edge,'bitrate':bitrate,'audio':False}
            if not subtitles and duration <= 90:
                report(stage='encoding')
                # The capped stream fits well within 23 MiB for short clips.
                # Encode directly, without an intermediate file or a second pass.
                run_process([config.FFMPEG,'-v','error','-xerror','-nostdin','-y','-threads','2',
                             *http_options,'-i',source,'-map','0:V:0','-an','-sn','-dn',
                             '-map_metadata','-1','-map_chapters','-1','-filter_threads','1',
                             '-vf',filters,'-c:v','libx264','-preset','veryfast','-threads','1',
                             '-pix_fmt','yuv420p','-crf','18','-maxrate',str(bitrate),
                             '-bufsize',str(2*bitrate),'-movflags','+faststart',
                             '-fs',str(NATIVE_VIDEO_MAX_BYTES),str(output)],remaining(),cancelled)
                size = output.stat().st_size
                if 0 < size <= NATIVE_VIDEO_MAX_BYTES and abs(
                    duration - video_duration(str(output), [], remaining(), cancelled)
                ) <= 1 / fps + .05:
                    return artwork({'duration':duration,'video':base64.b64encode(output.read_bytes()).decode(),
                            'video_bytes':size,'network_bytes':used[0],'context_frames':[],
                            'context_revision':3,'encoding':{**encoding,'passes':1,'rate_control':'crf'}},output)
                output.unlink()
            # Decode the cloud source once. Both x264 passes reuse a bounded lossless copy.
            decode = [config.FFMPEG,'-v','error','-xerror','-nostdin','-y','-threads','2',
                         *http_options,'-i',source,'-map','0:V:0','-an','-sn','-dn',
                         '-map_metadata','-1','-map_chapters','-1','-filter_threads','1',
                         '-vf',filters,'-c:v','ffv1','-threads','1','-pix_fmt','yuv420p',
                         '-fs',str(config.VIDEO_WORK_MAX_BYTES),str(scaled)]
            subtitle_data = None
            report(stage='decode_subtitles' if subtitles else 'decoding')
            if subtitles:
                from .subtitles import decode_with_ocr, parse_srt
                if subtitle_index is not None:
                    track = Path(temp)/'track.srt'
                    run_process([*decode,'-map',f'0:{subtitle_index}','-c:s','srt','-f','srt',
                                 '-fs',str(1024**2),str(track)],remaining(),cancelled)
                    if track.stat().st_size >= 1024**2:
                        raise MediaError('字幕轨超过 1 MiB 提取上限')
                    subtitle_data = {'source':'text-track','cues':parse_srt(track.read_text(encoding='utf-8'))}
                else:
                    from .subtitles import crop_filter
                    interval = max(1, math.ceil(duration/7200))
                    selected_crop = config.SUBTITLE_OCR_CROP
                    if config.SUBTITLE_OCR_AUTO_CROP:
                        selected_crop = detect_subtitle_crop(source,http_options,duration,Path(temp),remaining,cancelled)
                    report(stage='decode_subtitles')
                    crop = f"fps=1/{interval}:start_time=0,"+crop_filter(selected_crop)
                    subtitle_data = decode_with_ocr([
                        *decode,'-map','0:V:0','-an','-sn','-dn','-filter_threads','1','-vf',crop,
                        '-c:v','rawvideo','-threads','1','-pix_fmt','gray','-f','rawvideo','pipe:1'],
                        remaining(),interval,cancelled)
                    subtitle_data['crop'] = selected_crop
                    subtitle_data['crop_auto_detected'] = config.SUBTITLE_OCR_AUTO_CROP
                if not subtitle_data['cues']:
                    raise MediaError('未提取到可靠字幕，请检查字幕位置、语言或 OCR 字库')
                for cue in subtitle_data['cues']:
                    cue['end'] = min(duration,cue['end'])
            else:
                run_process(decode,remaining(),cancelled)
            report(stage='validating')
            scaled_duration = video_duration(str(scaled), [], remaining(), cancelled)
            if scaled.stat().st_size > config.VIDEO_WORK_MAX_BYTES or abs(duration - scaled_duration) > 1 / fps + .05:
                raise MediaError('无损中间视频超出临时存储上限或未覆盖完整时间线')
            for pass_number in (1, 2):
                report(stage='encoding_analysis' if pass_number==1 else 'encoding')
                destination = ['-f','null','-'] if pass_number == 1 else [
                    '-movflags','+faststart','-fs',str(NATIVE_VIDEO_MAX_BYTES),str(output)]
                run_process([config.FFMPEG,'-v','error','-xerror','-nostdin','-y','-threads','1',
                             '-i',str(scaled),'-map','0:V:0','-an','-sn','-dn',
                             '-map_metadata','-1','-map_chapters','-1',
                             '-c:v','libx264','-preset','veryfast','-threads','1','-pix_fmt','yuv420p',
                             '-b:v',str(bitrate),'-pass',str(pass_number),'-passlogfile',str(Path(temp)/'x264'),
                             *destination], remaining(), cancelled)
            size = output.stat().st_size
            if not 0 < size <= NATIVE_VIDEO_MAX_BYTES:
                raise MediaError('压缩视频超过插件的 23 MiB 上传上限')
            report(stage='validating')
            encoded_duration = video_duration(str(output), [], remaining(), cancelled)
            if abs(duration - encoded_duration) > 1 / fps + .05:
                raise MediaError('压缩视频未覆盖原始时间线，已停止上传')
            context_frames = []
            if subtitle_data:
                report(stage='context_frames')
                from .subtitles import relation_points
                for i, point in enumerate(relation_points(subtitle_data,duration,settings.get('context_frames',5))):
                    path = Path(temp)/f'context-{i}.jpg'
                    run_process([config.FFMPEG,'-v','error','-nostdin','-y','-threads','1',*http_options,
                                 '-ss',str(point['second']),'-i',source,'-frames:v','1','-an',
                                 '-vf','scale=1920:1920:force_original_aspect_ratio=decrease',
                                 '-threads','1','-q:v','3',str(path)],min(40,remaining()),cancelled)
                    if not 0 < path.stat().st_size <= 2*1024**2:
                        raise MediaError('关系核对画面过大或未能提取')
                    context_frames.append({**point,'data':base64.b64encode(path.read_bytes()).decode()})
            sample = {'duration':duration,'video':base64.b64encode(output.read_bytes()).decode(),
                    'video_bytes':size,'network_bytes':used[0],
                    'context_frames':context_frames,
                    'context_revision':3,
                    'encoding':encoding}
            if subtitle_data:
                sample['subtitles'] = subtitle_data
            return artwork(sample,output)
