#!/usr/bin/env python3
"""Fetch the pinned reference framework through the same mirror/fallback cache."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import fcntl
from preflight import load_env
from resources import Resources
from git_cache import repo_for

ROOT = Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--env-file',type=Path,default=ROOT/'.env.local')
    args=p.parse_args();load_env(args.env_file)
    pin=json.loads((ROOT/'configs/vendor_lock.json').read_text())['Agentless']['commit']
    vendor=ROOT/'vendor/Agentless'
    (ROOT/'repos').mkdir(exist_ok=True)
    with (ROOT/'repos/.rq4.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        resource=Resources(ROOT,ROOT/'reports/setup',float(os.getenv('RQ4_MIN_FREE_GB','10')))
        resource.ensure()
        if not vendor.exists():
            repo=repo_for(ROOT,{'repo':'OpenAutoCoder/Agentless','base_commit':pin},resource)
            vendor.parent.mkdir(exist_ok=True)
            if not vendor.is_symlink():
                vendor.symlink_to(os.path.relpath(repo,vendor.parent),target_is_directory=True)
        if not (vendor/'.git/index').exists():
            resource.run(['git','-C',str(vendor),'checkout','--detach',pin])
        if subprocess.check_output(['git','-C',str(vendor),'status','--porcelain','--untracked-files=no'],text=True).strip():
            raise SystemExit('Agentless has local edits; preserve them before setup')
        if subprocess.check_output(['git','-C',str(vendor),'rev-parse','HEAD'],text=True).strip()!=pin:
            resource.run(['git','-C',str(vendor),'checkout','--detach',pin])


if __name__=='__main__':main()
