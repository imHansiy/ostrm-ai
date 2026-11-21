import json,re,struct,subprocess,shutil
import httpx,pytest
from aitags.container_probe import mp4_duration,ContainerChanged


def atom(kind,data):return struct.pack('>I4s',len(data)+8,kind)+data


def movie(clock_version=0,video=True):
    if clock_version:
        mvhd=b'\1'+b'\0'*19+struct.pack('>IQ',1000,10000)
        tkhd=b'\1'+b'\0'*27+struct.pack('>Q',9000)
        mdhd=b'\1'+b'\0'*19+struct.pack('>IQ',90000,810000)
    else:
        mvhd=b'\0'*12+struct.pack('>II',1000,10000)
        tkhd=b'\0'*20+struct.pack('>I',9000)
        mdhd=b'\0'*12+struct.pack('>II',90000,810000)
    hdlr=b'\0'*8+(b'vide' if video else b'soun')
    return atom(b'moov',atom(b'mvhd',mvhd)+atom(b'trak',atom(b'tkhd',tkhd)+atom(b'mdia',atom(b'mdhd',mdhd)+atom(b'hdlr',hdlr))))


def source(data,*,change=False):
    calls=[]
    def respond(request):
        start,end=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',request.headers['range']).groups());end=min(end,len(data)-1)
        calls.append((start,end))
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"changed"' if change and len(calls)>1 else '"stable"'},content=data[start:end+1])
    return httpx.MockTransport(respond),calls


@pytest.mark.parametrize('version',[0,1])
@pytest.mark.parametrize('tail',[False,True])
def test_track_duration_uses_bounded_headers_and_skips_media_packets(version,tail):
    ftyp=atom(b'ftyp',b'isom'+b'\0'*4);mdat=atom(b'mdat',b'x'*(2*1024**2));moov=movie(version)
    data=ftyp+mdat+moov if tail else ftyp+moov+mdat
    transport,calls=source(data)
    assert mp4_duration('https://video.test/file',10,transport=transport)==9
    assert sum(end-start+1 for start,end in calls)<=2*65536
    if tail:assert calls[-1][0]>len(data)//2


def test_no_video_track_is_not_accepted():
    transport,calls=source(atom(b'ftyp',b'isom')+movie(video=False))
    assert mp4_duration('https://video.test/file',10,transport=transport) is None


def test_changed_entity_between_header_ranges_is_rejected():
    transport,calls=source(atom(b'ftyp',b'isom')+atom(b'mdat',b'x'*131072)+movie(),change=True)
    with pytest.raises(ContainerChanged):mp4_duration('https://video.test/file',10,transport=transport)


@pytest.mark.parametrize('data',[b'',b'not a video',struct.pack('>I4s',3,b'ftyp'),struct.pack('>I4sQ',1,b'moov',2**63)])
def test_invalid_or_truncated_container_has_no_trusted_duration(data):
    if not data:
        transport=httpx.MockTransport(lambda r:httpx.Response(416))
        with pytest.raises(httpx.HTTPError):mp4_duration('https://video.test/file',10,transport=transport)
    else:
        transport,calls=source(data)
        assert mp4_duration('https://video.test/file',10,transport=transport) is None


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='requires real FFmpeg tools')
def test_actual_mp4_duration_agrees_with_ffprobe(tmp_path):
    path=tmp_path/'real.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=duration=1.5:size=64x64:rate=12',
                    '-c:v','libx264','-movflags','+faststart',str(path)],check=True,capture_output=True,timeout=30)
    expected=float(json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','V:0','-show_entries','stream=duration','-of','json',str(path)],timeout=20))['streams'][0]['duration'])
    transport,calls=source(path.read_bytes())
    assert mp4_duration('https://video.test/file',10,transport=transport)==pytest.approx(expected,abs=.001)
