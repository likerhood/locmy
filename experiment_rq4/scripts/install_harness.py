#!/usr/bin/env python3
"""Cache the pinned harness through mirror/fallback, then install locally."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from preflight import load_env
from resources import Resources
from git_cache import repo_for

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--env-file',type=Path,default=ROOT/'.env.local')
    p.add_argument('--skip-dataset',action='store_true')
    args=p.parse_args();load_env(args.env_file)
    pin=json.loads((ROOT/'configs/harness_lock.json').read_text())['commit']
    (ROOT/'repos').mkdir(exist_ok=True)
    with (ROOT/'repos/.rq4.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        resource=Resources(ROOT,ROOT/'reports/setup',float(os.getenv('RQ4_MIN_FREE_GB','10')))
        repo=repo_for(ROOT,{'repo':'SWE-bench/SWE-bench','base_commit':pin},resource)
        if not (repo/'.git/index').exists():
            resource.run(['git','-C',str(repo),'checkout','--detach',pin])
        if subprocess.check_output(['git','-C',str(repo),'status','--porcelain','--untracked-files=no'],text=True).strip():
            raise SystemExit('Harness source has edits; preserve them before reinstalling')
        resource.run(['git','-C',str(repo),'checkout','--detach',pin])
        with (ROOT/'reports/setup/pip.log').open('a') as log:
            resource.run([sys.executable,'-m','pip','install','--no-cache-dir',str(repo)],stdout=log,stderr=subprocess.STDOUT,timeout=1800)
        with (ROOT/'reports/server_dependencies.txt').open('w') as f:
            subprocess.run([sys.executable,'-m','pip','freeze'],stdout=f,check=True)
        if not args.skip_dataset:
            resource.run([sys.executable,str(ROOT/'scripts/prepare_harness_dataset.py')],stdout=None,stderr=None,timeout=1800)


if __name__=='__main__':main()
