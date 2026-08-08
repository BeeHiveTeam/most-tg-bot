#!/usr/bin/env python3
"""
most-tg-bot — Telegram watcher for the MOST pool repositories.

Why it exists: pool issues are taken within minutes. On 2026-08-02 an issue we had prepared
code for was claimed and approved in seven minutes, and another was requested three minutes
after the requester's previous PR merged. A claim slot is worth nothing if you learn about it
the next morning.

Pure stdlib (urllib, json). Single-threaded loop:
  - every POLL_INTERVAL diffs each repo's issues against the last snapshot and pushes alerts
  - long-polls Telegram getUpdates for commands (/free, /pool, /pr, /rate, /help)

Config: <dir>/config.env   State: <dir>/state.json
"""
import json, os, ssl, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.env")
STATE_PATH = os.path.join(HERE, "state.json")

# The pool, from https://most.devnads.com/. Kept explicit rather than scraped: the site is
# marketing HTML, and a silent parse failure would leave the bot watching nothing while
# looking healthy.
DEFAULT_REPOS = [
    "portdeveloper/nad-agent",
    "portdeveloper/monad-monitor",
    "portdeveloper/puddleswap",
    "portdeveloper/mipland",
    "haythemsellami/mpamm.wtf",
    "nishuzumi/moss",
    "therealharpaljadeja/knot",
]

import re

# A comment is a claim heads-up. Two failure modes, both seen in the live pool:
#
#   - false positive: "Not claiming this one" opens with the word "claim", and a naive
#     substring match flagged it. So negations are checked FIRST and veto the whole comment.
#   - false miss: real claims came as "I'd like to take this", "can I take this?", "i want ti
#     tackle" (a typo in lora-sys's actual claim on #19), "I'll pick this up" — none of which
#     the old fixed-string list caught. The patterns below are typo-tolerant on the verb.
#
# This is a heads-up, not an authority: the maintainer's assignment is the real signal. Erring
# toward catching more is fine as long as an explicit negation still wins.

# Explicit "not claiming" — these veto a match no matter what else the comment says.
CLAIM_NEGATIONS = (
    "not claiming", "not a claim", "won't claim", "will not claim", "no claim",
    "not taking this", "neither of those was a claim", "not claiming this",
)

# Verb phrases that read as taking an issue. \S{0,2} after the verb tolerates a typo
# ("ti tackle") without matching unrelated words.
CLAIM_PATTERNS = tuple(re.compile(p, re.I) for p in (
    # "claim/claiming", but NOT the maintainer's "claim approved / registered / assigned",
    # which are about someone else's claim, not a new one.
    # "claim"/"claiming" as taking THIS issue. Not "claimed" (past participle — appears in
    # the maintainer's boilerplate "one claimed issue at a time"), and not "claim approved/
    # registered/assigned".
    r"\bclaim(?:ing)?\s+(?:this|it|#?\d)",
    r"^\s*claiming\b",
    r"\btak(?:ing|e|e\s+up)?\s+(?:this|it|#?\d)",    # taking this / take this / take it / take #12
    r"\bi(?:'|)?ll\s+tak",                            # I'll take
    r"\b(?:want|wanna|like|wish|plan)\s+to\s+\S{0,3}\s*(?:tak|tackl|work|pick|grab|do)",
    r"\bwant\s+\S{0,2}\s+tackl",                     # "want ti tackle" (typo)
    r"\b(?:can|may|could)\s+i\s+\S{0,4}\s*(?:tak|hav|grab|do)\b",  # can I take
    r"\bi(?:'|)?ll\s+pick\s+(?:this|it)\s*(?:up)?",  # I'll pick this up / pick it
    r"\bpick(?:ing)?\s+(?:this|it)\s+up\b",
    r"\bi\s+(?:want|wanna|wish)\s+to\s+work\b",
    r"\bgrabbing\s+(?:this|it)\b",
    r"\bmine\b.*\bnow\b",
))


