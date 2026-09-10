#!/usr/bin/env python3
"""Check generated patch application against captured base-commit file contents.

Runs Git in an isolated temporary directory; does not run repository tests.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from repair import validate_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    prediction = json.loads((args.run_dir / 'prediction.jsonl').read_text())
    request = json.loads((args.run_dir / 'request.json').read_text())
    files = json.loads(request['messages'][1]['content'])['files']
    patch = prediction['model_patch']
    report = {'instance_id': prediction['instance_id'], 'nonempty': bool(patch),
              'applied': False, 'official_tests_run': False, 'resolved': None}
    if patch:
        with tempfile.TemporaryDirectory(prefix='rq4-patch-check-') as temp:
            root = Path(temp)
            subprocess.run(['git', 'init', '-q', temp], check=True)
            for path, content in files.items():
                target = root / validate_path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            result = subprocess.run(['git', '-C', temp, 'apply', '--check', '-'],
                                    input=patch, text=True, capture_output=True)
            report['applied'] = result.returncode == 0
            report['git_apply_error'] = result.stderr.strip()
    (args.run_dir / 'application_check.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
