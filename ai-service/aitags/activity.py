"""Bounded in-memory task telemetry. No credentials, command lines or per-frame DB writes."""
import math
import posixpath
import threading
import time
from collections import deque
from contextvars import ContextVar

_reporter = ContextVar('task_activity_reporter', default=None)


def reporter():
    return _reporter.get()


def report(**fields):
    callback = reporter()
    if callback:
        callback(**fields)


class Activity:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = {}
        self.recent = deque(maxlen=5)

    def begin(self, task):
        now = time.time()
        identifier = (task.get('kind','video'),task['id'])
        with self.lock:
            self.active[identifier] = {
                'id': task['id'], 'run_id': task['run_id'], 'path': task['path'],
                'kind': identifier[0], 'name': posixpath.basename(task['path']) or '/', 'size': task.get('size', 0),
                'started_at': now, 'stage_started_at': now, 'heartbeat_at': now,
                'last_progress_at': now, 'stage': 'reading', 'network_bytes': 0,
            }
        return _reporter.set(lambda **fields: self.update(identifier, **fields))

    def update(self, identifier, **fields):
        with self.lock:
            item = self.active.get(identifier)
            if item is None:
                return
            now = time.time()
            item['heartbeat_at'] = now
            changed = bool(fields.get('advanced'))
            if fields.get('stage') and fields['stage'] != item['stage']:
                item['stage'] = str(fields['stage'])[:60]
                item['stage_started_at'] = now
                for key in ('frame', 'fps', 'speed', 'out_seconds', 'output_bytes', 'process'):
                    item.pop(key, None)
                changed = True
            if fields.get('new_model_request'):
                item['model_request'] = item.get('model_request',0)+1
            delta = fields.get('network_delta', 0)
            if isinstance(delta, (int, float)) and math.isfinite(delta) and delta > 0:
                item['network_bytes'] += int(delta)
                changed = True
            for key in ('size', 'duration', 'frame', 'fps', 'speed', 'out_seconds', 'output_bytes',
                        'compressed_bytes', 'model_request', 'model_attempt', 'deadline_at',
                        'timeout_seconds', 'model_http_status', 'processed_entries', 'total_entries'):
                value = fields.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    # Speed estimates alone are not evidence that more content was processed.
                    if key in ('frame', 'out_seconds', 'output_bytes', 'processed_entries') and value > item.get(key, 0):
                        changed = True
                    item[key] = value
            for key in ('process', 'cache_hit'):
                if key in fields:
                    item[key] = fields[key]
            if changed:
                item['last_progress_at'] = now

    def finish(self, identifier, outcome, token, error='', kind='video'):
        try:
            with self.lock:
                item = self.active.pop((kind,identifier), None)
                if item:
                    self.recent.appendleft({
                        'id': item['id'], 'name': item['name'], 'state': outcome,
                        'kind':kind,
                        'finished_at': time.time(), 'elapsed_seconds': max(0, time.time()-item['started_at']),
                        'error': str(error)[:500],
                    })
        finally:
            _reporter.reset(token)

    def snapshot(self):
        now = time.time()
        with self.lock:
            active = []
            for item in self.active.values():
                row = dict(item)
                row['elapsed_seconds'] = max(0, now-item['started_at'])
                row['stage_elapsed_seconds'] = max(0, now-item['stage_started_at'])
                row['heartbeat_age_seconds'] = max(0, now-item['heartbeat_at'])
                row['progress_age_seconds'] = max(0, now-item['last_progress_at'])
                duration = item.get('duration', 0)
                row['stage_percent'] = min(100, item['out_seconds']/duration*100) if item['stage'] in ('decoding','decode_subtitles','encoding_analysis','encoding') and duration > 0 and 'out_seconds' in item else None
                if item['stage']=='indexing' and item.get('total_entries',0)>0:
                    row['stage_percent'] = min(100,item.get('processed_entries',0)/item['total_entries']*100)
                active.append(row)
            return {'active': active, 'recent': list(self.recent)}


def read_ffmpeg_progress(path):
    """Read only the bounded tail of FFmpeg's key=value progress stream."""
    try:
        with open(path, 'rb') as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(max(0, size-4096))
            raw = source.read(4096).decode('utf-8', errors='replace')
        values = {}
        for line in raw.splitlines():
            key, separator, value = line.partition('=')
            if separator:
                values[key] = value.strip()
        result = {}
        for source, target in [('frame', 'frame'), ('fps', 'fps'), ('out_time_us', 'out_seconds'), ('speed', 'speed')]:
            try:
                number = float(values[source].removesuffix('x'))
                if math.isfinite(number) and number >= 0:
                    result[target] = number/1_000_000 if source == 'out_time_us' else number
            except (KeyError, ValueError):
                pass
        return result
    except OSError:
        return {}
