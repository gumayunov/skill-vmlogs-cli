import json
import os
import shlex
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from vmlogs_cli.errors import CliError
from vmlogs_cli.transport import KubectlTransport

SERVICE_PATH = "/api/v1/namespaces/monitoring/services/vl:9428/proxy/select/logsql"


class FakeRun:
    """Вместо subprocess.run: отдаёт заготовленные (код, stdout, stderr) по очереди."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.kwargs = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.kwargs.append(kwargs)
        code, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(cmd, code, out.encode(), err.encode())


class KubectlCommandTest(unittest.TestCase):
    def test_local_command(self):
        transport = KubectlTransport("monitoring/vl:9428", kubeconfig="/k/config", context="home")
        self.assertEqual(
            transport.command("query", {"query": "level:error | stats count()", "limit": "5"}),
            [
                "kubectl",
                "--kubeconfig",
                "/k/config",
                "--context",
                "home",
                "get",
                "--raw",
                f"{SERVICE_PATH}/query?query=level%3Aerror%20%7C%20stats%20count%28%29&limit=5",
            ],
        )

    def test_ssh_command_is_one_quoted_remote_string(self):
        transport = KubectlTransport("monitoring/vl:9428", kubeconfig="~/k", ssh_host="box")
        cmd = transport.command("query", {"query": "a", "limit": "5"})
        self.assertEqual(cmd[:4], ["ssh", "-o", "BatchMode=yes", "box"])
        self.assertIn("--kubeconfig ~/k ", cmd[4])  # тильду раскроет удалённая оболочка
        self.assertEqual(
            shlex.split(cmd[4]),
            [
                "kubectl",
                "--kubeconfig",
                "~/k",
                "get",
                "--raw",
                f"{SERVICE_PATH}/query?query=a&limit=5",
            ],
        )

    def test_describe(self):
        self.assertEqual(
            KubectlTransport("monitoring/vl:9428", ssh_host="box").describe(),
            "kubectl ssh box: monitoring/vl:9428",
        )


class KubectlFetchTest(unittest.TestCase):
    def test_lines(self):
        run = FakeRun((0, '{"a":1}\n\n{"a":2}\n', ""))
        transport = KubectlTransport("monitoring/vl:9428", run=run)
        self.assertEqual(list(transport.fetch("query", {"query": "*"})), ['{"a":1}', '{"a":2}'])

    def test_stdin_is_not_inherited(self):
        # ssh иначе съест stdin цикла `… | while read ref; do vmlogs-cli get "$ref"; done`.
        run = FakeRun((0, "", ""))
        KubectlTransport("monitoring/vl:9428", run=run).fetch("query", {"query": "*"})
        self.assertIs(run.kwargs[0]["stdin"], subprocess.DEVNULL)

    def test_bad_request_message_from_verbose_rerun(self):
        verbose = (
            'I1026 get.go:174] "Response Body" body=<\n'
            "\tcannot parse ...\n"
            '        "message": "cannot parse `query` arg [x | bad(]: unexpected pipe name '
            '\\"bad\\"; context: [x | bad]"\n'
        )
        run = FakeRun(
            (1, "", "Error from server (BadRequest): the server rejected our request\n"),
            (1, "", verbose),
        )
        transport = KubectlTransport("monitoring/vl:9428", run=run)
        with self.assertRaisesRegex(
            CliError, r'VictoriaLogs не принял запрос: cannot parse .*unexpected pipe name "bad"'
        ):
            transport.fetch("query", {"query": "x | bad("})
        self.assertEqual(run.calls[1][:2], ["kubectl", "-v=8"])

    def test_kubectl_error(self):
        run = FakeRun((1, "", 'error: context "x" does not exist\n'))
        transport = KubectlTransport("monitoring/vl:9428", context="x", run=run)
        with self.assertRaisesRegex(CliError, 'kubectl: error: context "x" does not exist'):
            transport.fetch("query", {"query": "*"})

    def test_ssh_failure(self):
        run = FakeRun((255, "", "ssh: connect to host box port 22: Connection refused\n"))
        transport = KubectlTransport("monitoring/vl:9428", ssh_host="box", run=run)
        with self.assertRaisesRegex(CliError, r"ssh box: ssh: connect.*\n.*ssh box true"):
            transport.fetch("query", {"query": "*"})

    def test_missing_binary(self):
        def run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        transport = KubectlTransport("monitoring/vl:9428", run=run)
        with self.assertRaisesRegex(CliError, "kubectl не найден"):
            transport.fetch("query", {"query": "*"})


KUBECTL_STUB = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print('{"c":"1"}')
"""

# Настоящий ssh склеивает аргументы и отдаёт удалённой оболочке; заглушка делает то же через sh -c.
SSH_STUB = """#!/usr/bin/env python3
import subprocess, sys
args = sys.argv[1:]
while args[0] == "-o":
    args = args[2:]
sys.exit(subprocess.run(["sh", "-c", " ".join(args[1:])]).returncode)
"""


class KubectlOverSshStubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bin_dir = Path(self.tmp.name)
        self.log = bin_dir / "kubectl.log"
        for name, text in (("kubectl", KUBECTL_STUB), ("ssh", SSH_STUB)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_with_stubs(self, cmd, **kwargs):
        return subprocess.run(cmd, env=self.env, **kwargs)

    def test_query_and_limit_survive_remote_shell(self):
        transport = KubectlTransport(
            "monitoring/vl:9428",
            kubeconfig="/etc/my kube/config",
            ssh_host="box",
            run=self.run_with_stubs,
        )
        query = 'kubernetes.pod_namespace:=shop "it\'s broken" $HOME `id` | stats count()'
        self.assertEqual(
            list(transport.fetch("query", {"query": query, "limit": "5"})), ['{"c":"1"}']
        )
        argv = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(argv[:2], ["--kubeconfig", "/etc/my kube/config"])
        path = argv[argv.index("--raw") + 1]
        sent = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        self.assertEqual((sent["query"], sent["limit"]), ([query], ["5"]))


if __name__ == "__main__":
    unittest.main()
