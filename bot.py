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
                  "released this claim", "release this claim", "releasing my claim")


def is_release(body):
    b = (body or "").lower()
    return any(r in b for r in CLAIM_RELEASES)

UA = "most-tg-bot (+https://github.com/BeeHiveTeam)"


# ── i18n ──────────────────────────────────────────────────────────────────────
# One chat per bot, so language is a single setting, not per-user. Default English for a
# public repo; set LANG=ru in config.env (or tap the 🌐 button) for Russian.
DEFAULT_LANG = "en"
_lang = DEFAULT_LANG  # set from state at the start of each poll cycle and each command

T = {
  "btn_free":   {"en": "🟢 Free",    "ru": "🟢 Свободные",  "de": "🟢 Frei"},
  "btn_taken":  {"en": "🔒 Taken",   "ru": "🔒 Занятые",    "de": "🔒 Vergeben"},
  "btn_pool":   {"en": "📊 Summary", "ru": "📊 Сводка",     "de": "📊 Übersicht"},
  "btn_pr":     {"en": "🔀 Our PR",  "ru": "🔀 Наш PR",     "de": "🔀 Unser PR"},
  "btn_rate":   {"en": "📈 Quota",   "ru": "📈 Квота",      "de": "📈 Kontingent"},
  "btn_help":   {"en": "❓ Help",     "ru": "❓ Помощь",     "de": "❓ Hilfe"},
  "btn_lang":   {"en": "🌐 EN",      "ru": "🌐 RU",         "de": "🌐 DE"},  # current language

  "a_new":      {"en": "🆕 <b>NEW ISSUE</b>", "ru": "🆕 <b>НОВАЯ ЗАДАЧА</b>", "de": "🆕 <b>NEUES ISSUE</b>"},
  "a_claimed":  {"en": "🔒 <b>CLAIMED</b> by", "ru": "🔒 <b>ЗАНЯЛИ</b> —", "de": "🔒 <b>VERGEBEN</b> an"},
  "a_claim_lost":{"en": "🚨 <b>YOUR CLAIM WAS REASSIGNED</b> to",
                  "ru": "🚨 <b>НАШУ ЗАЯВКУ ПЕРЕДАЛИ</b> —",
                  "de": "🚨 <b>UNSER ANSPRUCH WURDE ÜBERTRAGEN</b> an"},
  "a_freed":    {"en": "🟢 <b>FREED</b>", "ru": "🟢 <b>ОСВОБОДИЛАСЬ</b>", "de": "🟢 <b>FREI</b>"},
  "a_freed_was":{"en": "— was", "ru": "— была у", "de": "— war bei"},
  "a_closed":   {"en": "✅ <b>CLOSED</b>", "ru": "✅ <b>ЗАКРЫТА</b>", "de": "✅ <b>GESCHLOSSEN</b>"},
  "a_claimreq": {"en": "✋ <b>CLAIM REQUESTED</b> by", "ru": "✋ <b>ПРОСЯТ ЗАЯВКУ</b> —", "de": "✋ <b>ANSPRUCH GEMELDET</b> von"},
  "a_prreview": {"en": "📤 <b>PR FOR REVIEW</b> by", "ru": "📤 <b>PR НА РЕВЬЮ</b> —", "de": "📤 <b>PR ZUR PRÜFUNG</b> von"},
  "a_merged":   {"en": "🎉 <b>PR MERGED</b>", "ru": "🎉 <b>PR СМЕРЖЕН</b>", "de": "🎉 <b>PR GEMERGT</b>"},
  "a_merged_note":{"en": "⚡ Claim slot is free. Claim the next one <b>now</b> — issues go in minutes.",
                   "ru": "⚡ Слот заявки свободен. Заявляться <b>сейчас</b> — задачи разбирают за минуты.",
                   "de": "⚡ Anspruchsplatz ist frei. Jetzt das nächste <b>sofort</b> beanspruchen — Issues gehen in Minuten weg."},
  "a_prstate":  {"en": "⚠️ <b>PR {st}</b>", "ru": "⚠️ <b>PR {st}</b>", "de": "⚠️ <b>PR {st}</b>"},
  "a_prcomm":   {"en": "💬 <b>PR comments: +{n}</b>", "ru": "💬 <b>Комментариев на PR: +{n}</b>", "de": "💬 <b>PR-Kommentare: +{n}</b>"},
  "a_conflict": {"en": "⛔ <b>CONFLICTS</b> on {pr} — upstream moved, rebase needed",
                 "ru": "⛔ <b>КОНФЛИКТЫ</b> на {pr} — upstream ушёл вперёд, нужен ребейз",
                 "de": "⛔ <b>KONFLIKTE</b> bei {pr} — Upstream ist weiter, Rebase nötig"},

  "free_head":  {"en": "🟢 <b>FREE — {n} issues</b>\n⭐ = good first issue",
                 "ru": "🟢 <b>СВОБОДНО — {n} задач</b>\n⭐ = good first issue",
                 "de": "🟢 <b>FREI — {n} Issues</b>\n⭐ = good first issue"},
  "free_repo":  {"en": "(free: {n})", "ru": "(свободно: {n})", "de": "(frei: {n})"},
  "free_none":  {"en": "\n\nNothing free.", "ru": "\n\nСвободных задач нет.", "de": "\n\nNichts frei."},
  "free_mine":  {"en": "\n<i>our claim {x} is excluded</i>", "ru": "\n<i>наша заявка {x} в список не входит</i>", "de": "\n<i>unser Anspruch {x} ist ausgenommen</i>"},
  "free_claimed":{"en": "claimed by {who}?", "ru": "заявка от {who}?", "de": "beansprucht von {who}?"},

  "taken_head": {"en": "🔒 <b>TAKEN — {n} issues</b>\n", "ru": "🔒 <b>ЗАНЯТО — {n} задач</b>\n", "de": "🔒 <b>VERGEBEN — {n} Issues</b>\n"},
  "taken_us":   {"en": "US", "ru": "МЫ", "de": "WIR"},

  "pool_head":  {"en": "<b>MOST pool</b>\n{free} free of {open} open", "ru": "<b>Пул MOST</b>\nСвободно {free} из {open} открытых", "de": "<b>MOST-Pool</b>\n{free} frei von {open} offen"},
  "pool_repo":  {"en": "{free} of {open}", "ru": "{free} из {open}", "de": "{free} von {open}"},

  "pr_none":    {"en": "No PR watched (set WATCH_PR in config.env).", "ru": "PR не отслеживается (задайте WATCH_PR в config.env).", "de": "Kein PR beobachtet (WATCH_PR in config.env setzen)."},
  "pr_readfail":{"en": "could not read {pr}: {err}", "ru": "не удалось прочитать {pr}: {err}", "de": "{pr} nicht lesbar: {err}"},
  "pr_ci_none": {"en": "not run — awaiting maintainer approval (first PR from a fork)", "ru": "не запускался — ждём кнопки мейнтейнера (первый PR из форка)", "de": "nicht gestartet — wartet auf Maintainer-Freigabe (erster PR aus einem Fork)"},
  "pr_m_ok":    {"en": "no conflicts", "ru": "конфликтов нет", "de": "keine Konflikte"},
  "pr_m_bad":   {"en": "⛔ CONFLICTS", "ru": "⛔ КОНФЛИКТЫ", "de": "⛔ KONFLIKTE"},
  "pr_m_wait":  {"en": "GitHub still computing", "ru": "GitHub ещё считает", "de": "GitHub rechnet noch"},
  "pr_merged":  {"en": " · 🎉 MERGED", "ru": " · 🎉 СМЕРЖЕН", "de": " · 🎉 GEMERGT"},
  "pr_body":    {"en": "<b>PR {pr}</b>\nstate: {st}{mg}\nmerge: {m}\ncomments: {c}\nCI: {ci}\n{link}",
                 "ru": "<b>PR {pr}</b>\nсостояние: {st}{mg}\nслияние: {m}\nкомментариев: {c}\nCI: {ci}\n{link}",
                 "de": "<b>PR {pr}</b>\nStatus: {st}{mg}\nMerge: {m}\nKommentare: {c}\nCI: {ci}\n{link}"},

  "rate_body":  {"en": "<b>GitHub quota</b>\n{rem} of {lim} left, resets in {mm}m {ss}s\ntoken: {tok}\npoll: every {iv}s over {nr} repos",
                 "ru": "<b>Квота GitHub</b>\nосталось {rem} из {lim}, сброс через {mm} мин {ss} с\nтокен: {tok}\nопрос: раз в {iv} с по {nr} репозиториям",
                 "de": "<b>GitHub-Kontingent</b>\n{rem} von {lim} übrig, Reset in {mm}m {ss}s\nToken: {tok}\nAbfrage: alle {iv}s über {nr} Repos"},
  "rate_tok_y": {"en": "yes", "ru": "есть", "de": "ja"},
  "rate_tok_n": {"en": "NO — 60/hr cap", "ru": "НЕТ — потолок 60/час", "de": "NEIN — 60/Std-Limit"},

  "help":       {"en": ("<b>MOST pool watcher</b>\n"
                        "Watches 7 repos and pushes:\n"
                        "🆕 new issue · 🔒 claimed · 🟢 <b>freed</b>\n"
                        "✋ claim requested · 📤 PR for review · ✅ closed\n"
                        "🎉 your PR merged · 💬 comment on your PR · ⛔ conflicts\n\n"
                        "<b>Commands</b>\n"
                        "/free — what is free, by repo\n/taken — who holds what\n"
                        "/pool — per-repo summary\n/pr — your PR and its CI\n"
                        "/rate — GitHub quota\n/lang — switch language\n/help — this message"),
                 "ru": ("<b>Наблюдатель за пулом MOST</b>\n"
                        "Следит за 7 репозиториями и сам присылает:\n"
                        "🆕 новая задача · 🔒 задачу заняли · 🟢 <b>задача освободилась</b>\n"
                        "✋ кто-то просит заявку · 📤 PR на ревью · ✅ закрыта\n"
                        "🎉 наш PR смержен · 💬 комментарий на PR · ⛔ конфликты\n\n"
                        "<b>Команды</b>\n"
                        "/free — что свободно, по репозиториям\n/taken — кто что держит\n"
                        "/pool — сводка по репозиториям\n/pr — наш PR и состояние CI\n"
                        "/rate — квота GitHub\n/lang — сменить язык\n/help — это сообщение"),
                 "de": ("<b>MOST-Pool-Watcher</b>\n"
                        "Beobachtet 7 Repos und meldet:\n"
                        "🆕 neues Issue · 🔒 vergeben · 🟢 <b>frei geworden</b>\n"
                        "✋ Anspruch gemeldet · 📤 PR zur Prüfung · ✅ geschlossen\n"
                        "🎉 dein PR gemergt · 💬 Kommentar an deinem PR · ⛔ Konflikte\n\n"
                        "<b>Befehle</b>\n"
                        "/free — was frei ist, nach Repo\n/taken — wer was hält\n"
                        "/pool — Übersicht nach Repo\n/pr — dein PR und dessen CI\n"
                        "/rate — GitHub-Kontingent\n/lang — Sprache wechseln\n/help — diese Nachricht")},
  "lang_set":   {"en": "Language: English. Tap 🌐 or /lang to cycle.",
                 "ru": "Язык: русский. Нажмите 🌐 или /lang для смены.",
                 "de": "Sprache: Deutsch. 🌐 oder /lang zum Wechseln."},
}


