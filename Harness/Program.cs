// Copyright (c) Charlie Poole, Rob Prouse and Contributors. MIT License - see LICENSE.txt

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Xml.Linq;

using Microsoft.TestPlatform.VsTestConsole.TranslationLayer;
using Microsoft.VisualStudio.TestPlatform.ObjectModel;
using Microsoft.VisualStudio.TestPlatform.ObjectModel.Client;
using Microsoft.VisualStudio.TestPlatform.ObjectModel.Logging;

namespace NUnit.AdapterCompatibility.Harness
{
    /// <summary>
    /// Drives the public VSTest protocol and MTP executable without modifying either adapter.
    /// </summary>
    internal static class Program
    {
        private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };
        private static bool _negativeParameter;

        private static async Task<int> Main(string[] args)
        {
            if (args.Length is < 4 or > 5 || (args.Length == 5 && args[4] != "--baseline-only" && args[4] != "--negative-parameter"))
            {
                Console.Error.WriteLine("Usage: Harness <vstest|mtp> <assembly> <vstest-console.dll|dotnet> <new-evidence-directory> [--baseline-only|--negative-parameter]");
                return 2;
            }

            var evidence = Path.GetFullPath(args[3]);
            if (Directory.Exists(evidence))
                throw new IOException("Use a fresh evidence directory; previous results must remain intact.");

            Directory.CreateDirectory(evidence);
            _negativeParameter = args.Length == 5 && args[4] == "--negative-parameter";
            try
            {
                object receipt = args[0] switch
                {
                    "vstest" => await RunVSTest(Path.GetFullPath(args[1]), args[2], evidence, args.Length == 5),
                    "mtp" => await RunMtp(Path.GetFullPath(args[1]), args[2], evidence, args.Length == 5),
                    _ => throw new ArgumentException("Unknown execution path.")
                };
                var json = JsonSerializer.Serialize(receipt, JsonOptions);
                File.WriteAllText(Path.Combine(evidence, "receipt.json"), json);
                Console.WriteLine("Compatibility checks completed: " + evidence);
                using var document = JsonDocument.Parse(json);
                if (document.RootElement.TryGetProperty("ContractGaps", out var gaps) && gaps.GetArrayLength() != 0)
                    return 1;
                return 0;
            }
            catch (Exception exception)
            {
                File.WriteAllText(Path.Combine(evidence, "failure.txt"), exception.ToString());
                Console.Error.WriteLine(exception);
                return 1;
            }
        }

