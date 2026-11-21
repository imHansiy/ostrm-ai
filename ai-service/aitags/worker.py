import base64
import hashlib
import json
import posixpath
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from fastapi import HTTPException
from . import config
from .openlist import clean_path
from .media import compress_video, sample_frames, refresh_context_frames, use_fetch_relay
from .model import infer, ModelRoutingError
from .subtitles import save_subtitles
from .evidence_cache import EvidenceCache
from .lock import WorkerLock
from .covers import cached_cover, extract_cover, read_sidecar, save_cover
from .activity import Activity, report
from .postprocess import Postprocess
from .events import safe_event


def fingerprint(obj):
    value = {k: obj.get(k) for k in ('size','modified','hash_info','hashinfo')}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def identity(obj):
    # Only strong full-file hashes allow safe reuse across rename/copy.
    hashes = obj.get('hash_info') or {}
    strong = {k.lower():v for k,v in hashes.items() if k.lower() in ('sha1','sha256','md5') and v}
    return json.dumps({'size':obj.get('size'), 'hashes':strong}, sort_keys=True) if strong else ''


def safe_error(exc):
    if isinstance(exc, HTTPException):
        return str(exc.detail)[:500]
    # Our own errors have no signed URLs or credentials. Third-party errors are not persisted.
    if isinstance(exc, RuntimeError):
        return str(exc)[:500]
    return '处理失败：' + type(exc).__name__


