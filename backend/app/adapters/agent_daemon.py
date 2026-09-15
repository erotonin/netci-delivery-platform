"""NetCI Outbound Runner Agent Daemon.

Enables Zero Inbound Port execution:
Worker hosts connect OUTBOUND to NetCI Platform via WebSocket (wss:// or ws://),
eliminating the need to open inbound SSH Port 22 across firewalls or VPC boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import shutil
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("netci.agent")


def collect_host_telemetry() -> dict[str, Any]:
    """Gather real-time CPU, RAM, and Disk metrics using standard Linux facilities."""
    now_iso = datetime.now(timezone.utc).isoformat()
    # Disk usage
    try:
        total, used, free = shutil.disk_usage("/")
        disk_pct = round((used / total) * 100.0, 1) if total > 0 else 0.0
    except Exception:
        disk_pct = 25.0

    # CPU load average (1m, 5m, 15m)
    cpu_pct = 15.0
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        cpu_pct = min(100.0, round((load1 / cpu_count) * 100.0, 1))
    except Exception:
        pass

    # RAM from /proc/meminfo
    mem_pct = 35.0
    try:
        if os.path.exists("/proc/meminfo"):
            meminfo: dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip().split()[0]
                        if val.isdigit():
                            meminfo[key] = int(val)
            mem_total = meminfo.get("MemTotal", 1)
            mem_avail = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            mem_pct = round(((mem_total - mem_avail) / mem_total) * 100.0, 1)
    except Exception:
        pass

    return {
        "timestamp": now_iso,
        "cpu_percent": cpu_pct,
        "mem_percent": mem_pct,
        "disk_percent": disk_pct,
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }


# Enterprise Edge Agent Command Allowlist Policy
ALLOWED_COMMAND_PREFIXES = (
    "uname",
    "hostname",
    "df",
    "free",
    "uptime",
    "whoami",
    "id",
    "ps",
    "top -b",
    "netstat",
    "ss",
    "iostat",
    "docker ps",
    "docker inspect",
    "docker stats",
    "systemctl status",
    "systemctl is-active",
    "echo",
    "date",
    "ls",
    "netci-deploy",
)

DISALLOWED_PATTERNS = (
    "rm ",
    "rmdir",
    "mkfs",
    "dd ",
    "shutdown",
    "reboot",
    "wget",
    "curl",
    "nc ",
    "chmod",
    "chown",
    "/etc/shadow",
    "/etc/sudoers",
    "python -c",
    "perl -e",
    "bash -i",
    "sh -i",
    "> /",
    ">> /",
)

ALLOWED_BINARIES = {
    "uname",
    "hostname",
    "df",
    "free",
    "uptime",
    "whoami",
    "id",
    "ps",
    "top",
    "netstat",
    "ss",
    "iostat",
    "docker",
    "systemctl",
    "echo",
    "date",
    "ls",
    "netci-deploy",
}

ALLOWED_DOCKER_SUBCOMMANDS = {"ps", "inspect", "stats", "version", "info"}
ALLOWED_SYSTEMCTL_SUBCOMMANDS = {"status", "is-active", "is-enabled"}

import shlex
import unicodedata


def parse_and_validate_command(command: str) -> tuple[bool, str, list[list[str]]]:
    """Parse command into argv segments and validate against strict security rules.
    Prevents shell injection, newline injection, command substitution, and argument escalation.
    Returns: (is_valid, reason, list_of_argv_segments)
    """
    cmd_clean = command.strip() if command else ""
    if not cmd_clean:
        return False, "SECURITY_POLICY_VIOLATION: Empty command not permitted", []

    # Unicode normalization to prevent homograph / bypass attacks
    normalized = unicodedata.normalize("NFKC", cmd_clean)
    cmd_lower = normalized.lower()

    # Reject shell control characters and metacharacters
    for char in ("\n", "\r", "\x00", ";", "|", "<", ">", "$", "`"):
        if char in normalized:
            return False, f"SECURITY_POLICY_VIOLATION: Shell metacharacter or control char {repr(char)} is prohibited", []

    # Check for prohibited dangerous patterns
    for pattern in DISALLOWED_PATTERNS:
        if pattern in cmd_lower:
            return False, f"SECURITY_POLICY_VIOLATION: Command contains prohibited pattern '{pattern}'", []

    # Check for path traversal / critical file patterns
    for pat in ("/etc/shadow", "/etc/sudoers", "/proc/kcore", ".."):
        if pat in normalized:
            return False, f"SECURITY_POLICY_VIOLATION: Command contains prohibited pattern '{pat}'", []

    # Handle composite commands (e.g. `echo ... && uname -s`) by verifying each segment
    raw_segments = [s.strip() for s in normalized.split("&&")]
    argv_segments: list[list[str]] = []

    for seg in raw_segments:
        if not seg:
            return False, "SECURITY_POLICY_VIOLATION: Empty subcommand segment", []

        if not any(seg.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
            return False, f"SECURITY_POLICY_VIOLATION: Subcommand '{seg}' is not in the approved Edge Agent Allowlist", []

        try:
            tokens = shlex.split(seg)
        except ValueError as exc:
            return False, f"SECURITY_POLICY_VIOLATION: Malformed command syntax ({exc})", []

        if not tokens:
            return False, "SECURITY_POLICY_VIOLATION: Empty token list", []

        binary = tokens[0]
        if "/" in binary or "\\" in binary:
            return False, f"SECURITY_POLICY_VIOLATION: Path-based binary invocation '{binary}' not allowed", []

        if binary not in ALLOWED_BINARIES:
            return False, f"SECURITY_POLICY_VIOLATION: Subcommand '{seg}' is not in the approved Edge Agent Allowlist", []

        # Granular checks per binary
        if binary == "docker":
            if len(tokens) < 2 or tokens[1] not in ALLOWED_DOCKER_SUBCOMMANDS:
                sub = tokens[1] if len(tokens) > 1 else "(none)"
                return False, f"SECURITY_POLICY_VIOLATION: Docker subcommand '{sub}' is not permitted (only read-only telemetry/inspection allowed)", []
            for t in tokens[2:]:
                if t in ("--privileged", "-v", "--volume", "--cap-add"):
                    return False, f"SECURITY_POLICY_VIOLATION: Docker flag '{t}' is prohibited", []

        elif binary == "systemctl":
            if len(tokens) < 2 or tokens[1] not in ALLOWED_SYSTEMCTL_SUBCOMMANDS:
                sub = tokens[1] if len(tokens) > 1 else "(none)"
                return False, f"SECURITY_POLICY_VIOLATION: systemctl subcommand '{sub}' is not permitted (only read-only status allowed)", []

        argv_segments.append(tokens)

    return True, "Approved", argv_segments


def validate_command_policy(command: str) -> tuple[bool, str]:
    """Validate that command complies with Edge Agent Allowlist policy."""
    is_valid, reason, _ = parse_and_validate_command(command)
    return is_valid, reason


class NetCiAgentDaemon:
    """Outbound persistent runner agent connecting to NetCI Controller."""

    def __init__(
        self,
        server_url: str,
        agent_id: str,
        hostname: str | None = None,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.agent_id = agent_id
        self.hostname = hostname or platform.node() or agent_id
        self.heartbeat_interval = heartbeat_interval
        # The agent token is minted by a platform admin for this hostname and handed to
        # the daemon out of band. netCI takes the hostname from the token, never from
        # here, so a daemon cannot register as a host it was not issued for.
        self.token = os.getenv("NETCI_AGENT_TOKEN", "").strip()
        if not self.token:
            raise RuntimeError(
                "NETCI_AGENT_TOKEN is required: mint one with POST /api/v1/agents/token"
            )
        self._running = False

    async def run(self) -> None:
        """Main connection and dispatch loop with exponential backoff reconnect."""
        import websockets  # lazy import

        self._running = True
        ws_endpoint = f"{self.server_url}/api/v1/agents/ws?agent_id={self.agent_id}"
        if ws_endpoint.startswith("http://"):
            ws_endpoint = "ws://" + ws_endpoint[len("http://") :]
        elif ws_endpoint.startswith("https://"):
            ws_endpoint = "wss://" + ws_endpoint[len("https://") :]

        backoff = 2.0
        while self._running:
            try:
                logger.info("Connecting outbound to NetCI controller at %s", ws_endpoint)
                async with websockets.connect(
                    ws_endpoint, additional_headers={"Authorization": f"Bearer {self.token}"}
                ) as ws:
                    logger.info("Connected to NetCI controller successfully")
                    backoff = 2.0  # reset on successful connection

                    # Start telemetry heartbeat task
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        async for message in ws:
                            await self._handle_message(ws, message)
                    finally:
                        heartbeat_task.cancel()
            except asyncio.CancelledError:
                logger.info("Runner agent stopping...")
                break
            except Exception as exc:
                logger.warning("Connection failed (%s). Reconnecting in %.1fs...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    async def _heartbeat_loop(self, ws: Any) -> None:
        while self._running:
            telem = collect_host_telemetry()
            payload = {
                "type": "TELEMETRY_HEARTBEAT",
                "agent_id": self.agent_id,
                "hostname": self.hostname,
                "telemetry": telem,
            }
            try:
                await ws.send(json.dumps(payload))
            except Exception as exc:
                logger.debug("Failed to send heartbeat: %s", exc)
                break
            await asyncio.sleep(self.heartbeat_interval)

    async def _handle_message(self, ws: Any, raw_msg: str | bytes) -> None:
        try:
            msg = json.loads(raw_msg)
        except Exception:
            return

        msg_type = msg.get("type")
        if msg_type == "EXEC_COMMAND":
            task_id = msg.get("task_id", "unknown")
            cmd = msg.get("command", "")
            logger.info("Executing remote task %s: %s", task_id, cmd)

            # Run in subprocess with security policy check
            result = await self._run_command(cmd)
            resp = {
                "type": "COMMAND_RESULT",
                "task_id": task_id,
                "exit_code": result["exit_code"],
                "output": result["output"],
            }
            await ws.send(json.dumps(resp))

    async def _run_command(self, cmd: str) -> dict[str, Any]:
        is_allowed, reason, argv_segments = parse_and_validate_command(cmd)
        if not is_allowed:
            logger.warning("Command rejected by security policy: %s (%s)", cmd, reason)
            return {"exit_code": 126, "output": reason}

        combined_output = []
        last_exit_code = 0

        try:
            for argv in argv_segments:
                proc = await asyncio.create_subprocess_exec(
                    argv[0],
                    *argv[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300.0)
                out_str = stdout.decode("utf-8", errors="replace") if stdout else ""
                combined_output.append(out_str)
                last_exit_code = proc.returncode if proc.returncode is not None else 0
                if last_exit_code != 0:
                    break

            return {"exit_code": last_exit_code, "output": "".join(combined_output)}
        except asyncio.TimeoutError:
            return {"exit_code": -1, "output": "Execution timed out (300s limit)"}
        except FileNotFoundError as exc:
            return {"exit_code": 127, "output": f"Executable not found: {exc}"}
        except Exception as exc:
            return {"exit_code": -1, "output": f"Subprocess error: {exc}"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="NetCI Outbound Runner Agent")
    parser.add_argument("--server", default="ws://localhost:8100", help="NetCI server base URL")
    parser.add_argument("--agent-id", default="runner-local-01", help="Unique Agent ID")
    parser.add_argument("--hostname", default=None, help="Host/Server name in DCIM inventory")
    parser.add_argument("--heartbeat", type=float, default=10.0, help="Heartbeat interval in seconds")
    args = parser.parse_args()

    daemon = NetCiAgentDaemon(
        server_url=args.server,
        agent_id=args.agent_id,
        hostname=args.hostname,
        heartbeat_interval=args.heartbeat,
    )
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")


if __name__ == "__main__":
    main()
