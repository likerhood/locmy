import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from prewarm_images import PULL_ENV_KEYS, RetainingResources, load_pull_settings


class PrewarmImagesTests(unittest.TestCase):
    def test_only_docker_pull_settings_are_loaded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.env'
            path.write_text(
                'RQ4_IMAGE_PULL_TIMEOUT=7200\n'
                'RQ4_API_KEY=must-not-load\n'
                'BASE_URL=https://example.invalid\n')
            before = {key: os.environ.get(key) for key in PULL_ENV_KEYS}
            api_before = os.environ.get('RQ4_API_KEY')
            try:
                os.environ.pop('RQ4_API_KEY', None)
                load_pull_settings(path)
                self.assertEqual(os.environ['RQ4_IMAGE_PULL_TIMEOUT'], '7200')
                self.assertNotIn('RQ4_API_KEY', os.environ)
            finally:
                for key, value in before.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                if api_before is not None:
                    os.environ['RQ4_API_KEY'] = api_before

    def test_retaining_resources_never_enables_cleanup(self):
        resource = RetainingResources.__new__(RetainingResources)
        with mock.patch('resources.Resources.ensure', return_value={'free': 1}) as ensure:
            self.assertEqual(resource.ensure(cleanup=True), {'free': 1})
        ensure.assert_called_once_with(cleanup=False)


if __name__ == '__main__':
    unittest.main()
