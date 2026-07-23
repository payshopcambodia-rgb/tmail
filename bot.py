"""Telegram disposable-mail bot.

The mail provider is intentionally configured through environment variables because
the provider API URL/schema was not supplied. This bot does not retrieve or relay
2FA/OTP codes or automate account verification.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("tmail-bot")


SENSITIVE_WORDS = re.compile(
    r"\b(2fa|mfa|otp|one[- ]time|verification|verify|security code|login code|passcode)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_chat_id: int
    allowed_chat_ids: frozenset[int]
    supabase_url: str
    supabase_service_role_key: str
    mail_api_base_url: str
    mail_api_key: str
    mail_api_auth_header: str
    mail_api_mailbox_token_header: str
    mail_api_create_path: str
    mail_api_inbox_path: str
    mail_default_domain: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"Missing required environment variable: {name}")
            return value

        admin_chat_id = int(required("ADMIN_CHAT_ID"))
        configured_ids = {
            int(value.strip())
            for value in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
            if value.strip()
        }
        configured_ids.add(admin_chat_id)

        return cls(
            telegram_token=required("TELEGRAM_BOT_TOKEN"),
            admin_chat_id=admin_chat_id,
            allowed_chat_ids=frozenset(configured_ids),
            supabase_url=required("SUPABASE_URL").rstrip("/"),
            supabase_service_role_key=required("SUPABASE_SERVICE_ROLE_KEY"),
            mail_api_base_url=required("MAIL_API_BASE_URL").rstrip("/"),
            mail_api_key=required("MAIL_API_KEY"),
            mail_api_auth_header=os.getenv("MAIL_API_AUTH_HEADER", "X-API-Key"),
            mail_api_mailbox_token_header=os.getenv(
                "MAIL_API_MAILBOX_TOKEN_HEADER", "X-Mailbox-Token"
            ),
            mail_api_create_path=os.getenv("MAIL_API_CREATE_PATH", "/mailboxes"),
            mail_api_inbox_path=os.getenv(
                "MAIL_API_INBOX_PATH", "/mailboxes/{mailbox_id}/messages"
            ),
            mail_default_domain=os.getenv("MAIL_DEFAULT_DOMAIN") or None,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def random_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("messages", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


class SupabaseStore:
    def __init__(self, settings: Settings) -> None:
        self.base_url = f"{settings.supabase_url}/rest/v1"
        self.headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, table: str, **kwargs: Any) -> Any:
        request_headers = kwargs.pop("headers", self.headers)
        response = await self.client.request(
            method, f"{self.base_url}/{table}", headers=request_headers, **kwargs
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    async def upsert_user(self, update: Update) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return
        record = {
            "chat_id": chat.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_seen_at": utc_now(),
        }
        await self._request(
            "POST",
            "bot_users",
            params={"on_conflict": "chat_id"},
            headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
            json=record,
        )

    async def add_mailbox(
        self,
        chat_id: int,
        address: str,
        provider_mailbox_id: str | None,
        provider_token: str | None,
    ) -> None:
        await self._request(
            "POST",
            "mailboxes",
            params={"on_conflict": "chat_id,address"},
            headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
            json={
                "chat_id": chat_id,
                "address": address,
                "provider_mailbox_id": provider_mailbox_id,
                "provider_token": provider_token,
            },
        )

    async def latest_mailbox(self, chat_id: int) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "mailboxes",
            params={
                "chat_id": f"eq.{chat_id}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        return rows[0] if rows else None


class MailProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=25)

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self, mailbox_token: str | None = None) -> dict[str, str]:
        headers = {
            self.settings.mail_api_auth_header: self.settings.mail_api_key,
            "Accept": "application/json",
        }
        if mailbox_token:
            headers[self.settings.mail_api_mailbox_token_header] = mailbox_token
        return headers

    def _url(self, path: str) -> str:
        return f"{self.settings.mail_api_base_url}/{path.lstrip('/')}"

    async def create_mailbox(self) -> dict[str, Any]:
        local_part = "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12)
        )
        address = (
            f"{local_part}@{self.settings.mail_default_domain}"
            if self.settings.mail_default_domain
            else None
        )
        payload: dict[str, Any] = {"password": random_password()}
        if address:
            payload["address"] = address

        response = await self.client.post(
            self._url(self.settings.mail_api_create_path),
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Mail API returned an unexpected create response")

        result = data.get("data") if isinstance(data.get("data"), dict) else data
        resolved_address = (
            result.get("address")
            or result.get("email")
            or result.get("mailbox")
            or address
        )
        if not resolved_address:
            raise RuntimeError(
                "Mail API did not return an address. Set MAIL_DEFAULT_DOMAIN or adapt the provider contract."
            )
        return {
            "address": str(resolved_address),
            "provider_mailbox_id": result.get("id")
            or result.get("mailbox_id")
            or result.get("uuid"),
            "provider_token": result.get("token") or result.get("access_token"),
        }

    async def list_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        mailbox_id = mailbox.get("provider_mailbox_id") or mailbox.get("address")
        path = self.settings.mail_api_inbox_path.format(
            mailbox_id=mailbox_id,
            address=mailbox.get("address", ""),
        )
        response = await self.client.get(
            self._url(path),
            headers=self._headers(mailbox.get("provider_token")),
            params={"limit": 10},
        )
        response.raise_for_status()
        return normalize_list(response.json())


def sensitive_message(message: dict[str, Any]) -> bool:
    fields = (
        message.get("subject"),
        message.get("title"),
        message.get("preview"),
        message.get("snippet"),
    )
    return bool(SENSITIVE_WORDS.search(" ".join(str(v or "") for v in fields)))


def format_message(message: dict[str, Any]) -> str:
    sender = message.get("from") or message.get("sender") or "unknown"
    if isinstance(sender, dict):
        sender = sender.get("address") or sender.get("email") or sender.get("name") or "unknown"
    subject = message.get("subject") or message.get("title") or "(no subject)"
    received = message.get("received_at") or message.get("created_at") or message.get("date") or ""
    return f"• {str(subject)[:90]}\n  from: {str(sender)[:90]}\n  at: {str(received)[:40]}"


class TMailBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = SupabaseStore(settings)
        self.provider = MailProvider(settings)

    async def close(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        if not chat or chat.id not in self.settings.allowed_chat_ids:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "ACCESS DENIED\nThis bot is private. Ask the operator to add your chat ID."
                )
            return False
        try:
            await self.store.upsert_user(update)
        except httpx.HTTPError:
            log.exception("Supabase user upsert failed")
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        await update.effective_message.reply_text(
            "╔══════════════════════╗\n"
            "║  T-MAIL // ONLINE    ║\n"
            "╚══════════════════════╝\n\n"
            "Disposable mailbox control panel.\n\n"
            "/newmail  generate a mailbox\n"
            "/inbox    show safe message metadata\n"
            "/status   show active mailbox\n"
            "/help     show commands\n\n"
            "Verification codes and message bodies are not displayed."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.authorized(update):
            await self.start(update, context)

    async def newmail(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        message = update.effective_message
        await message.reply_text("[+] provisioning mailbox...")
        try:
            mailbox = await self.provider.create_mailbox()
            await self.store.add_mailbox(
                update.effective_chat.id,
                mailbox["address"],
                mailbox.get("provider_mailbox_id"),
                mailbox.get("provider_token"),
            )
            await message.reply_text(
                "╔══ MAILBOX READY ══╗\n"
                f"address: {mailbox['address']}\n"
                "status:  ACTIVE\n"
                "╚═══════════════════╝\n\n"
                "Use /inbox to view non-sensitive message metadata."
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.exception("Mailbox creation failed")
            await message.reply_text(f"[!] provider error: {str(exc)[:180]}")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        mailbox = await self.store.latest_mailbox(update.effective_chat.id)
        if not mailbox:
            await update.effective_message.reply_text("[i] no mailbox yet — use /newmail")
            return
        await update.effective_message.reply_text(
            f"ACTIVE MAILBOX\naddress: {mailbox['address']}\ncreated: {mailbox.get('created_at', '')}"
        )

    async def inbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        mailbox = await self.store.latest_mailbox(update.effective_chat.id)
        if not mailbox:
            await update.effective_message.reply_text("[i] no mailbox yet — use /newmail")
            return
        try:
            messages = await self.provider.list_messages(mailbox)
            visible = [message for message in messages if not sensitive_message(message)]
            hidden = len(messages) - len(visible)
            lines = [f"INBOX // {mailbox['address']}", ""]
            lines.extend(format_message(message) for message in visible[:10])
            if hidden:
                lines.extend(["", f"[{hidden} sensitive verification message(s) hidden]"])
            if not visible and not hidden:
                lines.append("(empty)")
            await update.effective_message.reply_text("\n".join(lines))
        except httpx.HTTPError as exc:
            log.exception("Inbox fetch failed")
            await update.effective_message.reply_text(f"[!] provider error: {str(exc)[:180]}")


async def post_init(application: Application) -> None:
    bot: TMailBot = application.bot_data["service"]
    log.info("T-mail bot started")


async def post_shutdown(application: Application) -> None:
    bot: TMailBot = application.bot_data["service"]
    await bot.close()


def build_application(settings: Settings) -> Application:
    service = TMailBot(settings)
    application = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["service"] = service
    application.add_handler(CommandHandler("start", service.start))
    application.add_handler(CommandHandler("help", service.help))
    application.add_handler(CommandHandler("newmail", service.newmail))
    application.add_handler(CommandHandler("status", service.status))
    application.add_handler(CommandHandler("inbox", service.inbox))
    return application


def main() -> None:
    settings = Settings.from_env()
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
