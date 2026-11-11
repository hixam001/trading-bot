# Trusted parent: only the inspected gate and pinned manifests are exposed.
import os,pathlib,shutil,subprocess,sys,tempfile
repo=pathlib.Path(__file__).resolve().parents[2]
stage=sys.argv[1]
if stage not in ('3','7'):raise ValueError('only advisory stages 3 and 7 are permitted')
with tempfile.TemporaryDirectory(prefix='remediation-gate-advisory-') as directory:
 root=pathlib.Path(directory)
 for part in ('home','tmp','cache','scripts','backend','frontend','.venv/bin'):(root/part).mkdir(parents=True,exist_ok=True)
 for filename in ('npm-user.conf','npm-global.conf'):(root/filename).write_text('')
 shutil.copyfile(repo/'scripts/prepush_check.sh',root/'scripts/prepush_check.sh')
 shutil.copyfile(repo/'backend/requirements.lock',root/'backend/requirements.lock')
 for filename in ('package.json','package-lock.json'):shutil.copyfile(repo/'frontend'/filename,root/'frontend'/filename)
 for filename in ('python','pip','pip-audit'):(root/'.venv/bin'/filename).symlink_to(repo/'.venv/bin'/filename)
 env={'PATH':'/usr/bin','HOME':str(root/'home'),'TMPDIR':str(root/'tmp'),'PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1','PIP_CONFIG_FILE':'/dev/null','PIP_DISABLE_PIP_VERSION_CHECK':'1','NPM_CONFIG_USERCONFIG':str(root/'npm-user.conf'),'NPM_CONFIG_GLOBALCONFIG':str(root/'npm-global.conf'),'NPM_CONFIG_CACHE':str(root/'cache'),'NPM_CONFIG_UPDATE_NOTIFIER':'false','NPM_CONFIG_REGISTRY':'https://registry.npmjs.org'}
 result=subprocess.run(['/bin/bash','scripts/prepush_check.sh','--stage',stage],cwd=root,env=env,capture_output=True,text=True,timeout=180)
 output=result.stdout+result.stderr
 print(output,end='')
 sys.exit(result.returncode)