def tr(key, **kw):
    val = T[key].get(_lang, T[key]["en"])
    return val.format(**kw) if kw else val


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
# A repo must be "owner/name"; a bare token would break every per-repo path. Drop it loudly
# rather than crash mid-poll on the first request.
_bad = [r for r in REPOS if r.count("/") != 1]
if _bad:
    print(f"ignoring malformed REPOS entries (need owner/name): {_bad}", flush=True)
    REPOS = [r for r in REPOS if r.count("/") == 1]
if not REPOS:
    sys.exit("no valid repositories to watch (REPOS)")
WATCH_PR = CFG.get("WATCH_PR", "").strip()
# Pool maintainers post claim bookkeeping ("approved, it's yours", "one claimed issue at a
# time") that can read like a claim. Their comments are never a fresh claim. Comma-separated
# logins; defaults to the pool's admin.
MAINTAINERS = {m.strip().lower() for m in CFG.get("MAINTAINERS", "portdeveloper").split(",") if m.strip()}          # e.g. portdeveloper/nad-agent#42
# Our own claim, "owner/repo#number". The maintainer approves in a comment and does not always
# set an assignee — issue #2 is ours and shows no assignee — so without this the watcher would
# list our own work as free.
MY_CLAIM = CFG.get("MY_CLAIM", "").strip()
# Our GitHub login, so a claim assigned to us still reads as ours.
MY_LOGIN = CFG.get("MY_LOGIN", "").strip()
DEFAULT_LANG_CFG = CFG.get("LANG", "en").strip().lower()
if DEFAULT_LANG_CFG in ("en", "ru", "de"):
    DEFAULT_LANG = DEFAULT_LANG_CFG
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


