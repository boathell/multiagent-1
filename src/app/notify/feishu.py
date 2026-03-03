from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

import httpx


class FeishuNotifier:
    def __init__(self, webhook_url: str, signing_secret: str = "") -> None:
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret
        self._logger = logging.getLogger("app.notify.feishu")

    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _sign(self, ts: str) -> str:
        string_to_sign = f"{ts}\n{self.signing_secret}".encode("utf-8")
        h = hmac.new(string_to_sign, digestmod=hashlib.sha256)
        signature = base64.b64encode(h.digest()).decode("utf-8")
        return signature

    async def send_text(self, title: str, lines: list[str]) -> None:
        if not self.enabled():
            self._logger.info("Feishu disabled, skip sending")
            return

        text = "\n".join([title, *lines])
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": text},
        }

        if self.signing_secret:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = self._sign(ts)

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self.webhook_url, json=payload)
            if resp.status_code >= 300:
                raise RuntimeError(f"Feishu notify failed: {resp.status_code} {resp.text}")

    @staticmethod
    def compact_json(data: dict) -> str:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

