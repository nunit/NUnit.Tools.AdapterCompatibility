"""Fail closed on absent/invalid reports and publish independent compatibility/stop statuses."""
import argparse
import json
from pathlib import Path
import sys

from compatibility import require, summary


def policy(result, strict):
    """An infrastructure or compatibility failure always fails; strict mode also requires completed stop observations."""
    require(type(result['contracts']['passed']) is bool and type(result['runnerStop']['passed']) is bool,
            'Result statuses must be Boolean.')
    require(type(result['exitCode']) is int and result['exitCode'] in [0, 1, 2], 'Invalid producer exit code.')
    require(bool(result['contracts']['failures']) == (not result['contracts']['passed']), 'Compatibility status contradicts its failures.')
    require(bool(result['runnerStop']['incomplete']) == (not result['runnerStop']['passed']), 'Stop status contradicts its observations.')
    if result['exitCode'] == 2:
        return 2
    if result['exitCode'] == 1 or not result['contracts']['passed'] or (strict and not result['runnerStop']['passed']):
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--require-stop', choices=['true', 'false'], default='false')
    args = parser.parse_args()
    try:
        result = json.loads(args.results.read_text(encoding='utf-8'))
        code = policy(result, args.require_stop == 'true')
        text = summary(result) + '\nStrict cooperative-stop requirement: ' + args.require_stop + '\n'
        if code == 2:
            text = '# Infrastructure verification FAILED\n\n' + text
    except Exception as error:
        code = 2
        text = '# Adapter compatibility: infrastructure failure\n\nNo valid complete result was available: ' + type(error).__name__ + '\n'
    with args.summary.open('a', encoding='utf-8') as output:
        output.write(text)
    print(text)
    return code


if __name__ == '__main__':
    sys.exit(main())
