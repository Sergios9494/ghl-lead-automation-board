import csv, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tg_harvest as th
from telethon import types


class Msg:
    """Duck-typed stand-in for a Telethon Message."""
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.date = kw.get("date")
        self.sender = kw.get("sender")
        self.from_id = kw.get("from_id")
        self.message = kw.get("message", "")
        self.entities = kw.get("entities", [])
        self.action = kw.get("action")


src = {"title": "Test Chat", "link": "https://t.me/test"}
store = {}
t1 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
t2 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

ann = types.User(id=1, first_name="Ann", last_name="A", username="ann", bot=False, premium=True)
bo = types.User(id=2, first_name="Bo", username=None, bot=False)
botty = types.User(id=3, first_name="Botty", username="botty_bot", bot=True)

# emoji before the mention: proves utf-16 offset handling
text = "hi 👋 @carol and @dave"
off_carol = len("hi 👋 ".encode("utf-16-le")) // 2
off_dave = len("hi 👋 @carol and ".encode("utf-16-le")) // 2
ents = [
    types.MessageEntityMention(offset=off_carol, length=6),
    types.MessageEntityMention(offset=off_dave, length=5),
]

th.harvest_from_message(store, Msg(sender=ann, date=t1, message=text, entities=ents), src)
th.harvest_from_message(store, Msg(sender=ann, date=t2, message="again"), src)          # 2nd msg
th.harvest_from_message(store, Msg(sender=bo, date=t1, message="yo"), src)
th.harvest_from_message(store, Msg(sender=botty, date=t1, message="beep"), src)
th.harvest_from_message(store, Msg(from_id=types.PeerUser(user_id=9), date=t1), src)    # sender not cached
th.harvest_from_message(store, Msg(date=t2, action=types.MessageActionChatAddUser(users=[77]),
                                   from_id=types.PeerUser(user_id=88)), src)
th.harvest_from_message(store, Msg(date=t2, message="cc", entities=[
    types.MessageEntityMentionName(offset=0, length=2, user_id=55)]), src)

# incomplete profile first, fuller profile later -> fields should fill in
th.record_user(store, types.User(id=100, first_name="Eve", username=None, bot=False), src, "participant")
th.record_user(store, types.User(id=100, first_name="Eve", username="eve_h", phone="15551234", bot=False),
               src, "sender", t2)

def row(key):
    return store[str(key)]

assert row(1)["msg_count"] == 2, row(1)["msg_count"]
assert row(1)["is_premium"] is True
assert row(1)["first_seen_utc"] == "2026-10-01T12:00:00Z"
assert row(1)["last_seen_utc"] == "2026-10-05T12:00:00Z"
assert store["@carol"]["username"] == "carol", store["@carol"]
assert store["@dave"]["username"] == "dave", store["@dave"]
assert row(9)["discovery"] == {"sender"}
assert row(77)["discovery"] == {"join"} and row(88)["discovery"] == {"join"}
assert row(55)["discovery"] == {"mention"}
assert row(100)["username"] == "eve_h" and row(100)["phone"] == "15551234"
assert row(100)["tg_link"] == "https://t.me/eve_h"
assert row(100)["discovery"] == {"participant", "sender"}

out = Path(tempfile.mkdtemp(prefix="tg-harvest-test-"))
summary = th.write_outputs(store, out, include_bots=False)
users = list(csv.DictReader((out / "users.csv").open(encoding="utf-8")))
ghl = list(csv.DictReader((out / "ghl_import.csv").open(encoding="utf-8")))
jsonl = (out / "users.jsonl").read_text(encoding="utf-8").strip().splitlines()

assert len(users) == len(store) - 1, f"{len(users)} vs {len(store)-1} (bot should be filtered)"
assert len(ghl) == len(users) == len(jsonl)
assert not any(u["username"] == "botty_bot" for u in users)
assert users[0]["username"] == "ann", users[0]          # sorted by msg_count desc
assert summary["with_phone"] == 1
ann_ghl = next(g for g in ghl if g["Telegram ID"] == "1")
assert ann_ghl["Tags"] == "telegram,sender" and ann_ghl["First Name"] == "Ann"

with_bots = th.write_outputs(store, out, include_bots=True)
assert with_bots["rows"] == len(store)

print(f"all assertions passed: {len(store)} users in store, {summary['rows']} written, bot filtered")
print("csv header:", ",".join(users[0].keys()))
print("sample row:", {k: v for k, v in users[0].items() if v})