        private static async Task<object> RunVSTest(string assembly, string consolePath, string evidence, bool baselineOnly)
        {
            var wrapper = new VsTestConsoleWrapper(consolePath,
                new ConsoleParameters { LogFilePath = Path.Combine(evidence, "vstest.log") });
            wrapper.StartSession();
            try
            {
                wrapper.InitializeExtensions(new[] { Path.Combine(Path.GetDirectoryName(assembly)!, "NUnit3.TestAdapter.dll") });
                var discoveries = new List<TestCase[]>();
                for (int index = 0; index < 2; index++)
                {
                    var handler = new DiscoveryEvents();
                    wrapper.DiscoverTests(new[] { assembly }, Settings(Path.Combine(evidence, "discovery")), handler);
                    Require(!handler.Aborted && handler.Tests.Count == 10, "Expected all ten discovered cases.");
                    discoveries.Add(handler.Tests.ToArray());
                }

                var tests = discoveries[0];
                Require(tests.OrderBy(item => item.Id).Select(item => (item.Id, item.FullyQualifiedName, item.DisplayName))
                    .SequenceEqual(discoveries[1].OrderBy(item => item.Id).Select(item => (item.Id, item.FullyQualifiedName, item.DisplayName))),
                    "Discovery identities changed between requests in the same session.");
                Save(Path.Combine(evidence, "discovery.json"), tests.Select(Identity));

                var runDirectory = Path.Combine(evidence, "all");
                var ordinary = tests.Where(item => !item.FullyQualifiedName.EndsWith("Cancellation", StringComparison.Ordinal)).ToArray();
                var all = new RunEvents();
                wrapper.RunTests(ordinary, Settings(runDirectory), all);
                SaveRun(Path.Combine(evidence, "all-results.json"), all);
                Require(all.Results.Count == 8 && all.Results.Count(item => item.Outcome == TestOutcome.Passed) == 6
                    && all.Results.Count(item => item.Outcome == TestOutcome.Failed) == 1
                    && all.Results.Count(item => item.Outcome == TestOutcome.Skipped) == 1,
                    "Unexpected ordinary pass/fail/ignore results.");
                Require(all.Results.All(item => ordinary.Any(test => test.Id == item.TestCase.Id)), "Result identity did not match discovery.");
                Require(all.Results.Single(item => item.Outcome == TestOutcome.Failed).ErrorMessage?.Contains("compatibility-intentional-failure", StringComparison.Ordinal) == true,
                    "Expected failure message was lost.");
                Require(all.Results.Single(item => item.Outcome == TestOutcome.Failed).ErrorStackTrace?.Contains("IntentionalFailure", StringComparison.Ordinal) == true,
                    "Expected failure stack trace was lost.");
                Require(all.Results.SelectMany(item => item.Messages).Any(item => item.Text?.Contains("compatibility-standard-output", StringComparison.Ordinal) == true),
                    "Expected test output was lost.");
                ValidateObservations(runDirectory, assembly, expectEntryAssembly: false);

                var selections = tests.Where(item => item.FullyQualifiedName.Contains("ParameterizedIdentity", StringComparison.Ordinal)
                    || item.DisplayName == "Named case (with space)").ToArray();
                Require(selections.Length == 4, "Expected four parameterized/custom identity probes.");
                for (int index = 0; index < selections.Length; index++)
                {
                    var selected = new RunEvents();
                    wrapper.RunTests(new[] { selections[index] }, Settings(Path.Combine(evidence, "selected-" + index)), selected);
                    SaveRun(Path.Combine(evidence, "selected-" + index + ".json"), selected);
                    Require(selected.Results.Count == 1 && selected.Results.Single().TestCase.Id == selections[index].Id
                        && selected.Results.Single().Outcome == TestOutcome.Passed, "Exact selected identity was not preserved.");
                }

                var timerDirectory = Path.Combine(evidence, "per-test-timer");
                var timer = new RunEvents();
                wrapper.RunTests(tests.Where(item => item.FullyQualifiedName.EndsWith(".PerTestCancellation", StringComparison.Ordinal)),
                    Settings(timerDirectory), timer);
                SaveRun(Path.Combine(evidence, "per-test-timer.json"), timer);
                Require(timer.Results.Count == 1 && timer.Results.Single().Outcome == TestOutcome.Failed
                    && HasObservation(timerDirectory, "token-cancelled") && HasObservation(timerDirectory, "finally"),
                    "The framework's per-test cancellation positive control did not execute as expected.");

                var baseline = new
                {
                    Path = "VSTest protocol",
                    Assembly = assembly,
                    Host = consolePath,
                    Discovered = tests.Length,
                    OrdinaryPassed = 6,
                    ExpectedFailed = 1,
                    ExpectedSkipped = 1,
                    ExactSelections = selections.Length,
                    PerTestTimerTokenAndCleanup = true,
                    ContractGaps = Array.Empty<string>(),
                    CancellationExecuted = false,
                    TypedSettingsScope = "String run parameters and framework conversions only; arbitrary typed settings are not proved."
                };
                Save(Path.Combine(evidence, "baseline.json"), baseline);
                if (baselineOnly)
                    return baseline;

                var cancellationDirectory = Path.Combine(evidence, "cancellation");
                var cancellation = new RunEvents();
                var runTask = Task.Run(() => wrapper.RunTests(
                    tests.Where(item => item.FullyQualifiedName.EndsWith(".CooperativeCancellation", StringComparison.Ordinal)),
                    Settings(cancellationDirectory), cancellation));
                await WaitForObservation(cancellationDirectory, "started", runTask);
                wrapper.CancelTestRun();
                bool timedOut = false;
                try
                {
                    await runTask.WaitAsync(TimeSpan.FromSeconds(20));
                }
                catch (TimeoutException)
                {
                    timedOut = true;
                    throw;
                }
                finally
                {
                    SaveRun(Path.Combine(evidence, "cancellation-results.json"), cancellation);
                    Save(Path.Combine(evidence, "cancellation-probe.json"), new
                    {
                        TimedOut = timedOut,
                        cancellation.Completed,
                        cancellation.Cancelled,
                        cancellation.Aborted,
                        TokenDelivered = HasObservation(cancellationDirectory, "token-cancelled"),
                        FinallyExecuted = HasObservation(cancellationDirectory, "finally")
                    });
                }
                var tokenDelivered = HasObservation(cancellationDirectory, "token-cancelled");
                var finallyExecuted = HasObservation(cancellationDirectory, "finally");
                Require(cancellation.Completed && (cancellation.Cancelled || cancellation.Aborted), "VSTest did not acknowledge cancellation.");
                Require(tokenDelivered && finallyExecuted, "The runner stopped without cooperative token delivery and finally cleanup.");

                return new
                {
                    Path = "VSTest protocol",
                    Assembly = assembly,
                    Host = consolePath,
                    Discovered = tests.Length,
                    OrdinaryPassed = 6,
                    ExpectedFailed = 1,
                    ExpectedSkipped = 1,
                    ExactSelections = selections.Length,
                    CancellationAcknowledged = true,
                    CancellationTokenDelivered = tokenDelivered,
                    CancellationFinallyExecuted = finallyExecuted,
                    CancellationResultCount = cancellation.Results.Count,
                    TypedSettingsScope = "String run parameters and framework conversions only; arbitrary typed settings are not proved."
                };
            }
            finally
            {
                wrapper.EndSession();
            }
        }

