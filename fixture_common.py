"""Run bounded, independent MTP contracts against an unchanged NUnit specimen.

Evidence is append-only by run directory. Expected assertion failures are checked
as outcomes; missing results, process termination and token delivery are distinct.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def execute(command, directory, cwd, environment, timeout=45):
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    with (directory / "stdout.txt").open("w", encoding="utf-8") as stdout, \
            (directory / "stderr.txt").open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=stdout,
                                 stderr=stderr, start_new_session=os.name != "nt")
        timed_out = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdout=stderr, stderr=stderr, check=False)
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    result = {"command": command, "cwd": str(cwd), "pid": child.pid,
              "exit": child.returncode, "timedOut": timed_out,
              "seconds": round(time.monotonic() - started, 3)}
    save(directory / "process.json", result)
    return result


def discovered(directory):
    value = json.loads((directory / "stdout.txt").read_text(encoding="utf-8-sig"))
    if "testContainers" in value:
        assert value["version"] == "1.0" and len(value["testContainers"]) == 1
        value = value["testContainers"][0]
    else:
        assert value["schemaVersion"] == 1
    return sorted([(test["uid"], test["displayName"]) for test in value["tests"]])


def results(directory):
    files = list(directory.rglob("*.trx"))
    assert len(files) == 1, "Expected exactly one TRX report, found " + str(len(files))
    return [item for item in ET.parse(files[0]).iter()
            if item.tag.split("}")[-1] == "UnitTestResult"]


def observations(directory, phase):
    return [ET.parse(path).getroot() for path in
            (directory / "observations").glob(phase + "-*.xml")]


def settings(directory, negative=False):
    document = ET.Element("RunSettings")
    nunit = ET.SubElement(document, "NUnit")
    ET.SubElement(nunit, "NumberOfTestWorkers").text = "0"
    parameters = ET.SubElement(document, "TestRunParameters")
    values = {"evidence": str(directory / "observations"),
              "text": "alpha; beta = café & λ", "integer": "41" if negative else "42",
              "boolean": "true"}
    for key, value in values.items():
        ET.SubElement(parameters, "Parameter", {"name": key, "value": value})
    return ET.tostring(document, encoding="unicode")


