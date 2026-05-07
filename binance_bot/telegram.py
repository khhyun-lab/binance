from __future__ import annotations

import logging
from typing import Iterable

import aiohttp


def format_telegram_message(
    title: str,
    fields: Iterable[tuple[str, object]] | None = None,
    sections: Iterable[tuple[str, Iterable[object]]] | None = None,
    footer: str | None = None,
) -> str:
    lines = [title.strip()]

    if fields:
        normalized_fields = [(label, value) for label, value in fields if value is not None and str(value) != ""]
        if normalized_fields:
            lines.append("")
            lines.extend(f"{label}: {value}" for label, value in normalized_fields)

    if sections:
        for section_title, section_lines in sections:
            normalized_lines = [str(line) for line in section_lines if str(line).strip()]
            if not normalized_lines:
                continue
            lines.append("")
            lines.append(section_title)
            lines.extend(f"- {line}" for line in normalized_lines)

    if footer:
        lines.append("")
        lines.append(footer)

    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, message: str) -> None:
        if not self.enabled:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status >= 400:
                        body = await response.text()
                        self.logger.error("텔레그램 전송 실패: status=%s body=%s", response.status, body)
        except Exception as exc:
            self.logger.exception("텔레그램 전송 중 예외가 발생했습니다: %s", exc)