# Telegram keep-alive — "Ryan's Assistant"

The Telegram bot **Ryan's Assistant** is a bridge between Telegram and Claude
Code on the desktop Mac. The bot itself (registered with Telegram's BotFather)
never goes down — what dies is the bridge **process on the Mac** that polls
Telegram and talks to Claude. When that process dies, the chat goes silent
until someone jump-starts it by hand.

This kit ends the jump-starting. It is desktop-side by design: cloud Claude
sessions have no path to the Mac, so the keep-alive has to live where the
bridge lives.

Two layers:

1. **KeepAlive supervisor** (`com.anthonyflores.fully-aware.telegram-assistant`)
   — launchd runs the bridge and restarts it within seconds of any crash,
   including after reboots. This is what actually keeps the connection alive.
2. **5-minute watchdog** (`com.anthonyflores.fully-aware.telegram-watchdog`)
   — `telegram/watchdog.sh` every 300s: logs a health line (process up?
   Telegram API reachable? optional end-to-end health command?), force-restarts
   a bridge that is running but wedged, and restarts anything KeepAlive somehow
   missed. `state/logs/telegram-watchdog.log` becomes the audit trail of the
   connection.

## Setup (one time, on the desktop)

The bridge's start command must be identified on the Mac itself. Paste this
into a Claude Code session **on the desktop**:

> Read `telegram/README.md` in the fully-aware repo. The Telegram bot "Ryan's
> Assistant" bridges to Claude Code on this Mac and keeps dying. First identify
> what runs it: check `launchctl list | grep -viE 'com\.apple'`,
> `ls ~/Library/LaunchAgents`, `ps aux | grep -iE 'telegram|bot|claw' | grep -v grep`,
> `crontab -l`, `docker ps` if docker exists, and grep likely project dirs for
> `api.telegram.org` or a bot-token pattern (`[0-9]{8,10}:[A-Za-z0-9_-]{35}`).
> Then copy `telegram/watchdog.env.example` to `state/telegram-watchdog.env`,
> fill in the start command, a unique pgrep pattern, and (optionally) the bot
> token and a health command, and run `telegram/install-telegram-watchdog.sh`.
> Verify with the commands the installer prints, then have me confirm the
> round trip by messaging the bot from Telegram.

If the bridge turns out to already have its own LaunchAgent, set
`ASSISTANT_LAUNCHD_LABEL` in the config instead of `ASSISTANT_START_CMD` — the
kit then leaves supervision to that agent and only adds the watchdog.

## Day-to-day

- **Status:** `launchctl list | grep fully-aware.telegram` and
  `tail state/logs/telegram-watchdog.log`
- **Manual restart** (what "restart the connection" means from now on):
  `launchctl kickstart -k gui/$(id -u)/com.anthonyflores.fully-aware.telegram-assistant`
- **Disarm:** `launchctl unload ~/Library/LaunchAgents/com.anthonyflores.fully-aware.telegram-*.plist`

Asking the desktop Claude session to "restart the Telegram connection" now
means one `kickstart` command — and with both layers armed it should almost
never be needed.

## Discipline

Consistent with the repo rules: config and logs live only under gitignored
`state/` (the config may hold the bot token — never commit it). The watchdog
writes nothing outside `state/` and the only thing it ever restarts is the
configured launchd label on this Mac. Arming is a manual, desktop-side act via
the installer; nothing in a cloud session can or does trigger it.
