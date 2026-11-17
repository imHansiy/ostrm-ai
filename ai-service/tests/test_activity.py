import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from aitags.activity import Activity, report, reporter, read_ffmpeg_progress
from aitags.config import FFMPEG
from aitags.media import run_process, gateway


def task(identifier=1,**kwargs):
    return {'id':identifier,'run_id':36,'path':f'/videos/{identifier}.mp4','size':2048,**kwargs}


def test_heartbeat_is_distinct_from_real_progress(monkeypatch):
    clock=[100.0]
    monkeypatch.setattr('aitags.activity.time.time',lambda:clock[0])
    monitor=Activity();token=monitor.begin(task())
    try:
        report(stage='encoding',duration=100,frame=20,out_seconds=10)
        clock[0]+=30
        report(process='ffmpeg',fps=0,speed=0)
        row=monitor.snapshot()['active'][0]
        assert row['heartbeat_age_seconds']==0
        assert row['progress_age_seconds']==30 and row['stage_percent']==10
        report(out_seconds=20,network_delta=1024)
        row=monitor.snapshot()['active'][0]
        assert row['stage_percent']==20 and row['network_bytes']==1024
        report(stage='model_video')
        assert monitor.snapshot()['active'][0]['stage_percent'] is None
    finally:monitor.finish(1,'done',token)
    assert reporter() is None and not monitor.snapshot()['active']


def test_concurrent_jobs_and_directory_ids_are_isolated():
    monitor=Activity();barrier=threading.Barrier(3)
    def work(identifier):
        token=monitor.begin(task(identifier))
        try:
            report(network_delta=identifier*100,stage='encoding')
            barrier.wait(timeout=5);barrier.wait(timeout=5)
        finally:monitor.finish(identifier,'done',token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(work,identifier) for identifier in (1,2)]
        # A separate directory scope can have the same numeric ID as a video.
        token=monitor.begin(task(1,kind='directory',path='/videos'))
        try:
            report(stage='listing')
            barrier.wait(timeout=5)
            rows=monitor.snapshot()['active']
            assert len(rows)==3
            assert {(row['id'],row['network_bytes']) for row in rows if row['kind']=='video'}=={(1,100),(2,200)}
            barrier.wait(timeout=5)
        finally:
            barrier.abort()
            monitor.finish(1,'page_done',token,kind='directory')
        for future in futures:
            try:future.result()
            except threading.BrokenBarrierError:pass
    assert not monitor.snapshot()['active']
    assert reporter() is None


def test_real_ffmpeg_reports_frames_without_exposing_arguments(tmp_path):
    import shutil
    if not shutil.which(FFMPEG):pytest.skip('FFmpeg is not installed')
    monitor=Activity();token=monitor.begin(task())
    output=tmp_path/'video.mp4'
    try:
        report(stage='encoding',duration=2)
        run_process([FFMPEG,'-v','error','-f','lavfi','-i','color=c=blue:s=32x32:r=8:d=2',
                     '-c:v','libx264','-threads','1','-metadata','comment=https://secret.test/?key=private',str(output)],20)
        row=monitor.snapshot()['active'][0]
        assert row['frame']>=8 and row['out_seconds']>=1
        assert row['output_bytes']==output.stat().st_size
        assert row['process'].startswith('ffmpeg')
        assert 'private' not in json.dumps(row) and 'secret.test' not in json.dumps(row)
    finally:monitor.finish(1,'done',token)


def test_network_thread_updates_parent_task(tmp_path):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import httpx
    data=b'video data'*100
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    monitor=Activity();token=monitor.begin(task())
    try:
        with gateway(origin+'/private-signed-url',origin,4096,10) as (url,used):
            assert httpx.get(url).content==data
        row=monitor.snapshot()['active'][0]
        assert row['network_bytes']==len(data)==used[0]
        assert 'private-signed-url' not in json.dumps(row)
    finally:
        monitor.finish(1,'done',token);server.shutdown();server.server_close();thread.join(2)


def test_malformed_progress_tail_is_bounded(tmp_path):
    path=tmp_path/'progress.txt'
    path.write_text('noise\n'*10000+'frame=32\nfps=N/A\nout_time_us=2000000\nspeed=nan\n')
    assert read_ffmpeg_progress(path)=={'frame':32,'out_seconds':2}


def test_subtitle_decoder_reports_real_consumed_frames(monkeypatch):
    import shutil
    if not shutil.which(FFMPEG):pytest.skip('FFmpeg is not installed')
    from aitags.subtitles import decode_with_ocr,OCR_WIDTH,OCR_HEIGHT
    monkeypatch.setattr('aitags.subtitles.recognize_pgm',lambda *a:'测试字幕')
    monitor=Activity();token=monitor.begin(task())
    try:
        report(stage='decode_subtitles',duration=2)
        result=decode_with_ocr([FFMPEG,'-v','error','-f','lavfi','-i',f'color=c=white:s={OCR_WIDTH}x{OCR_HEIGHT}:r=1:d=2',
            '-frames:v','2','-c:v','rawvideo','-threads','1','-pix_fmt','gray','-f','rawvideo','pipe:1'],20,1,lambda:False)
        row=monitor.snapshot()['active'][0]
        assert result['frames']==row['frame']==2
        assert row['out_seconds']==2 and row['stage_percent']==100
        assert row['process']=='subtitle_ocr'
    finally:monitor.finish(1,'done',token)


def test_worker_failure_cleans_activity_and_reports_last_success(env,monkeypatch):
    db,ol,worker,client=env
    run=worker.create_run('/public')
    worker.enqueue('/public/a.mp4',ol.objects['/public/a.mp4'],run)
    def failed(*a,**kwargs):
        assert worker.activity.snapshot()['active'][0]['stage']=='probe'
        raise RuntimeError('fixture decode failure')
    monkeypatch.setattr('aitags.worker.compress_video',failed)
    assert worker.work_once()
    snapshot=client.get('/ai/api/overview',headers={'Authorization':'admin'}).json()
    assert snapshot['activity']['active']==[]
    assert snapshot['activity']['recent'][0]['state']=='failed'
    assert snapshot['activity']['recent'][0]['error']=='fixture decode failure'
    assert client.get('/ai/api/overview').status_code==403
