# 来源：D:/Programs/Python/OOAD/Assignment1/support/cases.py 的真实文件/进程验证（113–242 行）及离线检查。
# 功能：独立验证文件、路径、参数、批准/拒绝、真实输出和超时，使用临时工作区。
# 后续：增加持久化审批恢复、执行结果 UNKNOWN、并发路径替换及补偿冲突测试。
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tools.builtin import BuiltinTools
from app.tools.registry import create_builtin_registry, validate_tool_interfaces
from app.tools.workspace import Workspace


class ApprovalStub:
    def __init__(self, decision):
        self.decision, self.requests = decision, []

    def approve(self, request):
        self.requests.append(request)
        return self.decision


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harness tools ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.approval = ApprovalStub(True)
        self.registry = create_builtin_registry(self.root, self.approval)

    def execute(self, name, arguments):
        return self.registry.execute("test-call", name, arguments)

    def test_interfaces_match_and_schemas_are_copied(self):
        self.assertEqual(validate_tool_interfaces(self.registry.schemas, self.registry.functions), [])
        schemas = self.registry.schemas
        schemas.clear()
        self.assertEqual(len(self.registry.schemas), 5)

    def test_interface_validator_reports_mismatches(self):
        schemas = self.registry.schemas
        schemas[0]["function"]["parameters"]["properties"] = {}
        self.assertTrue(validate_tool_interfaces(schemas, self.registry.functions))
        self.assertTrue(validate_tool_interfaces(self.registry.schemas, {}))

    def test_approval_failure_does_not_execute(self):
        class BrokenApproval:
            def approve(self, request):
                raise RuntimeError("approval service unavailable")
        registry = create_builtin_registry(self.root, BrokenApproval())
        with patch("app.tools.builtin.subprocess.run", side_effect=AssertionError("Unapproved process started")):
            result = registry.execute("call", "execute_command", {"command": [sys.executable, "-c", "print(1)"]})
        self.assertEqual(result.status, "error")
        self.assertIn("approval service unavailable", result.data)

    def test_utf8_create_replace_and_read(self):
        result = self.execute("write_file", {"file_path": "nested notes/info.txt", "content": "中文\n"})
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["file_path"], "nested notes/info.txt")
        self.assertEqual(self.execute("read_file", {"file_path": "nested notes/info.txt"}).data, "中文\n")
        self.execute("write_file", {"file_path": "nested notes/info.txt", "content": "replacement"})
        self.assertEqual((self.root / "nested notes/info.txt").read_text(encoding="utf-8"), "replacement")

    def test_listing_contains_nested_files_and_empty_directories(self):
        (self.root / "empty").mkdir()
        (self.root / "nested notes/deeper").mkdir(parents=True)
        (self.root / "nested notes/info.txt").write_text("text", encoding="utf-8")
        result = self.execute("list_files", {"directory": "."})
        self.assertEqual(result.data, {"files": ["nested notes/info.txt"],
                                      "directories": ["empty", "nested notes", "nested notes/deeper"]})
        self.assertEqual(self.approval.requests, [])

    def test_missing_and_wrong_path_types_return_errors(self):
        for name, arguments in [("read_file", {"file_path": "missing"}),
                                ("list_files", {"directory": "missing"}),
                                ("write_file", {"file_path": ".", "content": "text"})]:
            with self.subTest(name=name):
                self.assertEqual(self.execute(name, arguments).status, "error")

    def test_parent_and_absolute_escape_are_rejected(self):
        for path in ("../outside.txt", str(self.root.parent / "outside.txt")):
            with self.subTest(path=path):
                result = self.execute("write_file", {"file_path": path, "content": "bad"})
                self.assertEqual(result.status, "error")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_links_are_rejected_for_read_and_listing(self):
        target = self.root / "target.txt"
        target.write_text("text", encoding="utf-8")
        try:
            (self.root / "link.txt").symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        self.assertEqual(self.execute("read_file", {"file_path": "link.txt"}).status, "error")
        self.assertEqual(self.execute("list_files", {"directory": "."}).status, "error")

    def test_reparse_point_detection_is_enforced_without_link_privilege(self):
        target = self.root / "reparse.txt"
        target.write_text("text", encoding="utf-8")
        with patch("app.tools.workspace.linked", side_effect=lambda path: path.name == "reparse.txt"):
            self.assertEqual(self.execute("read_file", {"file_path": "reparse.txt"}).status, "error")
            self.assertEqual(self.execute("list_files", {"directory": "."}).status, "error")

    def test_two_workspaces_do_not_share_relative_paths(self):
        with tempfile.TemporaryDirectory(prefix="harness second ") as second:
            second_root = Path(second)
            (self.root / "text.txt").write_text("first", encoding="utf-8")
            (second_root / "text.txt").write_text("second", encoding="utf-8")
            second_registry = create_builtin_registry(second_root)
            self.assertEqual(self.execute("read_file", {"file_path": "text.txt"}).data, "first")
            self.assertEqual(second_registry.execute("second", "read_file", {"file_path": "text.txt"}).data, "second")
            self.assertEqual(self.execute("read_file", {"file_path": "text.txt"}).data, "first")

    def test_default_denial_blocks_all_sensitive_tools(self):
        registry = create_builtin_registry(self.root)
        with patch("app.tools.builtin.subprocess.run", side_effect=AssertionError("Denied process started")):
            for name, arguments in [("write_file", {"file_path": "created.txt", "content": "bad"}),
                                    ("run_python", {"file_path": "missing.py"}),
                                    ("execute_command", {"command": [sys.executable, "-c", "print(1)"]})]:
                with self.subTest(name=name):
                    result = registry.execute("deny", name, arguments)
                    self.assertEqual(result.status, "denied")
                    self.assertIs(result.approved, False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_argument_validation_happens_before_approval(self):
        invalid = [("write_file", {"file_path": "x", "content": 123}),
                   ("read_file", {"file_path": "x", "extra": True}),
                   ("run_python", {"file_path": "x", "args": [1]}),
                   ("execute_command", {"command": []}),
                   ("execute_command", {"command": "echo unsafe"}),
                   ("read_file", {}), ("unknown", {})]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.execute(name, arguments).status, "error")
        self.assertEqual(self.approval.requests, [])

    def test_approval_request_identifies_exact_call(self):
        self.execute("write_file", {"file_path": "text.txt", "content": "text"})
        request = self.approval.requests[0]
        self.assertEqual(request.call_id, "test-call")
        self.assertEqual(request.tool_name, "write_file")
        self.assertEqual(Path(request.workspace), self.root.resolve())
        self.assertEqual(request.arguments, {"file_path": "text.txt", "content": "text"})

    def test_python_captures_argv_stdin_and_unicode(self):
        script = self.root / "script with spaces.py"
        script.write_text('import json, sys\nprint(json.dumps([sys.argv[1:], sys.stdin.read(), "中文"], ensure_ascii=False))\nprint("err", file=sys.stderr)\n', encoding="utf-8")
        cwd = Path.cwd()
        result = self.execute("run_python", {"file_path": script.name, "args": ["--name", "with spaces"]})
        self.assertEqual(result.status, "success")
        self.assertEqual(json.loads(result.data["stdout"]), [["--name", "with spaces"], "", "中文"])
        self.assertEqual(result.data["stderr"], "err\n")
        self.assertEqual(Path.cwd(), cwd)

    def test_command_runs_only_after_approval_and_uses_workspace(self):
        events = []
        real_run = subprocess.run
        class Approval:
            def approve(self, request):
                events.append("approval")
                return True
        registry = create_builtin_registry(self.root, Approval())
        def observed(*args, **kwargs):
            events.append("process")
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["cwd"], self.root.resolve())
            self.assertGreater(kwargs["timeout"], 0)
            return real_run(*args, **kwargs)
        with patch("app.tools.builtin.subprocess.run", side_effect=observed):
            result = registry.execute("command", "execute_command", {"command": [sys.executable, "-c", 'from pathlib import Path; Path("created.txt").write_text("ok")']})
        self.assertEqual(events, ["approval", "process"])
        self.assertEqual(result.data, {"returncode": 0, "stdout": "", "stderr": ""})
        self.assertEqual((self.root / "created.txt").read_text(), "ok")

    def test_process_environment_omits_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-value", "OTHER_ACCESS_TOKEN": "private-token"}):
            result = self.execute("execute_command", {"command": [sys.executable, "-c", 'import json, os; print(json.dumps([os.getenv("OPENAI_API_KEY"), os.getenv("OTHER_ACCESS_TOKEN")]))']})
        self.assertEqual(json.loads(result.data["stdout"]), [None, None])

    def test_real_nonzero_and_missing_executable(self):
        result = self.execute("execute_command", {"command": [sys.executable, "-c", 'import sys; print("out"); print("err", file=sys.stderr); sys.exit(7)']})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.data, {"returncode": 7, "stdout": "out\n", "stderr": "err\n"})
        result = self.execute("execute_command", {"command": [str(self.root / "missing.exe")]})
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.data["returncode"])

    def test_timeout_preserves_partial_output(self):
        registry = create_builtin_registry(self.root, self.approval, timeout=1.0)
        result = registry.execute("timeout", "execute_command", {"command": [sys.executable, "-c", 'import time; print("partial", flush=True); time.sleep(4)']})
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.data["returncode"])
        self.assertEqual(result.data["stdout"], "partial\n")
        self.assertIn("timed out", result.data["error"])

    def test_invalid_timeout_is_rejected(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                BuiltinTools(Workspace(self.root), timeout)