import contextlib
import io
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from official_eval import run_with_git_mode


class FakeContainer:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.calls = []

    def exec_run(self, command, *, workdir, user):
        self.calls.append((command, workdir, user))
        return SimpleNamespace(exit_code=self.exit_code, output=b'')


class OfficialEvalTests(unittest.TestCase):
    def test_git_mode_is_set_before_unmodified_official_script(self):
        container = FakeContainer()
        observed = []

        def official(container, command, timeout):
            observed.append((len(container.calls), command, timeout))
            return 'official-result'

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_with_git_mode(
                container, '/bin/bash /eval.sh', 1800, official,
                workdir='/testbed', user='root',
            )
        self.assertEqual(result, 'official-result')
        self.assertEqual(observed, [(1, '/bin/bash /eval.sh', 1800)])
        self.assertEqual(container.calls[0][1:], ('/testbed', 'root'))
        self.assertIn('git config --local core.filemode false', container.calls[0][0][-1])
        self.assertIn('git diff --quiet HEAD -- package.json', container.calls[0][0][-1])

    def test_real_package_change_stops_before_test(self):
        container = FakeContainer(exit_code=1)
        with self.assertRaisesRegex(RuntimeError, 'package.json has a real content change'):
            run_with_git_mode(
                container, '/bin/bash /eval.sh', 1800,
                lambda *_: self.fail('Official test started after failed baseline check'),
                workdir='/testbed', user='root',
            )


if __name__ == '__main__':
    unittest.main()
