import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
from supervise_pipeline import supervise


class SupervisorTests(unittest.TestCase):
    def test_output_failure_and_append_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(supervise([sys.executable, '-c', 'print("first"); raise SystemExit(7)'], folder, .05), 7)
            self.assertEqual(json.loads((folder/'status.json').read_text())['state'], 'failed')
            self.assertEqual(supervise([sys.executable, '-c', 'print("second")'], folder, .05), 0)
            output = (folder/'pipeline.log').read_text()
            self.assertIn('first', output)
            self.assertIn('second', output)
            self.assertEqual(json.loads((folder/'status.json').read_text())['state'], 'completed')

    def test_sigterm_requests_child_cleanup_and_records_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            child = 'import signal,time; signal.signal(signal.SIGINT, lambda *args: exit(0)); print("READY",flush=True); time.sleep(60)'
            driver = f'import sys; sys.path.insert(0,{str(SCRIPTS)!r}); from supervise_pipeline import supervise; from pathlib import Path; sys.exit(supervise([sys.executable,"-c",{child!r}],Path({tmp!r}),.05))'
            p = subprocess.Popen([sys.executable, '-c', driver], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    log = folder/'pipeline.log'
                    if log.exists() and 'READY' in log.read_text(): break
                    time.sleep(.02)
                else: self.fail('Child never became ready')
                p.send_signal(signal.SIGTERM)
                self.assertEqual(p.wait(timeout=5), 143)
                self.assertEqual(json.loads((folder/'status.json').read_text())['state'], 'interrupted')
            finally:
                if p.poll() is None:
                    p.kill(); p.wait()
