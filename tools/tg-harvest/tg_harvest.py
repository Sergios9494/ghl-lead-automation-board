#!/usr/bin/env python3
"""Harvest Telegram users from channels and groups you can read.

Free and self-hosted: your own Telegram account via MTProto (Telethon), no paid
scraping service in the loop.

What Telegram actually allows:

  * Broadcast channel subscribers are NOT enumerable unless your account is an
    admin of that channel. The API answers CHAT_ADMIN_REQUIRED. This script does
    not pretend otherwise -- for a channel it falls back to the channel's linked
    discussion group, where the people who comment on posts live.
  * Supergroup member lists are readable while an admin has not hidden them, and
    the API stops handing out rows at roughly 10k per chat. --deep pages the list
    by search prefix to get past part of that ceiling.
  * Message senders, @mentions and "joined the group" service messages are
    always readable in a chat you can read, which is where most of the yield is.

Usage:

    export TG_API_ID=... TG_API_HASH=...        # from my.telegram.org
    python tg_harvest.py --source https://t.me/some_channel --messages 5000

Output lands in --out (default ./out): users.csv, users.jsonl, ghl_import.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, functions, types

# Prefixes used by --deep to page a member list that the API truncates. Latin,
# Cyrillic and digits cover most member names in practice.
SEARCH_PREFIXES = [
    *"abcdefghijklmnopqrstuvwxyz",
    *"0123456789",
    *"абвгдежзийклмнопрстуфхцчшщэюя",
]

CSV_FIELDS = [
    "user_id",
    "username",
    "first_name",
    "last_name",
    "phone",
    "is_bot",
    "is_premium",
    "is_verified",
    "is_deleted",
    "discovery",
    "msg_count",
    "first_seen_utc",
    "last_seen_utc",
    "source_title",
    "source_link",
    "tg_link",
    "bio",
]

GHL_FIELDS = [
    "First Name",
    "Last Name",
    "Phone",
    "Email",
    "Tags",
    "Source",
    "Telegram Username",
    "Telegram ID",
    "Telegram Link",
    "Message Count",
    "Last Active",
]


def utc_iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def tg_link(username: str | None, user_id: int | None) -> str:
    if username:
        return f"https://t.me/{username}"
    if user_id:
        return f"tg://user?id={user_id}"
    return ""


def record_user(store: dict, user, source: dict, discovery: str, seen_at=None) -> dict | None:
    """Merge one user into the store. Returns the stored row, or None if skipped."""
    if not isinstance(user, types.User):
        return None

    key = str(user.id)
    row = store.get(key)
    if row is None:
        row = {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "phone": user.phone or "",
            "is_bot": bool(user.bot),
            "is_premium": bool(getattr(user, "premium", False)),
            "is_verified": bool(getattr(user, "verified", False)),
            "is_deleted": bool(getattr(user, "deleted", False)),
            "discovery": set(),
            "msg_count": 0,
            "first_seen_utc": "",
            "last_seen_utc": "",
            "source_title": source.get("title", ""),
            "source_link": source.get("link", ""),
            "tg_link": tg_link(user.username, user.id),
            "bio": "",
        }
        store[key] = row
    else:
        # Later sightings usually carry a fuller profile than the first one.
        for field, value in (
            ("username", user.username),
            ("first_name", user.first_name),
            ("last_name", user.last_name),
            ("phone", user.phone),
        ):
            if value and not row[field]:
                row[field] = value
        if row["username"]:
            row["tg_link"] = tg_link(row["username"], row["user_id"])

    row["discovery"].add(discovery)
    if discovery == "sender":
        row["msg_count"] += 1

    stamp = utc_iso(seen_at)
    if stamp:
        if not row["first_seen_utc"] or stamp < row["first_seen_utc"]:
            row["first_seen_utc"] = stamp
        if stamp > row["last_seen_utc"]:
            row["last_seen_utc"] = stamp
    return row


def blank_row(source: dict, user_id=None, username: str = "") -> dict:
    return {
        "user_id": user_id or "",
        "username": username,
        "first_name": "",
        "last_name": "",
        "phone": "",
        "is_bot": False,
        "is_premium": False,
        "is_verified": False,
        "is_deleted": False,
        "discovery": set(),
        "msg_count": 0,
        "first_seen_utc": "",
        "last_seen_utc": "",
        "source_title": source.get("title", ""),
        "source_link": source.get("link", ""),
        "tg_link": tg_link(username or None, user_id),
        "bio": "",
    }


def record_user_id(store: dict, user_id: int, source: dict, discovery: str, seen_at=None) -> None:
    """Record a user we only have an id for (join events, mention-by-id)."""
    row = store.get(str(user_id))
    if row is None:
        row = blank_row(source, user_id=user_id)
        store[str(user_id)] = row
    row["discovery"].add(discovery)
    stamp = utc_iso(seen_at)
    if stamp:
        if not row["first_seen_utc"] or stamp < row["first_seen_utc"]:
            row["first_seen_utc"] = stamp
        if stamp > row["last_seen_utc"]:
            row["last_seen_utc"] = stamp


def record_username_only(store: dict, username: str, source: dict) -> None:
    """A @mention whose user id we never saw. Worth keeping, cheap to resolve later."""
    key = f"@{username.lower()}"
    row = store.get(key)
    if row is None:
        row = blank_row(source, username=username)
        store[key] = row
    row["discovery"].add("mention")


def peer_user_id(peer) -> int | None:
    return peer.user_id if isinstance(peer, types.PeerUser) else None


def entity_text(message: str, ent) -> str:
    """Slice an entity out of message text, honouring Telegram's utf-16 offsets."""
    if not message:
        return ""
    buf = message.encode("utf-16-le")
    chunk = buf[ent.offset * 2 : (ent.offset + ent.length) * 2]
    return chunk.decode("utf-16-le", errors="ignore")