def keyboard():
    """Inline keyboard in the current language, with a language toggle."""
    return json.dumps({"inline_keyboard": [
        [{"text": tr("btn_free"), "callback_data": "free"},
         {"text": tr("btn_taken"), "callback_data": "taken"}],
        [{"text": tr("btn_pool"), "callback_data": "pool"},
         {"text": tr("btn_pr"), "callback_data": "pr"}],
        [{"text": tr("btn_rate"), "callback_data": "rate"},
         {"text": tr("btn_help"), "callback_data": "help"}],
        [{"text": tr("btn_lang"), "callback_data": "lang"}],
    ]})


def _split(text, limit=3900):
    """
    Split into Telegram-sized parts on line boundaries, never mid-line.

    A blind slice at N characters can land inside an <a href="..."> tag; Telegram then rejects
    the whole part with "can't parse entities" and the operator sees half a list or nothing.
    Every alert and command output here is newline-separated, so breaking on newlines keeps
    each tag intact. A single line longer than the limit (never produced here) is hard-cut as
    a last resort.
    """
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(line) > limit:
            if cur:
                parts.append(cur); cur = ""
            for i in range(0, len(line), limit):
                parts.append(line[i:i + limit])
            continue
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [""]


def say(text, kb=True):
    """Отправить сообщение. Кнопки цепляются к последней части длинного текста."""
    chunks = _split(text)
    for n, chunk in enumerate(chunks):
        params = dict(chat_id=TG_CHAT, text=chunk, parse_mode="HTML",
                      disable_web_page_preview="true")
        if kb and n == len(chunks) - 1:
            params["reply_markup"] = keyboard()
        tg("sendMessage", **params)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── github ──────────────────────────────────────────────────────────────────

