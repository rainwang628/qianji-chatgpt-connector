from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qianji_chatgpt import (  # noqa: E402
    ALLOWED_TOOLS, BridgeError, ReadOnlyBridge, Settings, redact,
)

ROOT = Path(__file__).resolve().parents[1]
TEST_PAT = "mt_pat_" + "b" * 64


class ConfigTests(unittest.TestCase):
    def settings(self, url: str, pat: str = TEST_PAT) -> Settings:
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "token"
            file.write_text(pat, encoding="utf-8")
            with patch.dict(os.environ, {"QIANJI_UPSTREAM_URL": url, "QIANJI_PAT_FILE": str(file)}):
                return Settings.from_env()

    def test_loopback_and_https_allowed(self):
        for url in ("http://127.0.0.1:3000/mcp", "http://[::1]:3000/mcp", "https://example.test/mcp"):
            self.assertEqual(self.settings(url).upstream_url, url)

    def test_remote_plaintext_rejected(self):
        with self.assertRaises(BridgeError):
            self.settings("http://example.test/mcp")

    def test_credentials_query_and_fragment_in_url_rejected(self):
        for url in ("https://user:pass@example.test/mcp", "https://example.test/mcp?token=secret",
                    "https://example.test/mcp#fragment", "https://example.test/other"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                self.settings(url)

    def test_invalid_pat_rejected(self):
        for pat in ("", "wrong", "mt_pat_" + "b" * 40 + "\nembedded"):
            with self.subTest(pat=pat), self.assertRaises(BridgeError):
                self.settings("http://127.0.0.1:3000/mcp", pat)

    def test_recursive_redaction(self):
        result = redact({"content": [{"text": TEST_PAT}], "nested": {"key": "prefix" + TEST_PAT}}, TEST_PAT)
        self.assertNotIn(TEST_PAT, str(result))
        self.assertIn("[REDACTED]", str(result))

    def test_no_financial_write_tools_allowed(self):
        for name in ("create_bills", "update_bills", "delete_bills", "create_transfer",
                     "create_refund", "create_pat", "disconnect_qianji", "execute_sql"):
            self.assertNotIn(name, ALLOWED_TOOLS)

    def test_read_only_flag_from_env(self):
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value), patch.dict(os.environ, {"QIANJI_READ_ONLY": value}):
                self.assertTrue(self.settings("http://127.0.0.1:3000/mcp").read_only)
        for value in ("", "0", "false", "no"):
            with self.subTest(value=value), patch.dict(os.environ, {"QIANJI_READ_ONLY": value}):
                self.assertFalse(self.settings("http://127.0.0.1:3000/mcp").read_only)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.token_file = Path(cls.tmp.name) / "pat"
        cls.token_file.write_text(TEST_PAT, encoding="utf-8")
        cls.marker = Path(cls.tmp.name) / "write-marker"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        env = {**os.environ, "FIXTURE_PAT": TEST_PAT, "FIXTURE_PORT": str(cls.port),
               "FIXTURE_WRITE_MARKER": str(cls.marker)}
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "upstream_fixture.py")],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if cls.proc.poll() is not None:
                raise RuntimeError("Mock upstream failed to start")
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.05)
        cls.proc.terminate()
        raise RuntimeError("Mock upstream startup timeout")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.tmp.cleanup()

    def bridge(self, pat: str = TEST_PAT) -> ReadOnlyBridge:
        return ReadOnlyBridge(Settings(f"http://127.0.0.1:{self.port}/mcp", pat, 10))

    async def test_upstream_requires_bearer(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{self.port}/mcp", json={})
            self.assertEqual(response.status_code, 401)

    async def test_filtered_tool_discovery_and_annotations(self):
        tools = {t.name: t for t in await self.bridge().list_tools()}
        self.assertEqual(set(tools), {"list_books", "list_bills", "list_assets", "list_categories", "get_user_info", "connect_qianji", "refresh_cache",
                                      "get_bills", "preview_bill_edit", "apply_bill_edit",
                                      "preview_batch_categories", "apply_batch_categories"})
        self.assertTrue(tools["list_bills"].annotations.readOnlyHint)
        self.assertFalse(tools["connect_qianji"].annotations.readOnlyHint)
        self.assertFalse(tools["refresh_cache"].annotations.readOnlyHint)

    async def test_denied_calls_do_not_reach_upstream(self):
        for name in ("create_bills", "execute_sql", "create_pat", "update_bills", "update_transfer", "update_refund", "delete_bills"):
            result = await self.bridge().call_tool(name, {})
            self.assertTrue(result.isError)
        self.assertFalse(self.marker.exists())

    async def test_read_only_mode_hides_and_blocks_edit_tools(self):
        bridge = ReadOnlyBridge(Settings(f"http://127.0.0.1:{self.port}/mcp", TEST_PAT, 10, True))
        names = {tool.name for tool in await bridge.list_tools()}
        self.assertNotIn("preview_bill_edit", names)
        self.assertNotIn("apply_bill_edit", names)
        self.assertNotIn("preview_batch_categories", names)
        self.assertNotIn("apply_batch_categories", names)
        self.assertIn("list_books", names)
        result = await bridge.call_tool("apply_bill_edit", {"planId": "a" * 64})
        self.assertTrue(result.isError)
        result = await bridge.call_tool("apply_batch_categories", {"planId": "a" * 64, "reviewed": []})
        self.assertTrue(result.isError)
        self.assertFalse(self.marker.exists())

    async def test_query_preserves_structured_results(self):
        result = await self.bridge().call_tool("list_bills", {"limit": 3})
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["bills"][0]["money"], "35.00")
        self.assertIsNone(result.structuredContent["nextCursor"])

    async def test_invalid_input_is_rejected(self):
        result = await self.bridge().call_tool("list_bills", {"limit": 0})
        self.assertTrue(result.isError)
        self.assertIn("参数", result.content[0].text)

    async def test_missing_upstream_tool_is_rejected(self):
        result = await self.bridge().call_tool("get_budget", {})
        self.assertTrue(result.isError)
        self.assertIn("未提供", result.content[0].text)

    async def test_token_not_leaked_by_result_or_upstream_error(self):
        for keyword in ("leak", "fail"):
            result = await self.bridge().call_tool("list_bills", {"remarkKeyword": keyword})
            self.assertNotIn(TEST_PAT, result.model_dump_json())
            self.assertIn("[REDACTED]", result.model_dump_json())

    async def test_authentication_failure_has_safe_message(self):
        result = await self.bridge("mt_pat_" + "c" * 64).call_tool("list_books", {})
        self.assertTrue(result.isError)
        self.assertNotIn("cccccccc", result.model_dump_json())

    async def test_connection_link_is_preserved(self):
        result = await self.bridge().call_tool("connect_qianji", {})
        self.assertFalse(result.isError)
        self.assertIn("fictional_ticket", result.structuredContent["url"])

    async def test_real_stdio_mcp_round_trip(self):
        params = StdioServerParameters(
            command=sys.executable, args=[str(ROOT / "qianji_chatgpt.py")],
            env={**os.environ, "QIANJI_UPSTREAM_URL": f"http://127.0.0.1:{self.port}/mcp",
                 "QIANJI_PAT_FILE": str(self.token_file)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                self.assertIn("没有新增、删除", init.instructions)
                tools = await session.list_tools()
                self.assertNotIn("create_bills", {tool.name for tool in tools.tools})
                result = await session.call_tool("list_books", {})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["books"][0]["id"], "101")
                rejected = await session.call_tool("create_bills", {})
                self.assertTrue(rejected.isError)
                self.assertFalse(self.marker.exists())

    async def test_stdio_edit_preview_and_save_with_fictional_data(self):
        params = StdioServerParameters(
            command=sys.executable, args=[str(ROOT / "qianji_chatgpt.py")],
            env={**os.environ, "QIANJI_UPSTREAM_URL": f"http://127.0.0.1:{self.port}/mcp",
                 "QIANJI_PAT_FILE": str(self.token_file)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                preview = await session.call_tool("preview_bill_edit", {"billId": "9002", "patch": {"remark": "虚构编辑验收"}})
                self.assertFalse(preview.isError)
                self.assertFalse(preview.structuredContent["saved"])
                args = {"planId": preview.structuredContent["planId"]}
                saved = await session.call_tool("apply_bill_edit", args)
                self.assertFalse(saved.isError)
                self.assertTrue(saved.structuredContent["saved"])
                self.assertEqual(saved.structuredContent["record"]["remark"], "虚构编辑验收")
                replay = await session.call_tool("apply_bill_edit", args)
                self.assertTrue(replay.isError)
                self.assertFalse(self.marker.exists())

    async def test_stdio_batch_is_one_upstream_write_with_fictional_data(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            for name in ("qianji_chatgpt.py", "bill_edits.py", "batch_category_edits.py"):
                shutil.copy(ROOT / name, project / name)
            (project / ".private").mkdir(mode=0o700)
            params = StdioServerParameters(
                command=sys.executable, args=[str(project / "qianji_chatgpt.py")],
                env={**os.environ, "QIANJI_UPSTREAM_URL": f"http://127.0.0.1:{self.port}/mcp",
                     "QIANJI_PAT_FILE": str(self.token_file)},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    preview = await session.call_tool("preview_batch_categories", {"updates": [
                        {"billId": "9003", "categoryId": "201"},
                        {"billId": "9004", "categoryId": "201"}]})
                    self.assertFalse(preview.isError, preview.model_dump_json())
                    self.assertFalse(preview.structuredContent["saved"])
                    args = {key: preview.structuredContent[key] for key in ("planId", "reviewed")}
                    saved = await session.call_tool("apply_batch_categories", args)
                    self.assertFalse(saved.isError, saved.model_dump_json())
                    self.assertTrue(saved.structuredContent["saved"])
                    self.assertEqual(Path(str(self.marker) + "-batch").read_text(), "2")
                    self.assertEqual(len(saved.structuredContent["records"]), 2)
                    self.assertTrue((await session.call_tool("apply_batch_categories", args)).isError)

    async def test_stdio_batch_account_move_uses_one_approved_write(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            for name in ("qianji_chatgpt.py", "bill_edits.py", "batch_category_edits.py"):
                shutil.copy(ROOT / name, project / name)
            (project / ".private").mkdir(mode=0o700)
            params = StdioServerParameters(command=sys.executable, args=[str(project / "qianji_chatgpt.py")],
                env={**os.environ, "QIANJI_UPSTREAM_URL": f"http://127.0.0.1:{self.port}/mcp",
                     "QIANJI_PAT_FILE": str(self.token_file)})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    preview = await session.call_tool("preview_batch_categories", {"updates": [
                        {"billId": "9005", "categoryId": "201", "assetId": "701"},
                        {"billId": "9006", "categoryId": "201", "assetId": "701"}]})
                    self.assertFalse(preview.isError, preview.model_dump_json())
                    self.assertEqual(preview.structuredContent["balancePreview"][1]["after"], "31989.00")
                    args = {key: preview.structuredContent[key] for key in ("planId", "reviewed", "balancePreview")}
                    saved = await session.call_tool("apply_batch_categories", args)
                    self.assertFalse(saved.isError, saved.model_dump_json())
                    self.assertTrue(saved.structuredContent["saved"])
                    self.assertEqual(Path(str(self.marker) + "-batch").read_text(), "2")
                    self.assertTrue((await session.call_tool("apply_batch_categories", args)).isError)


if __name__ == "__main__":
    unittest.main()
