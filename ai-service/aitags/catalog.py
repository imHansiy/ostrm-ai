"""Bounded, session-scoped metadata checks for the administrator's catalog."""
import hashlib
import posixpath
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException


class CatalogChecks:
    def __init__(self, ol):
        self.ol = ol
        self.pool = ThreadPoolExecutor(max_workers=2,thread_name_prefix='catalog-check')
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(16)
        self.entries = OrderedDict()
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True,cancel_futures=True)

    def _lookup(self, key, fetch):
        with self.lock:
            entry = self.entries.get(key)
            if entry:
                future, expires = entry
                if not future.done():
                    return 'checking', None
                if time.monotonic() < expires:
                    self.entries.move_to_end(key)
                    return future.result()
                del self.entries[key]
            if self.closed or not self.slots.acquire(blocking=False):
                return 'checking', None
            future = self.pool.submit(fetch)
            self.entries[key] = (future,float('inf'))
            while len(self.entries) > 128:
                oldest = next(k for k,(job,_) in self.entries.items() if job.done())
                del self.entries[oldest]

        def finished(job):
            self.slots.release()
            if job.cancelled():
                return
            state, _ = job.result()
            with self.lock:
                if key in self.entries and self.entries[key][0] is job:
                    self.entries[key] = (job,time.monotonic()+(15 if state=='available' else 60))
        future.add_done_callback(finished)
        return 'checking', None

    def lookup(self, path, token, password=''):
        parent = posixpath.dirname(path)
        scope = hashlib.sha256((token+'\0'+password).encode()).hexdigest()

        def directory():
            try:
                data = self.ol.listing(parent,token,1,500,password,timeout=4)
                objects = {obj['name']:obj for obj in data.get('content') or [] if obj.get('name')}
                return 'available',(objects,data.get('total',0)<=500)
            except Exception:
                return 'unavailable',None

        state, data = self._lookup((scope,'directory',parent),directory)
        if state!='available':
            return state,None
        objects, complete = data
        obj = objects.get(posixpath.basename(path))
        if obj is not None:
            return ('unavailable',None) if obj.get('is_dir') else ('available',obj)
        if complete:
            return 'missing',None

        def file():
            try:
                obj = self.ol.get(path,token,password,timeout=4)
                return ('unavailable',None) if obj.get('is_dir') else ('available',obj)
            except HTTPException as error:
                return ('missing' if error.status_code==404 else 'unavailable'),None
            except Exception:
                return 'unavailable',None
        return self._lookup((scope,'file',path),file)
