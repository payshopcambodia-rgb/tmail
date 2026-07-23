# T-Mail Telegram Bot

A private, hacker-style Telegram interface for provisioning disposable mailboxes and viewing safe inbox metadata. It stores mailbox metadata in Supabase and is designed to run as a Railway worker.

The bot deliberately does not retrieve, display, forward, or automate 2FA/OTP codes or other account-verification flows.

## 1. Install and configure

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill `.env` with your rotated Telegram token, Supabase service-role key, and mail provider key. Boomlify is configured by default:

- `POST https://v1.boomlify.com/api/v1/emails/create`
- `GET https://v1.boomlify.com/api/v1/emails/{id}/messages?include_dashboard=true`

`MAIL_API_EMAIL_TIME` may be `10min`, `1hour`, `1day`, or `permanent`. Boomlify message retrieval may require a paid plan and credits. For another provider, the generic mail provider contract expected by `bot.py` is:

- `POST MAIL_API_CREATE_PATH` with JSON `{ "address": "optional", "password": "generated" }`
- a JSON object containing `address` or `email`, and optionally `id`/`mailbox_id` and `token`/`access_token`
- `GET MAIL_API_INBOX_PATH`, where `{mailbox_id}` and `{address}` are available placeholders
- a JSON list, or an object containing `messages`, `items`, `data`, or `results`

If the provider uses another schema, adjust only `MailProvider` in `bot.py`.

## 2. Prepare Supabase

Run [`schema.sql`](schema.sql) in the Supabase SQL Editor. Keep the service-role key server-side only.

## 3. Run locally

```powershell
python bot.py
```

The configured owner chat ID is allowed automatically. Add additional numeric IDs to `ALLOWED_CHAT_IDS` as a comma-separated list.

## 4. Deploy on Railway

Create a Railway project from this folder, add the variables from `.env`, and deploy. `railway.toml` starts the polling worker with `python bot.py`; no public HTTP port is required.

## Security

The credentials pasted into chat should be revoked and regenerated before deployment. Never commit `.env`, Telegram tokens, API keys, or the Supabase service-role key.
