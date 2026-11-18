"""Compare exact-name filtering and genuine runner stop across NUnit 4 and 5."""

from concurrent.futures import ThreadPoolExecutor
import platform
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parent
TARGETS = json.loads(os.environ["NUNIT_COMPAT_TARGETS"])

from fixture_common import execute, observations, results, save, settings

dotnet = str(Path(sys.argv[1]).resolve())
evidence = root / "evidence"
evidence.mkdir(exist_ok=False)
environment = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
                   TESTINGPLATFORM_TELEMETRY_OPTOUT="1", MSBUILDDISABLENODEREUSE="1",
                   DOTNET_CLI_HOME=str(root / ".cli-home"),
                   DOTNET_ROOT=str(Path(dotnet).parent), NUGET_PACKAGES=str(root / ".packages"))
environment.pop("DOTNET_TEST_RUNNER", None)
environment["PATH"] = str(Path(dotnet).parent) + os.pathsep + environment.get("PATH", "")

for project in ["Harness/Harness.csproj"] + [version + "/" + host + "/" + host + ".csproj"
                                             for version in ["baseline", "candidate"] for host in ["VSTest", "MTP"]]:
    name = "build-" + project.replace("/", "-")
    record = execute([dotnet, "build", project, "-c", "Release", "--disable-build-servers",
                      "--no-restore", "-v", "minimal"],
                     evidence / name, root, environment, timeout=240)
    assert record["exit"] == 0, project + " build failed"

identities = json.loads((root / "identities.json").read_text())
selections = [identity for identity in identities
              if identity[1] in ["Named case (with space)", 'ParameterizedIdentity("a \\"quoted\\" value")']]
assert len(selections) == 2, selections
selections.insert(0, ["NUnit.AdapterCompatibility.CompatibilityCases.OutputAndPass", "OutputAndPass"])
rows = []
for version in ["baseline", "candidate"]:
    for target in TARGETS:
        for host in ["VSTest", "MTP"]:
            assembly = root / version / host / "bin/Release" / target / ("AdapterCompatibility." + host + ".dll")
            for index, (uid, display) in enumerate(selections):
                name = "-".join([version, target, host, "filter", str(index)])
                directory = evidence / name
                configuration = evidence / (name + ".runsettings")
                configuration.write_text(settings(directory), encoding="utf-8")
                if host == "VSTest":
                    command = [dotnet, "test", str(assembly), "--filter", "FullyQualifiedName=" + uid,
                               "--settings", str(configuration), "--results-directory", str(directory),
                               "--logger", "trx;LogFileName=results.trx"]
                else:
                    command = [dotnet, str(assembly), "--filter-uid", uid, "--settings", str(configuration),
                               "--results-directory", str(directory), "--report-trx", "--report-trx-filename", "results.trx"]
                record = execute(command, directory, root, environment)
                row = {"name": name, "framework": version, "target": target, "host": host,
                       "uid": uid, "displayName": display, "process": record}
                try:
                    tests = results(directory)
                    row["results"] = [{"name": test.get("testName"), "outcome": test.get("outcome")} for test in tests]
                    row["selectedExactlyOnePassingTest"] = (record["exit"] == 0 and not record["timedOut"]
                                                            and len(tests) == 1 and tests[0].get("outcome") == "Passed")
                except AssertionError as error:
                    row.update(resultError=str(error), selectedExactlyOnePassingTest=False)
                rows.append(row)
                save(evidence / "filters.json", rows)
                print(json.dumps({key: row[key] for key in ["name", "selectedExactlyOnePassingTest"]}), flush=True)


def stop_probe(specification):
    version, target, host = specification
    name = "-".join([version, target, host, "runner-stop"])
    directory = evidence / name
    assembly = root / version / host / "bin/Release" / target / ("AdapterCompatibility." + host + ".dll")
    if host == "VSTest":
        harness = root / "Harness/bin/Release/net10.0/Harness.dll"
        console = Path(dotnet).parent / ("sdk/" + os.environ["NUNIT_COMPAT_SDK"] + "/vstest.console.dll")
        child_evidence = evidence / (name + "-harness")
        command = [dotnet, str(harness), "vstest", str(assembly), str(console), str(child_evidence)]
        observed = child_evidence / "cancellation"
    else:
        configuration = evidence / (name + ".runsettings")
        configuration.write_text(settings(directory), encoding="utf-8")
        command = [dotnet, str(assembly), "--settings", str(configuration), "--results-directory", str(directory),
                   "--filter", "FullyQualifiedName=NUnit.AdapterCompatibility.CompatibilityCases.CooperativeCancellation",
                   "--timeout", "5s", "--report-trx", "--report-trx-filename", "results.trx"]
        observed = directory
    record = execute(command, directory, root, environment, timeout=45)
    observation_root = observed / "observations" if host == "MTP" else observed
    counts = {phase: len(list(observation_root.glob(phase + "-*.xml")))
              for phase in ["started", "token-cancelled", "finally", "teardown"]}
    logs = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in
                     list(directory.glob("*.txt")) + (list(child_evidence.glob("*.json")) + list(child_evidence.glob("*.txt"))
                                                       if host == "VSTest" else []))
    row = {"name": name, "framework": version, "target": target, "host": host,
           "process": record, "observations": counts,
           "parameterCountMismatch": "TargetParameterCountException" in logs}
    if host == "VSTest":
        p = child_evidence / "cancellation-probe.json"
        if p.exists():
            row["vstestCancellation"] = json.loads(p.read_text())
    save(directory / "stop-observations.json", row)
    print(json.dumps({key: row[key] for key in ["name", "observations", "parameterCountMismatch"]}), flush=True)
    return row


specifications = [(version, target, host) for version in ["baseline", "candidate"]
                  for target in TARGETS for host in ["VSTest", "MTP"]]
with ThreadPoolExecutor(max_workers=2) as executor:
    stop_rows = list(executor.map(stop_probe, specifications))
save(evidence / "stops.json", stop_rows)
save(evidence / "summary.json", {"sdk": os.environ["NUNIT_COMPAT_SDK"], "scope": platform.platform() + " " + platform.machine(), "filters": rows, "stops": stop_rows,
                                 "allProductContractsPassed": False})
print("Comparison completed; product failures retained in evidence.", flush=True)
