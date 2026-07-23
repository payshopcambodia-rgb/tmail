-- Run this once in Supabase SQL Editor.
-- The bot uses the service-role key server-side; never expose it to Telegram users.

create table if not exists public.bot_users (
  chat_id bigint primary key,
  username text,
  first_name text,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table if not exists public.mailboxes (
  id bigint generated always as identity primary key,
  chat_id bigint not null references public.bot_users(chat_id) on delete cascade,
  address text not null,
  provider_mailbox_id text,
  provider_token text,
  created_at timestamptz not null default now(),
  unique (chat_id, address)
);

create index if not exists mailboxes_chat_id_created_at_idx
  on public.mailboxes (chat_id, created_at desc);

alter table public.bot_users enable row level security;
alter table public.mailboxes enable row level security;

-- No anon/authenticated policies are intentionally added. The bot accesses these
-- tables with the server-side service-role key only.
