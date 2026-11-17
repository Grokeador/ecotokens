#!/usr/bin/env python3
"""
EcoTokens 2.0 — Universal Zero-Bloat MCP Multiplexer, Auditor & Output Shield.
100% Python Stdlib (zero external dependencies) so it works immediately on any Windows/Mac/Linux machine.

Solves the 3 biggest 2026 AI agent bottlenecks across Claude Pro/Max, Cursor, Windsurf, Antigravity & Ollama:
  1. MCP Schema Context Bloat: replaces 30,000-60,000+ upfront tool tokens with 3 compact meta-tools (~420 tokens).
  2. Recursive Tool Loops: trips on 3 identical failing/repeated calls before draining 5-hour limits or API credits.
  3. Giant Output Overflows: traps >8KB tool responses, saves full raw output locally, and returns structured previews.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MCP_PROTOCOL_VERSION = "2024-11-05"
GATEWAY_SERVER_NAME = "ecotokens-zerobloat"
DEFAULT_MAX_OUTPUT_CHARS = 8000
DEFAULT_SUBPROCESS_TIMEOUT = 25.0
DEFAULT_LOOP_THRESHOLD = 3


def default_state_dir(custom_home: Path | None = None) -> Path:
    root = (custom_home or Path.home()) / ".ecotokens"
    root.mkdir(parents=True, exist_ok=True)
    return root


def estimate_tokens(obj: Any) -> int:
    """Fast deterministic token estimate (~3.7 chars/token for dense JSON schemas & payloads)."""
    if isinstance(obj, str):
        text = obj
    else:
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if not text:
        return 0
    return max(1, round(len(text) / 3.7))


def _normalize_windows_command(command: str, args: list[str]) -> tuple[str, list[str]]:
    """Wrap raw npm/npx/uvx commands cleanly in cmd.exe /c on Windows to prevent WinError 2."""
    cmd_clean = str(command or "").strip()
    args_clean = [str(a) for a in (args or [])]
    if os.name != "nt":
        return cmd_clean, args_clean
    low = Path(cmd_clean).name.lower()
    if low in {"npx", "npx.cmd", "npm", "npm.cmd", "uvx", "uvx.cmd", "pnpx"}:
        bare = low.replace(".cmd", "")
        return "cmd.exe", ["/c", bare, *args_clean]
    if low.startswith("cmd") and len(args_clean) >= 2 and args_clean[0].lower() == "/c":
        return cmd_clean, args_clean
    return cmd_clean, args_clean


# ---------------------------------------------------------------------------
# Meta-Tools Exposed by EcoTokens Zero-Bloat Gateway (~420 tokens total!)
# ---------------------------------------------------------------------------

META_TOOLS_MANIFEST: list[dict[str, Any]] = [
    {
        "name": "ecotokens_search_tools",
        "description": "Search available tools across connected MCP servers on demand (JIT discovery).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Intent or keyword, e.g. 'read file', 'sql', 'git diff'"},
                "server": {"type": "string", "description": "Optional filter by server name"},
                "limit": {"type": "integer", "description": "Max tools returned (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "ecotokens_call_tool",
        "description": "Execute a discovered MCP tool lazily with loop-protection and output compression.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "tool_name": {"type": "string"},
                "arguments": {"type": "object"},
                "max_output_chars": {"type": "integer"},
            },
            "required": ["server", "tool_name"],
        },
    },
    {
        "name": "ecotokens_stats",
        "description": "Show live token savings, blocked loops, and managed upstream MCP servers.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

ZEROBLOAT_OVERHEAD_TOKENS = estimate_tokens(META_TOOLS_MANIFEST)


# ---------------------------------------------------------------------------
# Real STDIO Upstream MCP Subprocess Client
# ---------------------------------------------------------------------------

class UpstreamMcpSession:
    """Manages a live STDIO JSON-RPC 2.0 MCP child process lazily with thread-safe line reading."""

    def __init__(self, name: str, cfg: dict[str, Any], timeout: float = DEFAULT_SUBPROCESS_TIMEOUT) -> None:
        self.name = name
        self.cfg = cfg
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._next_id = 1
        self.tools_cache: list[dict[str, Any]] | None = None
        self.last_used: float = 0.0

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self) -> None:
        if self.is_alive():
            self.last_used = time.time()
            return
        cmd, args = _normalize_windows_command(
            str(self.cfg.get("command", "")),
            list(self.cfg.get("args") or []),
        )
        if not cmd:
            raise RuntimeError(f"Server '{self.name}' has no valid command configured.")

        env = os.environ.copy()
        for k, v in (self.cfg.get("env") or {}).items():
            env[str(k)] = str(v)

        cwd = self.cfg.get("cwd") or None
        self._msg_queue = queue.Queue()
        self._proc = subprocess.Popen(
            [cmd, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=cwd,
        )
        threading.Thread(target=self._reader_loop, daemon=True).start()
        # Official MCP Handshake
        _ = self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ecotokens-zerobloat", "version": "2.0.0"},
        })
        self._notify("notifications/initialized", {})
        self.last_used = time.time()

    def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    self._msg_queue.put(parsed)
            except json.JSONDecodeError:
                continue
        self._msg_queue.put(None)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.is_alive() or self._proc is None or self._proc.stdin is None:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.is_alive() or self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"Upstream MCP process '{self.name}' is not running.")
        req_id = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(req) + "\n")
        self._proc.stdin.flush()

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                item = self._msg_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                stderr_tail = ""
                if self._proc and self._proc.stderr:
                    try:
                        stderr_tail = self._proc.stderr.read()[:300]
                    except Exception:
                        pass
                raise RuntimeError(f"Upstream MCP '{self.name}' exited cleanly or crashed: {stderr_tail}")
            if item.get("id") == req_id:
                return item
        raise TimeoutError(f"Upstream MCP '{self.name}' timed out waiting for '{method}'.")

    def list_tools(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if self.tools_cache is not None and not force_refresh:
                return self.tools_cache
            self.ensure_started()
            resp = self._rpc("tools/list", {})
            tools = ((resp.get("result") or {}).get("tools")) or []
            self.tools_cache = tools
            self.last_used = time.time()
            return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.ensure_started()
            resp = self._rpc("tools/call", {"name": tool_name, "arguments": arguments or {}})
            self.last_used = time.time()
            return resp

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=1)
                except Exception:
                    pass
            for pipe in (self._proc.stdout, self._proc.stderr):
                if pipe is not None and not pipe.closed:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            self._proc = None


# ---------------------------------------------------------------------------
# Zero-Bloat Gateway Core (JIT Search + Lazy Spawn + Loop Breaker + Output Shield)
# ---------------------------------------------------------------------------

@dataclass
class ZeroBloatStats:
    calls_proxied: int = 0
    loops_blocked: int = 0
    outputs_compressed: int = 0
    raw_output_chars: int = 0
    delivered_output_chars: int = 0
    search_queries: int = 0

    @property
    def output_tokens_saved(self) -> int:
        delta = max(0, self.raw_output_chars - self.delivered_output_chars)
        return round(delta / 3.7)


class ZeroBloatMcpGateway:
    """
    Universal Meta-MCP Router:
      - Indexes multiple upstream STDIO MCP servers on demand
      - Exposes only 3 meta-tools (~420 schema tokens instead of 30K-60K+!)
      - Protects context windows with Output Bloat Shield & 3x Loop Breaker
    """

    def __init__(
        self,
        registry: dict[str, dict[str, Any]] | None = None,
        state_dir: Path | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        loop_threshold: int = DEFAULT_LOOP_THRESHOLD,
    ) -> None:
        self.state_dir = state_dir or default_state_dir()
        self.overflow_dir = self.state_dir / "overflow"
        self.overflow_dir.mkdir(parents=True, exist_ok=True)
        self.max_output_chars = max_output_chars
        self.loop_threshold = max(2, loop_threshold)
        self.registry: dict[str, dict[str, Any]] = registry if registry is not None else self._load_registry_file()
        self.sessions: dict[str, UpstreamMcpSession] = {
            name: UpstreamMcpSession(name, cfg) for name, cfg in self.registry.items()
        }
        self._tool_index: dict[str, list[dict[str, Any]]] = {}
        self._recent_call_hashes: list[str] = []
        self.stats = ZeroBloatStats()

    def _load_registry_file(self) -> dict[str, dict[str, Any]]:
        reg_path = self.state_dir / "zerobloat_registry.json"
        if reg_path.exists():
            try:
                data = json.loads(reg_path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers") if isinstance(data, dict) and "mcpServers" in data else data
                if isinstance(servers, dict):
                    return {k: v for k, v in servers.items() if isinstance(v, dict)}
            except Exception:
                pass
        return {}

    def _ensure_index_for_server(self, server_name: str) -> list[dict[str, Any]]:
        if server_name in self._tool_index:
            return self._tool_index[server_name]
        sess = self.sessions.get(server_name)
        if not sess:
            return []
        try:
            tools = sess.list_tools()
            self._tool_index[server_name] = tools
            return tools
        except Exception as exc:
            self._tool_index[server_name] = []
            return [{"name": "__error__", "description": str(exc), "inputSchema": {"type": "object"}}]

    def refresh_all_indexes(self) -> dict[str, Any]:
        """Probe every registered server once and compute exact raw vs Zero-Bloat schema savings."""
        per_server: list[dict[str, Any]] = []
        raw_total = 0
        tool_total = 0
        for name in list(self.sessions.keys()):
            tools = self._ensure_index_for_server(name)
            valid_tools = [t for t in tools if t.get("name") != "__error__"]
            tok = estimate_tokens(valid_tools)
            raw_total += tok
            tool_total += len(valid_tools)
            per_server.append({
                "server": name,
                "tool_count": len(valid_tools),
                "schema_tokens": tok,
                "alive_now": self.sessions[name].is_alive(),
                "error": tools[0].get("description") if len(tools) == 1 and tools[0].get("name") == "__error__" else None,
            })

        reduction_pct = round(((raw_total - ZEROBLOAT_OVERHEAD_TOKENS) / raw_total) * 100.0, 1) if raw_total > 0 else 0.0
        return {
            "servers": per_server,
            "total_tools": tool_total,
            "raw_upstream_schema_tokens": raw_total,
            "zerobloat_schema_tokens": ZEROBLOAT_OVERHEAD_TOKENS,
            "tokens_saved_per_turn": max(0, raw_total - ZEROBLOAT_OVERHEAD_TOKENS),
            "reduction_pct": max(0.0, reduction_pct),
        }

    def search_tools(self, query: str, server: str = "", limit: int = 5) -> dict[str, Any]:
        self.stats.search_queries += 1
        terms = [w for w in re.findall(r"[a-zA-Z0-9_]+", (query or "").lower()) if len(w) >= 2]
        candidates: list[tuple[int, str, dict[str, Any]]] = []

        target_servers = [server] if server and server in self.sessions else list(self.sessions.keys())
        for sname in target_servers:
            tools = self._ensure_index_for_server(sname)
            for tool in tools:
                if tool.get("name") == "__error__":
                    continue
                tname = str(tool.get("name", ""))
                desc = str(tool.get("description", ""))
                props = (tool.get("inputSchema") or {}).get("properties") or {}
                hay = f"{sname} {tname} {desc} {' '.join(props.keys())}".lower()
                score = 0
                if not terms:
                    score = 1
                else:
                    for term in terms:
                        if term in tname.lower():
                            score += 5
                        if term in hay:
                            score += 2
                if score > 0:
                    candidates.append((score, sname, tool))

        candidates.sort(key=lambda item: (-item[0], item[1], str(item[2].get("name", ""))))
        picked = []
        for _sc, sname, tool in candidates[: max(1, min(limit, 15))]:
            schema = tool.get("inputSchema") or {}
            compact_props = {
                k: (v.get("type", "any") if isinstance(v, dict) else "any")
                for k, v in (schema.get("properties") or {}).items()
            }
            picked.append({
                "server": sname,
                "tool_name": tool.get("name"),
                "description": str(tool.get("description", ""))[:140],
                "required": schema.get("required") or [],
                "params": compact_props,
            })

        return {
            "ok": True,
            "query": query,
            "matches_found": len(candidates),
            "returned": len(picked),
            "tools": picked,
        }

    def _apply_output_shield(self, server: str, tool_name: str, text_out: str, max_chars: int) -> dict[str, Any]:
        raw_len = len(text_out)
        self.stats.raw_output_chars += raw_len
        if raw_len <= max_chars:
            self.stats.delivered_output_chars += raw_len
            return {"compressed": False, "text": text_out, "raw_chars": raw_len}

        digest = hashlib.sha256(f"{time.time()}:{server}:{tool_name}".encode("utf-8")).hexdigest()[:10]
        full_file = self.overflow_dir / f"{server}_{tool_name}_{digest}.txt"
        full_file.write_text(text_out, encoding="utf-8")

        head_sz = max(500, int(max_chars * 0.65))
        tail_sz = max(250, int(max_chars * 0.25))
        lines_total = text_out.count("\n") + 1
        summary = (
            f"[ECOTOKENS OUTPUT BLOAT SHIELD] Trimmed {raw_len:,} chars ({lines_total:,} lines) -> {head_sz + tail_sz:,} chars "
            f"(~{estimate_tokens(text_out) - estimate_tokens(text_out[:head_sz + tail_sz]):,} tokens saved).\n"
            f"Full uncropped log saved at: {full_file}\n\n"
            f"--- HEAD ---\n{text_out[:head_sz]}\n\n"
            f"--- TAIL ---\n{text_out[-tail_sz:]}"
        )
        self.stats.outputs_compressed += 1
        self.stats.delivered_output_chars += len(summary)
        return {
            "compressed": True,
            "text": summary,
            "raw_chars": raw_len,
            "delivered_chars": len(summary),
            "overflow_file": str(full_file),
        }

    def call_upstream_tool(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        max_output_chars: int | None = None,
    ) -> dict[str, Any]:
        sess = self.sessions.get(server)
        if not sess:
            return {"ok": False, "error": f"Unknown MCP server '{server}'. Known servers: {list(self.sessions.keys())}"}

        call_sig = hashlib.sha256(
            json.dumps({"s": server, "t": tool_name, "a": arguments or {}}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._recent_call_hashes.append(call_sig)
        if len(self._recent_call_hashes) > self.loop_threshold:
            self._recent_call_hashes = self._recent_call_hashes[-self.loop_threshold :]

        if (
            len(self._recent_call_hashes) == self.loop_threshold
            and len(set(self._recent_call_hashes)) == 1
        ):
            self.stats.loops_blocked += 1
            return {
                "ok": False,
                "blocked_by": "ecotokens_loop_breaker",
                "message": (
                    f"Blocked identical call '{server}.{tool_name}' repeated {self.loop_threshold}x in a row "
                    "to protect your token budget & rate limits. Adjust arguments or ask the user."
                ),
            }

        resp = sess.call_tool(tool_name, arguments or {})
        self.stats.calls_proxied += 1
        if "error" in resp:
            return {"ok": False, "server": server, "tool": tool_name, "upstream_error": resp["error"]}

        content = (resp.get("result") or {}).get("content") or []
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            else:
                parts.append(json.dumps(c))
        joined = "\n".join(parts)
        shield = self._apply_output_shield(server, tool_name, joined, max_output_chars or self.max_output_chars)
        return {
            "ok": True,
            "server": server,
            "tool": tool_name,
            "compressed": shield["compressed"],
            "raw_chars": shield["raw_chars"],
            "overflow_file": shield.get("overflow_file"),
            "output": shield["text"],
        }

    def get_stats(self) -> dict[str, Any]:
        return {
            "ok": True,
            "gateway_version": "2.0.0",
            "servers_managed": list(self.sessions.keys()),
            "servers_alive": [n for n, s in self.sessions.items() if s.is_alive()],
            "zerobloat_schema_tokens": ZEROBLOAT_OVERHEAD_TOKENS,
            "search_queries": self.stats.search_queries,
            "calls_proxied": self.stats.calls_proxied,
            "loops_blocked": self.stats.loops_blocked,
            "outputs_compressed": self.stats.outputs_compressed,
            "output_tokens_saved": self.stats.output_tokens_saved,
        }

    def handle_rpc(self, req: dict[str, Any]) -> dict[str, Any] | None:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": GATEWAY_SERVER_NAME, "version": "2.0.0"},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": META_TOOLS_MANIFEST}}
        if method == "tools/call":
            tname = str(params.get("name", ""))
            targs = params.get("arguments") or {}
            if tname == "ecotokens_search_tools":
                res = self.search_tools(
                    query=str(targs.get("query", "")),
                    server=str(targs.get("server", "")),
                    limit=int(targs.get("limit", 5) or 5),
                )
            elif tname == "ecotokens_call_tool":
                res = self.call_upstream_tool(
                    server=str(targs.get("server", "")),
                    tool_name=str(targs.get("tool_name", "")),
                    arguments=targs.get("arguments") or {},
                    max_output_chars=targs.get("max_output_chars"),
                )
            elif tname == "ecotokens_stats":
                res = self.get_stats()
            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {tname}"}}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]},
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unsupported method: {method}"}}

    def close_all(self) -> None:
        for sess in self.sessions.values():
            sess.close()


# ---------------------------------------------------------------------------
# Multi-Client Discovery, Audit & 1-Click Compress + SHA-256 Rollback
# ---------------------------------------------------------------------------

def discover_client_config_paths(
    custom_home: Path | None = None,
    custom_appdata: Path | None = None,
) -> dict[str, Path]:
    home = custom_home or Path.home()
    appdata = custom_appdata or Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    return {
        "claude_desktop": appdata / "Claude" / "claude_desktop_config.json",
        "claude_code": home / ".claude.json",
        "antigravity": home / ".gemini" / "antigravity" / "mcp_config.json",
        "cursor": home / ".cursor" / "mcp.json",
        "windsurf": home / ".codeium" / "windsurf" / "mcp_config.json",
        "vscode": appdata / "Code" / "User" / "mcp.json",
    }


def _read_servers_from_config(cfg_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    if not cfg_path.exists():
        return {}, {}, "mcpServers"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}, {}, "mcpServers"
        key = "servers" if "servers" in raw and "mcpServers" not in raw else "mcpServers"
        srv = raw.get(key)
        if not isinstance(srv, dict):
            return raw, {}, key
        return raw, {k: v for k, v in srv.items() if isinstance(v, dict)}, key
    except Exception:
        return {}, {}, "mcpServers"


def audit_mcp_clients(
    custom_home: Path | None = None,
    custom_appdata: Path | None = None,
    probe_live: bool = False,
) -> dict[str, Any]:
    """Scan all supported AI apps, calculate current MCP schema token tax & savings."""
    paths = discover_client_config_paths(custom_home, custom_appdata)
    clients_report: list[dict[str, Any]] = []
    merged_servers: dict[str, dict[str, Any]] = {}

    for cname, cpath in paths.items():
        _raw, servers, _key = _read_servers_from_config(cpath)
        non_gw = {k: v for k, v in servers.items() if k != GATEWAY_SERVER_NAME}
        for k, v in non_gw.items():
            merged_servers.setdefault(k, v)
        clients_report.append({
            "client": cname,
            "path": str(cpath),
            "exists": cpath.exists(),
            "server_count": len(non_gw),
            "compressed": GATEWAY_SERVER_NAME in servers and len(non_gw) == 0,
            "server_names": list(servers.keys()),
        })

    # Include any already-extracted registry servers so re-auditing after compress keeps metrics accurate
    sdir = default_state_dir(custom_home)
    reg_file = sdir / "zerobloat_registry.json"
    registry_servers: dict[str, dict[str, Any]] = {}
    if reg_file.exists():
        try:
            registry_servers = json.loads(reg_file.read_text(encoding="utf-8")).get("mcpServers") or {}
        except Exception:
            registry_servers = {}

    all_servers = {**registry_servers, **merged_servers}
    if probe_live and all_servers:
        gw = ZeroBloatMcpGateway(registry=all_servers, state_dir=sdir)
        try:
            probe_stats = gw.refresh_all_indexes()
        finally:
            gw.close_all()
        estimated_raw_tokens = probe_stats["raw_upstream_schema_tokens"]
        estimated_tools = probe_stats["total_tools"]
        per_server = probe_stats["servers"]
    else:
        # Industry-average conservative estimate: ~12 tools x ~260 schema tokens = ~3,120 tokens per MCP server
        estimated_tools = len(all_servers) * 12
        estimated_raw_tokens = len(all_servers) * 3120
        per_server = [
            {"server": k, "tool_count": 12, "schema_tokens": 3120, "estimated": True}
            for k in all_servers
        ]

    reduction = round(((estimated_raw_tokens - ZEROBLOAT_OVERHEAD_TOKENS) / estimated_raw_tokens) * 100.0, 1) if estimated_raw_tokens > ZEROBLOAT_OVERHEAD_TOKENS else 0.0
    return {
        "ok": True,
        "clients": clients_report,
        "upstream_servers_found": len(all_servers),
        "estimated_upstream_tools": estimated_tools,
        "estimated_raw_schema_tokens_per_turn": estimated_raw_tokens,
        "zerobloat_tokens_per_turn": ZEROBLOAT_OVERHEAD_TOKENS,
        "schema_reduction_pct": reduction,
        "per_server": per_server,
        "registry_path": str(reg_file),
    }


def compress_clients_to_zerobloat(
    custom_home: Path | None = None,
    custom_appdata: Path | None = None,
    target_clients: list[str] | None = None,
) -> dict[str, Any]:
    """
    Back up client MCP configs (SHA-256), move upstream MCP servers cleanly into
    ~/.ecotokens/zerobloat_registry.json, and wire the single Zero-Bloat gateway!
    """
    paths = discover_client_config_paths(custom_home, custom_appdata)
    sdir = default_state_dir(custom_home)
    bkp_dir = sdir / "backups"
    bkp_dir.mkdir(parents=True, exist_ok=True)
    reg_file = sdir / "zerobloat_registry.json"
    manifest_file = bkp_dir / "latest_zerobloat_rollback.json"

    existing_reg: dict[str, dict[str, Any]] = {}
    if reg_file.exists():
        try:
            existing_reg = json.loads(reg_file.read_text(encoding="utf-8")).get("mcpServers") or {}
        except Exception:
            existing_reg = {}

    gateway_entry = {
        "command": sys.executable,
        "args": [str(Path(__file__).resolve()), "gateway", "--state-dir", str(sdir)],
    }

    manifest: dict[str, Any] = {"entries": {}}
    compressed_clients: list[str] = []

    for cname, cpath in paths.items():
        if target_clients is not None and cname not in target_clients:
            continue
        if not cpath.exists():
            continue
        raw_text = cpath.read_text(encoding="utf-8", errors="ignore")
        raw_obj, servers, root_key = _read_servers_from_config(cpath)
        non_gw = {k: v for k, v in servers.items() if k != GATEWAY_SERVER_NAME}
        if not non_gw and GATEWAY_SERVER_NAME in servers:
            continue

        sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
        bkp_path = bkp_dir / f"{cname}_{sha}_{cpath.name}.bak"
        bkp_path.write_text(raw_text, encoding="utf-8")
        manifest["entries"][cname] = {
            "target_path": str(cpath),
            "backup_path": str(bkp_path),
            "sha256": sha,
        }

        existing_reg.update(non_gw)
        new_obj = dict(raw_obj)
        new_obj[root_key] = {GATEWAY_SERVER_NAME: gateway_entry}
        cpath.write_text(json.dumps(new_obj, indent=2) + "\n", encoding="utf-8")
        compressed_clients.append(cname)

    reg_file.write_text(json.dumps({"mcpServers": existing_reg}, indent=2) + "\n", encoding="utf-8")
    if manifest["entries"]:
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "compressed_clients": compressed_clients,
        "servers_consolidated": len(existing_reg),
        "registry_path": str(reg_file),
        "rollback_manifest": str(manifest_file),
        "zerobloat_tokens_per_turn": ZEROBLOAT_OVERHEAD_TOKENS,
    }


def rollback_zerobloat_clients(custom_home: Path | None = None) -> dict[str, Any]:
    """Restore original client MCP configs verbatim from SHA-256 backups."""
    sdir = default_state_dir(custom_home)
    manifest_file = sdir / "backups" / "latest_zerobloat_rollback.json"
    if not manifest_file.exists():
        return {"ok": False, "error": "No Zero-Bloat rollback backup found yet."}
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    restored: list[str] = []
    for cname, info in (manifest.get("entries") or {}).items():
        bkp = Path(info["backup_path"])
        tgt = Path(info["target_path"])
        if bkp.exists():
            tgt.write_text(bkp.read_text(encoding="utf-8"), encoding="utf-8")
            restored.append(cname)
    return {"ok": True, "restored_clients": restored}


def run_stdio_gateway_loop(state_dir: Path | None = None) -> None:
    gw = ZeroBloatMcpGateway(state_dir=state_dir)
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = gw.handle_rpc(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            except Exception as exc:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
    finally:
        gw.close_all()


def main() -> None:
    parser = argparse.ArgumentParser(description="EcoTokens Zero-Bloat MCP Gateway CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gw = sub.add_parser("gateway", help="Run STDIO Zero-Bloat Meta-MCP Gateway")
    p_gw.add_argument("--state-dir", type=Path, default=None)

    p_aud = sub.add_parser("audit", help="Scan AI clients and calculate MCP token bloat")
    p_aud.add_argument("--live", action="store_true", help="Probe configured MCP subprocesses live")

    sub.add_parser("compress", help="Compress client configs into Zero-Bloat gateway with SHA-256 backup")
    sub.add_parser("rollback", help="Restore original MCP configs from backup")

    args = parser.parse_args()
    if args.cmd == "gateway":
        run_stdio_gateway_loop(state_dir=args.state_dir)
    elif args.cmd == "audit":
        print(json.dumps(audit_mcp_clients(probe_live=args.live), indent=2))
    elif args.cmd == "compress":
        print(json.dumps(compress_clients_to_zerobloat(), indent=2))
    elif args.cmd == "rollback":
        print(json.dumps(rollback_zerobloat_clients(), indent=2))


if __name__ == "__main__":
    main()
