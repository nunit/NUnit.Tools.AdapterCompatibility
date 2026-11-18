"""Build and run one explicitly pinned local compatibility profile."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from compatibility import PROJECTS, assess, profile, require, save, summary


ROOT = Path(__file__).resolve().parent


def invoke(command, log, work, environment, timeout=1000):
    """Retain command failure and fail the orchestration instead of interpreting it as a test result."""
    with log.open('x', encoding='utf-8') as output:
        result = subprocess.run(command, cwd=work, env=environment, stdout=output,
                                stderr=subprocess.STDOUT, timeout=timeout)
    save(log.with_suffix('.exit.json'), {'exitCode': result.returncode, 'command': command})
    require(result.returncode == 0, 'Command failed; inspect ' + str(log))


def stage(work, selected, selected_path, prepare_locks):
    """Snapshot only maintained source and the chosen lock set, never historical results or caches."""
    sources = list(ROOT.glob('*.py')) + list(ROOT.glob('*.props')) + [ROOT / name for name in [
        '.editorconfig', 'global.json', 'NuGet.Config', 'identities.json', 'EscapedNames.json']]
    sources += list((ROOT / 'Shared').glob('*.cs')) + list((ROOT / 'Harness').glob('*.cs'))
    sources += [ROOT / project for project in PROJECTS]
    identities = []
    for source in sources:
        destination = work / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        destination.write_bytes(data)
        identities.append({'file': source.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(data).hexdigest()})
    # Version selection is serialized as XML, never inserted into shell source.
    props = ET.Element('Project')
    group = ET.SubElement(props, 'PropertyGroup')
    for name, value in [('LangVersion', '14'), ('Nullable', 'enable'), ('ImplicitUsings', 'disable'),
                        ('IsPackable', 'false'), ('TreatWarningsAsErrors', 'true'), ('GenerateDocumentationFile', 'true'),
                        ('RestorePackagesWithLockFile', 'true'), ('BaselineFrameworkVersion', selected['baselineFramework']),
                        ('CandidateFrameworkVersion', selected['candidateFramework']), ('AdapterVersion', selected['adapter'])]:
        ET.SubElement(group, name).text = value
    (work / 'Directory.Build.props').write_bytes(ET.tostring(props, encoding='utf-8'))
    for project in PROJECTS[1:]:
        path = work / project
        document = ET.parse(path)
        document.find('.//TargetFrameworks').text = ';'.join(selected['targets'])
        document.write(path, encoding='utf-8', xml_declaration=True)
    save(work / 'global.json', {'sdk': {'version': selected['sdk'], 'rollForward': 'disable'}, 'test': {'runner': 'VSTest'}})
    if not prepare_locks:
        locks = selected_path.parent / selected['locks']
        for project in PROJECTS:
            relative = Path(project).parent / 'packages.lock.json'
            source = locks / relative
            require(source.is_file(), 'Reviewed dependency lock missing: ' + str(relative))
            shutil.copyfile(source, work / relative)
    save(work / 'InputSources.json', identities)
    save(work / 'SelectedProfile.json', selected)


def download(work, selected):
    """Fetch exactly the approved public inputs and reject any changed bytes."""
    directory = work / 'reviewed-packages'
    directory.mkdir()
    rows = []
    for item in selected['packages']:
        target = directory / (item['id'] + '.' + item['version'] + '.nupkg')
        with urllib.request.urlopen(item['url'], timeout=90) as response, target.open('xb') as output:
            require(response.geturl().startswith('https://'), 'Package download left HTTPS.')
            shutil.copyfileobj(response, output)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        require(digest == item['sha256'], 'Package hash mismatch: ' + target.name)
        rows.append(dict(item, bytes=target.stat().st_size))
    save(work / 'PackageDownloads.json', rows)


def audit(work):
    """Independently reject remaining native process references to this owned run."""
    if os.name == 'nt':
        command = [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
                   '-NoProfile', '-NonInteractive', '-Command',
                   '$ErrorActionPreference="Stop"; $p=@(Get-CimInstance Win32_Process | Where-Object { '
                   '$_.ProcessId -ne [int]$env:NUNIT_AUDIT_PID -and $_.CommandLine -and '
                   '$_.CommandLine.Replace("/","\\").IndexOf($env:NUNIT_AUDIT_ROOT+"\\",[StringComparison]::OrdinalIgnoreCase) -ge 0 '
                   '} | Select-Object ProcessId,Name); ConvertTo-Json -InputObject $p -Compress']
        environment = dict(os.environ, NUNIT_AUDIT_ROOT=str(work), NUNIT_AUDIT_PID=str(os.getpid()))
        matches = json.loads(subprocess.check_output(command, env=environment, text=True, timeout=30))
    else:
        output = subprocess.check_output(['/bin/ps', '-axo', 'pid=,comm=,args='], text=True, timeout=30)
        matches = [line for line in output.splitlines() if str(work) + '/' in line and int(line.split(None, 1)[0]) != os.getpid()]
    save(work / 'NativeProcessAudit.json', {'references': matches, 'passed': not matches})
    require(not matches, 'Native processes still reference this run; retain its work directory for inspection.')


def collect(work, output):
    """Copy allowlisted text evidence for CI artifacts, excluding package caches and compiled outputs."""
    blocked = {'.packages', '.http-cache', '.cli-home', 'bin', 'obj', '__pycache__', 'reviewed-packages'}
    for path in work.rglob('*'):
        relative = path.relative_to(work)
        if path.is_file() and not blocked.intersection(relative.parts) and path.suffix in {
                '.txt', '.log', '.json', '.xml', '.trx', '.runsettings'}:
            destination = output / 'evidence' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


def execute(args, selected, selected_path):
    """Run a fresh profile, retaining infrastructure, assertion and stop outcomes separately."""
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.gettempdir()) / 'nunit-adapter-compatibility' / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=False)
    save(output / 'run-location.json', {'workDirectory': str(work)})
    print('Work: ' + str(work), flush=True)
    dotnet = str(Path(shutil.which(args.dotnet) or args.dotnet).resolve())
    environment = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_NOLOGO='1',
                       TESTINGPLATFORM_TELEMETRY_OPTOUT='1', MSBUILDDISABLENODEREUSE='1',
                       DOTNET_ROOT=str(Path(dotnet).parent), DOTNET_CLI_HOME=str(work / '.cli-home'),
                       NUGET_PACKAGES=str(work / '.packages'), NUGET_HTTP_CACHE_PATH=str(work / '.http-cache'),
                       NUNIT_COMPAT_TARGETS=json.dumps(selected['targets']), NUNIT_COMPAT_SDK=selected['sdk'])
    environment['PATH'] = str(Path(dotnet).parent) + os.pathsep + environment.get('PATH', '')
    environment.pop('DOTNET_TEST_RUNNER', None)
    exit_code, result = 2, None
    try:
        stage(work, selected, selected_path, args.command == 'prepare-locks')
        sdk = subprocess.check_output([dotnet, '--version'], cwd=work, env=environment, text=True).strip()
        require(sdk == selected['sdk'], 'Selected SDK not available: ' + selected['sdk'])
        runtimes = subprocess.check_output([dotnet, '--list-runtimes'], env=environment, text=True)
        if args.command == 'run':
            require(all('Microsoft.NETCore.App ' + target[3:].split('.')[0] + '.' in runtimes for target in selected['targets']),
                    'Matching-architecture target runtimes must be installed before execution.')
        (work / 'runtime-inventory.txt').write_text(runtimes, encoding='utf-8')
        invoke([dotnet, '--info'], work / 'sdk-info.txt', work, environment)
        download(work, selected)
        before = {project: hashlib.sha256((work / Path(project).parent / 'packages.lock.json').read_bytes()).hexdigest()
                  for project in PROJECTS} if args.command != 'prepare-locks' else {}
        for index, project in enumerate(PROJECTS):
            command = [dotnet, 'restore', project, '--no-http-cache', '--configfile', str(work / 'NuGet.Config')]
            if args.command != 'prepare-locks':
                command.append('--locked-mode')
            invoke(command, work / ('restore-' + str(index) + '.txt'), work, environment, timeout=300)
        if args.command == 'prepare-locks':
            for project in PROJECTS:
                relative = Path(project).parent / 'packages.lock.json'
                destination = output / 'proposed-locks' / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work / relative, destination)
            save(output / 'lock-proposal.json', {'profile': selected, 'reviewRequired': True, 'testsExecuted': False})
            exit_code = 0
        else:
            require(all(hashlib.sha256((work / Path(project).parent / 'packages.lock.json').read_bytes()).hexdigest() == digest
                        for project, digest in before.items()), 'Locked restore changed its inputs.')
            for index, project in enumerate(['Harness/Harness.csproj', 'baseline/MTP/MTP.csproj']):
                invoke([dotnet, 'format', 'style', project, '--no-restore', '--verify-no-changes', '--severity', 'warn'],
                       work / ('style-' + str(index) + '.txt'), work, environment, timeout=180)
            invoke([sys.executable, str(work / 'compare_hosts.py'), dotnet], work / 'comparison.stdout.txt', work, environment)
            invoke([sys.executable, str(work / 'verify_controls.py'), dotnet, '--include-candidate-timer'],
                   work / 'controls.stdout.txt', work, environment)
            result = assess(work, selected)
            result.update(platform=platform.platform(), architecture=platform.machine(), sdk=sdk,
                          strictStopRequired=args.require_cooperative_stop)
            exit_code = 0 if result['contracts']['passed'] and (result['runnerStop']['passed'] or not args.require_cooperative_stop) else 1
    except Exception as exception:
        save(output / 'infrastructure-failure.json', {'type': type(exception).__name__, 'error': str(exception), 'exitCode': 2})
        print(str(exception), file=sys.stderr)
    finally:
        try:
            audit(work)
        except Exception as exception:
            save(output / 'audit-failure.json', {'error': str(exception)})
            exit_code = 2
        collect(work, output)
    if result is not None:
        result['exitCode'] = exit_code
        save(output / 'results.json', result)
        (output / 'summary.md').write_text(summary(result), encoding='utf-8')
        print(summary(result), flush=True)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'profile', 'prepare-locks'])
    parser.add_argument('--profile', type=Path, default=ROOT / 'profiles/historical-alpha.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/run')
    parser.add_argument('--dotnet', default='dotnet')
    parser.add_argument('--github-output', type=Path)
    parser.add_argument('--require-cooperative-stop', action='store_true', help='Fail for any incomplete runner-stop observation.')
    args = parser.parse_args()
    path = args.profile.resolve()
    selected = profile(path)
    if args.command == 'profile':
        print(json.dumps(selected, indent=2))
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8') as output:
                output.write('sdk=' + selected['sdk'] + '\nname=' + selected['name'] + '\n')
                sdk_major = selected['sdk'].split('.')[0]
                installs = [target[3:] + '.x' for target in selected['targets'] if target[3:].split('.')[0] != sdk_major]
                output.write('dotnet-versions<<NUNIT_DOTNET_VERSIONS\n' + '\n'.join(installs + [selected['sdk']])
                             + '\nNUNIT_DOTNET_VERSIONS\n')
        return 0
    return execute(args, selected, path)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exception:
        print('Input or prerequisite error: ' + str(exception), file=sys.stderr)
        sys.exit(2)
