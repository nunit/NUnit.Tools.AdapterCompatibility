# NUnit.Tools.AdapterCompatibility

Checks NUnit framework behavior through the NUnit adapter's direct Microsoft
Testing Platform (MTP) executable and VSTest CLI/TranslationLayer paths.

The supplied workflow targets GitHub-hosted Windows, Linux and macOS workers.
The same command-line entry point runs locally without those workflows.

## Run locally

Use Python 3.9+ and the exact SDK named by the selected profile, with the matching
architecture's .NET 8, 10 and 11 runtimes for the initial profile. Nothing uses a
desktop, device, processor, SSH connection or remote test backend.

```sh
python -m unittest discover -s tests -v
python run.py run --profile profiles/historical-alpha.json --output artifacts/run
```

Use `python3` on systems where `python` is not available. `--dotnet` accepts an
explicit existing executable. The output directory must be new. A short, owned
working directory is created under the system temporary directory and retained
for inspection. The runner snapshots maintained source, downloads pinned public
packages, verifies SHA-256, restores in locked mode with dedicated caches, and
builds/runs the fixtures. No SDK or runtime is installed by the local command.

Do not run the fixture projects directly to interpret their exit code as an
overall pass. The specimen includes an intentional assertion failure, an ignored
case and explicit cancellation cases. The runner checks their expected results.

## Two independent results

`results.json` and the job summary distinguish:

- **Compatibility checks:** exact-name discovery/selection, VSTest escaping,
  per-test cancellation controls, exact target runtime, and complete evidence.
  A failure makes the run fail.
- **Cooperative runner stop:** actual started/token-callback/finally/teardown
  observations. The historical profile has an unresolved stop gap. It is recorded
  as **INCOMPLETE**, not converted into a passing product check.

The default command gates compatibility and reports stop observations. To require
complete cooperative stop as well:

```sh
python run.py run --profile profiles/historical-alpha.json --output artifacts/strict --require-cooperative-stop
```

Exit codes are 0 for the selected requirements being met, 1 for unmet product
contracts, and 2 for input/infrastructure/audit failure. A zero exit under the
default reporting policy does not claim complete runner cancellation. Missing or
duplicate observations, an absent started test, and an invalid/missing report
cannot be treated as the known gap.

## GitHub Actions

The workflow runs on `windows-2025`, `ubuntu-24.04` and `macos-15`. Each worker
installs the profile's exact SDK and .NET 8/10 SDKs to supply those target runtimes.
Actual architecture and runtime patches are recorded; labels do not imply identical
hardware or runtime patch levels. The initial profile selects SDK 11.0.100.

Pull requests and pushes run the compatibility requirements and report stop
observations. Manual dispatch can additionally require cooperative stop. All
jobs retain text/XML/JSON/TRX evidence, including failures and the native process
audit. Package caches and compiled binaries are excluded. The workflow uses
read-only repository permissions, does not persist checkout credentials, and has
no publishing/deployment step or `continue-on-error` escape.

The initial historical profile uses NUnit 4.6.1, NUnit 5.1.0-alpha.0.50 and
NUnit3TestAdapter 6.4.0-alpha.0.46. Its package contains engine core 3.22.0, so its
stop observations do not establish behavior of later engine/adapter corrections.

## Select a candidate reproducibly

Versions, download hashes, SDK, targets and dependency locks are selected by a
reviewed profile. No command resolves a moving `latest` version or rewrites the
maintained lock files during an ordinary run.

1. Copy `profiles/historical-alpha.json` to a new profile filename.
2. Set its name, candidate/adapter versions, exact official public download URLs
   and independently reviewed SHA-256 values. Set a new lock-directory name.
   Keep or deliberately change the baseline, SDK and target list.
3. Propose a new lock set without running a test:

   ```sh
   python run.py prepare-locks --profile profiles/candidate.json --output artifacts/lock-proposal
   ```

4. Review `lock-proposal.json`, package downloads and the proposed dependency
   differences. Copy `proposed-locks` into the profile's named directory under
   `profiles/`. The command does not overwrite source or adopt its proposal.
5. Run the candidate with `run.py run`. Include the profile and reviewed locks in
   a PR. Add its name to the workflow's manual-dispatch choices when appropriate.

The alpha remains available as a recorded baseline; no old expectation requires
a new candidate to reproduce a bug. Use the strict-stop policy when checking a
candidate intended to fix cancellation. If a historical package disappears from
its public feed, report the restore failure rather than substitute other bytes.

## Scope and maintenance

The initial matrix targets modern .NET 8/10/11. It does not establish IDE UI,
remote execution, engine-v4 adapter integration, x86, .NET Framework/Mono behavior,
or the framework's numeric/parallel-dispatch regression coverage. This complements
the framework suite rather than replacing it. There is no NuGet release or new
public NUnit API in this proposal.

The fixture and VSTest harness retain the validated specimen logic; orchestration
has been adapted for explicit profiles, separate result policies and CI artifacts.
NUnit's framework `.editorconfig` supplies C# formatting conventions. The C#
sources use C# 14. No historical logs or private machine configuration are part
of the source candidate. See [PROVENANCE.md](PROVENANCE.md).

Cancellation deadlines contain only spawned test processes. They do not prove
test teardown occurred. If an independent audit finds remaining process references,
the run fails and retains its work directory; it does not kill unrelated processes.
Apply normal resource coordination if using a shared machine. The hosted workflow
uses disposable GitHub workers rather than any private test machines.

MIT licensed. Prepared jointly by Neil Colvin and OpenAI Codex.
