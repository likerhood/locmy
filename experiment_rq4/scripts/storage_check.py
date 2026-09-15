#!/usr/bin/env python3
"""Report each actual filesystem; never equate inaccessible paths with low space."""
import argparse
import json
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


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--min-free-gb',type=float,default=10)
    args=p.parse_args()
    if not 0 < args.min_free_gb < float('inf'):
        p.error('Reserve must be positive and finite')
    result=subprocess.run(['docker','info','--format','{{.DockerRootDir}}'],capture_output=True,text=True)
    if result.returncode or not result.stdout.strip():
        raise SystemExit('Cannot obtain DockerRootDir; run docker info to inspect daemon availability')
    paths=[Path(__file__).resolve().parents[1],Path(result.stdout.strip())]
    if Path('/var/lib/containerd').is_dir():
        paths.append(Path('/var/lib/containerd'))
    check_storage(paths,args.min_free_gb)


if __name__=='__main__':main()
