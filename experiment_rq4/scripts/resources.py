"""Bounded local resource use; only removes images first acquired by this RQ4 cache."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time


class Paused(RuntimeError):
    pass


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


class Resources:
    def __init__(self, root, batch, min_free_gb=10, policy='budget', docker=False):
        if not 0 < min_free_gb < float('inf') or policy not in ['budget','sample']:
            raise ValueError('Invalid resource reserve or image policy')
        self.root, self.batch = Path(root), Path(batch)
        self.minimum = int(min_free_gb * 1024**3)
        self.policy = policy
        self.paths = [self.root]
        self.client = None
        self.daemon_id = None
        self.protected = None
        self.registry = self.root / 'repos/.rq4_images.json'
        self.owned = json.loads(self.registry.read_text()) if self.registry.exists() else {}
        if docker:
            import docker as sdk
            self.client = sdk.from_env(timeout=60)
            if not self.client.api.base_url.startswith('http+docker://'):
                raise Paused('Only a local Docker daemon is supported for disk monitoring')
            info = self.client.info()
            self.daemon_id = info['ID']
            from storage_check import inspect_storage, docker_storage_paths, docker_info
            # Ensure CLI pull/cleanup and SDK grading reach the identical daemon.
            if docker_info().get('ID') != self.daemon_id:
                raise Paused('Docker CLI and SDK target different daemons; set DOCKER_HOST explicitly')
            for location in docker_storage_paths(info):
                probe = inspect_storage(location, 0)
                if probe['status'] != 'ok':
                    raise Paused(f'Docker storage {location}: {probe["status"]}; run scripts/storage_check.py')
                self.paths.append(location)

    def event(self, kind, **fields):
        self.batch.mkdir(parents=True, exist_ok=True)
        with (self.batch / 'resources.jsonl').open('a') as f:
            f.write(json.dumps({'time': time.time(), 'event': kind, **fields}) + '\n')

    def space(self):
        try:
            return {str(p): shutil.disk_usage(p).free for p in self.paths}
        except OSError as e:
            raise Paused('Cannot inspect storage filesystem') from e

    def drop(self, image_id):
        if image_id not in self.owned or image_id == self.protected or not self.client:
            return False
        if self.owned[image_id].get('daemon') != self.daemon_id:
            return False
        try:
            if self.client.containers.list(all=True, filters={'ancestor': image_id}):
                return False
            # Never force deletion, never global prune; foreign tags/references can block removal.
            self.client.images.remove(image_id, force=False, noprune=True)
        except Exception:
            return False
        self.owned.pop(image_id, None)
        save(self.registry, self.owned)
        self.event('image_removed', image_id=image_id)
        return True

    def ensure(self, cleanup=True):
        free = self.space()
        if min(free.values()) < self.minimum and cleanup:
            for image_id in list(self.owned):
                self.drop(image_id)
                free = self.space()
                if min(free.values()) >= self.minimum:
                    break
        if min(free.values()) < self.minimum:
            details = '; '.join(f'{p}: {n/1024**3:.2f} GiB free' for p,n in free.items())
            raise Paused(f'Free space below {self.minimum/1024**3:.2f} GiB reserve: {details}')
        return free

    def run(self, command, *, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=None, timeout=600):
        self.ensure()
        process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
        start = time.monotonic()
        try:
            while process.poll() is None:
                self.ensure(cleanup=False)
                if time.monotonic() - start > timeout:
                    raise subprocess.TimeoutExpired(command[0], timeout)
                time.sleep(1)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command[0])
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        finally:
            self.event('command_finished', executable=command[0], seconds=time.monotonic()-start, free_bytes=self.space())

    def pull_image(self, requested, log):
        """Retry Docker pulls without discarding layers completed by earlier attempts."""
        timeout = int(os.getenv('RQ4_IMAGE_PULL_TIMEOUT', '1800'))
        attempts = int(os.getenv('RQ4_IMAGE_PULL_RETRIES', '3'))
        delay = int(os.getenv('RQ4_IMAGE_PULL_RETRY_DELAY', '10'))
        if timeout <= 0 or not 1 <= attempts <= 5 or delay < 0:
            raise ValueError('Image pull timeout must be positive, retries 1..5, and delay nonnegative')
        for attempt in range(1, attempts + 1):
            log.write(f'\n[rq4-image-pull] image={requested} attempt={attempt}/{attempts} timeout={timeout}s\n')
            log.flush()
            self.event('image_pull_started', ref=requested, attempt=attempt, attempts=attempts)
            try:
                self.run(['docker', 'pull', requested], stdout=log, stderr=subprocess.STDOUT,
                         timeout=timeout)
                self.event('image_pull_succeeded', ref=requested, attempt=attempt, attempts=attempts)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                kind = type(exc).__name__
                self.event('image_pull_failed', ref=requested, attempt=attempt,
                           attempts=attempts, error=kind)
                log.write(f'[rq4-image-pull] attempt={attempt}/{attempts} failed={kind}\n')
                log.flush()
                if attempt == attempts:
                    raise
                if delay:
                    time.sleep(delay * attempt)

    def image(self, ref, pin=None):
        requested = pin or ref
        self.ensure()
        try:
            image = self.client.images.get(requested)
        except Exception as exc:
            from docker.errors import ImageNotFound
            if not isinstance(exc, ImageNotFound):
                raise
            existing_ids = {i.id for i in self.client.images.list(all=True)}
            with (self.batch / 'image_pull.log').open('a') as log:
                self.pull_image(requested, log)
            image = self.client.images.get(requested)
            # Mark ownership only after a successful pull; existing images are never adopted.
            if image.id not in existing_ids:
                self.owned[image.id] = {'ref': requested, 'acquired': time.time(), 'daemon': self.daemon_id}
                save(self.registry, self.owned)
        digests = image.attrs.get('RepoDigests', [])
        if pin:
            if pin not in digests:
                raise Paused('Cached image does not match pinned registry digest')
            digest = pin
        else:
            repository = ref.split('@')[0]
            if ':' in repository.rsplit('/', 1)[-1]:
                repository = repository.rsplit(':', 1)[0]
            matching = [d for d in digests if d.split('@')[0] == repository]
            if not matching:
                raise Paused('Official image has no matching registry digest; refusing mutable-only evaluation')
            digest = matching[0]
        self.protected = image.id
        self.event('image_ready', ref=ref, digest=digest, image_id=image.id, free_bytes=self.space())
        return digest

    def finish_sample(self):
        previous = self.protected
        self.protected = None
        if self.policy == 'sample' and previous:
            self.drop(previous)
        self.ensure()
