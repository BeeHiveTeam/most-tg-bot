#!/usr/bin/env bash
#
# most-tg-bot installer.
#
#   curl -fsSL https://raw.githubusercontent.com/BeeHiveTeam/most-tg-bot/main/install.sh | bash
#
# Asks for what it cannot know, checks what it can, and refuses rather than guessing. Typed
# tokens are read without echo and never enter shell history; when the script is piped into
# bash, stdin is the pipe, so prompts come from /dev/tty instead.
#
# Unattended (note: values given this way DO land in your shell history):
#   TG_TOKEN=... TG_CHAT_ID=... [MOST_GH_TOKEN=...] [MY_LOGIN=...] [BOT_LANG=en] \
#     [INSTALL_SERVICE=1] [MOST_FORCE=1] bash install.sh
#
# Re-running is safe: an existing config is kept unless you pass MOST_FORCE=1, an existing
# systemd unit pointing elsewhere is never replaced without confirmation, and state.json —
# which remembers what has already been alerted — is never touched.

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/BeeHiveTeam/most-tg-bot/main"
DEST="${MOST_BOT_DIR:-/opt/most-tg-bot}"
# Canonicalise before validating: /opt/../etc passed the system-dir check literally.
case "$DEST" in /*) DEST="$(realpath -m "$DEST")" ;; esac
SERVICE="most-tg-bot"

# Colours only when stdout is a terminal, so piping to a file stays readable.
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# When curl-piped, stdin is the script itself, so answers must come from the terminal. Probe by
# actually opening /dev/tty: it can exist and still be unopenable (containers, cron, CI), and a
# `-r` test passes there while every read fails. Getting this wrong is worse than failing — the
# prompts would silently return empty and we would write a config with no token in it.
if { : < /dev/tty; } 2>/dev/null; then HAVE_TTY=1; else HAVE_TTY=0; fi

ask() {  # ask "prompt" [default]
    local p="$1" d="${2:-}" a=""
    if [ "$HAVE_TTY" -eq 1 ]; then printf '%s' "$p" > /dev/tty; read -r a < /dev/tty || a=""
    else a=""; fi
    printf '%s' "${a:-$d}"
}

ask_secret() {
    local p="$1" a=""
    if [ "$HAVE_TTY" -eq 1 ]; then
        printf '%s' "$p" > /dev/tty
        read -rs a < /dev/tty || a=""
        printf '\n' > /dev/tty
    fi
    printf '%s' "$a"
}

# Values may also be supplied up-front for an unattended install:
#   TG_TOKEN=... TG_CHAT_ID=... bash install.sh
# Anything given this way is not prompted for.
ENV_TG_TOKEN="${TG_TOKEN:-}"; ENV_TG_CHAT="${TG_CHAT_ID:-}"
ENV_GH_TOKEN="${MOST_GH_TOKEN:-}"; ENV_MY_LOGIN="${MY_LOGIN:-}"; ENV_LANG="${BOT_LANG:-}"

say ""
say "${B}most-tg-bot — MOST pool watcher${N}"
say "Watches the seven pool repositories and alerts on claims, freed slots and new PRs."
say ""

# ── preflight ────────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.7+ and re-run."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'EOF' || die "Python $PYV is too old — 3.7+ required."
import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)
EOF
ok "Python $PYV"

command -v curl >/dev/null 2>&1 || die "curl not found."

# systemd is optional: without it the bot still runs in the foreground.
HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then HAVE_SYSTEMD=1; fi

# Privilege is needed only for /opt and the unit file.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "Not root and sudo not found. Re-run as root, or set MOST_BOT_DIR to a writable path."
  SUDO="sudo"
fi

# DEST reaches `chown -R` as root, so validate before anything touches the filesystem. An
# unset variable, a relative path or a system directory would otherwise recurse over it.
case "$DEST" in
  /*) ;;
  *) die "MOST_BOT_DIR must be an absolute path (got: $DEST)" ;;
esac
case "$DEST" in
  *[!A-Za-z0-9/._-]*) die "MOST_BOT_DIR must not contain spaces or shell metacharacters (got: $DEST)" ;;
esac
case "$DEST" in
  / | /etc | /etc/* | /usr | /usr/* | /var | /var/* | /bin | /bin/* | /sbin | /sbin/* | /lib | /lib/* | /boot | /boot/* | /dev | /dev/* | /proc | /proc/* | /sys | /sys/*)
    die "Refusing to install into a system directory: $DEST" ;;
esac

say ""
say "Install directory: ${B}${DEST}${N}"

# ── fetch ────────────────────────────────────────────────────────────────────
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for f in bot.py config.env.example most-tg-bot.service; do
  curl -fsSL "$REPO_RAW/$f" -o "$TMP/$f" || die "Could not download $f"
done
python3 -m py_compile "$TMP/bot.py" || die "Downloaded bot.py does not compile — aborting rather than installing a broken file."
ok "Downloaded and verified bot.py"

$SUDO mkdir -p "$DEST"
$SUDO cp "$TMP/bot.py" "$DEST/bot.py"
$SUDO cp "$TMP/config.env.example" "$DEST/config.env.example"

# ── config ───────────────────────────────────────────────────────────────────
CFG="$DEST/config.env"
WRITE_CFG=1
if $SUDO test -f "$CFG" 2>/dev/null; then
  say ""
  warn "$CFG already exists."
  if [ "${MOST_FORCE:-}" = "1" ]; then
    ok "Overwriting it (MOST_FORCE=1)."
  else
    # Default no: an unattended re-run must not silently discard working credentials. Token
    # rotation and re-provisioning go through MOST_FORCE=1.
    a=$(ask "Overwrite it? Existing tokens will be lost. [y/N]: " "n")
    case "$a" in
      y|Y|yes) ;;
      *) WRITE_CFG=0; ok "Keeping the existing config. Use MOST_FORCE=1 to replace it." ;;
    esac
  fi
fi

if [ "$WRITE_CFG" -eq 1 ]; then
  # No terminal and nothing pre-supplied: every prompt would return empty and we would write a
  # config with no token. Stop with instructions instead of installing something broken.
  if [ "$HAVE_TTY" -eq 0 ] && [ -z "$ENV_TG_TOKEN" ]; then
    say ""
    die "No terminal available for the prompts (running under cron/CI, or a container without a tty).
   Either download and run it directly:
     curl -fsSLO $REPO_RAW/install.sh && bash install.sh
   or supply the values up front:
     TG_TOKEN=... TG_CHAT_ID=... [MOST_GH_TOKEN=...] [MY_LOGIN=...] [BOT_LANG=en] bash install.sh"
  fi

  say ""
  say "${B}Configuration${N} — paste each value and press Enter. Tokens are not echoed."
  say ""
  TG_TOKEN="$ENV_TG_TOKEN"
  if [ -z "$TG_TOKEN" ]; then
    say "1. Telegram bot token, from @BotFather (looks like 123456789:AA...)."
    TG_TOKEN=$(ask_secret "   token: ")
  else
    ok "1. Telegram token taken from the environment."
  fi
  [ -n "$TG_TOKEN" ] || die "A Telegram token is required."

  say ""
  say "2. Chat id to alert. Your own user id for a private bot."
  say "   ${Y}Do not know it?${N} Message your bot once, then press Enter here and the"
  say "   script will read it from the bot's updates."
  TG_CHAT="$ENV_TG_CHAT"
  [ -n "$TG_CHAT" ] || TG_CHAT=$(ask "   chat id (Enter to detect): ")
  if [ -z "$TG_CHAT" ]; then
    say "   asking Telegram…"
    # The token must not appear in argv: /proc/PID/cmdline is world-readable, so any local
    # user running `ps` during this call would read it. Pass the URL through curl's config
    # on stdin instead, which never reaches the process list.
    TG_CHAT=$(printf 'url = "https://api.telegram.org/bot%s/getUpdates"\n' "$TG_TOKEN" \
      | curl -fsS --config - 2>/dev/null \
      | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
if not d.get("ok"): sys.exit(0)
ids=[]
for u in d.get("result", []):
    m = u.get("message") or u.get("channel_post") or {}
    c = (m.get("chat") or {}).get("id")
    if c is not None and c not in ids: ids.append(c)
print(" ".join(str(i) for i in ids))' || true)
    set -- $TG_CHAT
    if [ "$#" -eq 1 ]; then TG_CHAT="$1"; ok "   detected chat id: $TG_CHAT"
    elif [ "$#" -gt 1 ]; then
      # More than one chat wrote to the bot: guessing would send every alert to a stranger.
      die "Several chats have messaged the bot ($*). Set TG_CHAT_ID to the one you want."
    else die "Could not detect a chat id. Send your bot a message first, then re-run."; fi
  fi

  say ""
  say "3. GitHub token — ${B}public read only, no scopes needed${N}. Optional but recommended:"
  say "   without it GitHub allows 60 requests/hour and the poll drops to once per 10 min."
  # Deliberately MOST_GH_TOKEN, not GH_TOKEN: the latter is the GitHub CLI's variable and is
  # often a broad write-scoped PAT. Copying that to disk unasked is not ours to do.
  GH_TOKEN="$ENV_GH_TOKEN"
  if [ -n "$GH_TOKEN" ]; then ok "   token taken from MOST_GH_TOKEN."
  else GH_TOKEN=$(ask_secret "   token (Enter to skip): "); fi

  say ""
  say "4. Your GitHub login. Used to tell your own claim apart, and to follow your own"
  say "   pull requests in the pool automatically — no PR number to keep updated."
  MY_LOGIN="$ENV_MY_LOGIN"
  [ -n "$MY_LOGIN" ] || MY_LOGIN=$(ask "   login (Enter to skip): ")

  say ""
  say "5. UI language: en, ru or de. Switchable later with the 🌐 button."
  LANG_CHOICE="$ENV_LANG"
  [ -n "$LANG_CHOICE" ] || LANG_CHOICE=$(ask "   [en]: " "en")
  case "$LANG_CHOICE" in en|ru|de) ;; *) warn "Unknown language '$LANG_CHOICE', using en."; LANG_CHOICE=en ;; esac

  # umask so the file is never briefly world-readable while it holds a token.
  TMPCFG="$TMP/config.env"; (umask 077; : > "$TMPCFG")
  {
    echo "TG_TOKEN=$TG_TOKEN"
    echo "TG_CHAT_ID=$TG_CHAT"
    [ -n "$GH_TOKEN" ] && echo "GH_TOKEN=$GH_TOKEN"
    [ -n "$MY_LOGIN" ] && echo "MY_LOGIN=$MY_LOGIN"
    echo "LANG=$LANG_CHOICE"
  } >> "$TMPCFG"
  # install(1) creates with the mode, so the token is never briefly world-readable, and it
  # does not follow a symlink at $CFG the way cp would.
  $SUDO install -m 600 "$TMPCFG" "$CFG"
  ok "Wrote $CFG (mode 600)"
fi

# ── run ──────────────────────────────────────────────────────────────────────
say ""
if [ "$HAVE_SYSTEMD" -eq 1 ]; then
  if [ "$HAVE_TTY" -eq 1 ]; then
    a=$(ask "Install as a systemd service and start it now? [Y/n]: " "y")
    case "$a" in n|N|no) HAVE_SYSTEMD=0 ;; esac
  elif [ "${INSTALL_SERVICE:-}" = "1" ]; then
    ok "Installing the systemd service (INSTALL_SERVICE=1)."
  else
    # Writing a root-owned unit is the most intrusive step here. With nobody to ask, the
    # default must be to not do it — an unattended run should not quietly claim a system
    # service name that may already belong to something else.
    HAVE_SYSTEMD=0
    warn "No terminal: skipping the systemd service. Re-run with INSTALL_SERVICE=1 to install it."
  fi
fi

if [ "$HAVE_SYSTEMD" -eq 1 ]; then
  # The unit hardens itself with PrivateTmp and ProtectHome, which make /tmp and /home
  # invisible inside its namespace. Installing there yields 226/NAMESPACE at every start —
  # a crash loop that looks like a code fault. Relax exactly what the chosen path needs.
  EXTRA_SED=""
  case "$DEST" in
    /tmp/*|/var/tmp/*) EXTRA_SED="-e s|^PrivateTmp=.*|PrivateTmp=false|" ;;
    /home/*|/root/*)   EXTRA_SED="-e s|^ProtectHome=.*|ProtectHome=false|" ;;
  esac
  [ -n "$EXTRA_SED" ] && warn "Relaxing unit sandboxing so $DEST is visible to the service."

  RUN_USER="${SUDO_USER:-$(id -un)}"
  [ "$RUN_USER" = "root" ] && warn "Installing to run as root; a non-privileged user is preferable."
  $SUDO chown -R "$RUN_USER" "$DEST"
  # The shipped unit hardcodes User= and the path; rewrite both for this machine.
  sed -e "s|^User=.*|User=$RUN_USER|" \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=$DEST|" \
      -e "s|^ExecStart=.*|ExecStart=$(command -v python3) $DEST/bot.py|" \
      -e "s|^ReadWritePaths=.*|ReadWritePaths=$DEST|" \
      $EXTRA_SED \
      "$TMP/most-tg-bot.service" > "$TMP/unit"
  UNIT="/etc/systemd/system/${SERVICE}.service"
  # Never silently replace a unit that already points somewhere else: on a machine where the
  # bot is already installed under a different path, overwriting it and reloading would kill a
  # working service. Back it up and say so.
  if $SUDO test -f "$UNIT"; then
    OLD_WD=$($SUDO grep -m1 '^WorkingDirectory=' "$UNIT" 2>/dev/null | cut -d= -f2- || true)
    if [ -n "$OLD_WD" ] && [ "$OLD_WD" != "$DEST" ]; then
      warn "An existing $SERVICE unit points at $OLD_WD, not $DEST."
      a=$(ask "Replace it? The old unit is backed up. [y/N]: " "n")
      case "$a" in y|Y|yes) ;; *) die "Left the existing service alone. Nothing was changed." ;; esac
    fi
    $SUDO cp "$UNIT" "${UNIT}.bak-$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  fi
  $SUDO cp "$TMP/unit" "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now "$SERVICE" >/dev/null 2>&1 || die "systemctl enable failed."

  sleep 3
  if $SUDO systemctl is-active --quiet "$SERVICE"; then
    ok "Service ${SERVICE} is running as ${RUN_USER}."
  else
    warn "Service is not active. Recent log:"
    $SUDO journalctl -u "$SERVICE" -n 15 --no-pager || true
    die "Start failed — fix the above and run: sudo systemctl restart $SERVICE"
  fi

  say ""
  say "  logs:    ${B}sudo journalctl -u $SERVICE -f${N}"
  say "  restart: ${B}sudo systemctl restart $SERVICE${N}"
else
  $SUDO chown -R "$(id -un)" "$DEST" 2>/dev/null || true
  say "Run it with:"
  say "  ${B}cd $DEST && python3 bot.py${N}"
fi

say ""
ok "Done. The first poll seeds state silently — alerts start from the next change."
say "Send the bot ${B}/help${N} to see the commands."
say ""
