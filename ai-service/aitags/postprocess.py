"""Durable, idempotent cover and protocol outbox, separate from recognition."""
import base64
import time
from .covers import cached_cover, save_cover, store_sidecar
from .events import safe_event


class Postprocess:
    def __init__(self, db, ol, publish):
        self.db, self.ol, self.publish = db, ol, publish
        self.cache_checked = float('-inf')

    @staticmethod
    def decode_cover(value):
        jpeg = base64.b64decode(value, validate=True)
        if not (0 < len(jpeg) <= 1024**2 and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9')):
            raise ValueError('Invalid cached cover')
        return jpeg

    def restore_covers(self, limit=32):
        """Make durable images visible without waiting for remote uploads/retries."""
        jobs = self.db.all("""SELECT p.path,p.fingerprint,c.sidecar FROM postprocess_jobs p
            JOIN videos v ON p.path=v.path AND p.fingerprint=v.fingerprint
            LEFT JOIN covers c ON p.path=c.path AND p.fingerprint=c.fingerprint
            WHERE v.status='done' AND LENGTH(p.cover)>0 ORDER BY p.created""")
        restored = 0
        for job in jobs:
            if cached_cover(self.db, job['path'], job['fingerprint']):
                continue
            row = self.db.one('SELECT cover FROM postprocess_jobs WHERE path=? AND fingerprint=?',
                              (job['path'], job['fingerprint']))
            if not row:
                continue
            try:
                jpeg = self.decode_cover(row['cover'])
            except (ValueError, TypeError):
                continue
            save_cover(self.db, job['path'], job['fingerprint'], job['sidecar'], jpeg)
            restored += 1
            if restored >= limit:
                break
        return restored

    @staticmethod
    def enqueue(connection, task, cover):
        # Enqueued in the same transaction that commits the recognition result.
        connection.execute('DELETE FROM postprocess_jobs WHERE path=?', (task['path'],))
        connection.execute('INSERT INTO postprocess_jobs(path,fingerprint,cover,created,next_attempt) VALUES (?,?,?,?,?)',
                           (task['path'], task['fingerprint'], cover or '', time.time(), 0))

    def once(self):
        if time.monotonic()-self.cache_checked >= 60:
            self.restore_covers()
            self.cache_checked = time.monotonic()
        # Old permission/network failures must not starve newly completed work.
        job = self.db.one('SELECT * FROM postprocess_jobs WHERE next_attempt<=? ORDER BY attempts,created LIMIT 1', (time.time(),))
        if not job:
            return False
        version = (job['path'], job['fingerprint'], job['created'])
        try:
            row = self.db.one("SELECT fingerprint FROM videos WHERE path=? AND status='done'", (job['path'],))
            if row and row['fingerprint'] == job['fingerprint']:
                cover_error = None
                try:
                    if job['cover']:
                        jpeg = self.decode_cover(job['cover'])
                        cached = cached_cover(self.db, job['path'], job['fingerprint'])
                        # Make the local cover available even when the sidecar upload is slow.
                        if not cached:
                            metadata = self.db.one('SELECT sidecar FROM covers WHERE path=? AND fingerprint=?', (job['path'], job['fingerprint']))
                            save_cover(self.db, job['path'], job['fingerprint'], metadata['sidecar'] if metadata else None, jpeg)
                        metadata = self.db.one('SELECT sidecar FROM covers WHERE path=? AND fingerprint=?', (job['path'], job['fingerprint']))
                        if not metadata or not metadata['sidecar']:
                            sidecar = store_sidecar(self.ol, job['path'], job['fingerprint'], jpeg, self.ol.admin_token())
                            save_cover(self.db, job['path'], job['fingerprint'], sidecar, jpeg)
                except Exception as exc:
                    cover_error = exc
                self.publish(job['path'])
                if cover_error:
                    raise cover_error
            self.db.execute('DELETE FROM postprocess_jobs WHERE path=? AND fingerprint=? AND created=?', version)
        except Exception as exc:
            attempts = job['attempts'] + 1
            self.db.execute('UPDATE postprocess_jobs SET attempts=?,next_attempt=?,error=? WHERE path=? AND fingerprint=? AND created=?',
                            (attempts, time.time()+min(300, 5*2**min(attempts,6)), type(exc).__name__, *version))
            if attempts == 1:
                safe_event(self.db, job['path'], 'warning', '识别已完成，封面与协议资料将在后台重试')
        return True
