import pytest

from aitags.covers import save_cover
from aitags.db import Database
from aitags.storage import copy_database
from aitags.worker import fingerprint
from .conftest import add_video


def test_snapshot_copy_preserves_queue_cursors_and_cover_metadata(env,tmp_path):
    source,ol,worker,_=env
    run=add_video(source,worker,'/public/a.mp4',ol)
    worker.enqueue('/secret/b.mp4',ol.objects['/secret/b.mp4'],run)
    source.execute('UPDATE directories SET page=7,depth=3 WHERE run_id=?',(run,))
    source.save_settings({'interval_minutes':15})
    version=fingerprint(ol.objects['/public/a.mp4'])
    save_cover(source,'/public/a.mp4',version,'/public/a.cover.jpg',b'jpeg')

    destination=Database(tmp_path/'destination.sqlite3')
    counts=copy_database(source,destination)
    assert counts['videos']==2 and counts['tasks']==2 and counts['covers']==1
    assert destination.all('SELECT * FROM tasks ORDER BY id')==source.all('SELECT * FROM tasks ORDER BY id')
    assert destination.one('SELECT page,depth FROM directories')=={'page':7,'depth':3}
    assert destination.one('SELECT sidecar,jpeg FROM covers')=={'sidecar':'/public/a.cover.jpg','jpeg':b'jpeg'}
    assert destination.settings()['interval_minutes']==15
    with pytest.raises(ValueError,match='not empty'):
        copy_database(source,destination)