def harvest_from_message(store: dict, msg, source: dict) -> None:
    """Pull every user reference out of one message: sender, mentions, join events."""
    seen_at = getattr(msg, "date", None)
    action = getattr(msg, "action", None)

    # A service message ("X joined") has an author but is not a sent message,
    # so it must not count towards msg_count or the "sender" tag.
    if action is None:
        sender = getattr(msg, "sender", None)
        if isinstance(sender, types.User):
            record_user(store, sender, source, "sender", seen_at)
        else:
            # Sender object not cached on the message; the peer id is still useful.
            uid = peer_user_id(getattr(msg, "from_id", None))
            if uid:
                record_user_id(store, uid, source, "sender", seen_at)

    if action is not None:
        for uid in getattr(action, "users", None) or []:
            record_user_id(store, uid, source, "join", seen_at)
        if isinstance(
            action,
            (
                types.MessageActionChatJoinedByLink,
                types.MessageActionChatJoinedByRequest,
                types.MessageActionChatAddUser,
            ),
        ):
            uid = peer_user_id(getattr(msg, "from_id", None))
            if uid:
                record_user_id(store, uid, source, "join", seen_at)

    text = getattr(msg, "message", "") or ""
    for ent in getattr(msg, "entities", None) or []:
        if isinstance(ent, types.MessageEntityMentionName):
            record_user_id(store, ent.user_id, source, "mention", seen_at)
        elif isinstance(ent, types.InputMessageEntityMentionName):
            uid = peer_user_id(getattr(ent, "user_id", None))
            if uid:
                record_user_id(store, uid, source, "mention", seen_at)
        elif isinstance(ent, types.MessageEntityMention):
            handle = entity_text(text, ent).lstrip("@").strip()
            if handle:
                record_username_only(store, handle, source)


async def with_flood_retry(coro_factory, label: str, max_wait: int):
    """Run an awaitable, sleeping through FLOOD_WAIT once if Telegram asks us to."""
    while True:
        try:
            return await coro_factory()
        except errors.FloodWaitError as exc:
            if exc.seconds > max_wait:
                print(f"  ! {label}: FLOOD_WAIT {exc.seconds}s exceeds --max-wait, skipping")
                return None
            print(f"  . {label}: FLOOD_WAIT {exc.seconds}s, sleeping")
            await asyncio.sleep(exc.seconds + 5)