def fetch_open(repo, max_pages=5):
    """
    Open issues AND pull requests, following pagination.

    The /issues endpoint already returns PRs (each carries a "pull_request" key), so one call
    gets both. per_page=100 covers most repos in a single request, but a busy one (moss is past
    50 open items) can exceed a page — without following the next page the overflow is invisible:
    never snapshotted, never alerted, never in /free. Follow up to max_pages so a large repo is
    fully seen; the cap bounds the request cost.
    """
    items = []
    for page in range(1, max_pages + 1):
        data, err = gh(f"/repos/{repo}/issues?state=open&per_page=100&page={page}")
        if err:
            return None, None, err
        items.extend(data)
        if len(data) < 100:
            break
    issues = [i for i in items if "pull_request" not in i]
    prs = [i for i in items if "pull_request" in i]
    return issues, prs, None


def pr_snapshot(prs):
    return {str(p["number"]): {"title": p["title"], "author": p["user"]["login"]} for p in prs}


def diff_prs(repo, old, new, seeded):
    """
    Alert when a pull request is newly opened.

    `seeded` is the fix for a subtle miss: a repo with zero open PRs stores {}, which is
    indistinguishable from "never polled" by truthiness. puddleswap and mipland sit at 0 PRs,
    so their first PR would seed silently and never alert. The caller passes whether this repo
    has been seen before, so an empty snapshot is no longer mistaken for a first sight.
    """
    if not seeded:
        return []
    out = []
    for num, cur in new.items():
        if num in old:
            continue
        # Our own PR is covered in detail by check_pr; do not double-report it here.
        if f"{repo}#{num}" == (WATCH_PR or ""):
            continue
        out.append(f"{tr('a_prreview')} {esc(cur['author'])}, {esc(short(repo))} #{num}\n"
                   f"{esc(cur['title'])}\nhttps://github.com/{repo}/pull/{num}")
    return out


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

