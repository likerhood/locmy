from pathlib import Path
import sys
import unittest


HARNESS = Path(__file__).resolve().parents[1] / "repos/SWE-bench__SWE-bench"
sys.path.insert(0, str(HARNESS))

from swebench.harness.docker_utils import write_to_container  # noqa: E402


class FakeContainer:
    def __init__(self):
        self.commands = []

    def exec_run(self, command):
        self.commands.append(command)


class PatchTransportTests(unittest.TestCase):
    def test_multiline_patch_with_quotes_uses_explicit_shell_argv(self):
        container = FakeContainer()
        patch = "diff --git a/a b/a\n+'unclosed-looking quote\n"
        write_to_container(container, patch, Path("/tmp/patch.diff"))
        command = container.commands[0]
        self.assertEqual(command[:2], ["/bin/bash", "-lc"])
        self.assertIn(patch, command[2])


if __name__ == "__main__":
    unittest.main()
