import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ecotokens.mcp_zerobloat import (
    GATEWAY_SERVER_NAME,
    META_TOOLS_MANIFEST,
    ZEROBLOAT_OVERHEAD_TOKENS,
    ZeroBloatMcpGateway,
    audit_mcp_clients,
    compress_clients_to_zerobloat,
    rollback_zerobloat_clients,
)
from ecotokens.studio_ui import _built_in_demo_server_script, start_studio_server


class ZeroBloatMcpGatewayTests(unittest.TestCase):
    def test_real_subprocess_schema_compression_search_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            demo_py = _built_in_demo_server_script(td)
            registry = {
                "demo-heavy-mcp": {"command": sys.executable, "args": [str(demo_py)]},
            }
            # Include real sibling SI-ReArk-MCP server when present on disk for true dual-server verification
            reark_py = Path(r"C:\YOUTUBE\TOOLS\si-reark-mcp\si_reark_mcp.py")
            if reark_py.exists():
                registry["si-reark-mcp"] = {"command": sys.executable, "args": [str(reark_py), "mcp"]}

            gw = ZeroBloatMcpGateway(registry=registry, state_dir=td, max_output_chars=1200)
            try:
                # 1. Verify gateway exposes ONLY 3 meta-tools via JSON-RPC
                init = gw.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                self.assertEqual(init["result"]["serverInfo"]["name"], GATEWAY_SERVER_NAME)
                tools_resp = gw.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                self.assertEqual(len(tools_resp["result"]["tools"]), len(META_TOOLS_MANIFEST))

                # 2. Before searching/probing, upstream sessions are completely lazy (not spawned)
                self.assertFalse(gw.sessions["demo-heavy-mcp"].is_alive())

                # 3. Probe & prove genuine massive token reduction vs raw tool schemas
                probe = gw.refresh_all_indexes()
                self.assertGreaterEqual(probe["total_tools"], 24)
                self.assertGreater(probe["raw_upstream_schema_tokens"], ZEROBLOAT_OVERHEAD_TOKENS * 5)
                self.assertGreater(probe["reduction_pct"], 80.0)

                # 4. JIT Search retrieves compact match without sending all schemas
                srch = gw.search_tools("postgres query sql", limit=3)
                self.assertTrue(srch["ok"])
                self.assertGreaterEqual(srch["returned"], 1)
                self.assertEqual(srch["tools"][0]["server"], "demo-heavy-mcp")

                # 5. Normal upstream call passes through cleanly
                small_res = gw.call_upstream_tool("demo-heavy-mcp", "postgres_query_sql", {"target": "SELECT 1"})
                self.assertTrue(small_res["ok"])
                self.assertFalse(small_res["compressed"])
                self.assertIn("SELECT 1", small_res["output"])

                # 6. Giant >1.2KB log payload triggers Output Bloat Shield & writes full dump
                big_res = gw.call_upstream_tool("demo-heavy-mcp", "github_get_workflow_logs", {"target": "ci-main"})
                self.assertTrue(big_res["ok"])
                self.assertTrue(big_res["compressed"])
                self.assertGreater(big_res["raw_chars"], 10000)
                self.assertTrue(Path(big_res["overflow_file"]).exists())
                self.assertIn("ECOTOKENS OUTPUT BLOAT SHIELD", big_res["output"])
                self.assertGreater(gw.get_stats()["output_tokens_saved"], 1000)
            finally:
                gw.close_all()

    def test_recursive_loop_breaker_stops_third_identical_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            demo_py = _built_in_demo_server_script(td)
            gw = ZeroBloatMcpGateway(
                registry={"demo": {"command": sys.executable, "args": [str(demo_py)]}},
                state_dir=td,
                loop_threshold=3,
            )
            try:
                r1 = gw.call_upstream_tool("demo", "filesystem_read_file", {"target": "error_file"})
                r2 = gw.call_upstream_tool("demo", "filesystem_read_file", {"target": "error_file"})
                r3 = gw.call_upstream_tool("demo", "filesystem_read_file", {"target": "error_file"})
                self.assertTrue(r1["ok"])
                self.assertTrue(r2["ok"])
                self.assertFalse(r3["ok"])
                self.assertEqual(r3["blocked_by"], "ecotokens_loop_breaker")
                self.assertEqual(gw.get_stats()["loops_blocked"], 1)
            finally:
                gw.close_all()

    def test_multiclient_audit_compress_and_sha256_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            home = td / "home"
            app = td / "appdata"
            claude_desktop = app / "Claude" / "claude_desktop_config.json"
            claude_code = home / ".claude.json"
            vscode_mcp = app / "Code" / "User" / "mcp.json"
            for p in (claude_desktop, claude_code, vscode_mcp):
                p.parent.mkdir(parents=True, exist_ok=True)

            orig_cd = json.dumps({
                "mcpServers": {
                    "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
                    "postgres": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-postgres"]},
                },
                "theme": "dark",
            }, indent=2)
            orig_cc = json.dumps({
                "mcpServers": {
                    "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
                },
                "userID": "abc",
            }, indent=2)
            orig_vs = json.dumps({
                "servers": {
                    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
                }
            }, indent=2)
            claude_desktop.write_text(orig_cd, encoding="utf-8")
            claude_code.write_text(orig_cc, encoding="utf-8")
            vscode_mcp.write_text(orig_vs, encoding="utf-8")

            aud_before = audit_mcp_clients(custom_home=home, custom_appdata=app, probe_live=False)
            self.assertEqual(aud_before["upstream_servers_found"], 4)
            self.assertGreater(aud_before["schema_reduction_pct"], 80.0)

            comp = compress_clients_to_zerobloat(custom_home=home, custom_appdata=app)
            self.assertEqual(set(comp["compressed_clients"]), {"claude_desktop", "claude_code", "vscode"})
            self.assertEqual(comp["servers_consolidated"], 4)

            # Verify client configs now only carry lightweight ecotokens-zerobloat while preserving non-MCP keys
            cd_after = json.loads(claude_desktop.read_text(encoding="utf-8"))
            self.assertEqual(cd_after["theme"], "dark")
            self.assertEqual(list(cd_after["mcpServers"].keys()), [GATEWAY_SERVER_NAME])
            vs_after = json.loads(vscode_mcp.read_text(encoding="utf-8"))
            self.assertEqual(list(vs_after["servers"].keys()), [GATEWAY_SERVER_NAME])

            # Verify consolidated registry preserved every server verbatim
            reg = json.loads((home / ".ecotokens" / "zerobloat_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(set(reg["mcpServers"].keys()), {"github", "postgres", "playwright", "filesystem"})

            # Verify 1-Click Rollback restores original configs verbatim
            rb = rollback_zerobloat_clients(custom_home=home)
            self.assertTrue(rb["ok"])
            self.assertEqual(claude_desktop.read_text(encoding="utf-8"), orig_cd)
            self.assertEqual(claude_code.read_text(encoding="utf-8"), orig_cc)
            self.assertEqual(vscode_mcp.read_text(encoding="utf-8"), orig_vs)

    def test_studio_web_server_renders_and_runs_live_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            home = td / "home"
            app = td / "appdata"
            srv = start_studio_server(port=0, custom_home=home, custom_appdata=app)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/?view=audit", timeout=5) as r:
                    html_body = r.read().decode("utf-8")
                    self.assertEqual(r.status, 200)
                    self.assertIn("EcoTokens", html_body)

                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/live-bench", data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=15) as r2:
                    bench_html = r2.read().decode("utf-8")
                    self.assertEqual(r2.status, 200)
                    self.assertIn("REAL SUBPROCESS SCHEMA PROBE", bench_html)
                    self.assertIn("OUTPUT BLOAT SHIELD", bench_html)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