class Worker:
    def __init__(self, db, ol):
        self.db, self.ol = db, ol
        self.evidence_cache = EvidenceCache(Path(db.path).parent/'evidence-cache')
        self.stop = threading.Event()
        self.threads = []
        self.concurrency_lock = threading.Lock()
        self.concurrency = 0
        self.concurrency_checked = float('-inf')
        self.cover_cursor = 0
        self.on_result = None
        self.activity = Activity()
        self.stage_condition = threading.Condition()
        self.stage_counts = {'prepare':0, 'model':0}
        self.postprocess = Postprocess(db, ol, lambda path: self.on_result(path) if self.on_result else None)
        from .recovery import Recovery
        self.recovery = Recovery(db)

    def event(self, path, state, message):
        return safe_event(self.db, path, state, message)

    def start(self):
        self.process_lock=WorkerLock(self.db.path+'.worker.lock')
        self.db.recover()
        for fn, args in [(self.scan_loop, ()), (self.cover_loop, ()), (self.postprocess_loop, ()), *[(self.work_loop,(i,)) for i in range(4)]]:
            t = threading.Thread(target=fn, args=args, daemon=True)
            t.start()
            self.threads.append(t)

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=20)
        if hasattr(self,'process_lock') and not any(t.is_alive() for t in self.threads):
            self.process_lock.close()

    def cover_loop(self):
        while not self.stop.is_set():
            try:
                if self.db.settings().get('cover_backfill'):
                    self.cover_once()
            except Exception:
                self.event('/','warning','封面任务暂时失败')
            self.stop.wait(6)

    def postprocess_loop(self):
        while not self.stop.is_set():
            try:
                busy = self.postprocess.once()
            except Exception:
                busy = False
            self.stop.wait(.5 if busy else 3)

    @contextmanager
    def stage(self, name, limit):
        """Bound preparation and inference independently; release is idempotent."""
        acquired = False
        def release():
            nonlocal acquired
            with self.stage_condition:
                if acquired:
                    acquired = False
                    self.stage_counts[name] -= 1
                    self.stage_condition.notify_all()
        try:
            with self.stage_condition:
                while self.stage_counts[name] >= limit and not self.stop.is_set():
                    self.stage_condition.wait(.2)
                if self.stop.is_set():
                    yield None
                    return
                self.stage_counts[name] += 1
                acquired = True
            yield release
        finally:
            release()

    def cover_once(self):
        # A failed extraction still has a covers row. Revisit empty/evicted
        # private covers after a cooldown, rotating past failures and cache hits.
        rows = self.db.all("""SELECT v.rowid,v.path,v.fingerprint FROM videos v LEFT JOIN covers c
            ON v.path=c.path AND v.fingerprint=c.fingerprint
            WHERE v.status='done' AND v.rowid>? AND
                (c.path IS NULL OR (c.sidecar IS NULL AND c.updated<?)) AND NOT EXISTS (
                SELECT 1 FROM postprocess_jobs p WHERE p.path=v.path AND p.fingerprint=v.fingerprint)
            ORDER BY v.rowid LIMIT 64""", (self.cover_cursor, time.time()-600))
        if not rows:
            self.cover_cursor = 0
            return
        row = None
        for candidate in rows:
            self.cover_cursor = candidate['rowid']
            if not cached_cover(self.db, candidate['path'], candidate['fingerprint']):
                row = candidate
                break
        if row is None:
            return
        path, version = row['path'], row['fingerprint']
        jpeg = None
        try:
            token = self.ol.admin_token()
            obj = self.ol.get(path,token)
            if obj.get('is_dir') or fingerprint(obj)!=version:
                raise RuntimeError('视频版本已变化')
            # Cloud-provided video thumbnails avoid probing a large container
            # just to recover one frame. Keep the same bounded, approved reader.
            if obj.get('thumb'):
                try:
                    jpeg = read_sidecar(obj['thumb'], self.ol.url)
                except (HTTPException, RuntimeError, KeyError, ValueError, OSError):
                    pass
            if not jpeg:
                jpeg = extract_cover(obj['raw_url'],self.ol.url,self.stop.is_set,obj.get('size',0))
            if self.stop.is_set():
                return
            if fingerprint(self.ol.get(path,token))!=version:
                raise RuntimeError('视频版本已变化')
        except Exception:
            jpeg = None
        if not self.stop.is_set():
            if jpeg:
                # Persist upload/publication work before exposing the cache.
                # Slow read-only mounts must not hold up extraction of the next cover.
                with self.db.connect() as connection:
                    Postprocess.enqueue(connection, {'path':path,'fingerprint':version},
                                        base64.b64encode(jpeg).decode('ascii'))
            save_cover(self.db,path,version,None,jpeg)

    def create_run(self, root, content_type='auto', use_subtitles=False):
        root = clean_path(root)
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM runs WHERE root=? AND state IN ('running','paused')", (root,)).fetchone()
            if existing:
                return existing['id']
            rid = c.execute('INSERT INTO runs(root,created,content_type,use_subtitles) VALUES (?,?,?,?)', (root,time.time(),content_type,int(use_subtitles))).lastrowid
            c.execute('INSERT INTO directories(run_id,path) VALUES (?,?)', (rid,root))
            return rid

    def enqueue(self, path, obj, rid, force=False):
        if self.restore_deferred(path):
            return False
        fp, ident = fingerprint(obj), identity(obj)
        now = time.time()
        with self.db.connect() as c:
            old = c.execute('SELECT * FROM videos WHERE path=?', (path,)).fetchone()
            if old and old['fingerprint'] == fp and old['status'] == 'done' and not force:
                return False
            if c.execute("SELECT id FROM tasks WHERE path=? AND state IN ('queued','running')", (path,)).fetchone():
                return False
            reuse = c.execute("SELECT * FROM videos WHERE identity=? AND status='done' LIMIT 1", (ident,)).fetchone() if ident and not old else None
            size, modified = obj.get('size',0), obj.get('modified','')
            if old:
                result = old['result'] if old['fingerprint']==fp else '{}'
                c.execute("""UPDATE videos SET fingerprint=?,identity=?,size=?,modified=?,updated=?,
                    status='pending',error='',result=? WHERE path=?""",(fp,ident,size,modified,now,result,path))
            else:
                c.execute("""INSERT INTO videos(path,fingerprint,identity,size,modified,updated,result,manual,removed)
                    VALUES (?,?,?,?,?,?,'{}','[]','[]')""",(path,fp,ident,size,modified,now))
            if reuse and not force:
                c.execute("UPDATE videos SET status='done',result=?,manual=?,removed=? WHERE path=?",
                          (reuse['result'],reuse['manual'],reuse['removed'],path))
                return False
            if c.execute('SELECT id FROM tasks WHERE run_id=? AND path=?',(rid,path)).fetchone():
                c.execute("UPDATE tasks SET fingerprint=?,state='queued',error='',created=? WHERE run_id=? AND path=?",
                          (fp,now,rid,path))
            else:
                c.execute('INSERT INTO tasks(run_id,path,fingerprint,created) VALUES (?,?,?,?)',(rid,path,fp,now))
            return True

    def excluded(self, path, settings):
        return path in settings.get('restore_deferred_successes',{}) or any(path == p or path.startswith(p.rstrip('/') + '/') for p in settings['exclude'])

    def restore_deferred(self, path):
        row = self.db.one("SELECT value FROM settings WHERE `key`='restore_deferred_successes'")
        return bool(row and path in json.loads(row['value']))

    def scan_once(self):
        settings = self.db.settings()
        with self.db.connect() as c:
            row = c.execute("SELECT d.* FROM directories d JOIN runs r ON r.id=d.run_id WHERE d.state='queued' AND r.state='running' ORDER BY d.id LIMIT 1").fetchone()
            if not row:
                return False
            row = dict(row)
            c.execute("UPDATE directories SET state='running' WHERE id=?", (row['id'],))
        activity_token = self.activity.begin({**row,'kind':'directory'})
        outcome, activity_error = 'page_done', ''
        try:
            report(stage='listing', deadline_at=time.time()+config.OPENLIST_SCAN_TIMEOUT)
            if self.excluded(row['path'], settings):
                self.db.execute("UPDATE directories SET state='done' WHERE id=?", (row['id'],))
                return True
            data = self.ol.listing(row['path'], self.ol.admin_token(), row['page'], settings['page_size'],timeout=config.OPENLIST_SCAN_TIMEOUT)
            objects = data.get('content') or []
            report(stage='indexing', total_entries=len(objects))
            for index,obj in enumerate(objects,1):
                report(processed_entries=index)
                run = self.db.one('SELECT state FROM runs WHERE id=?', (row['run_id'],))
                if self.stop.is_set() or run['state'] == 'cancelled':
                    outcome = 'queued' if self.stop.is_set() else 'cancelled'
                    return True
                name = obj.get('name','')
                if not name or '/' in name or '\\' in name or name in ('.','..'):
                    continue
                path = clean_path(posixpath.join(row['path'],name))
                if self.excluded(path,settings):
                    continue
                if obj.get('is_dir'):
                    if row['depth'] >= settings['max_depth']:
                        self.event(path,'skipped','超过目录深度上限')
                        continue
                    self.db.add_directory(row['run_id'],path,row['depth']+1)
                elif posixpath.splitext(name)[1].lstrip('.').lower() in settings['extensions']:
                    count = self.db.one('SELECT count(*) n FROM videos')['n']
                    if count >= settings['max_files'] and not self.db.one('SELECT path FROM videos WHERE path=?',(path,)):
                        raise RuntimeError('超过视频索引数量上限')
                    self.enqueue(path,obj,row['run_id'])
            more = len(objects) > 0 and row['page'] * settings['page_size'] < data.get('total',0)
            self.db.execute('UPDATE directories SET state=?,page=page+? WHERE id=?', ('queued' if more else 'done',int(more),row['id']))
        except Exception as exc:
            message = safe_error(exc)
            outcome, activity_error = 'failed', message
            self.db.execute("UPDATE directories SET state='failed' WHERE id=?",(row['id'],))
            self.db.execute('UPDATE runs SET error=? WHERE id=?',(message,row['run_id']))
            self.event(row['path'],'failed',message)
        finally:
            self.activity.finish(row['id'],outcome,activity_token,activity_error,kind='directory')
        return True

    def finish_runs(self):
        self.db.execute("""UPDATE runs SET state=CASE WHEN error!='' OR EXISTS(
          SELECT 1 FROM tasks WHERE run_id=runs.id AND state='failed') OR EXISTS(
          SELECT 1 FROM directories WHERE run_id=runs.id AND state='failed') THEN 'failed' ELSE 'done' END
          WHERE state='running' AND NOT EXISTS(SELECT 1 FROM directories WHERE run_id=runs.id AND state IN ('queued','running'))
          AND NOT EXISTS(SELECT 1 FROM tasks WHERE run_id=runs.id AND state IN ('queued','running'))""")

    def scan_loop(self):
        while not self.stop.is_set():
            busy=False
            delay=5
            try:
                s = self.db.settings()
                delay=max(.1,s['scan_delay'])
                self.recovery.tick()
                interval = s['interval_minutes'] * 60
                last = self.db.one("SELECT value FROM settings WHERE `key`='last_schedule'")
                if interval and time.time() - (float(json.loads(last['value'])) if last else 0) >= interval:
                    for root in s['roots']:
                        self.create_run(root)
                    self.db.save_settings({'last_schedule':time.time()})
                busy=self.scan_once()
                self.finish_runs()
            except Exception:
                # Keep service alive, but make orchestration failures observable without secrets.
                self.event('/','failed','扫描调度失败，请检查配置和 OpenList 连接')
            self.stop.wait(delay if busy else 5)

    def work_once(self):
        with self.stage('prepare', 1) as release:
            return self.prepare_once(release) if release else False

    def prepare_once(self, release_prepare):
        with self.db.connect() as c:
            plan_row = c.execute("SELECT value FROM settings WHERE `key`='failure_recovery'").fetchone()
            plan = json.loads(plan_row['value']) if plan_row else {}
            if plan.get('model_hold') and plan.get('state') in ('running','paused'):
                return False
            priority = plan.get('priority_ids',[]) if plan.get('state')=='running' and plan.get('priority_state')=='running' else []
            scope = ' AND t.id IN ('+','.join('?' for _ in priority)+')' if priority else ''
            row = c.execute("SELECT t.*,r.content_type,r.use_subtitles FROM tasks t JOIN runs r ON r.id=t.run_id WHERE t.state='queued' AND r.state='running'"+scope+" ORDER BY t.id LIMIT 1",priority).fetchone()
            if not row:
                return False
            task = dict(row)
            if self.restore_deferred(task['path']):
                c.execute("UPDATE tasks SET state='cancelled',error=? WHERE id=?",('已在远端识别成功，等待恢复资料',task['id']))
                return True
            current = c.execute('SELECT status,fingerprint,size FROM videos WHERE path=?',(task['path'],)).fetchone()
            if current and current['status']=='done' and current['fingerprint']==task['fingerprint']:
                c.execute("UPDATE tasks SET state='done',error='' WHERE id=?",(task['id'],))
                return True
            c.execute("UPDATE tasks SET state='running',attempts=attempts+1 WHERE id=?",(task['id'],))
            c.execute("UPDATE videos SET status='running',error='' WHERE path=?",(task['path'],))
        next_cancel_check = 0
        run_cancelled = False
        activity_token = self.activity.begin({**task, 'size': current['size'] if current else 0})
        outcome, activity_error = 'failed', ''
        started_at = time.monotonic()
        prepared_at = model_at = None

        def cancelled(force=False):
            nonlocal next_cancel_check, run_cancelled
            if self.stop.is_set() or run_cancelled:
                return True
            now = time.monotonic()
            if force or now >= next_cancel_check:
                # FFmpeg and the HTTP gateway both poll this callback. Avoid a
                # database round trip for every chunk of a remote video.
                next_cancel_check = now + 1
                run = self.db.one('SELECT state FROM runs WHERE id=?',(task['run_id'],))
                run_cancelled = not run or run['state'] == 'cancelled'
            return run_cancelled
        try:
            self.event(task['path'],'processing','正在读取视频信息')
            obj = (self.ol.get_media(task['path'], timeout=config.OPENLIST_SCAN_TIMEOUT)
                   if config.MEDIA_DIRECT_DRIVERS else
                   self.ol.get(task['path'],self.ol.admin_token(),timeout=config.OPENLIST_SCAN_TIMEOUT))
            source_options = {'request_headers':obj['_media_headers']} if obj.get('_media_headers') else {}
            report(size=obj.get('size',0))
            if fingerprint(obj) != task['fingerprint']:
                raise RuntimeError('视频已变化，请重新扫描后识别')
            self.db.execute("UPDATE videos SET modified=?,identity=?,size=? WHERE path=? AND modified=''",
                            (obj.get('modified',''),identity(obj),obj.get('size',0),task['path']))
            settings = self.db.settings()
            if obj.get('size',0) > settings['max_video_bytes']:
                raise RuntimeError('视频超过大小上限')
            if not obj.get('raw_url') or obj.get('is_dir'):
                raise RuntimeError('视频没有可读取的媒体地址')
            if config.MODEL_NATIVE_VIDEO and obj.get('size',0) > settings['max_network_bytes']:
                raise RuntimeError('源视频超过整段读取流量上限，请在 AI 设置中提高读取上限')
            if task['use_subtitles'] and not config.MODEL_NATIVE_VIDEO:
                raise RuntimeError('字幕联合识别需要启用整片视频模式')
            if config.MODEL_NATIVE_VIDEO:
                cache_path = self.evidence_cache.path(task['path'],task['fingerprint'],task['use_subtitles'],settings)
                sample = self.evidence_cache.load(cache_path)
                if sample is not None:
                    report(stage='cached', cache_hit=True)
                if (sample is not None and sample.get('subtitles') and task['content_type']=='movie'
                        and sample.get('context_revision',0)<3 and settings.get('context_frames',5)>0):
                    report(stage='context_frames')
                    sample = refresh_context_frames(sample,obj['raw_url'],self.ol.url,settings,cancelled,**source_options)
                    try:
                        self.evidence_cache.save(cache_path,sample)
                    except OSError:
                        self.event(task['path'],'warning','补充画面缓存未能保存，本次继续识别')
                if sample is None:
                    report(stage='probe', deadline_at=time.time()+settings['media_timeout'])
                    self.event(task['path'],'processing','正在压缩完整视频与提取证据')
                    def renew_source():
                        if cancelled():raise RuntimeError('任务已取消')
                        fresh=self.ol.get_media(task['path'],timeout=min(10,config.OPENLIST_SCAN_TIMEOUT))
                        if fresh.get('is_dir') or fingerprint(fresh)!=task['fingerprint']:
                            raise RuntimeError('视频已变化，停止刷新直链')
                        return fresh['raw_url'],fresh.get('_media_headers',{})
                    renewal={'refresh_source':renew_source} if use_fetch_relay(obj['raw_url']) else {}
                    sample = compress_video(obj['raw_url'],self.ol.url,settings,cancelled,
                                            subtitles=bool(task['use_subtitles']),source_size=obj.get('size',0),**source_options,**renewal)
                    try:
                        self.evidence_cache.save(cache_path,sample)
                    except OSError:
                        self.event(task['path'],'warning','压缩证据缓存未能保存，本次继续识别')
            else:
                report(stage='sampling', deadline_at=time.time()+settings['media_timeout'])
                sample = sample_frames(obj['raw_url'],self.ol.url,settings,cancelled,**source_options)
            report(duration=sample.get('duration',0), compressed_bytes=sample.get('video_bytes',0))
            sample['content_type'] = task['content_type']
            if sample.get('subtitles'):
                sample['source_name'] = obj.get('name','')[:500]
                save_subtitles(task['path'],task['fingerprint'],sample['subtitles'])
            if cancelled():
                raise RuntimeError('任务已取消')
            report(stage='waiting_model')
            prepared_at = time.monotonic()
            release_prepare()
            with self.stage('model', max(1, settings['concurrency'])) as slot:
                if slot is None or cancelled(force=True):
                    raise RuntimeError('任务已取消')
                model_at = time.monotonic()
                self.recognize(task, sample, settings, cancelled)
            outcome = 'done'
        except Exception as exc:
            message = safe_error(exc)
            activity_error = message
            with self.db.connect() as c:
                run = c.execute('SELECT state FROM runs WHERE id=?',(task['run_id'],)).fetchone()
                plan_row=c.execute("SELECT value FROM settings WHERE `key`='failure_recovery'").fetchone() if isinstance(exc,ModelRoutingError) else None
                plan=json.loads(plan_row['value']) if plan_row else {}
                deferred=bool(run and run['state']!='cancelled' and plan and plan.get('state') in ('running','paused'))
                state = 'queued' if self.stop.is_set() or deferred else ('cancelled' if not run or run['state']=='cancelled' else 'failed')
                if deferred:
                    plan.setdefault('model_hold',{'reason':'lis_global_ambiguous_model','message':message,'created':time.time(),'checks':0,'next_check_at':time.time()+300})
                    c.execute("UPDATE settings SET value=? WHERE `key`='failure_recovery'",(json.dumps(plan),))
                outcome = state
                c.execute('UPDATE tasks SET state=?,error=? WHERE id=?',(state,message,task['id']))
                c.execute('UPDATE videos SET status=?,error=?,updated=? WHERE path=?',
                          ('pending' if state=='queued' else state,message,time.time(),task['path']))
            self.event(task['path'],state,message)
        finally:
            if outcome == 'done':
                try:
                    self.event(task['path'], 'performance', json.dumps({
                        'task_id':task['id'], 'prepare_seconds':round(prepared_at-started_at,3),
                        'model_wait_seconds':round(model_at-prepared_at,3),
                        'model_save_seconds':round(time.monotonic()-model_at,3),
                    }, separators=(',',':')))
                except Exception:
                    pass  # Telemetry must never turn an already committed result into a failure.
            self.activity.finish(task['id'], outcome, activity_token, activity_error)
        return True

    def recognize(self, task, sample, settings, cancelled):
        model_plan=self.recovery.status()
        if model_plan.get('model_hold') and model_plan.get('state') in ('running','paused'):
            raise ModelRoutingError('模型服务路由不可用，已保留任务和压缩证据，等待服务商恢复')
        self.event(task['path'],'processing','视频证据已准备，正在调用模型识别')
        report(stage='model_video', deadline_at=time.time()+settings['model_timeout'])
        result = infer(sample,settings)
        report(stage='saving', deadline_at=time.time()+config.OPENLIST_SCAN_TIMEOUT)
        # Re-read identity after inference so changed files cannot inherit stale results.
        latest = self.ol.get(task['path'],self.ol.admin_token(),timeout=config.OPENLIST_SCAN_TIMEOUT)
        if fingerprint(latest) != task['fingerprint']:
            raise RuntimeError('识别期间视频发生变化，请重新扫描')
        with self.db.connect() as c:
            run = c.execute('SELECT state FROM runs WHERE id=?',(task['run_id'],)).fetchone()
            if self.stop.is_set() or not run or run['state'] == 'cancelled':
                raise RuntimeError('任务已取消')
            c.execute("UPDATE videos SET status='done',result=?,error='',updated=? WHERE path=? AND fingerprint=?",
                      (json.dumps(result,ensure_ascii=False),time.time(),task['path'],task['fingerprint']))
            c.execute("UPDATE tasks SET state='done',error='' WHERE id=?",(task['id'],))
            Postprocess.enqueue(c, task, sample.get('cover'))
        self.event(task['path'],'done','压缩视频识别完成' if 'video' in sample else '视频抽帧识别完成')

    def work_loop(self,index):
        while not self.stop.is_set():
            busy=False
            try:
                with self.concurrency_lock:
                    now = time.monotonic()
                    if now - self.concurrency_checked >= 3:
                        self.concurrency = self.db.settings()['concurrency']
                        self.concurrency_checked = now
                    concurrency = self.concurrency
                if index < min(4, concurrency + 1) and concurrency > 0:
                    busy=self.work_once()
            except Exception:
                self.event('/','failed','识别调度失败')
            self.stop.wait(.5 if busy else 3)

    def control(self,rid,action):
        with self.db.connect() as c:
            run = c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not run:
                raise HTTPException(404,'任务不存在')
            if action == 'pause' and run['state']=='running':
                c.execute("UPDATE runs SET state='paused' WHERE id=?",(rid,))
            elif action == 'resume' and run['state']=='paused':
                c.execute("UPDATE runs SET state='running' WHERE id=?",(rid,))
            elif action == 'cancel':
                c.execute("UPDATE runs SET state='cancelled' WHERE id=?",(rid,))
                c.execute("UPDATE videos SET status='cancelled' WHERE path IN (SELECT path FROM tasks WHERE run_id=? AND state='queued')",(rid,))
                c.execute("UPDATE tasks SET state='cancelled' WHERE run_id=? AND state='queued'",(rid,))
                c.execute("UPDATE directories SET state='cancelled' WHERE run_id=? AND state='queued'",(rid,))
            elif action == 'retry' and run['state'] in ('done','failed'):
                c.execute("UPDATE runs SET state='running',error='' WHERE id=?",(rid,))
                c.execute("UPDATE directories SET state='queued' WHERE run_id=? AND state='failed'",(rid,))
                c.execute("UPDATE tasks SET state='queued',error='' WHERE run_id=? AND state='failed' AND path NOT IN (SELECT path FROM tasks WHERE state IN ('running','queued')) AND path NOT IN (SELECT path FROM videos WHERE status='done')",(rid,))
                c.execute("UPDATE videos SET status='pending',error='' WHERE path IN (SELECT path FROM tasks WHERE run_id=? AND state='queued')",(rid,))
            else:
                raise HTTPException(409,'当前状态不支持此操作')
