import threading
from types import SimpleNamespace

from aitags.catalog import CatalogChecks


def test_background_checks_bound_backlog_and_isolate_sessions():
    release=threading.Event()
    started=threading.Event()
    calls=[]
    def listing(parent,token,*args,**kwargs):
        calls.append((parent,token))
        started.set()
        release.wait(2)
        return {'content':[{'name':'clip.mp4','is_dir':False}],'total':1}
    checks=CatalogChecks(SimpleNamespace(listing=listing))
    try:
        for index in range(100):
            assert checks.lookup(f'/folder-{index}/clip.mp4','one')[0]=='checking'
        assert started.wait(1)
        with checks.lock:
            assert len(checks.entries)==16
            futures=[future for future,_ in checks.entries.values()]
        assert len(calls)<=2
        release.set()
        for future in futures:
            future.result(timeout=2)
        assert checks.lookup('/folder-0/clip.mp4','one')[0]=='available'
        assert checks.lookup('/folder-0/clip.mp4','two')[0]=='checking'
        assert checks.lookup('/folder-0/clip.mp4','one','different-password')[0]=='checking'
    finally:
        release.set()
        checks.close()