def is_claim(body):
    """True if a comment reads as taking the issue. An explicit negation always wins."""
    b = (body or "").lower()
    if any(neg in b for neg in CLAIM_NEGATIONS):
        return False
    return any(p.search(b) for p in CLAIM_PATTERNS)


# A claim is cancelled either by the holder ("releasing this") or by the maintainer reclaiming
# the slot ("Releasing this claim ..."). After a release the issue is free again, so a claim
# older than the newest release does not count.
CLAIM_RELEASES = ("releasing this", "releasing the claim", "releasing this claim",
                  "released this", "release this claim", "open for anyone again",
                  "open again", "up for anyone")


def is_release(body):
    b = (body or "").lower()
    return any(r in b for r in CLAIM_RELEASES)

UA = "most-tg-bot (+https://github.com/BeeHiveTeam)"


# ── config ──────────────────────────────────────────────────────────────────

def load_cfg():
    """Read config.env. Missing file is fatal: a bot with no chat id alerts no one."""
    cfg = {}
    if not os.path.exists(CFG_PATH):
        sys.exit(f"config not found: {CFG_PATH}")
    with open(CFG_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for required in ("TG_TOKEN", "TG_CHAT_ID"):
        if not cfg.get(required):
            sys.exit(f"{required} is not set in {CFG_PATH}")
    return cfg


CFG = load_cfg()
TG_TOKEN = CFG["TG_TOKEN"]
TG_CHAT = CFG["TG_CHAT_ID"]
GH_TOKEN = CFG.get("GH_TOKEN", "")
REPOS = [r.strip() for r in CFG.get("REPOS", ",".join(DEFAULT_REPOS)).split(",") if r.strip()]
WATCH_PR = CFG.get("WATCH_PR", "").strip()
# Pool maintainers post claim bookkeeping ("approved, it's yours", "one claimed issue at a
# time") that can read like a claim. Their comments are never a fresh claim. Comma-separated
# logins; defaults to the pool's admin.
MAINTAINERS = {m.strip().lower() for m in CFG.get("MAINTAINERS", "portdeveloper").split(",") if m.strip()}          # e.g. portdeveloper/nad-agent#42
# Our own claim, "owner/repo#number". The maintainer approves in a comment and does not always
# set an assignee — issue #2 is ours and shows no assignee — so without this the watcher would
# list our own work as free.
MY_CLAIM = CFG.get("MY_CLAIM", "").strip()
# Without a token GitHub allows 60 requests/hour, and one poll costs one request per repo.
# 7 repos every 10 minutes is 42/hour, which fits with headroom for the /commands. With a
# token the ceiling is 5000/hour and a one-minute poll is comfortable.
POLL_INTERVAL = int(CFG.get("POLL_INTERVAL", "60" if GH_TOKEN else "600"))

CTX = ssl.create_default_context()


# ── state ───────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)  # atomic: a crash mid-write must not leave a truncated file


# ── http ────────────────────────────────────────────────────────────────────

def http_json(url, headers=None, timeout=25):
    """GET JSON. Returns (data, error). Never raises: a poll failure must not kill the loop."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return None, f"HTTP {e.code} {body}"
    except Exception as e:
        return None, str(e)


def gh(path):
    headers = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    return http_json("https://api.github.com" + path, headers)


def tg(method, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=35, context=CTX) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"telegram {method} failed: {e}", flush=True)
        return None


KEYBOARD = json.dumps({"inline_keyboard": [
    [{"text": "🟢 Свободные", "callback_data": "free"},
     {"text": "🔒 Занятые", "callback_data": "taken"}],
    [{"text": "📊 Сводка", "callback_data": "pool"},
     {"text": "🔀 Наш PR", "callback_data": "pr"}],
    [{"text": "📈 Квота", "callback_data": "rate"},
     {"text": "❓ Помощь", "callback_data": "help"}],
]})


def say(text, kb=True):
    """Отправить сообщение. Кнопки цепляются к последней части длинного текста."""
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for n, chunk in enumerate(chunks):
        params = dict(chat_id=TG_CHAT, text=chunk, parse_mode="HTML",
                      disable_web_page_preview="true")
        if kb and n == len(chunks) - 1:
            params["reply_markup"] = KEYBOARD
        tg("sendMessage", **params)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── github ──────────────────────────────────────────────────────────────────

def fetch_issues(repo):
    """Open issues only, pull requests filtered out. Returns (list, error)."""
    data, err = gh(f"/repos/{repo}/issues?state=open&per_page=100")
    if err:
        return None, err
    return [i for i in data if "pull_request" not in i], None


def snapshot(issues):
    """The fields whose change is worth a message."""
    return {
        str(i["number"]): {
            "title": i["title"],
            "assignee": (i.get("assignee") or {}).get("login"),
            "labels": sorted(l["name"] for l in i["labels"]),
            "comments": i["comments"],
        }
        for i in issues
    }


def is_gfi(labels):
    return any("good first issue" in l.lower() for l in labels)


def issue_url(repo, num):
    return f"https://github.com/{repo}/issues/{num}"


def latest_claim_comment(repo, num):
    """The newest comment that reads like a claim, or None. One request; used sparingly."""
    data, err = gh(f"/repos/{repo}/issues/{num}/comments?per_page=100")
    if err or not data:
        return None
    # Skip the pool maintainer's bookkeeping. When a claim is approved, the maintainer posts a
    # confirmation carrying an invisible "<!-- most-claims -->" marker ("the claim is
    # registered", "you already hold a claim on ..."). Those read like a claim but announce
    # someone else's, so a marker comment is never itself a fresh claim.
    for c in reversed(data):
        body = c.get("body") or ""
        login = (c.get("user") or {}).get("login", "")
        # Walking newest-first: a release means everything older is stale, so stop — the issue
        # is free regardless of any earlier "taking this".
        if is_release(body):
            return None
        # A maintainer's confirmation carries "<!-- most-claims -->", but their plain-text
        # approvals ("approved, it's yours") do not — skip the maintainer either way.
        if "<!-- most-claims -->" in body or login.lower() in MAINTAINERS:
            continue
        if is_claim(body):
            return login, body[:180]
    return None


# ── diffing ─────────────────────────────────────────────────────────────────

def diff_repo(repo, old, new):
    """Alerts for one repo. `old` empty means first sight — seed silently, do not spam."""
    if not old:
        return []
    out = []
    for num, cur in new.items():
        prev = old.get(num)
        tag = "★ good first issue" if is_gfi(cur["labels"]) else ", ".join(cur["labels"][:2])
        link = issue_url(repo, num)
        if prev is None:
            out.append(f"🆕 <b>НОВАЯ ЗАДАЧА</b> {esc(short(repo))} #{num}\n{esc(cur['title'])}\n<i>{esc(tag)}</i>\n{link}")
            continue
        if prev["assignee"] != cur["assignee"]:
            if cur["assignee"] is None:
                # The one alert worth waking up for: a claim was released and the slot is open.
                out.append(f"🟢 <b>ОСВОБОДИЛАСЬ</b> {esc(short(repo))} #{num} — была у {esc(prev['assignee'])}\n"
                           f"{esc(cur['title'])}\n<i>{esc(tag)}</i>\n{link}")
            else:
                out.append(f"🔒 <b>ЗАНЯЛИ</b> — {esc(cur['assignee'])}, {esc(short(repo))} #{num}\n"
                           f"{esc(cur['title'])}\n{link}")
    for num, prev in old.items():
        if num not in new:
            out.append(f"✅ <b>ЗАКРЫТА</b> {esc(short(repo))} #{num}\n{esc(prev['title'])}\n{issue_url(repo, num)}")
    return out


def diff_comments(repo, old, new, budget):
    """
    Someone commenting on a free issue is the earliest signal there is — the assignee only
    appears once a maintainer approves, and by then it is decided. Costs one request per
    changed issue, so it is capped by `budget`.
    """
    out = []
    for num, cur in new.items():
        if budget[0] <= 0:
            break
        prev = old.get(num)
        if not prev or cur["comments"] <= prev["comments"] or cur["assignee"]:
            continue
        # Our own claim is ours; a new comment on it (a maintainer reply, our own progress
        # note) must not fire "someone is claiming this".
        if mine(repo, num):
            continue
        budget[0] -= 1
        hit = latest_claim_comment(repo, num)
        if hit:
            who, text = hit
            out.append(f"✋ <b>ПРОСЯТ ЗАЯВКУ</b> — {esc(who)}, {esc(short(repo))} #{num}\n"
                       f"{esc(cur['title'])}\n<i>{esc(text)}</i>\n{issue_url(repo, num)}")
    return out


def check_pr(state):
    """Watch one pull request of ours: reviews, comments, merge, CI."""
    if not WATCH_PR or "#" not in WATCH_PR:
        return []
    repo, num = WATCH_PR.split("#", 1)
    pr, err = gh(f"/repos/{repo}/pulls/{num}")
    if err:
        return []
    cur = {
        "state": pr["state"],
        "merged": bool(pr.get("merged_at")),
        "comments": pr["comments"] + pr["review_comments"],
        "mergeable": pr.get("mergeable"),
    }
    prev = state.get("pr")
    state["pr"] = cur
    if prev is None:
        return []
    out = []
    link = f"https://github.com/{repo}/pull/{num}"
    if cur["merged"] and not prev["merged"]:
        out.append(f"🎉 <b>PR СМЕРЖЕН</b> {esc(WATCH_PR)}\n{link}\n\n"
                   f"⚡ Слот заявки свободен. Заявляться <b>сейчас</b> — задачи разбирают за минуты.")
    elif cur["state"] != prev["state"]:
        out.append(f"⚠️ <b>PR {esc(cur['state'].upper())}</b> {esc(WATCH_PR)}\n{link}")
    if cur["comments"] > prev["comments"]:
        out.append(f"💬 <b>Комментариев на PR: +{cur['comments'] - prev['comments']}</b> {esc(WATCH_PR)}\n{link}")
    if prev["mergeable"] and cur["mergeable"] is False:
        out.append(f"⛔ <b>КОНФЛИКТЫ</b> на {esc(WATCH_PR)} — upstream ушёл вперёд, нужен ребейз\n{link}")
    return out


# ── commands ────────────────────────────────────────────────────────────────

DIFFICULTY = ("good first issue", "difficulty:beginner", "beginner",
              "intermediate", "difficulty:intermediate", "advanced", "difficulty:advanced")


def difficulty(labels):
    """The one label a human picks by. Empty when the repo does not label difficulty."""
    for l in labels:
        low = l.lower()
        for d in DIFFICULTY:
            if low == d:
                return d.replace("difficulty:", "")
    return ""


def short(repo):
    return repo.split("/", 1)[1]


def mine(repo, num):
    return MY_CLAIM and MY_CLAIM == f"{repo}#{num}"


def cmd_free(state):
    """Every unclaimed issue, grouped by repo, with difficulty and a link."""
    blocks, total = [], 0
    for repo in REPOS:
        snap = state.get("repos", {}).get(repo, {})
        free = [(n, v) for n, v in snap.items() if not v["assignee"] and not mine(repo, n)]
        if not free:
            continue
        total += len(free)
        rows = [f"\n<b>{esc(short(repo))}</b>  (свободно: {len(free)})"]
        for n, v in sorted(free, key=lambda x: int(x[0])):
            d = difficulty(v["labels"])
            star = "⭐ " if is_gfi(v["labels"]) else "• "
            tag = f" <i>[{esc(d)}]</i>" if d and not is_gfi(v["labels"]) else ""
            rows.append(f'{star}<a href="{issue_url(repo, n)}">#{n}</a> {esc(v["title"][:64])}{tag}')
        blocks.append("\n".join(rows))
    head = f"<b>🟢 СВОБОДНО — {total} задач</b>\n⭐ = good first issue"
    if MY_CLAIM:
        head += f"\n<i>наша заявка {esc(MY_CLAIM)} в список не входит</i>"
    return head + "\n" + "\n".join(blocks) if blocks else head + "\n\nСвободных задач нет."


def cmd_taken(state):
    """Who holds what. A held issue frees up when its holder's PR lands, so it is a watch list."""
    blocks, total = [], 0
    for repo in REPOS:
        snap = state.get("repos", {}).get(repo, {})
        held = [(n, v) for n, v in snap.items() if v["assignee"] or mine(repo, n)]
        if not held:
            continue
        total += len(held)
        rows = [f"\n<b>{esc(short(repo))}</b>"]
        for n, v in sorted(held, key=lambda x: int(x[0])):
            who = "МЫ" if mine(repo, n) else esc(v["assignee"])
            mark = "🟡" if mine(repo, n) else "🔒"
            rows.append(f'{mark} <a href="{issue_url(repo, n)}">#{n}</a> <b>{who}</b> — {esc(v["title"][:52])}')
        blocks.append("\n".join(rows))
    return f"<b>🔒 ЗАНЯТО — {total} задач</b>\n" + "\n".join(blocks)


def cmd_pool(state):
    """One line per repo: how much is open, how much of it is up for grabs."""
    rows, t_open, t_free, t_gfi = [], 0, 0, 0
    for repo in REPOS:
        snap = state.get("repos", {}).get(repo, {})
        free = [v for n, v in snap.items() if not v["assignee"] and not mine(repo, n)]
        gfi = sum(1 for v in free if is_gfi(v["labels"]))
        t_open += len(snap); t_free += len(free); t_gfi += gfi
        bar = "🟢" if free else "⚪"
        rows.append(f'{bar} <b>{esc(short(repo))}</b> — {len(free)} из {len(snap)}'
                    + (f' · ⭐{gfi}' if gfi else ""))
    head = (f"<b>Пул MOST</b>\nСвободно {t_free} из {t_open} открытых"
            + (f" · ⭐{t_gfi} good-first" if t_gfi else "") + "\n")
    return head + "\n".join(rows)


def cmd_pr():
    if not WATCH_PR or "#" not in WATCH_PR:
        return "PR не отслеживается (задайте WATCH_PR в config.env)."
    repo, num = WATCH_PR.split("#", 1)
    pr, err = gh(f"/repos/{repo}/pulls/{num}")
    if err:
        return f"не удалось прочитать {esc(WATCH_PR)}: {esc(err)}"
    checks, _ = gh(f"/repos/{repo}/commits/{pr['head']['sha']}/check-runs")
    n_checks = (checks or {}).get("total_count", 0)
    runs = ", ".join(f"{c['name']}: {c['conclusion'] or c['status']}"
                     for c in (checks or {}).get("check_runs", []))
    ci = runs if n_checks else "не запускался — ждём кнопки мейнтейнера (первый PR из форка)"
    merge = {True: "конфликтов нет", False: "⛔ КОНФЛИКТЫ", None: "GitHub ещё считает"}
    return (f"<b>PR {esc(WATCH_PR)}</b>\n"
            f"состояние: {esc(pr['state'])}{' · 🎉 СМЕРЖЕН' if pr.get('merged_at') else ''}\n"
            f"слияние: {esc(merge.get(pr.get('mergeable'), pr.get('mergeable')))}\n"
            f"комментариев: {pr['comments'] + pr['review_comments']}\n"
            f"CI: {esc(ci)}\n"
            f"https://github.com/{repo}/pull/{num}")


def cmd_rate():
    data, err = gh("/rate_limit")
    if err:
        return f"rate_limit failed: {esc(err)}"
    c = data["resources"]["core"]
    left = max(0, c["reset"] - int(time.time()))
    return (f"<b>Квота GitHub</b>\n"
            f"осталось {c['remaining']} из {c['limit']}, сброс через {left // 60} мин {left % 60} с\n"
            f"токен: {'есть' if GH_TOKEN else 'НЕТ — потолок 60/час'}\n"
            f"опрос: раз в {POLL_INTERVAL} с по {len(REPOS)} репозиториям")


HELP = ("<b>Наблюдатель за пулом MOST</b>\n"
        "Следит за 7 репозиториями и сам присылает:\n"
        "🆕 новая задача · 🔒 задачу заняли · 🟢 <b>задача освободилась</b>\n"
        "✋ кто-то просит заявку · ✅ закрыта\n"
        "🎉 наш PR смержен · 💬 комментарий на PR · ⛔ конфликты\n\n"
        "<b>Команды</b>\n"
        "/free — что свободно, по репозиториям\n"
        "/taken — кто что держит\n"
        "/pool — сводка по репозиториям\n"
        "/pr — наш PR и состояние CI\n"
        "/rate — квота GitHub\n"
        "/help — это сообщение")


def dispatch(cmd, state):
    """Одно место, откуда отвечают и команды, и кнопки — иначе они разъезжаются."""
    if cmd.startswith("free"):
        return cmd_free(state)
    if cmd.startswith("taken"):
        return cmd_taken(state)
    if cmd.startswith("pool"):
        return cmd_pool(state)
    if cmd.startswith("pr"):
        return cmd_pr()
    if cmd.startswith("rate"):
        return cmd_rate()
    return HELP


def handle_commands(state):
    """
    Разбор апдейтов Telegram: команды и нажатия кнопок.

    allowed_updates обязан содержать callback_query — иначе нажатия просто не доставляются,
    и бот выглядит исправным, отвечая только на текст. Смещение сохраняется, чтобы рестарт
    не проигрывал старые команды заново.
    """
    offset = state.get("tg_offset", 0)
    res = tg("getUpdates", offset=offset + 1, timeout=0, limit=20,
             allowed_updates='["message","callback_query"]')
    if not res or not res.get("ok"):
        return
    for upd in res["result"]:
        state["tg_offset"] = max(state.get("tg_offset", 0), upd["update_id"])

        cb = upd.get("callback_query")
        if cb:
            # Гасим «часики» первым делом: Telegram считает запрос протухшим через 10-15 с,
            # а сбор ответа по семи репозиториям может занять дольше.
            tg("answerCallbackQuery", callback_query_id=cb.get("id", ""))
            chat = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
            if chat != str(TG_CHAT):
                continue
            data = (cb.get("data") or "").lower()
            # Дебаунс: то же нажатие не чаще раза в 5 с — гасит серию тапов по одной кнопке.
            now = time.time()
            last = state.get("cb_last", {})
            if now - last.get(data, 0) < 5:
                continue
            last[data] = now
            state["cb_last"] = last
            say(dispatch(data, state))
            continue

        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(TG_CHAT):
            continue
        text = (msg.get("text") or "").strip().split("@")[0].lower().lstrip("/")
        if text:
            say(dispatch(text, state))


# ── main ────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    state.setdefault("repos", {})
    first_run = not state["repos"]
    if first_run:
        print("first run: seeding state without alerts", flush=True)

    while True:
        started = time.time()
        alerts = []
        # One claim-comment lookup per cycle at most when running tokenless, so a busy repo
        # cannot exhaust the hourly quota and blind the watcher entirely.
        budget = [8 if GH_TOKEN else 1]

        for repo in REPOS:
            issues, err = fetch_issues(repo)
            if err:
                print(f"{repo}: {err}", flush=True)
                if "rate limit" in err.lower() or "403" in err:
                    break
                continue
            new = snapshot(issues)
            old = state["repos"].get(repo, {})
            alerts += diff_repo(repo, old, new)
            if old:
                alerts += diff_comments(repo, old, new, budget)
            state["repos"][repo] = new

        alerts += check_pr(state)

        if alerts and not first_run:
            say("\n\n".join(alerts))
        first_run = False
        save_state(state)

        # Answer commands while waiting, so /free is not stuck behind a long poll interval.
        while time.time() - started < POLL_INTERVAL:
            try:
                handle_commands(state)
                save_state(state)
            except Exception as e:
                print(f"command loop: {e}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
