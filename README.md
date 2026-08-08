# most-tg-bot

A Telegram watcher for the [MOST](https://most.devnads.com) contribution pool (Monad Open Source
Track). One Python file, standard library only. Polls the pool repositories and tells you the
moment something needs your attention — a claim on an issue, a review on your PR, a slot opening
up — so you are not refreshing GitHub tabs to find work before someone else does.

Built and run by a MOST participant. This is the tool we actually use, minus our config.

## Why

Pool issues get taken within minutes, and the pool is **seven repositories**, not one:

```
portdeveloper/nad-agent      portdeveloper/monad-monitor   portdeveloper/puddleswap
portdeveloper/mipland        haythemsellami/mpamm.wtf      nishuzumi/moss
therealharpaljadeja/knot
```

Watching all of them by hand is how you learn an issue was claimed the morning after.

## What it sends

- 🆕 **new issue** — with ⭐ when it is `good first issue`
- 🔒 **claimed** — by whom
- 🟢 **freed** — a claim was released and the slot is open again (the alert worth waking for)
- ✋ **claim requested** — someone commented a claim on a free issue, the earliest signal there
  is: the assignee only appears once a maintainer approves, and by then it is decided
- 📤 **PR opened for review** — by whom, anywhere in the pool: a sign that contributor is
  about to earn a contribution day and free their claimed issue
- ✅ **closed**
- 🎉 **your PR merged** · 💬 **new comment on your PR** · ⛔ **your PR went conflict-dirty**

Commands: `/free` (unclaimed issues, by repo, with difficulty) · `/taken` (who holds what) ·
`/pool` (per-repo counts) · `/pr` (the PR you are watching, and its CI) · `/rate` (GitHub quota).

## Reading a claim correctly is the hard part

A claim is a plain-English comment, so the naive "does it contain the word claim" is wrong both
ways, and both ways happen in the live pool:

- **"Not claiming this one"** is *not* a claim, even though it says "claim".
- **"I'd like to take this"**, **"can I take this?"**, **"i want ti tackle"** (a real typo in an
  actual claim) *are* claims, even though they never say "claim".

So negations veto first; the verb is matched with typo tolerance; the maintainer's bookkeeping
("one claimed issue at a time", "approved, it's yours") is excluded; and a release
("releasing this claim") cancels an older claim, because after it the issue is free again.

## Config

Copy `config.env.example` to `config.env`, `chmod 600`, fill in on the server. Never paste real
tokens into a chat or a commit.

| Key | |
|---|---|
| `TG_TOKEN`, `TG_CHAT_ID` | Telegram bot token and the chat to alert |
| `GH_TOKEN` | GitHub PAT, **public read only**, no scopes needed. Optional but strongly advised: without it GitHub allows 60 requests/hour, which forces a 10-minute poll across seven repos. With it the poll is 60s. |
| `WATCH_PR` | a PR to watch, `owner/repo#number` |
| `MY_CLAIM` | your own claimed issue, `owner/repo#number` — kept out of the "someone is claiming this" alerts and marked as yours in `/taken` |
| `MAINTAINERS` | comma-separated logins whose claim bookkeeping to ignore (defaults to the pool admin) |

## Run

```
cp config.env.example config.env    # fill in, chmod 600
python3 bot.py                        # foreground
# or as a service:
sudo cp most-tg-bot.service /etc/systemd/system/ && sudo systemctl enable --now most-tg-bot
```

The first poll seeds state silently — it does not replay every existing issue as an alert.

## Notes

- Pure standard library: `urllib`, `json`, `ssl`. No dependencies to install.
- State (last snapshot, seen events, Telegram offset) lives in `state.json`, written atomically.
- The pool list is explicit in the source rather than scraped, so a marketing-page change cannot
  silently leave the bot watching nothing while looking healthy.