async def collect_participants(client, entity, store, source, deep: bool, max_wait: int) -> int:
    """Member list, where the API allows it. Returns how many rows we got."""
    before = len(store)
    try:
        async for user in client.iter_participants(entity):
            record_user(store, user, source, "participant")
    except errors.ChatAdminRequiredError:
        print("  - member list is admin-only here (expected for broadcast channels)")
        return 0
    except errors.FloodWaitError as exc:
        if exc.seconds > max_wait:
            print(f"  ! FLOOD_WAIT {exc.seconds}s exceeds --max-wait, skipping member list")
            return 0
        print(f"  . FLOOD_WAIT {exc.seconds}s, sleeping")
        await asyncio.sleep(exc.seconds + 5)
    except errors.RPCError as exc:
        print(f"  - member list unavailable: {exc.__class__.__name__}")
        return 0

    if deep:
        print(f"  . deep pass over {len(SEARCH_PREFIXES)} search prefixes")
        for prefix in SEARCH_PREFIXES:
            async def page(prefix=prefix):
                async for user in client.iter_participants(entity, search=prefix):
                    record_user(store, user, source, "participant")
                return True

            await with_flood_retry(page, f"participants[{prefix}]", max_wait)

    return len(store) - before


async def collect_messages(
    client, entity, store, source, limit: int, min_id: int, max_wait: int
) -> tuple[int, int]:
    """Scan message history. Returns (rows added, highest message id seen)."""
    before = len(store)
    top_id = min_id
    scanned = 0
    try:
        async for msg in client.iter_messages(entity, limit=limit, min_id=min_id):
            harvest_from_message(store, msg, source)
            top_id = max(top_id, msg.id or 0)
            scanned += 1
            if scanned % 500 == 0:
                print(f"  . {scanned} messages, {len(store)} users so far")
    except errors.FloodWaitError as exc:
        if exc.seconds <= max_wait:
            print(f"  . FLOOD_WAIT {exc.seconds}s, sleeping then stopping this source")
            await asyncio.sleep(exc.seconds + 5)
        else:
            print(f"  ! FLOOD_WAIT {exc.seconds}s exceeds --max-wait, stopping this source")
    except errors.RPCError as exc:
        print(f"  - history unavailable: {exc.__class__.__name__}")

    print(f"  = scanned {scanned} messages")
    return len(store) - before, top_id


async def enrich_bios(client, store, limit: int, max_wait: int) -> int:
    """Fetch About text for the most active users. One API call each, so capped."""
    candidates = [
        row
        for row in store.values()
        if row["user_id"] and not row["bio"] and not row["is_bot"]
    ]
    candidates.sort(key=lambda r: r["msg_count"], reverse=True)
    done = 0
    for row in candidates[:limit]:
        async def fetch(uid=row["user_id"]):
            return await client(functions.users.GetFullUserRequest(id=uid))

        result = await with_flood_retry(fetch, f"bio[{row['user_id']}]", max_wait)
        if result is not None:
            row["bio"] = (result.full_user.about or "").replace("\n", " ").strip()
            done += 1
        await asyncio.sleep(0.4)
    return done


