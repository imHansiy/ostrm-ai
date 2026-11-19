import base64
import time

from fastapi import HTTPException

from aitags.covers import cached_cover, save_cover
from aitags.db import Database
from aitags.postprocess import Postprocess
from aitags.worker import Worker, fingerprint
from .conftest import add_video


JPEG = b'\xff\xd8recovered-cover\xff\xd9'


def video(env, name):
    db, ol, worker, _ = env
    path = '/public/'+name+'.mp4'
    ol.objects[path] = {**ol.objects['/public/a.mp4'], 'name':name+'.mp4'}
    add_video(db, worker, path, ol)
    return path, fingerprint(ol.objects[path])


def test_failed_cover_retries_after_cooldown_and_restart(env, monkeypatch):
    db, ol, worker, _ = env
    path, version = video(env, 'a')
    attempts = []
    def extract(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('temporary download failure')
        return JPEG
    monkeypatch.setattr('aitags.worker.extract_cover', extract)
    monkeypatch.setattr('aitags.worker.store_sidecar', lambda *a: None)
    worker.cover_once()
    assert not cached_cover(db, path, version)
    restarted = Worker(db, ol)
    restarted.cover_once()
    assert len(attempts) == 1  # No hot loop against an offline mount.
    db.execute('UPDATE covers SET updated=?', (time.time()-601,))
    restarted.cover_once()
    assert cached_cover(db, path, version) == JPEG
    assert len(attempts) == 2


def test_backfill_rotates_past_cached_rows_and_failures(env, monkeypatch, tmp_path):
    db, ol, worker, _ = env
    db.cover_cache = Database(tmp_path/'local-covers.sqlite3')
    cached, cv = video(env, 'cached')
    broken, _ = video(env, 'broken')
    missing, mv = video(env, 'missing')
    save_cover(db, cached, cv, None, JPEG)
    db.execute('UPDATE covers SET updated=0')
    reads = []
    get = ol.get
    def source(path, *args):
        reads.append(path)
        if path == broken:
            raise HTTPException(503, 'offline')
        return get(path, *args)
    monkeypatch.setattr(ol, 'get', source)
    monkeypatch.setattr('aitags.worker.extract_cover', lambda *a: JPEG)
    monkeypatch.setattr('aitags.worker.store_sidecar', lambda *a: None)
    worker.cover_once()
    worker.cover_once()
    assert cached not in reads and broken in reads and missing in reads
    assert cached_cover(db, missing, mv) == JPEG
    assert db.one('SELECT jpeg FROM covers WHERE path=?', (missing,))['jpeg'] is None


def test_backfill_ignores_failed_videos_and_pending_outbox(env, monkeypatch):
    db, ol, worker, _ = env
    failed, _ = video(env, 'failed')
    pending, pv = video(env, 'pending')
    db.execute("UPDATE videos SET status='failed' WHERE path=?", (failed,))
    with db.connect() as conn:
        Postprocess.enqueue(conn, {'path':pending,'fingerprint':pv}, '')
    monkeypatch.setattr('aitags.worker.extract_cover', lambda *a: (_ for _ in ()).throw(AssertionError('unexpected extraction')))
    worker.cover_once()
    assert not db.one('SELECT * FROM covers')


def test_pending_images_visible_before_upload_and_new_jobs_not_starved(env, monkeypatch, tmp_path):
    db, ol, worker, _ = env
    db.cover_cache = Database(tmp_path/'local-covers.sqlite3')
    old, ov = video(env, 'old')
    new, nv = video(env, 'new')
    for path, version in [(old,ov),(new,nv)]:
        with db.connect() as conn:
            Postprocess.enqueue(conn, {'path':path,'fingerprint':version}, base64.b64encode(JPEG).decode())
    db.execute('UPDATE postprocess_jobs SET attempts=8 WHERE path=?', (old,))
    saved_sidecar='/public/existing.jpg'
    save_cover(db, old, ov, saved_sidecar, None)
    def upload(*args):
        # Both covers have already been made visible when the first upload starts.
        assert cached_cover(db, old, ov) == cached_cover(db, new, nv) == JPEG
        raise HTTPException(403, 'read-only')
    monkeypatch.setattr('aitags.postprocess.store_sidecar', upload)
    published=[]
    worker.on_result=published.append
    assert worker.postprocess.once()
    assert published == [new]
    assert db.one('SELECT sidecar FROM covers WHERE path=?', (old,))['sidecar'] == saved_sidecar
    assert db.one('SELECT attempts FROM postprocess_jobs WHERE path=?', (old,))['attempts'] == 8
    assert db.one('SELECT attempts FROM postprocess_jobs WHERE path=?', (new,))['attempts'] == 1
    assert all(row['status']=='done' for row in db.all('SELECT status FROM videos'))


def test_cache_restore_skips_stale_and_invalid_images_and_is_bounded(env):
    db, ol, worker, _ = env
    for name in ('invalid','stale','first','second'):
        path, version = video(env, name)
        with db.connect() as conn:
            Postprocess.enqueue(conn, {'path':path,'fingerprint':version},
                                'invalid!' if name=='invalid' else base64.b64encode(JPEG).decode())
    db.execute("UPDATE videos SET fingerprint='changed' WHERE path='/public/stale.mp4'")
    assert worker.postprocess.restore_covers(limit=1) == 1
    assert db.one('SELECT COUNT(*) AS n FROM covers')['n'] == 1
    assert worker.postprocess.restore_covers(limit=1) == 1
    assert db.one('SELECT COUNT(*) AS n FROM covers')['n'] == 2
    assert db.one('SELECT COUNT(*) AS n FROM postprocess_jobs')['n'] == 4


def test_native_thumbnail_avoids_large_video_download(env, monkeypatch):
    db, ol, worker, _ = env
    path, version = video(env, 'native')
    ol.objects[path]['thumb'] = 'https://media.example.test/thumbnail.jpg'
    monkeypatch.setattr('aitags.worker.read_sidecar', lambda *a: JPEG)
    monkeypatch.setattr('aitags.worker.extract_cover', lambda *a: (_ for _ in ()).throw(AssertionError('unnecessary video download')))
    monkeypatch.setattr('aitags.worker.store_sidecar', lambda *a: None)
    worker.cover_once()
    assert cached_cover(db, path, version) == JPEG


def test_unreadable_native_thumbnail_falls_back_to_video(env, monkeypatch):
    db, ol, worker, _ = env
    path, version = video(env, 'fallback')
    ol.objects[path]['thumb'] = 'https://media.example.test/expired.jpg'
    monkeypatch.setattr('aitags.worker.read_sidecar', lambda *a: (_ for _ in ()).throw(RuntimeError('unavailable')))
    monkeypatch.setattr('aitags.worker.extract_cover', lambda *a: JPEG)
    monkeypatch.setattr('aitags.worker.store_sidecar', lambda *a: None)
    worker.cover_once()
    assert cached_cover(db, path, version) == JPEG
