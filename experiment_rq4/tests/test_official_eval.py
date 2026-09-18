import contextlib
import io
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from official_eval import parse_calypso_without_stray_brace, run_with_git_mode


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
        self.assertEqual(container.calls[0][0],
                         ['git', 'config', '--local', 'core.filemode', 'false'])

    def test_git_mode_configuration_failure_stops_before_test(self):
        container = FakeContainer(exit_code=1)
        with self.assertRaisesRegex(RuntimeError, 'Cannot normalize Git file-mode tracking'):
            run_with_git_mode(
                container, '/bin/bash /eval.sh', 1800,
                lambda *_: self.fail('Official test started after failed baseline check'),
                workdir='/testbed', user='root',
            )

    def test_calypso_prefix_is_removed_only_with_full_expected_coverage(self):
        spec = SimpleNamespace(
            instance_id='Automattic__wp-calypso-21977',
            FAIL_TO_PASS=['selectors - should pass'],
            PASS_TO_PASS=['reducer - should remain passing'],
        )
        parsed = {
            '} - selectors - should pass': 'PASSED',
            '} - reducer - should remain passing': 'PASSED',
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = parse_calypso_without_stray_brace('', spec, lambda *_: parsed)
        self.assertEqual(set(result), set(spec.FAIL_TO_PASS + spec.PASS_TO_PASS))
        self.assertEqual(parsed['} - selectors - should pass'], 'PASSED')

        incomplete = {'} - selectors - should pass': 'PASSED'}
        self.assertIs(
            parse_calypso_without_stray_brace('', spec, lambda *_: incomplete),
            incomplete,
        )
        other_prefix = {'bad - selectors - should pass': 'PASSED'}
        self.assertIs(
            parse_calypso_without_stray_brace('', spec, lambda *_: other_prefix),
            other_prefix,
        )

    def test_calypso_mixed_wrapper_prefixes_are_normalized(self):
        spec = SimpleNamespace(
            instance_id='Automattic__wp-calypso-21409',
            FAIL_TO_PASS=['selectors - #getCountriesWithStates - returns countries'],
            PASS_TO_PASS=['selectors - #getStates - returns states'],
        )
        parsed = {
            '} - selectors - #getCountriesWithStates - returns countries': 'PASSED',
            'shell wrapper - selectors - #getStates - returns states': 'PASSED',
            'unrelated parser output': 'FAILED',
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = parse_calypso_without_stray_brace('', spec, lambda *_: parsed)
        self.assertEqual(result[spec.FAIL_TO_PASS[0]], 'PASSED')
        self.assertEqual(result[spec.PASS_TO_PASS[0]], 'PASSED')
        self.assertEqual(result['unrelated parser output'], 'FAILED')
        self.assertNotIn('} - selectors - #getCountriesWithStates - returns countries', result)

    def test_calypso_displaced_outer_suite_is_normalized(self):
        spec = SimpleNamespace(
            instance_id='Automattic__wp-calypso-21409',
            FAIL_TO_PASS=[
                'selectors - #getCountriesWithStates - should return countries',
            ],
            PASS_TO_PASS=[
                'selectors - #getStates - should return states',
            ],
        )
        parsed = {
            '} - #getCountriesWithStates - should return countries': 'PASSED',
            'return request( siteId ) - #getStates - should return states': 'PASSED',
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = parse_calypso_without_stray_brace('', spec, lambda *_: parsed)
        self.assertEqual(set(result), set(spec.FAIL_TO_PASS + spec.PASS_TO_PASS))
        self.assertTrue(all(status == 'PASSED' for status in result.values()))

    def test_calypso_missing_two_part_outer_suite_uses_unique_longest_suffix(self):
        spec = SimpleNamespace(
            instance_id='Automattic__wp-calypso-21648',
            FAIL_TO_PASS=[
                'reducer - disabled option keeps empty value',
                'actions - #emailSettingsSubmitSettings() - should dispatch a request action',
            ],
            PASS_TO_PASS=[
                'reducer - should store data from the action',
                'actions - #fetchEmailSettings() - should dispatch a request action',
            ],
        )
        parsed = {
            # The parser lost the two-part reducer suite but descriptions remain unique.
            'shell output - disabled option keeps empty value': 'PASSED',
            'shell output - should store data from the action': 'PASSED',
            # Repeated descriptions remain distinguishable by their function component.
            'wrapper - #emailSettingsSubmitSettings() - should dispatch a request action': 'PASSED',
            'wrapper - #fetchEmailSettings() - should dispatch a request action': 'PASSED',
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = parse_calypso_without_stray_brace('', spec, lambda *_: parsed)
        self.assertEqual(set(result), set(spec.FAIL_TO_PASS + spec.PASS_TO_PASS))
        self.assertTrue(all(status == 'PASSED' for status in result.values()))

    def test_calypso_ambiguous_suffix_is_not_normalized(self):
        spec = SimpleNamespace(
            instance_id='Automattic__wp-calypso-21409',
            FAIL_TO_PASS=['selectors - duplicate'],
            PASS_TO_PASS=[],
        )
        parsed = {
            '} - selectors - duplicate': 'PASSED',
            'wrapper - selectors - duplicate': 'FAILED',
        }
        self.assertIs(
            parse_calypso_without_stray_brace('', spec, lambda *_: parsed),
            parsed,
        )

    def test_calypso_dash_prefix_with_duplicate_descriptions_is_normalized(self):
        spec = SimpleNamespace(
            instance_id='Automattic__wp-calypso-30240',
            FAIL_TO_PASS=[
                '<SiteInformation /> - should render',
                '<SiteVerticalsSuggestionSearch /> - should render',
            ],
            PASS_TO_PASS=[
                '<SiteInformation /> - should call `submitStep()` from `handleSubmit()`',
            ],
        )
        parsed = {
            '- <SiteInformation /> - should render': 'PASSED',
            '- <SiteVerticalsSuggestionSearch /> - should render': 'PASSED',
            '- <SiteInformation /> - should call `submitStep()` from `handleSubmit()`': 'PASSED',
            'unrelated parser output': 'FAILED',
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = parse_calypso_without_stray_brace('', spec, lambda *_: parsed)
        self.assertTrue(all(result[test_id] == 'PASSED'
                            for test_id in spec.FAIL_TO_PASS + spec.PASS_TO_PASS))
        self.assertEqual(result['unrelated parser output'], 'FAILED')
        self.assertNotIn('- <SiteInformation /> - should render', result)
        self.assertIn('Removed stray dash suite prefix', output.getvalue())


if __name__ == '__main__':
    unittest.main()
