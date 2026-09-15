import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from collections import namedtuple
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from storage_check import inspect_storage, docker_storage_paths, docker_info
import os


class StorageTests(unittest.TestCase):
    def test_missing_path_is_not_reported_as_low_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(inspect_storage(Path(tmp)/'absent',10)['status'],'path_not_found')

    def test_permission_error_is_distinct(self):
        with patch.object(Path,'stat',side_effect=PermissionError):
            self.assertEqual(inspect_storage('/private/docker',10)['status'],'permission_denied')

    def test_low_disk_reports_actual_free_and_threshold(self):
        usage=namedtuple('usage','total used free')(50*1024**3,45*1024**3,5*1024**3)
        with tempfile.TemporaryDirectory() as tmp,patch('storage_check.shutil.disk_usage',return_value=usage):
            r=inspect_storage(tmp,10)
            self.assertEqual(r['status'],'below_reserve')
            self.assertEqual(r['free_gib'],5)
            self.assertEqual(r['required_free_gib'],10)


class DaemonStorageTests(unittest.TestCase):
    def test_rootless_overlay_ignores_system_containerd(self):
        self.assertEqual(docker_storage_paths({'DockerRootDir':'/data2/rq4','Driver':'overlay2','DriverStatus':[['Backing Filesystem','extfs']]}),[Path('/data2/rq4')])

    def test_snapshotter_requires_explicit_path(self):
        info={'DockerRootDir':'/data2/docker','Driver':'overlayfs','DriverStatus':[['driver-type','io.containerd.snapshotter.v1']]}
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):docker_storage_paths(info)
        with patch.dict(os.environ,{'RQ4_CONTAINERD_DATA_ROOT':'/data2/containerd'},clear=True):
            self.assertEqual(docker_storage_paths(info),[Path('/data2/docker'),Path('/data2/containerd')])


class DockerEndpointTests(unittest.TestCase):
    def test_explicit_socket_propagates_to_children(self):
        with patch.dict(os.environ, {'DOCKER_HOST':'unix:///run/user/1000/docker.sock', 'DOCKER_CONTEXT':'default'}, clear=True), patch('storage_check.subprocess.check_output', return_value='{}') as call:
            docker_info()
            self.assertNotIn('DOCKER_CONTEXT', os.environ)
            self.assertEqual(os.environ['DOCKER_HOST'], 'unix:///run/user/1000/docker.sock')
            self.assertEqual(call.call_args.args[0][:3], ['docker','--host','unix:///run/user/1000/docker.sock'])

    def test_default_socket_overrides_saved_cli_context(self):
        with patch.dict(os.environ, {}, clear=True), patch('storage_check.subprocess.check_output', return_value='{}'):
            docker_info()
            self.assertEqual(os.environ['DOCKER_HOST'], 'unix:///var/run/docker.sock')

    def test_ambiguous_context_rejected(self):
        with patch.dict(os.environ, {'DOCKER_CONTEXT':'rootless'}, clear=True):
            with self.assertRaises(ValueError): docker_info()
