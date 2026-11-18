"""Exercise failure-sensitive reporting boundaries without a feed, SDK or test process."""
import copy
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ci_summary import policy
from compatibility import PHASES, assess, profile, save


ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.selected = profile(ROOT / 'profiles/historical-alpha.json')
        self.selected['targets'] = ['net10.0']
        self.filters, self.escaped, self.stops = [], [], []
        for version in ['baseline', 'candidate']:
            for host, index in itertools.product(['MTP', 'VSTest'], range(3)):
                name = version + '-net10.0-' + host + '-filter-' + str(index)
                invalid = host == 'VSTest' and index == 2
                self.filters.append({'name': name, 'host': host, 'selectedExactlyOnePassingTest': not invalid,
                                     'process': {'exit': 1 if invalid else 0, 'timedOut': False}})
                if invalid:
                    folder = self.work / 'evidence' / name
                    folder.mkdir(parents=True)
                    (folder / 'stderr.txt').write_text('Filter contains an invalid escape sequence.')
            for index in range(2):
                self.escaped.append({'name': version + '-net10.0-escaped-' + str(index),
                                     'selectedExactlyOnePassingTest': True, 'process': {'exit': 0, 'timedOut': False}})
            for host in ['MTP', 'VSTest']:
                self.stops.append({'name': version + '-net10.0-' + host + '-runner-stop',
                                   'observations': dict.fromkeys(PHASES, 0)})
                self.stops[-1]['observations']['started'] = 1
            folder = self.work / 'control-evidence' / (version + '-net10.0-timer/observations')
            folder.mkdir(parents=True)
            for phase in PHASES:
                (folder / (phase + '-case.xml')).write_text('<observation runtime="10.0.15" />')

    def result(self):
        save(self.work / 'evidence/filters.json', self.filters)
        save(self.work / 'evidence/stops.json', self.stops)
        save(self.work / 'control-evidence/escaped-results.json', self.escaped)
        result = assess(self.work, self.selected)
        result['exitCode'] = 0 if result['contracts']['passed'] else 1
        return result

    def test_known_stop_gap_is_visible_and_strict_policy_fails(self):
        result = self.result()
        self.assertTrue(result['contracts']['passed'])
        self.assertFalse(result['runnerStop']['passed'])
        self.assertEqual(4, len(result['runnerStop']['incomplete']))
        self.assertEqual(0, policy(result, False))
        self.assertEqual(1, policy(result, True))

    def test_complete_stop_evidence_passes_both_policies(self):
        for row in self.stops:
            row['observations'] = dict.fromkeys(PHASES, 1)
        result = self.result()
        self.assertTrue(result['runnerStop']['passed'])
        self.assertEqual(0, policy(result, True))

    def test_ordinary_selection_failure_fails_compatibility(self):
        self.filters[0]['selectedExactlyOnePassingTest'] = False
        result = self.result()
        self.assertFalse(result['contracts']['passed'])
        self.assertEqual(1, policy(result, False))

    def test_escaped_filter_failure_fails_compatibility(self):
        self.escaped[0]['process']['exit'] = 1
        self.assertFalse(self.result()['contracts']['passed'])

    def test_wrong_raw_filter_error_is_not_an_expected_rejection(self):
        folder = self.work / 'evidence/baseline-net10.0-VSTest-filter-2'
        (folder / 'stderr.txt').write_text('Failed to load adapter.')
        self.assertFalse(self.result()['contracts']['passed'])

    def test_duplicate_filter_row_cannot_hide_an_omission(self):
        self.filters[-1] = copy.deepcopy(self.filters[0])
        with self.assertRaisesRegex(ValueError, 'matrix'):
            self.result()

    def test_missing_stop_row_is_infrastructure_failure(self):
        self.stops.pop()
        with self.assertRaisesRegex(ValueError, 'matrix'):
            self.result()

    def test_stop_that_never_started_is_not_a_known_gap(self):
        self.stops[0]['observations']['started'] = 0
        with self.assertRaisesRegex(ValueError, 'observed start'):
            self.result()

    def test_duplicate_cleanup_is_rejected(self):
        self.stops[0]['observations']['finally'] = 2
        with self.assertRaisesRegex(ValueError, 'observed start'):
            self.result()

    def test_missing_timer_cleanup_is_rejected(self):
        (self.work / 'control-evidence/candidate-net10.0-timer/observations/teardown-case.xml').unlink()
        with self.assertRaisesRegex(ValueError, 'timer-control'):
            self.result()

    def test_runtime_rollforward_is_rejected(self):
        path = self.work / 'control-evidence/baseline-net10.0-timer/observations/started-case.xml'
        path.write_text('<observation runtime="11.0.0" />')
        with self.assertRaisesRegex(ValueError, 'rolled forward'):
            self.result()

    def test_contradictory_pass_report_is_rejected(self):
        result = self.result()
        result['contracts']['failures'].append('hidden failure')
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            policy(result, False)

    def test_prior_infrastructure_error_cannot_become_a_pass(self):
        result = self.result()
        result['exitCode'] = 2
        self.assertEqual(2, policy(result, False))

    def test_missing_ci_report_fails_and_writes_summary(self):
        summary = self.work / 'summary.md'
        result = subprocess.run([sys.executable, str(ROOT / 'ci_summary.py'), '--results', str(self.work / 'missing.json'),
                                 '--summary', str(summary)], capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('infrastructure failure', summary.read_text())

    def test_profile_rejects_floating_version_and_credential_url(self):
        for field, value in [('adapter', 'latest'), ('sdk', '11.0.x')]:
            data = copy.deepcopy(self.selected)
            data[field] = value
            path = self.work / 'profile.json'
            save(path, data)
            with self.assertRaises(ValueError):
                profile(path)
        data = copy.deepcopy(self.selected)
        data['packages'][0]['url'] = 'https://name:password@www.myget.org/package.nupkg'
        save(self.work / 'profile.json', data)
        with self.assertRaisesRegex(ValueError, 'unauthenticated'):
            profile(self.work / 'profile.json')

    def test_workflow_installs_the_selected_target_runtime_channels(self):
        data = copy.deepcopy(self.selected)
        data['targets'] = ['net9.0', 'net11.0']
        path = self.work / 'profile.json'
        output = self.work / 'github-output.txt'
        save(path, data)
        result = subprocess.run([sys.executable, str(ROOT / 'run.py'), 'profile', '--profile', str(path),
                                 '--github-output', str(output)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('dotnet-versions<<NUNIT_DOTNET_VERSIONS\n9.0.x\n11.0.100\n', output.read_text())
        self.assertNotIn('8.0.x', output.read_text())


if __name__ == '__main__':
    unittest.main()