def diff_repo(repo, old, new, seeded):
    """Alerts for one repo. Unseeded repo seeds silently; an empty snapshot on a seen repo is
    a real state, not a first sight (a repo can legitimately reach zero open issues)."""
    if not seeded:
        return []
    out = []
    for num, cur in new.items():
        prev = old.get(num)
        tag = "★ good first issue" if is_gfi(cur["labels"]) else ", ".join(cur["labels"][:2])
        link = issue_url(repo, num)
        if prev is None:
            out.append(f"{tr('a_new')} {esc(short(repo))} #{num}\n{esc(cur['title'])}\n<i>{esc(tag)}</i>\n{link}")
            continue
        if prev["assignee"] != cur["assignee"]:
            if cur["assignee"] is None:
                # The one alert worth waking up for: a claim was released and the slot is open.
                out.append(f"{tr('a_freed')} {esc(short(repo))} #{num} {tr('a_freed_was')} {esc(prev['assignee'])}\n"
                           f"{esc(cur['title'])}\n<i>{esc(tag)}</i>\n{link}")
            else:
                # Losing our own claim is not the same event as any issue being claimed: it
                # ends our slot and needs a rebase/appeal decision, so it gets its own alert.
                lost = MY_CLAIM == f"{repo}#{num}" and not mine(repo, num, cur["assignee"])
                head = tr("a_claim_lost") if lost else tr("a_claimed")
                out.append(f"{head} {esc(cur['assignee'])}, {esc(short(repo))} #{num}\n"
                           f"{esc(cur['title'])}\n{link}")
    for num, prev in old.items():
        if num not in new:
            out.append(f"{tr('a_closed')} {esc(short(repo))} #{num}\n{esc(prev['title'])}\n{issue_url(repo, num)}")
    return out


def diff_comments(repo, old, new, budget, state_claims):
    """
    Someone commenting on a free issue is the earliest signal there is — the assignee only
    appears once a maintainer approves, and by then it is decided. Costs one request per
    changed issue, so it is capped by `budget`.
    """
    out = []
    # Who we have already flagged as claiming each issue, so a claimant's follow-up comment
    # ("any update?") does not re-fire the same "✋ ПРОСЯТ ЗАЯВКУ" every cycle.
    flagged = state_claims
    for num, cur in new.items():
        if budget[0] <= 0:
            break
        prev = old.get(num)
        if not prev or cur["comments"] <= prev["comments"] or cur["assignee"]:
            continue
        # Our own claim is ours; a new comment on it must not fire "someone is claiming this".
        # cur["assignee"] is falsy here (checked above), so this is the no-assignee case.
        if mine(repo, num, cur["assignee"]):
            continue
        budget[0] -= 1
        hit = latest_claim_comment(repo, num)
        key = f"{repo}#{num}"
        if hit:
            who, text = hit
            if flagged.get(key) == who:
                continue  # already alerted on this person's claim
            flagged[key] = who
            out.append(f"{tr('a_claimreq')} {esc(who)}, {esc(short(repo))} #{num}\n"
                       f"{esc(cur['title'])}\n<i>{esc(text)}</i>\n{issue_url(repo, num)}")
        else:
            # No claim in the comments — clear any stale flag so a real claim later still fires.
            flagged.pop(key, None)
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
        out.append(f"{tr('a_merged')} {esc(WATCH_PR)}\n{link}\n\n{tr('a_merged_note')}")
    elif cur["state"] != prev["state"]:
        out.append(f"{tr('a_prstate', st=esc(cur['state'].upper()))} {esc(WATCH_PR)}\n{link}")
    if cur["comments"] > prev["comments"]:
        out.append(f"{tr('a_prcomm', n=cur['comments'] - prev['comments'])} {esc(WATCH_PR)}\n{link}")
    # GitHub returns mergeable=None while recomputing after a push, so a real conflict can
    # arrive as True -> None -> False and slip past a naive prev/cur check. Track the last
    # DEFINITE value instead of the immediately-previous one.
    if cur["mergeable"] is False and state.get("pr_last_mergeable") is not False:
        out.append(f"{tr('a_conflict', pr=esc(WATCH_PR))}\n{link}")
    if cur["mergeable"] is not None:
        state["pr_last_mergeable"] = cur["mergeable"]
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
    # "owner/name" -> "name". Falls back to the whole string if a misconfigured REPOS entry
    # has no slash, rather than raising IndexError and killing the poll loop.
    return repo.split("/", 1)[1] if "/" in repo else repo


