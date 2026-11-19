import base64


def test_admin_connection_test_reads_real_packaged_image(env,monkeypatch):
    db,ol,worker,client=env
    calls=[]
    def infer(sample,settings):
        image=base64.b64decode(sample['frames'][0]['data'])
        assert image.startswith(b'\xff\xd8') and image.endswith(b'\xff\xd9')
        assert len(image)>1000
        calls.append(sample)
        return {'summary':'房屋和太阳测试图','tags':['房屋','太阳']}
    monkeypatch.setattr('aitags.app.infer',infer)
    response=client.post('/ai/api/model/test',headers={'Authorization':'admin'})
    assert response.status_code==200 and response.json()['tags']==['房屋','太阳']
    assert len(calls)==1