        private static async Task<object> RunMtp(string assembly, string dotnet, string evidence, bool baselineOnly)
        {
            var discovery = await InvokeMtp(dotnet, assembly, evidence, "discovery", "--list-tests", "json");
            Require(discovery.ExitCode == 0, "MTP discovery failed.");
            var repeat = await InvokeMtp(dotnet, assembly, evidence, "discovery-repeat", "--list-tests", "json");
            var identities = DiscoveryIdentities(discovery.Output);
            Require(identities.Length == 10 && identities.OrderBy(item => item.Uid, StringComparer.Ordinal)
                .SequenceEqual(DiscoveryIdentities(repeat.Output).OrderBy(item => item.Uid, StringComparer.Ordinal)), "MTP discovery identities changed or cases were lost.");
            Require(identities.Any(item => item.DisplayName == "ParameterizedIdentity(\"cafÃ© Î»\")"), "MTP discovery lost the Unicode identity.");
            Save(Path.Combine(evidence, "discovery.json"), identities);

            var all = await InvokeMtp(dotnet, assembly, evidence, "all", "--report-trx", "--report-trx-filename", "results.trx");
            var results = ReadTrx(Path.Combine(evidence, "all"));
            Require(all.ExitCode != 0 && results.Length == 10 && results.Count(item => item.Attribute("outcome")?.Value == "Passed") == 6
                && results.Count(item => item.Attribute("outcome")?.Value == "Failed") == 1
                && results.Count(item => item.Attribute("outcome")?.Value == "NotExecuted") == 3,
                "Unexpected MTP pass/fail results.");
            Require(results.Any(item => item.Value.Contains("compatibility-intentional-failure", StringComparison.Ordinal)), "MTP failure message was lost.");
            Require(results.Any(item => item.Value.Contains("compatibility-standard-output", StringComparison.Ordinal)), "MTP test output was lost.");
            Require(results.Select(item => item.Attribute("testName")!.Value).OrderBy(item => item, StringComparer.Ordinal)
                .SequenceEqual(identities.Select(item => item.DisplayName).OrderBy(item => item, StringComparer.Ordinal)),
                "MTP ordinary results did not retain their discovered display identities.");
            ValidateObservations(Path.Combine(evidence, "all", "observations"), assembly, expectEntryAssembly: true);

            var filtered = await InvokeMtp(dotnet, assembly, evidence, "selected", "--filter",
                "FullyQualifiedName=NUnit.AdapterCompatibility.CompatibilityCases.OutputAndPass",
                "--report-trx", "--report-trx-filename", "results.trx");
            var selected = ReadTrx(Path.Combine(evidence, "selected"));
            Require(filtered.ExitCode == 0 && selected.Length == 1 && selected[0].Attribute("outcome")?.Value == "Passed",
                "MTP exact method filter did not select one passing test.");

            var selections = identities.Where(item => item.Uid.Contains("ParameterizedIdentity", StringComparison.Ordinal)
                || item.DisplayName == "Named case (with space)").ToArray();
            Require(selections.Length == 4, "Expected four MTP parameterized/custom identities.");
            var identitySelections = new List<object>();
            var contractGaps = new List<string>();
            for (int index = 0; index < selections.Length; index++)
            {
                var run = await InvokeMtp(dotnet, assembly, evidence, "uid-" + index, "--filter-uid", selections[index].Uid,
                    "--report-trx", "--report-trx-filename", "results.trx");
                var result = ReadTrx(Path.Combine(evidence, "uid-" + index));
                bool passed = run.ExitCode == 0 && result.Length == 1 && result[0].Attribute("outcome")?.Value == "Passed"
                    && result[0].Attribute("testName")?.Value == selections[index].DisplayName;
                identitySelections.Add(new { selections[index].Uid, selections[index].DisplayName, run.ExitCode, ResultCount = result.Length, Passed = passed });
                if (!passed)
                    contractGaps.Add("MTP selected UID: " + selections[index].Uid);
            }
            Save(Path.Combine(evidence, "uid-results.json"), identitySelections);

            var timer = await InvokeMtp(dotnet, assembly, evidence, "per-test-timer", "--filter-uid",
                identities.Single(item => item.Uid.EndsWith(".PerTestCancellation", StringComparison.Ordinal)).Uid,
                "--report-trx", "--report-trx-filename", "results.trx");
            var timerDirectory = Path.Combine(evidence, "per-test-timer", "observations");
            var timerResults = ReadTrx(Path.Combine(evidence, "per-test-timer"));
            Require(timer.ExitCode != 0 && timerResults.Length == 1 && timerResults[0].Attribute("outcome")?.Value == "Failed"
                && HasObservation(timerDirectory, "token-cancelled") && HasObservation(timerDirectory, "finally"),
                "MTP per-test cancellation positive control did not execute as expected.");

            var baseline = new
            {
                Path = "MTP executable",
                Assembly = assembly,
                Host = dotnet,
                Discovered = identities.Length,
                OrdinaryPassed = 6,
                ExpectedFailed = 1,
                ReportedResults = results.Length,
                ExactSelectionAttempts = 5,
                ExactSelectionsPassed = 5 - contractGaps.Count,
                PerTestTimerTokenAndCleanup = true,
                ContractGaps = contractGaps.ToArray(),
                CancellationExecuted = false,
                TypedSettingsScope = "String run parameters and framework conversions only; arbitrary typed settings are not proved."
            };
            Save(Path.Combine(evidence, "baseline.json"), baseline);
            if (baselineOnly)
                return baseline;

            var cancellation = await InvokeMtp(dotnet, assembly, evidence, "cancellation", "--filter",
                "FullyQualifiedName=NUnit.AdapterCompatibility.CompatibilityCases.CooperativeCancellation", "--timeout", "5s",
                "--report-trx", "--report-trx-filename", "results.trx");
            var cancellationDirectory = Path.Combine(evidence, "cancellation", "observations");
            Require(HasObservation(cancellationDirectory, "started"), "MTP cancellation case never started.");
            Require(cancellation.ExitCode != 0, "MTP did not report the bounded session timeout.");
            Require(HasObservation(cancellationDirectory, "token-cancelled") && HasObservation(cancellationDirectory, "finally"),
                "MTP session stop did not reach the test cancellation token and finally cleanup.");

            return new
            {
                Path = "MTP executable",
                Assembly = assembly,
                Host = dotnet,
                Discovered = identities.Length,
                OrdinaryPassed = 6,
                ExpectedFailed = 1,
                ReportedResults = results.Length,
                ExactSelectionAttempts = 5,
                ExactSelectionsPassed = 5 - contractGaps.Count,
                CancellationExit = cancellation.ExitCode,
                CancellationTokenDelivered = HasObservation(cancellationDirectory, "token-cancelled"),
                CancellationFinallyExecuted = HasObservation(cancellationDirectory, "finally"),
                CancellationResultFiles = Directory.GetFiles(Path.Combine(evidence, "cancellation"), "*.trx", SearchOption.AllDirectories).Length,
                ContractGaps = contractGaps.ToArray(),
                TypedSettingsScope = "String run parameters and framework conversions only; arbitrary typed settings are not proved."
            };
        }