def mine(repo, num, assignee=None):
    """
    Is this issue ours?

    MY_CLAIM exists because the pool approves claims in a comment and does not always set the
    assignee — without it the bot would report our own issue as free. But it must not override
    reality in the other direction: once GitHub shows someone else as the assignee, the claim
    has moved and saying "US" would be a comfortable lie. Assignee wins when it disagrees.
    """
    if not MY_CLAIM or MY_CLAIM != f"{repo}#{num}":
        return False
    return not assignee or assignee.lower() == MY_LOGIN.lower()


def cmd_free(state):
    """Every unclaimed issue, grouped by repo, with difficulty and a link."""
    blocks, total = [], 0
    for repo in REPOS:
        snap = state.get("repos", {}).get(repo, {})
        flags = state.get("claim_flags", {})
        free = [(n, v) for n, v in snap.items() if not v["assignee"] and not mine(repo, n, v["assignee"])]
        if not free:
            continue
        total += len(free)
        rows = [f"\n<b>{esc(short(repo))}</b>  {tr('free_repo', n=len(free))}"]
        for n, v in sorted(free, key=lambda x: int(x[0])):
            d = difficulty(v["labels"])
            star = "⭐ " if is_gfi(v["labels"]) else "• "
            tag = f" <i>[{esc(d)}]</i>" if d and not is_gfi(v["labels"]) else ""
            # No assignee, but someone claimed it in a comment (the pool approves in comments):
            # mark it so the list does not read as genuinely open.
            claimer = flags.get(f"{repo}#{n}")
            note = f" <i>· {tr('free_claimed', who=esc(claimer))}</i>" if claimer else ""
            rows.append(f'{star}<a href="{issue_url(repo, n)}">#{n}</a> {esc(v["title"][:64])}{tag}{note}')
        blocks.append("\n".join(rows))
    head = tr("free_head", n=total)
    if MY_CLAIM:
        head += tr("free_mine", x=esc(MY_CLAIM))
    return head + "\n" + "\n".join(blocks) if blocks else head + tr("free_none")


def cmd_taken(state):
    """Who holds what. A held issue frees up when its holder's PR lands, so it is a watch list."""
    blocks, total = [], 0
    for repo in REPOS:
        snap = state.get("repos", {}).get(repo, {})
        held = [(n, v) for n, v in snap.items() if v["assignee"] or mine(repo, n, v["assignee"])]
        if not held:
            continue
        total += len(held)
        rows = [f"\n<b>{esc(short(repo))}</b>"]
        for n, v in sorted(held, key=lambda x: int(x[0])):
            who = tr("taken_us") if mine(repo, n, v["assignee"]) else esc(v["assignee"])
            mark = "🟡" if mine(repo, n, v["assignee"]) else "🔒"
            rows.append(f'{mark} <a href="{issue_url(repo, n)}">#{n}</a> <b>{who}</b> — {esc(v["title"][:52])}')
        blocks.append("\n".join(rows))
    return tr("taken_head", n=total) + "\n".join(blocks)


def cmd_pool(state):
    """One line per repo: how much is open, how much of it is up for grabs."""
    rows, t_open, t_free, t_gfi = [], 0, 0, 0
    for repo in REPOS:
        snap = state.get("repos", {}).get(repo, {})
        free = [v for n, v in snap.items() if not v["assignee"] and not mine(repo, n, v["assignee"])]
        gfi = sum(1 for v in free if is_gfi(v["labels"]))
        t_open += len(snap); t_free += len(free); t_gfi += gfi
        bar = "🟢" if free else "⚪"
        rows.append(f'{bar} <b>{esc(short(repo))}</b> — {tr("pool_repo", free=len(free), open=len(snap))}'
                    + (f' · ⭐{gfi}' if gfi else ""))
    head = (tr("pool_head", free=t_free, open=t_open)
            + (f" · ⭐{t_gfi} good-first" if t_gfi else "") + "\n")
    return head + "\n".join(rows)


