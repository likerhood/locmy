"""One repo per project, atomic clone, configurable mirror -> official fallback."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit


def urls(repo):
    official = f'https://github.com/{repo}.git'
    prefix = os.environ.get('RQ4_GITHUB_MIRROR_PREFIX', os.getenv('GITHUB_MIRROR_PREFIX',
        os.getenv('REPO_GITHUB_MIRROR_PREFIX', 'https://gh.xmly.dev'))).strip().rstrip('/')
    if prefix:
        parsed = urlsplit(prefix)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Git mirror must be an HTTPS prefix without credentials/query/fragment')
    return ([prefix + '/' + official] if prefix else []) + [official]


def has_commit(path, commit):
    return subprocess.run(['git', '-C', str(path), 'cat-file', '-e', commit+'^{commit}'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def repo_for(root, sample, resources):
    repo, commit = sample['repo'], sample['base_commit']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        raise ValueError('Invalid repository or full base_commit SHA')
    path = Path(root) / 'repos' / repo.replace('/', '__')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and has_commit(path, commit):
        resources.event('repo_reused', repo=repo, commit=commit)
        return path
    if path.exists():
        valid = subprocess.run(['git', '-C', str(path), 'rev-parse', '--git-dir'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not valid:
            raise RuntimeError('Incomplete existing repo; preserve/rename it before retrying')
    timeout = int(os.getenv('RQ4_GIT_TIMEOUT', '600'))
    retries = int(os.getenv('RQ4_GIT_RETRIES', '2'))
    if not 1 <= retries <= 5 or timeout <= 0:
        raise ValueError('Git retries must be 1..5 and timeout positive')
    git = ['git', '-c', 'http.lowSpeedLimit=1024', '-c', 'http.lowSpeedTime=60']
    for index, url in enumerate(urls(repo)):
        for attempt in range(retries):
            temporary = None
            try:
                if not path.exists():
                    temporary = Path(tempfile.mkdtemp(prefix='.rq4-clone-', dir=path.parent))
                    target = temporary / 'repo'
                    resources.run(git + ['clone', '--no-checkout', url, str(target)], timeout=timeout)
                else:
                    target = path
                if not has_commit(target, commit):
                    resources.run(git + ['-C', str(target), 'fetch', '--no-tags', url, commit], timeout=timeout)
                if not has_commit(target, commit):
                    raise RuntimeError('Downloaded repository lacks requested commit')
                if temporary:
                    target.rename(path)
                resources.event('repo_ready', repo=repo, commit=commit, source_index=index, attempt=attempt+1)
                return path
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
                from resources import Paused
                if isinstance(exc, Paused):
                    raise
                resources.event('git_retry', repo=repo, source_index=index, attempt=attempt+1, error=type(exc).__name__)
            finally:
                if temporary:
                    shutil.rmtree(temporary)
    raise RuntimeError('Git mirror and official attempts exhausted; no repair API called for this sample')
