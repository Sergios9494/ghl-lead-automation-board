# tg-harvest

Harvest Telegram users into CSV using your own Telegram account. No paid
scraping service, no third party holding your data — just Telethon and the
official MTProto API.

## What Telegram actually permits

This is the part most "Telegram member scraper" repos gloss over:

| Target | Enumerable? |
| --- | --- |
| Broadcast channel subscribers | **No** — `channels.getParticipants` answers `CHAT_ADMIN_REQUIRED` unless your account is an admin of that channel. |
| Supergroup / discussion group members | Yes, until an admin hides the list, and the API stops serving rows at roughly 10k per chat. |
| Message senders, @mentions, join events | Yes, in any chat you can read. This is where most of the yield comes from. |

So for a channel this tool does the thing that works: it finds the channel's
**linked discussion group** (where post comments live) and harvests the people
who actually post there. Those are engaged users rather than passive
subscribers, which is the better lead signal anyway.

If you are an admin of the channel, the member list is read too, automatically.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Get `api_id` / `api_hash` from <https://my.telegram.org> → API development tools,
then:

```bash
export TG_API_ID=1234567
export TG_API_HASH=0123456789abcdef0123456789abcdef
```

First run asks for your phone number and the login code Telegram sends you. It
writes `tg.session` so later runs skip the login.

> `tg.session` is full access to your Telegram account — treat it like a
> password. The bundled `.gitignore` keeps it out of git.

## Use

```bash
# a channel: harvests its linked discussion group's commenters
python tg_harvest.py --source https://t.me/some_channel --messages 5000

# a group: member list + senders + mentions + join events
python tg_harvest.py --source https://t.me/some_group --messages 10000

# several sources at once
python tg_harvest.py --source @chan_one --source @group_two

# get past the ~10k member-list ceiling by paging search prefixes
python tg_harvest.py --source @big_group --deep

# fetch About/bio text for the 200 most active users (1 API call each, slow)
python tg_harvest.py --source @some_group --enrich 200

# later runs: only scan messages newer than last time
python tg_harvest.py --source @some_group --resume
```

## Output

Written to `--out` (default `out/`):

- **`users.csv`** — one row per user: id, username, names, phone (when the user
  exposes it), bot/premium/verified flags, how we found them (`participant`,
  `sender`, `mention`, `join`), message count, first/last seen, source, bio.
- **`users.jsonl`** — same data, one JSON object per line, for piping onward.
- **`ghl_import.csv`** — column names GoHighLevel's contact importer expects.
- **`state.json`** — last message id per chat, so `--resume` works.

### On importing to GoHighLevel

GHL identifies contacts by phone or email, and Telegram gives you **neither**
for most users — a username is not a contact method. Realistically:

- Use `ghl_import.csv` to create contacts with the Telegram username in a custom
  field, and run outreach **inside Telegram** (manually or with a userbot you
  accept the risk of), not as cold email/SMS from GHL.
- The `phone` column is only populated for users who have chosen to make their
  number visible — expect that to be a small single-digit percentage.
- `msg_count` and `last_seen_utc` are your best lead-scoring columns. Sort by
  them before doing anything with the list.

## Rate limits

Telegram answers with `FLOOD_WAIT` when you pull too fast. Telethon sleeps
through short waits itself; this script sleeps through longer ones up to
`--max-wait` (default 900s) and then skips that step rather than hanging.

`--deep` and `--enrich` are the two expensive modes. Run them on their own, not
on five sources at once, and prefer a second run later over pushing through a
long flood wait.

## Tests

The extraction logic (sender/mention/join parsing, utf-16 entity offsets, dedupe
and merge, CSV writing, bot filtering) runs without a Telegram account:

```bash
python test_harvest.py
```

## Legal

Scraping member data is against Telegram's Terms of Service, and member
profiles are personal data under GDPR/CCPA — a harvested list is not a lawful
basis for unsolicited outreach on its own. Mass-inviting or mass-DMing harvested
users is the fastest route to `PeerFlood` and losing the account. Know which
side of that line you are on before running this at scale.
