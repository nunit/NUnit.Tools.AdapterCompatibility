"""Check discovered identities, supported filter escaping and the NUnit 4 timer control."""

import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
TARGETS = json.loads(os.environ["NUNIT_COMPAT_TARGETS"])

from fixture_common import discovered, execute, observations, results, save, settings

dotnet = str(Path(sys.argv[1]).resolve())
environment = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
                   TESTINGPLATFORM_TELEMETRY_OPTOUT="1", MSBUILDDISABLENODEREUSE="1",
                   DOTNET_CLI_HOME=str(root / ".cli-home"), DOTNET_ROOT=str(Path(dotnet).parent))
environment.pop("DOTNET_TEST_RUNNER", None)
environment["PATH"] = str(Path(dotnet).parent) + os.pathsep + environment.get("PATH", "")
evidence = root / "control-evidence"
evidence.mkdir(exist_ok=False)
escaped = json.loads((root / "EscapedNames.json").read_text(encoding="utf-8-sig"))
rows = []
for version in ["baseline", "candidate"]:
    for target in TARGETS:
        assembly = root / version / "MTP/bin/Release" / target / "AdapterCompatibility.MTP.dll"
        directory = evidence / (version + "-" + target + "-discovery")
        process = execute([dotnet, str(assembly), "--list-tests", "json"], directory, root, environment)
        identities = discovered(directory)
        assert process["exit"] == 0 and len(identities) == 10
        assert all([item["uid"], item["displayName"]] in [list(identity) for identity in identities] for item in escaped)
        save(directory / "identities.json", identities)
        if version == "baseline" or "--include-candidate-timer" in sys.argv[2:]:
            directory = evidence / (version + "-" + target + "-timer")
            configuration = evidence / (directory.name + ".runsettings")
            configuration.write_text(settings(directory), encoding="utf-8")
            process = execute([dotnet, str(assembly), "--settings", str(configuration),
                               "--results-directory", str(directory), "--filter",
                               "FullyQualifiedName=NUnit.AdapterCompatibility.CompatibilityCases.PerTestCancellation",
                               "--report-trx", "--report-trx-filename", "results.trx"], directory, root, environment)
            tests = results(directory)
            assert not process["timedOut"] and process["exit"] == 2 and len(tests) == 1
            assert tests[0].get("outcome") == "Failed"
            assert all(len(observations(directory, phase)) == 1 for phase in ["started", "token-cancelled", "finally", "teardown"])
        assembly = root / version / "VSTest/bin/Release" / target / "AdapterCompatibility.VSTest.dll"
        for index, item in enumerate(escaped):
            directory = evidence / (version + "-" + target + "-escaped-" + str(index))
            configuration = evidence / (directory.name + ".runsettings")
            configuration.write_text(settings(directory), encoding="utf-8")
            process = execute([dotnet, "test", str(assembly), "--filter", "FullyQualifiedName=" + item["escaped"],
                               "--settings", str(configuration), "--results-directory", str(directory),
                               "--logger", "trx;LogFileName=results.trx"], directory, root, environment)
            tests = results(directory)
            row = {"name": directory.name, "framework": version, "target": target,
                   "uid": item["uid"], "escaped": item["escaped"], "process": process,
                   "results": [{"name": test.get("testName"), "outcome": test.get("outcome")} for test in tests],
                   "selectedExactlyOnePassingTest": process["exit"] == 0 and not process["timedOut"]
                   and len(tests) == 1 and tests[0].get("outcome") == "Passed"}
            rows.append(row)
            save(evidence / "escaped-results.json", rows)
            print(json.dumps({"name": row["name"], "selectedExactlyOnePassingTest": row["selectedExactlyOnePassingTest"]}), flush=True)
print("Discovered identities and requested per-test timer controls verified.", flush=True)
