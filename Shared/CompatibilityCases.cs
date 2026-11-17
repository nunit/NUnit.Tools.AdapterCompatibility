// Copyright (c) Charlie Poole, Rob Prouse and Contributors. MIT License - see LICENSE.txt

using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;
using System.Xml.Linq;

using NUnit.Framework;

namespace NUnit.AdapterCompatibility
{
    /// <summary>
    /// Small shared contract suite for comparing adapter execution paths without device access.
    /// </summary>
    [TestFixture]
    [NonParallelizable]
    public sealed class CompatibilityCases
    {
        /// <summary>
        /// Records the actual test host, assembly identity and context for each executed case.
        /// </summary>
        [SetUp]
        public void RecordExecutionContext()
        {
            WriteReceipt("started");
        }

        /// <summary>
        /// Records cleanup independently of the runner's final result delivery.
        /// </summary>
        [TearDown]
        public void RecordCleanup()
        {
            WriteReceipt("teardown");
        }

        /// <summary>
        /// Establishes a passing case with recognizable output on each NUnit output channel.
        /// </summary>
        [Test]
        public void OutputAndPass()
        {
            TestContext.Out.WriteLine("compatibility-standard-output");
            TestContext.Error.WriteLine("compatibility-standard-error");
            TestContext.Progress.WriteLine("compatibility-progress-output");
            Assert.That(2 + 2, Is.EqualTo(4));
        }

        /// <summary>
        /// Checks parameterized discovery identities containing spaces, quotes and Unicode.
        /// </summary>
        /// <param name="value">The exact value retained through discovery and execution.</param>
        [TestCase("alpha beta")]
        [TestCase("a \"quoted\" value")]
        [TestCase("cafÃ© Î»")]
        public void ParameterizedIdentity(string value)
        {
            Assert.That(TestContext.CurrentContext.Test.Arguments, Is.EqualTo(new object[] { value }));
        }

        /// <summary>
        /// Exercises a display name that differs from the CLR method name.
        /// </summary>
        [TestCase(TestName = "Named case (with space)")]
        public void CustomIdentity()
        {
            Assert.That(TestContext.CurrentContext.Test.Name, Is.EqualTo("Named case (with space)"));
        }

        /// <summary>
        /// Confirms string test-run parameters and NUnit's explicit conversion API.
        /// This does not demonstrate preservation of arbitrary typed package settings.
        /// </summary>
        [Test]
        public void RunParameters()
        {
            Assert.Multiple(() =>
            {
                Assert.That(TestContext.Parameters["text"], Is.EqualTo("alpha; beta = cafÃ© & Î»"));
                Assert.That(TestContext.Parameters["integer"], Is.EqualTo("42"));
                Assert.That(TestContext.Parameters["boolean"], Is.EqualTo("true"));
                Assert.That(TestContext.Parameters.Get("integer", 0), Is.EqualTo(42));
                Assert.That(TestContext.Parameters.Get("boolean", false), Is.True);
            });
        }

        /// <summary>
        /// Supplies a recognizable failure to check failure delivery and stack traces.
        /// </summary>
        [Test]
        [Category("ExpectedFailure")]
        public void IntentionalFailure()
        {
            Assert.Fail("compatibility-intentional-failure");
        }

        /// <summary>
        /// Supplies a recognizable ignored result without executing a test body.
        /// </summary>
        [Test]
        [Ignore("compatibility-intentional-ignore")]
        public void IntentionalIgnore()
        {
            Assert.Fail("An ignored test must not execute.");
        }

        /// <summary>
        /// Waits for a real runner cancellation, recording token delivery and finally execution.
        /// The explicit attribute prevents accidental waits in ordinary test runs.
        /// </summary>
        /// <returns>A task completed or cancelled by the NUnit execution token.</returns>
        [Test]
        [Explicit("Selected only by the bounded cancellation probe.")]
        public async Task CooperativeCancellation()
        {
            await ObserveCancellation();
        }

        /// <summary>
        /// Positive control demonstrating framework token delivery from its per-test timer.
        /// This must not be confused with cancellation requested by the test runner.
        /// </summary>
        /// <returns>A task cancelled by NUnit's per-test timer.</returns>
        [Test]
        [Explicit("Selected only as the per-test cancellation control.")]
        [CancelAfter(250)]
        public async Task PerTestCancellation()
        {
            await ObserveCancellation();
        }

        private static async Task ObserveCancellation()
        {
            var token = TestContext.CurrentContext.CancellationToken;
            using (token.Register(() => WriteReceipt("token-cancelled")))
            {
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(60), token);
                    Assert.Fail("The runner failed to cancel the explicitly selected test.");
                }
                finally
                {
                    WriteReceipt("finally");
                }
            }
        }

        private static void WriteReceipt(string phase)
        {
            var directory = TestContext.Parameters["evidence"];
            if (string.IsNullOrEmpty(directory))
                throw new InvalidOperationException("The harness must provide an owned evidence directory.");

            Directory.CreateDirectory(directory);
            var context = TestContext.CurrentContext;
            var assembly = typeof(CompatibilityCases).Assembly;
            var document = new XElement("observation",
                new XAttribute("phase", phase),
                new XAttribute("test", context.Test.FullName),
                new XAttribute("id", context.Test.ID),
                new XAttribute("pid", Process.GetCurrentProcess().Id),
                new XAttribute("runtime", Environment.Version),
                new XAttribute("assembly", assembly.Location),
                new XAttribute("framework", typeof(TestAttribute).Assembly.GetName().Version!),
                new XElement("assemblies", AppDomain.CurrentDomain.GetAssemblies()
                    .Where(item => item.GetName().Name!.StartsWith("nunit", StringComparison.OrdinalIgnoreCase))
                    .Select(item => new XElement("assembly", item.FullName))));
    #if !NETFRAMEWORK
            document.Add(new XAttribute("load-context",
                System.Runtime.Loader.AssemblyLoadContext.GetLoadContext(assembly)?.Name ?? "<unnamed>"));
            document.Add(new XAttribute("entry-assembly", Assembly.GetEntryAssembly()?.Location ?? "<none>"));
    #endif
            document.Save(Path.Combine(directory, phase + "-" + Guid.NewGuid().ToString("N") + ".xml"));
        }
    }
}
