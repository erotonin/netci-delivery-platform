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
import subprocess
import sys
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
        self._running = False

    async def run(self) -> None:
        """Main connection and dispatch loop with exponential backoff reconnect."""
        import websockets  # lazy import

        self._running = True
        ws_endpoint = (
            f"{self.server_url}/api/v1/agents/ws"
            f"?agent_id={self.agent_id}&hostname={self.hostname}"
        )
        if ws_endpoint.startswith("http://"):
            ws_endpoint = "ws://" + ws_endpoint[len("http://") :]
        elif ws_endpoint.startswith("https://"):
            ws_endpoint = "wss://" + ws_endpoint[len("https://") :]

        backoff = 2.0
        while self._running:
            try:
                logger.info("Connecting outbound to NetCI controller at %s", ws_endpoint)
                async with websockets.connect(ws_endpoint) as ws:
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

            # Run in subprocess
            result = await self._run_command(cmd)
            resp = {
                "type": "COMMAND_RESULT",
                "task_id": task_id,
                "exit_code": result["exit_code"],
                "output": result["output"],
            }
            await ws.send(json.dumps(resp))

    async def _run_command(self, cmd: str) -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300.0)
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            return {"exit_code": proc.returncode, "output": output}
        except asyncio.TimeoutError:
            return {"exit_code": -1, "output": "Execution timed out (300s limit)"}
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
