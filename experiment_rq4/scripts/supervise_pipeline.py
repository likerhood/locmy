#!/usr/bin/env python3
"""Persist pipeline output and supervise its lifecycle without retrying paid work."""
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from progress_view import snapshot, render

ROOT = Path(__file__).resolve().parents[1]


def supervise(command, directory, interval=30):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'supervisor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('This run already has a pipeline supervisor; inspect its pipeline.log')
        with (directory/'pipeline.log').open('a', buffering=1) as log:
            mutex = threading.RLock()
            def emit(text):
                with mutex:
                    log.write(text)
                    log.flush()
                    try:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    except (BrokenPipeError, OSError):
                        pass
            start = time.monotonic()
            stop = []
            process = None
            def interrupted(signum, frame):
                if not stop:
                    stop.append(signum)
                    emit('\n[interrupt] Stopping child work; waiting for cleanup. Do not launch another copy.\n')
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
            previous = {s: signal.signal(s, interrupted) for s in (signal.SIGINT, signal.SIGTERM)}
            def status(state, code=None):
                data = dict(state=state, supervisor_pid=os.getpid(), child_pid=process.pid if process else None,
                            updated_at=datetime.now().astimezone().isoformat(), elapsed_seconds=round(time.monotonic()-start),
                            exit_code=code, log=str(directory/'pipeline.log'))
                if process is not None:
                    data['progress'] = snapshot(ROOT, directory, process.pid)
                tmp = directory/'status.json.tmp'
                tmp.write_text(json.dumps(data, indent=2)+'\n')
                tmp.replace(directory/'status.json')
            try:
                emit(f'\n[start] {datetime.now().astimezone().isoformat()} log={directory / "pipeline.log"}\n')
                env = dict(os.environ, PYTHONUNBUFFERED='1')
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors='replace', start_new_session=True, env=env)
                def copy():
                    with process.stdout:
                        for line in process.stdout:
                            emit(line)
                reader = threading.Thread(target=copy, daemon=True)
                reader.start()
                previous_progress = None
                while True:
                    status('stopping' if stop else 'running')
                    try:
                        code = process.wait(timeout=interval)
                        break
                    except subprocess.TimeoutExpired:
                        emit(f'[heartbeat] elapsed={int(time.monotonic()-start)}s\n')
                        progress = snapshot(ROOT, directory, process.pid)
                        emit(render(progress, previous_progress))
                        previous_progress = progress
                reader.join(timeout=5)
                state = 'interrupted' if stop else 'completed' if code == 0 else 'failed'
                code = 128+stop[0] if stop else code
                status(state, code)
                emit(f'[{state}] exit={code}; log={directory / "pipeline.log"}\n')
                if code:
                    emit('Resolve the error, then repeat the identical command. Already-started API requests are not automatically retried.\n')
                return code
            except Exception:
                status('failed')
                raise
            finally:
                for s, handler in previous.items():
                    signal.signal(s, handler)


def main():
    if '--help' in sys.argv:
        return subprocess.call([sys.executable, str(ROOT/'scripts/pipeline.py'), *sys.argv[1:]])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--run-id', default='swe_mimo_serial_v2')
    args, _ = parser.parse_known_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.run_id) or args.run_id in ('.', '..'):
        raise SystemExit('Invalid run-id')
    return supervise([sys.executable, '-u', str(ROOT/'scripts/pipeline.py'), *sys.argv[1:]],
                     ROOT/'runs/pipeline'/args.run_id)


if __name__ == '__main__':
    sys.exit(main())
