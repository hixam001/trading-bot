# Trusted parent-side runner: no imports from target. Only source allowlisted.
import os,sys,subprocess,tempfile,pathlib,shutil,selectors,time
repo=pathlib.Path(__file__).resolve().parents[2]
baseline_auth_tls='--baseline-auth-tls' in sys.argv
if baseline_auth_tls:sys.argv.remove('--baseline-auth-tls')
wall_seconds=300
if '--wall-seconds' in sys.argv:
 flag=sys.argv.index('--wall-seconds')
 wall_seconds=int(sys.argv[flag+1])
 if not 1 <= wall_seconds <= 300:raise ValueError('wall limit must be 1..300 seconds')
 del sys.argv[flag:flag+2]
with tempfile.TemporaryDirectory(prefix='remediation-source-') as tmp:
 root=pathlib.Path(tmp);src=root/'source';src.mkdir()
 files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=repo).decode().split('\0')
 with_frontend = any(flag in sys.argv for flag in ('--with-frontend','--frontend-only','--e2e','--gate-e2e'))

 for name in set(files):
  p=pathlib.Path(name)
  if not name or any(x in p.parts for x in ('.git','.agents','.venv','node_modules','state','ingested')):continue
  if (p.name.startswith('.env') and p.name != '.env.example') or p.suffix in ('.db','.enc','.key','.pem','.sqlite') or 'keypair' in p.name or 'credentials' in p.name:continue
  if p.suffix not in ('.py','.sh','.json','.txt','.lock','.ini','.toml','.md','.yml','.yaml','.html','.css','.sql','.example','.ts','.tsx','.js','.svg') and p.name not in ('.dockerignore','.gitignore','.gitleaksignore','Dockerfile'):continue
  origin=repo/p
  if not origin.is_file() or origin.is_symlink():continue
  dest=src/p;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(origin,dest)
  if baseline_auth_tls and name in ('backend/api/auth.py','backend/api/routes/auth.py','backend/api/db_pg.py'):
   dest.write_bytes(subprocess.check_output(['git','show','028d4e07375946d6c51072fb624342cc83616b77:'+name],cwd=repo))
 hosts=root/'hosts';hosts.write_text('127.0.0.1 localhost\n::1 localhost\n')
 supervisor=root/'supervisor.py'
 supervisor.write_text('''import os,sys,resource,shutil,subprocess
limits=[(resource.RLIMIT_CPU,240),(resource.RLIMIT_NOFILE,256),(resource.RLIMIT_FSIZE,16777216),(resource.RLIMIT_CORE,0)]
if '--e2e' not in sys.argv and '--gate-e2e' not in sys.argv:limits += [(resource.RLIMIT_AS,2147483648),(resource.RLIMIT_NPROC,64)]
for k,v in limits:resource.setrlimit(k,(v,v))
os.mkdir('/scratch/home');os.mkdir('/scratch/tmp')
shutil.copytree('/source','/scratch/repo')
os.chdir('/scratch/repo')
subprocess.run(['/usr/bin/git','init','-q'],check=True)
subprocess.run(['/usr/bin/git','add','-A'],check=True)
sys.path[:0]=['/deps','/scratch/repo','/scratch/repo/backend']
if any(flag in sys.argv for flag in ('--with-frontend','--frontend-only','--e2e','--gate-e2e')):
 os.symlink('/node_modules','/scratch/repo/frontend/node_modules')
 subprocess.run(['/usr/bin/npm','run','build'],cwd='frontend',check=True)
 subprocess.run(['/usr/bin/python3','scripts/scan_frontend_bundle.py'],check=True)
 if '--frontend-only' in sys.argv: raise SystemExit(0)
 if '--e2e' in sys.argv or '--gate-e2e' in sys.argv:
  import socket,time
  env=dict(os.environ,DATA_BACKEND='mock',PYTHONPATH='/scratch/repo/backend',DB_PATH='/scratch/mock.db',LIVE_STATE_DIR='/scratch/state',LIVE_EXECUTION_STATE_DIR='/scratch/state',LOG_DIR='/scratch/logs',ADMIN_TOKEN='dummy-operator-token-0123456789abcdef',SESSION_SECRET='dummy-session-secret-9876543210abcdef',RATE_LIMIT_GENERAL_PER_MINUTE='2000',RATE_LIMIT_PAID_PER_MINUTE='1000',PLAYWRIGHT_BROWSERS_PATH='/browsers',BASE_URL='http://127.0.0.1:8000')
  if '--gate-e2e' in sys.argv:
   raise SystemExit(subprocess.run(['/bin/bash','scripts/prepush_check.sh','--stage','8'],env=env).returncode)
  with open('/scratch/mock-api.log','w') as log:
   server=subprocess.Popen(['/usr/bin/python3','-m','uvicorn','api.main:app','--app-dir','backend','--host','127.0.0.1','--port','8000','--workers','1'],env=env,stdout=log,stderr=subprocess.STDOUT)
   try:
    ready=False
    for _ in range(100):
     if server.poll() is not None: break
     try:
      with socket.create_connection(('127.0.0.1',8000),timeout=.1): ready=True;break
     except OSError:time.sleep(.1)
    if not ready: raise RuntimeError('isolated mock API did not start')
    result=subprocess.run(['/usr/bin/npm','run','test:e2e'],cwd='frontend',env=env)
   finally:
    server.terminate()
    try:server.wait(timeout=5)
    except subprocess.TimeoutExpired:server.kill();server.wait()
  if not ready: print(open('/scratch/mock-api.log').read()[-8000:])
  raise SystemExit(result.returncode)
 sys.argv.remove('--with-frontend')
if len(sys.argv)>1 and sys.argv[1]=='--command':
 os.environ['PYTEST_PLUGINS']='pytest_asyncio.plugin'
 os.environ['PYTHONPATH']='/scratch/repo:/scratch/repo/backend'
 os.environ['PIP_DISABLE_PIP_VERSION_CHECK']='1'
 raise SystemExit(subprocess.run(sys.argv[2:]).returncode)
import pytest
raise SystemExit(pytest.main(['-p','pytest_asyncio.plugin']+sys.argv[1:]))
''')
 cmd=['/usr/bin/bwrap','--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL','--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--ro-bind','/lib','/lib','--ro-bind','/lib64','/lib64','--proc','/proc','--dev','/dev','--size','536870912','--tmpfs','/scratch','--ro-bind',str(src),'/source','--ro-bind',str(repo/'.venv/lib/python3.14/site-packages'),'/deps','--ro-bind',str(repo/'.venv/lib/python3.14/site-packages'),'/usr/lib/python3.14/site-packages','--size','134217728','--tmpfs','/tmp','--ro-bind',str(supervisor),'/supervisor.py','--ro-bind',str(hosts),'/etc/hosts','--clearenv','--setenv','PATH','/usr/bin','--setenv','HOME','/scratch/home','--setenv','TMPDIR','/tmp','--setenv','XDG_CACHE_HOME','/scratch/home','--setenv','PYTHONDONTWRITEBYTECODE','1','--setenv','PYTHONHASHSEED','0','--setenv','PYTEST_DISABLE_PLUGIN_AUTOLOAD','1','--remount-ro','/','--remount-ro','/proc','--remount-ro','/dev','/usr/bin/python3','-I','-B','/supervisor.py']+sys.argv[1:]
 if with_frontend:
  cmd[1:1]=['--ro-bind',str(repo/'frontend/node_modules'),'/node_modules']
 if '--e2e' in sys.argv or '--gate-e2e' in sys.argv:
  browser=pathlib.Path('/home/hixam/.cache/ms-playwright/chromium_headless_shell-1234')
  cmd[1:1]=['--ro-bind',str(browser),'/browsers/chromium_headless_shell-1234']
 cgroup=None
 if '--e2e' in sys.argv or '--gate-e2e' in sys.argv:
  # Trusted parent provisions a new private delegated cgroup. No target sees
  # cgroupfs or host control sockets. Refuse if this isolation is unavailable.
  membership=pathlib.Path('/proc/self/cgroup').read_text().strip().split('::',1)[1]
  delegated=pathlib.Path('/sys/fs/cgroup') / membership.lstrip('/')
  base=delegated.parent
  if base.stat().st_uid != os.getuid(): raise RuntimeError('no owner-delegated cgroup parent')
  cgroup=base / ('codex-remediation-'+str(os.getpid()))
  cgroup.mkdir(mode=0o700)
  try:
   (cgroup/'memory.max').write_text('2147483648')
   (cgroup/'memory.swap.max').write_text('0')
   (cgroup/'pids.max').write_text('256')
  except BaseException:
   cgroup.rmdir();raise

 def enter_cgroup():
  if cgroup is not None:(cgroup/'cgroup.procs').write_text('0')
 proc=None
 try:
  proc=subprocess.Popen(cmd,env={'PATH':'/usr/bin'},stdout=subprocess.PIPE,stderr=subprocess.STDOUT,preexec_fn=enter_cgroup if cgroup else None)
  selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
  output=bytearray();deadline=time.monotonic()+wall_seconds
  while selector.get_map():
   if time.monotonic()>deadline:
    proc.kill();proc.wait();print('Sandbox wall limit exceeded');sys.exit(124)
   for key,_ in selector.select(timeout=.25):
    chunk=os.read(key.fileobj.fileno(),65536)
    if not chunk:selector.unregister(key.fileobj);continue
    output.extend(chunk)
    if len(output)>8*1024*1024:
     proc.kill();proc.wait();print('Sandbox output limit 8MiB exceeded');sys.exit(125)
  print(output.decode('utf-8',errors='replace'),end='')
  try:code=proc.wait(timeout=max(.001,deadline-time.monotonic()))
  except subprocess.TimeoutExpired:
   proc.kill();proc.wait();print('Sandbox wall limit exceeded');sys.exit(124)
 finally:
  if proc is not None and proc.poll() is None:proc.kill();proc.wait()
  if cgroup is not None:
   if (cgroup/'cgroup.procs').read_text().strip():(cgroup/'cgroup.kill').write_text('1')
   for attempt in range(50):
    try:cgroup.rmdir();break
    except OSError:
     if attempt==49:raise
     time.sleep(.02)
 sys.exit(code)
