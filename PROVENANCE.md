# Source provenance

The starting specimen, scripts and dependency locks are in the previously posted
[validation bundle](https://github.com/user-attachments/files/34986913/NUnit51AdapterValidationRepro2026-11-17.zip).
Its SHA-256 is `50dbf739298370b334b2b22eb0b071036de3186b9d3e0a1c263517aa032856a9`.

`Shared/CompatibilityCases.cs` and `Harness/Program.cs` retain the validated C# logic, with NUnit's copyright header, block namespaces,
private-field naming and line endings applied. The framework-slot directories are
now called `baseline` and `candidate`. Their package versions are explicit profile
properties. The helper scripts use the selected target matrix/SDK, retain original
raw failures, and do not retry tests automatically.

The new runner replaces bundle-specific immutable manifests with a per-run source
snapshot, selected-profile receipt, package hashes and locked dependency restore.
Profiles and dependency locks are maintained source; run evidence is an artifact.
New locks are proposed separately and require review before adoption.

The original public bundle and its Windows/macOS/Linux evidence are unchanged.
This prepared repository is not itself evidence that hosted Actions have executed.

Conventions and workflow references checked during preparation:

- [NUnit coding configuration](https://github.com/nunit/nunit/blob/main/.editorconfig)
- [Existing internal-tool CI](https://github.com/nunit/NUnit.Tools.TestExplorerDump/blob/main/.github/workflows/TestExplorerDump.CI.yml)
- [GitHub setup-dotnet action](https://github.com/actions/setup-dotnet)
- [GitHub-hosted runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
