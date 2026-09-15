import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from collections import namedtuple
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from storage_check import inspect_storage


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
