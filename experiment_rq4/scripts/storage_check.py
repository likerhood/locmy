#!/usr/bin/env python3
"""Report each actual filesystem; never equate inaccessible paths with low space."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


def inspect_storage(path, minimum):
    path = Path(path)
    item = {'path': str(path), 'required_free_gib': minimum}
    try:
        if not stat.S_ISDIR(path.stat().st_mode):
            item['status'] = 'not_directory'
        else:
            usage = shutil.disk_usage(path)
            item.update(free_gib=round(usage.free / 1024**3, 3), total_gib=round(usage.total / 1024**3, 3))
            item['status'] = 'ok' if usage.free >= minimum*1024**3 else 'below_reserve'
    except PermissionError:
        item['status'] = 'permission_denied'
    except FileNotFoundError:
        item['status'] = 'path_not_found'
    except OSError as exc:
        item.update(status='filesystem_error', error_type=type(exc).__name__, errno=exc.errno)
    return item


def check_storage(paths, minimum):
    reports = [inspect_storage(p, minimum) for p in dict.fromkeys(paths)]
    print(json.dumps({'storage_checks': reports}, ensure_ascii=False, indent=2), flush=True)
    failed = [r for r in reports if r['status'] != 'ok']
    if failed:
        details = '; '.join(f"{r['path']}: {r['status']}" +
                            (f" (free {r['free_gib']} GiB < reserve {minimum} GiB)" if r['status']=='below_reserve' else '') for r in failed)
        raise SystemExit('Storage preflight stopped: ' + details)


def docker_storage_paths(info):
    """Graph drivers use this daemon's root; snapshotters require an explicit data path."""
    root = info.get('DockerRootDir', '')
    if not root or not Path(root).is_absolute() or not info.get('Driver'):
        raise ValueError('Docker did not report a valid absolute data root and driver')
    paths = [Path(root)]
    status = json.dumps(info.get('DriverStatus', []))
    if 'io.containerd.snapshotter' in status:
        extra = os.getenv('RQ4_CONTAINERD_DATA_ROOT', '').strip()
        if not extra or not Path(extra).is_absolute():
            raise ValueError('Containerd image store detected: set RQ4_CONTAINERD_DATA_ROOT to its verified absolute data path')
        paths.append(Path(extra))
    return list(dict.fromkeys(paths))


def docker_info():
    # Python Docker SDK does not resolve CLI contexts. Require the same endpoint for both.
    if os.getenv('DOCKER_CONTEXT') not in (None, '', 'default'):
        raise ValueError('Unset DOCKER_CONTEXT and select the daemon with DOCKER_HOST for both CLI and Python')
    host = os.getenv('DOCKER_HOST', '')
    if host and not host.startswith('unix:///'):
        raise ValueError('RQ4 storage monitoring requires a local unix:// Docker endpoint')
    # Pin child CLI processes too: their saved context may differ from SDK defaults.
    host = host or 'unix:///var/run/docker.sock'
    os.environ['DOCKER_HOST'] = host
    os.environ.pop('DOCKER_CONTEXT', None)
    command = ['docker', '--host', host]
    raw = subprocess.check_output(command + ['info','--format','{{json .}}'], text=True, timeout=30)
    return json.loads(raw)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--min-free-gb',type=float,default=10)
    args=p.parse_args()
    if not 0 < args.min_free_gb < float('inf'):
        p.error('Reserve must be positive and finite')
    paths = [Path(__file__).resolve().parents[1], *docker_storage_paths(docker_info())]
    check_storage(paths,args.min_free_gb)


if __name__=='__main__':main()