def write_outputs(store: dict, outdir: Path, include_bots: bool) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    rows = [r for r in store.values() if include_bots or not r["is_bot"]]
    rows.sort(key=lambda r: (-r["msg_count"], str(r["username"] or r["user_id"])))

    with (outdir / "users.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            flat["discovery"] = "|".join(sorted(row["discovery"]))
            writer.writerow(flat)

    with (outdir / "users.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            flat = dict(row)
            flat["discovery"] = sorted(row["discovery"])
            fh.write(json.dumps(flat, ensure_ascii=False) + "\n")

    with (outdir / "ghl_import.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=GHL_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "First Name": row["first_name"],
                    "Last Name": row["last_name"],
                    "Phone": row["phone"],
                    "Email": "",
                    "Tags": "telegram," + "-".join(sorted(row["discovery"])),
                    "Source": row["source_link"] or row["source_title"],
                    "Telegram Username": row["username"],
                    "Telegram ID": row["user_id"],
                    "Telegram Link": row["tg_link"],
                    "Message Count": row["msg_count"],
                    "Last Active": row["last_seen_utc"],
                }
            )

    with_contact = sum(1 for r in rows if r["phone"])
    return {"rows": len(rows), "with_phone": with_contact, "dir": str(outdir)}


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


async def run(args) -> int:
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        print("TG_API_ID and TG_API_HASH must be set (get them at my.telegram.org)")
        return 2

    outdir = Path(args.out)
    state_path = outdir / "state.json"
    outdir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    store: dict = {}

    client = TelegramClient(args.session, int(api_id), api_hash)
    # Telethon sleeps through short waits itself; longer ones surface to us.
    client.flood_sleep_threshold = 60
    await client.start()

    stop = asyncio.Event()

    def on_signal(*_):
        print("\ninterrupted, writing what we have")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, on_signal)

    try:
        for raw in args.source:
            if stop.is_set():
                break
            print(f"\n> {raw}")
            try:
                entity = await client.get_entity(raw)
            except (ValueError, errors.RPCError) as exc:
                print(f"  ! cannot resolve: {exc}")
                continue

            title = getattr(entity, "title", None) or getattr(entity, "username", "") or str(entity.id)
            source = {"title": title, "link": raw}
            is_broadcast = bool(getattr(entity, "broadcast", False))
            kind = "channel" if is_broadcast else "group"
            print(f"  {kind}: {title} (id {entity.id})")

            targets = [(entity, source)]
            if is_broadcast:
                try:
                    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
                    linked_id = getattr(full.full_chat, "linked_chat_id", None)
                    count = getattr(full.full_chat, "participants_count", None)
                    if count:
                        print(f"  {count} subscribers reported by the API")
                    if linked_id:
                        linked = next((c for c in full.chats if c.id == linked_id), None)
                        if linked is not None:
                            linked_title = getattr(linked, "title", str(linked_id))
                            print(f"  + linked discussion group: {linked_title}")
                            targets.append(
                                (
                                    linked,
                                    {"title": linked_title, "link": f"{raw} (discussion)"},
                                )
                            )
                    else:
                        print("  - no linked discussion group; comments are off for this channel")
                except errors.RPCError as exc:
                    print(f"  - channel details unavailable: {exc.__class__.__name__}")

            for target, target_source in targets:
                if stop.is_set():
                    break
                key = f"{target.id}"
                added = await collect_participants(
                    client, target, store, target_source, args.deep, args.max_wait
                )
                if added:
                    print(f"  + {added} users from the member list")

                min_id = state.get(key, {}).get("max_msg_id", 0) if args.resume else 0
                if args.messages > 0:
                    added, top_id = await collect_messages(
                        client, target, store, target_source, args.messages, min_id, args.max_wait
                    )
                    print(f"  + {added} new users from history")
                    if top_id:
                        state.setdefault(key, {})["max_msg_id"] = top_id
                        state[key]["title"] = target_source["title"]
                        state[key]["updated"] = utc_iso(datetime.now(timezone.utc))

        if args.enrich and not stop.is_set():
            print(f"\n> enriching bios (max {args.enrich})")
            print(f"  + {await enrich_bios(client, store, args.enrich, args.max_wait)} bios")
    finally:
        await client.disconnect()

    save_state(state_path, state)
    summary = write_outputs(store, outdir, args.include_bots)
    print(
        f"\n{summary['rows']} users -> {summary['dir']}/users.csv, users.jsonl, ghl_import.csv"
        f"\n{summary['with_phone']} have a phone number visible"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest Telegram users from channels/groups you can read."
    )
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="LINK",
        help="t.me link, @username or numeric id; repeat for several",
    )
    parser.add_argument("--out", default="out", help="output directory (default: out)")
    parser.add_argument(
        "--session", default="tg.session", help="Telethon session file (default: tg.session)"
    )
    parser.add_argument(
        "--messages",
        type=int,
        default=3000,
        help="messages to scan per chat, 0 to skip history (default: 3000)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="page the member list by search prefix to get past the ~10k truncation",
    )
    parser.add_argument(
        "--enrich",
        type=int,
        default=0,
        metavar="N",
        help="fetch About text for the N most active users (one API call each)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="only scan messages newer than the last run (uses out/state.json)",
    )
    parser.add_argument("--include-bots", action="store_true", help="keep bot accounts")
    parser.add_argument(
        "--max-wait",
        type=int,
        default=900,
        help="give up on a step if Telegram asks for a longer FLOOD_WAIT (default: 900s)",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
