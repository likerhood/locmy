import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cache_repo_from_image import verify_git


class ImageRepoCacheTests(unittest.TestCase):
    def test_verifies_frozen_commit_and_selected_file(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
            (repo / 'selected.js').write_text('const answer = 1;\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'selected.js'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'fixture'], check=True)
            commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            cache = repo / 'objects_only'
            cache.mkdir()
            shutil.copytree(repo / '.git', cache / '.git')
            self.assertEqual(verify_git(cache, commit, ['missing.js', 'selected.js']), 'selected.js')
            with self.assertRaises(RuntimeError):
                verify_git(cache, commit, ['missing.js'])
            with self.assertRaises(RuntimeError):
                verify_git(cache, '0' * 40, ['selected.js'])


if __name__ == '__main__':
    unittest.main()