        private static async Task<(int ExitCode, string Output)> InvokeMtp(string dotnet, string assembly, string evidence, string name, params string[] arguments)
        {
            var directory = Path.Combine(evidence, name);
            Directory.CreateDirectory(directory);
            var settingsPath = Path.Combine(directory, "test.runsettings");
            File.WriteAllText(settingsPath, Settings(Path.Combine(directory, "observations")));
            var start = new ProcessStartInfo(dotnet)
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
                CreateNoWindow = true,
                WorkingDirectory = directory
            };
            start.ArgumentList.Add(assembly);
            start.ArgumentList.Add("--settings");
            start.ArgumentList.Add(settingsPath);
            start.ArgumentList.Add("--results-directory");
            start.ArgumentList.Add(directory);
            foreach (var argument in arguments)
                start.ArgumentList.Add(argument);
            Save(Path.Combine(directory, "command.json"), new { Executable = dotnet, Arguments = start.ArgumentList.ToArray() });
            using var process = Process.Start(start) ?? throw new InvalidOperationException("MTP process did not start.");
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            try
            {
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(35));
            }
            catch (TimeoutException)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
                Save(Path.Combine(directory, "timeout.json"), new { ProcessId = process.Id, BoundSeconds = 35, OwnedTreeKilled = true });
                throw;
            }
            finally
            {
                File.WriteAllText(Path.Combine(directory, "stdout.txt"), await stdout);
                File.WriteAllText(Path.Combine(directory, "stderr.txt"), await stderr);
            }
            return (process.ExitCode, await stdout);
        }

        private static TestIdentity[] DiscoveryIdentities(string output)
        {
            using var document = JsonDocument.Parse(output);
            Require(document.RootElement.GetProperty("schemaVersion").GetInt32() == 1, "Unknown MTP discovery schema.");
            return document.RootElement.GetProperty("tests").EnumerateArray().Select(item => new TestIdentity(
                item.GetProperty("uid").GetString()!, item.GetProperty("displayName").GetString()!)).ToArray();
        }

        private static XElement[] ReadTrx(string directory)
        {
            var paths = Directory.GetFiles(directory, "*.trx", SearchOption.AllDirectories);
            Require(paths.Length == 1, "Expected exactly one MTP TRX result.");
            return XDocument.Load(paths[0]).Descendants().Where(item => item.Name.LocalName == "UnitTestResult").ToArray();
        }

        private static void ValidateObservations(string directory, string assembly, bool expectEntryAssembly)
        {
            var observations = Directory.GetFiles(directory, "started-*.xml").Select(path => XElement.Load(path)).ToArray();
            Require(observations.Length == 7, "Expected seven executed test bodies; ignored tests must not execute.");
            Require(observations.All(item => string.Equals(item.Attribute("assembly")?.Value, assembly, StringComparison.OrdinalIgnoreCase)),
                "Tests ran from an unexpected assembly location.");
            if (expectEntryAssembly)
            {
                Require(observations.All(item => string.Equals(item.Attribute("entry-assembly")?.Value, assembly, StringComparison.OrdinalIgnoreCase)),
                    "MTP did not retain the project executable as its entry assembly.");
            }
        }

        private static async Task WaitForObservation(string directory, string phase, Task runTask)
        {
            var deadline = Stopwatch.StartNew();
            while (!HasObservation(directory, phase))
            {
                Require(!runTask.IsCompleted, "The cancellation run completed before its test started.");
                Require(deadline.Elapsed < TimeSpan.FromSeconds(20), "The cancellation test did not start within its bound.");
                await Task.Delay(50);
            }
        }

        private static bool HasObservation(string directory, string phase) => Directory.Exists(directory)
            && Directory.GetFiles(directory, phase + "-*.xml").Length != 0;

        /// <summary>
        /// Creates run settings for the native harness architecture and isolated evidence.
        /// </summary>
        private static string Settings(string evidence) => new XElement("RunSettings",
            new XElement("RunConfiguration", new XElement("TargetPlatform", System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture.ToString().ToLowerInvariant())),
            new XElement("NUnit", new XElement("NumberOfTestWorkers", 0), new XElement("ShadowCopyFiles", false)),
            new XElement("TestRunParameters",
                new XElement("Parameter", new XAttribute("name", "evidence"), new XAttribute("value", evidence)),
                new XElement("Parameter", new XAttribute("name", "text"), new XAttribute("value", "alpha; beta = cafÃ© & Î»")),
                new XElement("Parameter", new XAttribute("name", "integer"), new XAttribute("value", _negativeParameter ? "41" : "42")),
                new XElement("Parameter", new XAttribute("name", "boolean"), new XAttribute("value", "true"))))
            .ToString();

        private static object Identity(TestCase test) => new { test.Id, test.FullyQualifiedName, test.DisplayName, test.Source };

        private static void SaveRun(string path, RunEvents handler) => Save(path, new
        {
            handler.Completed,
            handler.Cancelled,
            handler.Aborted,
            Error = handler.Error?.ToString(),
            handler.Logs,
            Results = handler.Results.Select(item => new
            {
                Test = Identity(item.TestCase),
                Outcome = item.Outcome.ToString(),
                item.ErrorMessage,
                item.ErrorStackTrace,
                Messages = item.Messages.Select(message => new { message.Category, message.Text })
            })
        });

        private static void Save(string path, object value) => File.WriteAllText(path, JsonSerializer.Serialize(value, JsonOptions));

        private static void Require(bool condition, string message)
        {
            if (!condition)
                throw new InvalidOperationException(message);
        }

        private sealed record TestIdentity(string Uid, string DisplayName);

        private sealed class DiscoveryEvents : ITestDiscoveryEventsHandler
        {
            internal List<TestCase> Tests { get; } = new();
            internal bool Aborted { get; private set; }

            public void HandleDiscoveredTests(IEnumerable<TestCase>? discoveredTestCases)
            {
                if (discoveredTestCases != null)
                    Tests.AddRange(discoveredTestCases);
            }

            public void HandleDiscoveryComplete(long totalTests, IEnumerable<TestCase>? lastChunk, bool isAborted)
            {
                Aborted = isAborted;
                HandleDiscoveredTests(lastChunk);
            }

            public void HandleLogMessage(TestMessageLevel level, string? message) => Console.WriteLine(level + ": " + message);
            public void HandleRawMessage(string rawMessage) { }
        }

        private sealed class RunEvents : ITestRunEventsHandler
        {
            internal ConcurrentQueue<TestResult> Results { get; } = new();
            internal ConcurrentQueue<string> Logs { get; } = new();
            internal bool Completed { get; private set; }
            internal bool Cancelled { get; private set; }
            internal bool Aborted { get; private set; }
            internal Exception? Error { get; private set; }

            public void HandleTestRunStatsChange(TestRunChangedEventArgs? args)
            {
                if (args?.NewTestResults != null)
                {
                    foreach (var result in args.NewTestResults)
                        Results.Enqueue(result);
                }
            }

            public void HandleTestRunComplete(TestRunCompleteEventArgs args, TestRunChangedEventArgs? lastChunk,
                ICollection<AttachmentSet>? runContextAttachments, ICollection<string>? executorUris)
            {
                HandleTestRunStatsChange(lastChunk);
                Cancelled = args.IsCanceled;
                Aborted = args.IsAborted;
                Error = args.Error;
                Completed = true;
            }

            public void HandleLogMessage(TestMessageLevel level, string? message) => Logs.Enqueue(level + ": " + message);
            public void HandleRawMessage(string rawMessage) { }
            public int LaunchProcessWithDebuggerAttached(TestProcessStartInfo testProcessStartInfo) => throw new NotSupportedException("No debugger is used by this fixture.");
        }
    }
}
