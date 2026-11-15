"""Start isolated real services, test the stack, optionally retain for browser tests.

Prerequisites: built backend JAR, generated Nuxt, Java21, Python package installed,
ffmpeg. No production credentials or network model calls are used.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
STATE=Path(os.getenv('TEST_STATE_DIR',str(ROOT/'.local'/'integration')));STATE.mkdir(parents=True,exist_ok=True)
JAVA=os.path.join(os.getenv('JAVA_HOME',''), 'bin','java.exe' if os.name=='nt' else 'java') if os.getenv('JAVA_HOME') else 'java'

def main():
    processes=[];logs=[]
    env=os.environ.copy()
    env.update(OPENLIST_URL='http://127.0.0.1:18789',OPENLIST_TOKEN='fixture-admin',
        MODEL_BASE_URL='http://127.0.0.1:18789/v1',MODEL_API_KEY='fixture-only',AI_BRIDGE_KEY='fixture-bridge-key',
        AI_DATA_DIR=str(STATE/'ai'),MEDIA_EXPORT_DIR=str(STATE/'exports'),AI_DB_BACKEND='sqlite',
        AI_SERVICE_URL='http://127.0.0.1:18787',JWT_SECRET='fixture-jwt-secret-at-least-32-characters-long',
        APP_DATA_PATH=str(STATE/'java'),APP_DATABASE_PATH=str(STATE/'java/db.sqlite3'),
        APP_CONFIG_PATH=str(STATE/'java/config'),APP_USER_INFO_PATH=str(STATE/'java/config/userInfo.json'),
        APP_LOG_PATH=str(STATE/'java/log'),APP_FRONTEND_LOGS_PATH=str(STATE/'java/log/frontend'),
        APP_STRM_PATH=str(STATE/'exports'),CLIENT_FIXTURE=str(STATE/'client.json'),
        AI_PROXY_ENABLED='false',OPENLIST_PASSWORD='',MODEL_PROXY_URL='',NO_PROXY='127.0.0.1,localhost')
    for name in ('java/config','java/log/frontend','ai','exports'):(STATE/name).mkdir(parents=True,exist_ok=True)
    commands=[('fixture',[sys.executable,str(ROOT/'tests/integration/mock_openlist.py')],ROOT),
        ('ai',[sys.executable,'-m','uvicorn','aitags.app:app','--host','127.0.0.1','--port','18787','--no-access-log'],ROOT/'ai-service'),
        ('java',[JAVA,'-jar',str(ROOT/'backend/build/libs/openlisttostrm.jar'),'--server.port=18080'],ROOT)]
    try:
        for name,command,cwd in commands:
            log=(STATE/(name+'.log')).open('w',encoding='utf-8');logs.append(log)
            processes.append(subprocess.Popen(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT))
        result=subprocess.run([sys.executable,str(ROOT/'tests/integration/check_stack.py')],cwd=ROOT,env=env)
        if result.returncode:raise SystemExit(result.returncode)
        if '--keep' in sys.argv:
            print('Stack ready; Ctrl+C stops services.',flush=True)
            while True:time.sleep(5)
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            try:process.wait(timeout=25)
            except subprocess.TimeoutExpired:process.kill()
        for log in logs:log.close()

if __name__=='__main__':main()
