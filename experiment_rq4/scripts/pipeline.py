#!/usr/bin/env python3
"""Load the selected env ONCE before setup, metadata download and serial execution."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from preflight import load_env
from storage_check import check_storage, docker_info, docker_storage_paths

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--env-file', type=Path, default=ROOT/'.env.local')
    p.add_argument('--eval-dataset', type=Path)
    p.add_argument('--min-free-gb', type=float)
    args, _ = p.parse_known_args()
    if '--help' in sys.argv:
        subprocess.run([sys.executable,str(ROOT/'scripts/run_batch.py'),'--help'],check=True)
        return
    if not args.env_file.is_file():
        raise SystemExit('Create the selected env file before starting the pipeline')
    load_env(args.env_file)
    minimum = args.min_free_gb if args.min_free_gb is not None else float(os.getenv('RQ4_MIN_FREE_GB','10'))
    if not 0 < minimum < float('inf'):
        raise SystemExit('Invalid disk reserve')
    os.environ['RQ4_MIN_FREE_GB'] = str(minimum)
    paths = [ROOT, *docker_storage_paths(docker_info())]
    check_storage(paths,minimum)
    subprocess.run(['bash',str(ROOT/'scripts/server_setup.sh'),'--env-file',str(args.env_file.resolve())],check=True)
    command = ['bash',str(ROOT/'scripts/install_harness.sh'),'--env-file',str(args.env_file.resolve())]
    if args.eval_dataset:
        command.append('--skip-dataset')
    subprocess.run(command,check=True)
    python = ROOT/'.venv/bin/python'
    os.execv(str(python),[str(python),str(ROOT/'scripts/run_batch.py'),'--mode','all',*sys.argv[1:]])


if __name__ == '__main__':
    main()
