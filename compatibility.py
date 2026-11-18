"""Validated input profiles and separate compatibility/runner-stop results."""
import itertools
import json
from pathlib import Path
import re
import urllib.parse
import xml.etree.ElementTree as ET


PROJECTS = ['Harness/Harness.csproj'] + [version + '/' + host + '/' + host + '.csproj'
    for version in ['baseline', 'candidate'] for host in ['MTP', 'VSTest']]
PHASES = ['started', 'token-cancelled', 'finally', 'teardown']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')


def profile(path):
    """Reject missing, ambiguous or unsafe profile values before starting a process."""
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    require(set(data) == {'name', 'sdk', 'targets', 'baselineFramework', 'candidateFramework', 'adapter', 'locks', 'packages'},
            'Unexpected or missing profile fields.')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', data['name']), 'Invalid profile name.')
    require(re.fullmatch(r'\d+\.\d+\.\d+', data['sdk']), 'Select an exact SDK version.')
    require(isinstance(data['targets'], list) and data['targets'] and len(set(data['targets'])) == len(data['targets'])
            and all(re.fullmatch(r'net(?:[8-9]|[1-9][0-9])\.0', target) for target in data['targets']), 'Invalid target matrix.')
    for field in ['baselineFramework', 'candidateFramework', 'adapter']:
        require(re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', data[field]), 'Select an explicit package version.')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', data['locks']), 'Locks must name one profile directory.')
    expected = {('NUnit', data['baselineFramework']), ('NUnit', data['candidateFramework']), ('NUnit3TestAdapter', data['adapter'])}
    require(len(data['packages']) == len(expected) and {(p['id'], p['version']) for p in data['packages']} == expected,
            'Package identities do not match the selected versions.')
    for package in data['packages']:
        require(set(package) == {'id', 'version', 'sha256', 'url'} and re.fullmatch(r'[0-9a-f]{64}', package['sha256']),
                'A SHA-256 and public download URL are required for each package.')
        url = urllib.parse.urlparse(package['url'])
        require(url.scheme == 'https' and url.hostname in {'api.nuget.org', 'www.myget.org'}
                and url.username is None and url.password is None and not url.query and not url.fragment,
                'Use an explicit unauthenticated official public package URL.')
        require(url.path.lower().endswith('/' + package['id'].lower() + '.' + package['version'].lower() + '.nupkg'),
                'Download URL does not identify the selected package.')
    return data


def matrix_rows(rows, versions, targets, hosts, suffixes, label):
    """Check exact row identities, including duplicates and omissions, before reading totals."""
    expected = {'-'.join(parts) for parts in itertools.product(versions, targets, hosts, suffixes)}
    names = [row['name'] for row in rows]
    require(len(names) == len(expected) and set(names) == expected, label + ' matrix has missing, duplicate or unexpected rows.')


def assess(work, selected):
    """Keep known stop failures visible without turning them into passing compatibility cases."""
    filters = json.loads((work / 'evidence/filters.json').read_text())
    escaped = json.loads((work / 'control-evidence/escaped-results.json').read_text())
    stops = json.loads((work / 'evidence/stops.json').read_text())
    versions, targets = ['baseline', 'candidate'], selected['targets']
    matrix_rows(filters, versions, targets, ['VSTest', 'MTP'], ['filter-0', 'filter-1', 'filter-2'], 'Filter')
    matrix_rows(escaped, versions, targets, ['escaped'], ['0', '1'], 'Escaped filter')
    matrix_rows(stops, versions, targets, ['VSTest', 'MTP'], ['runner-stop'], 'Stop')
    failures, runtime_rows = [], []
    for row in filters:
        # The raw quoted VSTest expression is deliberately invalid; all other selections must pass.
        invalid = row['host'] == 'VSTest' and row['name'].endswith('-filter-2')
        if invalid:
            logs = '\n'.join(path.read_text(encoding='utf-8', errors='replace')
                             for path in (work / 'evidence' / row['name']).glob('*.txt'))
            valid = not row['selectedExactlyOnePassingTest'] and not row['process']['timedOut'] and 'escape' in logs.lower()
        else:
            valid = row['selectedExactlyOnePassingTest'] and row['process']['exit'] == 0 and not row['process']['timedOut']
        if not valid:
            failures.append(row['name'])
    failures.extend(row['name'] for row in escaped if not row['selectedExactlyOnePassingTest']
                    or row['process']['exit'] != 0 or row['process']['timedOut'])
    for version, target in itertools.product(versions, targets):
        directory = work / 'control-evidence' / (version + '-' + target + '-timer/observations')
        require(all(len(list(directory.glob(phase + '-*.xml'))) == 1 for phase in PHASES), 'Missing timer-control evidence.')
        started = ET.parse(next(directory.glob('started-*.xml'))).getroot()
        require(started.get('runtime', '').split('.')[0] == target[3:].split('.')[0], 'Target runtime rolled forward unexpectedly.')
        runtime_rows.append({'framework': version, 'target': target, 'runtime': started.get('runtime')})
    # An absent start or duplicated phase is an incomplete probe, not evidence of the known stop gap.
    for row in stops:
        require(set(row['observations']) == set(PHASES) and row['observations']['started'] == 1
                and all(type(value) is int and value in [0, 1] for value in row['observations'].values()),
                'Runner-stop probe did not produce an unambiguous observed start.')
    missing = [row['name'] for row in stops if not all(row['observations'][phase] == 1 for phase in PHASES)]
    return {'profile': selected['name'], 'versions': {key: selected[key] for key in ['baselineFramework', 'candidateFramework', 'adapter']},
            'contracts': {'passed': not failures, 'failures': failures, 'filterSelections': len(filters) - 2 * len(targets),
                          'escapedSelections': len(escaped), 'invalidRawFilters': 2 * len(targets), 'timerControls': len(runtime_rows)},
            'runnerStop': {'passed': not missing, 'incomplete': missing, 'observations': stops}, 'actualRuntimes': runtime_rows}


def summary(result):
    """Produce a human-readable CI summary which explicitly names incomplete stop behavior."""
    lines = ['# Adapter compatibility: ' + result['profile'], '',
             'Compatibility checks: **' + ('passed' if result['contracts']['passed'] else 'FAILED') + '**', '',
             'Cooperative runner stop: **' + ('passed' if result['runnerStop']['passed'] else 'INCOMPLETE') + '**', '',
             'These statuses are independent. A compatibility pass does not establish complete runner cancellation.', '',
             '| Target | Framework | Actual runtime |', '| --- | --- | --- |']
    lines.extend('| ' + row['target'] + ' | ' + row['framework'] + ' | ' + row['runtime'] + ' |' for row in result['actualRuntimes'])
    if result['runnerStop']['incomplete']:
        lines += ['', 'Incomplete stop observations:'] + ['- `' + name + '`' for name in result['runnerStop']['incomplete']]
    return '\n'.join(lines) + '\n'
