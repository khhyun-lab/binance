from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from binance_bot.logging_utils import LOG_SCHEMA_VERSION, setup_logging
from binance_bot.telegram import TelegramNotifier, format_telegram_message


def _to_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(slots=True)
class WatchdogSettings:
    watchdog_interval_seconds: int
    watchdog_failure_threshold: int
    watchdog_heartbeat_stale_seconds: int
    watchdog_forced_restart_seconds: int
    command: str
    heartbeat_file: Path
    log_dir: Path
    log_level: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None


def load_watchdog_settings() -> WatchdogSettings:
    load_dotenv()
    root_dir = Path.cwd()
    return WatchdogSettings(
        watchdog_interval_seconds=_to_int(os.getenv("WATCHDOG_INTERVAL_SECONDS"), 60),
        watchdog_failure_threshold=_to_int(os.getenv("WATCHDOG_FAILURE_THRESHOLD"), 3),
        watchdog_heartbeat_stale_seconds=_to_int(os.getenv("WATCHDOG_HEARTBEAT_STALE_SECONDS"), 180),
        watchdog_forced_restart_seconds=max(0, _to_int(os.getenv("WATCHDOG_FORCED_RESTART_SECONDS"), 43200)),
        command=os.getenv("BINANCE_FUTURES_COMMAND", "python3 -m binance_bot.main"),
        heartbeat_file=root_dir / os.getenv("BINANCE_FUTURES_HEARTBEAT_FILE", "runtime/binance_heartbeat.json"),
        log_dir=root_dir / os.getenv("BINANCE_FUTURES_LOG_DIR", "logs"),
        log_level=os.getenv("BINANCE_FUTURES_LOG_LEVEL", "INFO").upper(),
        telegram_bot_token=os.getenv("BINANCE_FUTURES_TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("BINANCE_FUTURES_TELEGRAM_CHAT_ID") or None,
    )


class ManagedProcess:
    def __init__(self, name: str, command: str, heartbeat_file: Path, notifier: TelegramNotifier) -> None:
        self.name = name
        self.command = command
        self.heartbeat_file = heartbeat_file
        self.notifier = notifier
        self.process: subprocess.Popen[str] | None = None
        self.failure_count = 0
        self.started_at = 0.0


class ProcessWatchdog:
    def __init__(self) -> None:
        self.settings = load_watchdog_settings()
        setup_logging(self.settings.log_dir, self.settings.log_level, log_filename="watchdog.log")
        self.logger = logging.getLogger(self.__class__.__name__)
        self.notifier = TelegramNotifier(self.settings.telegram_bot_token, self.settings.telegram_chat_id)
        self.stop_requested = False
        self.managed_processes = self._build_managed_processes()

    def _build_managed_processes(self) -> list[ManagedProcess]:
        if not self.settings.command.strip():
            self.logger.info("감시 대상 비활성화 name=%s reason=empty_command", "binance-futures-bot")
            return []
        return [
            ManagedProcess(
                "binance-futures-bot",
                self.settings.command,
                self.settings.heartbeat_file,
                self.notifier,
            )
        ]

    def start_process(self, managed_process: ManagedProcess) -> None:
        command = self._resolve_command(managed_process.command)
        self.logger.info(
            "감시 대상 프로세스를 시작합니다: log_schema_version=%s name=%s command=%s resolved_command=%s",
            LOG_SCHEMA_VERSION,
            managed_process.name,
            managed_process.command,
            " ".join(command),
        )
        managed_process.process = subprocess.Popen(command, cwd=Path.cwd())
        managed_process.started_at = time.time()

    def _resolve_command(self, raw_command: str) -> list[str]:
        command = shlex.split(raw_command)
        if not command:
            raise ValueError("빈 명령은 실행할 수 없습니다.")

        if command[0] in {"python", "python3"}:
            command[0] = sys.executable
            return command

        if len(command) >= 2 and command[0] == "/usr/bin/env" and command[1] in {"python", "python3"}:
            return [sys.executable, *command[2:]]

        return command

    def stop_process(self, managed_process: ManagedProcess) -> None:
        if managed_process.process is None:
            return
        if managed_process.process.poll() is not None:
            return

        self.logger.warning("감시 대상 프로세스를 종료합니다: name=%s pid=%s", managed_process.name, managed_process.process.pid)
        managed_process.process.terminate()
        try:
            managed_process.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.logger.error("프로세스가 종료되지 않아 강제 종료합니다: name=%s pid=%s", managed_process.name, managed_process.process.pid)
            managed_process.process.kill()
            managed_process.process.wait(timeout=5)

    def is_process_alive(self, managed_process: ManagedProcess) -> bool:
        return managed_process.process is not None and managed_process.process.poll() is None

    def heartbeat_is_stale(self, managed_process: ManagedProcess) -> bool:
        heartbeat_file = managed_process.heartbeat_file
        if not heartbeat_file.exists():
            return True
        payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        timestamp = float(payload.get("timestamp", 0))
        age = time.time() - timestamp
        return age > self.settings.watchdog_heartbeat_stale_seconds

    def uptime_exceeded(self, managed_process: ManagedProcess) -> bool:
        if self.settings.watchdog_forced_restart_seconds <= 0:
            return False
        if managed_process.started_at <= 0:
            return False
        return (time.time() - managed_process.started_at) >= self.settings.watchdog_forced_restart_seconds

    async def monitor(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                pass

        for managed_process in self.managed_processes:
            self.start_process(managed_process)
        await self.notifier.send(
            format_telegram_message(
                "[watchdog 시작]",
                fields=[("대상", ", ".join(process.name for process in self.managed_processes))],
            )
        )

        while not self.stop_requested:
            await asyncio.sleep(self.settings.watchdog_interval_seconds)

            for managed_process in self.managed_processes:
                alive = self.is_process_alive(managed_process)
                stale = self.heartbeat_is_stale(managed_process)
                scheduled_restart = alive and not stale and self.uptime_exceeded(managed_process)
                self.logger.info(
                    "watchdog 상태=%s name=%s interval_seconds=%s alive=%s stale=%s scheduled_restart=%s failure_count=%s threshold=%s",
                    "정기재시작" if scheduled_restart else ("정상" if alive and not stale else "비정상"),
                    managed_process.name,
                    self.settings.watchdog_interval_seconds,
                    alive,
                    stale,
                    scheduled_restart,
                    managed_process.failure_count,
                    self.settings.watchdog_failure_threshold,
                )
                if scheduled_restart:
                    uptime_seconds = int(time.time() - managed_process.started_at)
                    await managed_process.notifier.send(
                        format_telegram_message(
                            "[watchdog 정기 재시작]",
                            fields=[
                                ("프로세스", managed_process.name),
                                ("가동 시간", f"{uptime_seconds}초"),
                                ("조치", "12시간 주기 재시작"),
                            ],
                        )
                    )
                    self.stop_process(managed_process)
                    self.start_process(managed_process)
                    managed_process.failure_count = 0
                    continue

                if alive and not stale:
                    managed_process.failure_count = 0
                    continue

                managed_process.failure_count += 1
                if managed_process.failure_count < self.settings.watchdog_failure_threshold:
                    continue

                await managed_process.notifier.send(
                    format_telegram_message(
                        "[watchdog 재시작]",
                        fields=[
                            ("프로세스", managed_process.name),
                            ("누적 실패", f"{managed_process.failure_count}회"),
                            ("조치", "강제 재시작 진행"),
                        ],
                    )
                )
                self.stop_process(managed_process)
                self.start_process(managed_process)
                await managed_process.notifier.send(
                    format_telegram_message(
                        "[watchdog 복구 완료]",
                        fields=[
                            ("프로세스", managed_process.name),
                            ("pid", managed_process.process.pid if managed_process.process is not None else "unknown"),
                        ],
                    )
                )
                managed_process.failure_count = 0

        for managed_process in self.managed_processes:
            self.stop_process(managed_process)
        await self.notifier.send(format_telegram_message("[watchdog 종료]"))

    def request_stop(self) -> None:
        self.stop_requested = True


def main() -> None:
    watchdog = ProcessWatchdog()
    asyncio.run(watchdog.monitor())


if __name__ == "__main__":
    main()