def cmd_pr():
    if not WATCH_PR or "#" not in WATCH_PR:
        return tr("pr_none")
    repo, num = WATCH_PR.split("#", 1)
    pr, err = gh(f"/repos/{repo}/pulls/{num}")
    if err:
        return tr("pr_readfail", pr=esc(WATCH_PR), err=esc(err))
    checks, _ = gh(f"/repos/{repo}/commits/{pr['head']['sha']}/check-runs")
    n_checks = (checks or {}).get("total_count", 0)
    runs = ", ".join(f"{c['name']}: {c['conclusion'] or c['status']}"
                     for c in (checks or {}).get("check_runs", []))
    ci = runs if n_checks else tr("pr_ci_none")
    merge = {True: tr("pr_m_ok"), False: tr("pr_m_bad"), None: tr("pr_m_wait")}
    return tr("pr_body",
              pr=esc(WATCH_PR),
              st=esc(pr['state']),
              mg=(tr("pr_merged") if pr.get('merged_at') else ''),
              m=esc(merge.get(pr.get('mergeable'), str(pr.get('mergeable')))),
              c=pr['comments'] + pr['review_comments'],
              ci=esc(ci),
              link=f"https://github.com/{repo}/pull/{num}")


def cmd_rate():
    data, err = gh("/rate_limit")
    if err:
        return f"rate_limit failed: {esc(err)}"
    c = data["resources"]["core"]
    left = max(0, c["reset"] - int(time.time()))
    return tr("rate_body",
              rem=c['remaining'], lim=c['limit'], mm=left // 60, ss=left % 60,
              tok=(tr("rate_tok_y") if GH_TOKEN else tr("rate_tok_n")),
              iv=POLL_INTERVAL, nr=len(REPOS))





def dispatch(cmd, state):
    """Single place both commands and buttons answer from, so they cannot drift apart."""
    global _lang
    if cmd.startswith("lang"):
        order = ["en", "ru", "de"]
        _lang = order[(order.index(_lang) + 1) % len(order)] if _lang in order else "en"
        state["lang"] = _lang
        # Show substantial content in the new language right away. A one-line confirmation left
        # the list the user was looking at unchanged (Telegram does not re-render old messages),
        # so the switch looked like it did nothing.
        return tr("lang_set") + "\n\n" + tr("help")
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
    return tr("help")


def handle_commands(state):
    """
    Разбор апдейтов Telegram: команды и нажатия кнопок.

    allowed_updates обязан содержать callback_query — иначе нажатия просто не доставляются,
    и бот выглядит исправным, отвечая только на текст. Смещение сохраняется, чтобы рестарт
    не проигрывал старые команды заново.
    """
    global _lang
    _lang = state.get("lang", DEFAULT_LANG)
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
        raw = (msg.get("text") or "").strip()
        # Only reply to explicit commands. Plain chatter in the alert channel must not draw a
        # HELP dump on every message.
        if raw.startswith("/"):
            text = raw.split("@")[0].lower().lstrip("/")
            if text:
                say(dispatch(text, state))


# ── main ────────────────────────────────────────────────────────────────────

def main():
    global _lang
    state = load_state()
    state.setdefault("repos", {})
    _lang = state.get("lang", DEFAULT_LANG)  # restore chosen language across restarts
    first_run = not state["repos"]
    if first_run:
        print("first run: seeding state without alerts", flush=True)

    while True:
        started = time.time()
        _lang = state.get("lang", DEFAULT_LANG)
        alerts = []
        # One claim-comment lookup per cycle at most when running tokenless, so a busy repo
        # cannot exhaust the hourly quota and blind the watcher entirely.
        budget = [8 if GH_TOKEN else 1]

        for repo in REPOS:
            issues, prs, err = fetch_open(repo)
            if err:
                print(f"{repo}: {err}", flush=True)
                if "rate limit" in err.lower() or "403" in err:
                    break
                continue
            seen = state.setdefault("seen", [])
            seeded = repo in seen

            new = snapshot(issues)
            old = state["repos"].get(repo, {})
            alerts += diff_repo(repo, old, new, seeded)
            if seeded:
                alerts += diff_comments(repo, old, new, budget, state.setdefault("claim_flags", {}))
            state["repos"][repo] = new

            new_prs = pr_snapshot(prs)
            alerts += diff_prs(repo, state.get("prs", {}).get(repo, {}), new_prs, seeded)
            state.setdefault("prs", {})[repo] = new_prs

            if repo not in seen:
                seen.append(repo)

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
