# Inkbird IBT-4XS -> Telegram bridge

An always-on Python daemon for Linux. It continuously scans for the
thermometer; the moment it powers on and is in range, it connects, streams
probe temperatures, and reports to a Telegram bot. You set per-probe alert
ranges live by texting the bot.

## How it works

- **Detects "on" automatically.** The IBT-4XS only advertises over Bluetooth
  when it is powered on, so a successful BLE scan *is* the on-signal. Power it
  off (or walk out of range) and the connection drops, which is the off-signal.
  The daemon then goes back to scanning and auto-reconnects next time.
- **Alerts in software.** Thresholds are checked against every reading, with
  hysteresis so you get one ping per crossing, not a stream of them.
- Everything is stored in Celsius internally and displayed in both C and F.

## One-time setup

### 1. Create the bot
Message **@BotFather** on Telegram, send `/newbot`, follow the prompts, and copy
the **token** it gives you.

### 2. Find your chat id
Send any message to your new bot, then visit (token filled in):
`https://api.telegram.org/bot<TOKEN>/getUpdates`
Look for `"chat":{"id":<NUMBER>}` - that number is your `TELEGRAM_CHAT_ID`.

### 3. Install
```bash
sudo apt install bluez python3-venv        # BlueZ is the Linux BT stack
cd ~/ibbq
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp ibbq.env.example ibbq.env
chmod 600 ibbq.env
# edit ibbq.env with your token + chat id
```

### 4. Test by hand first
```bash
set -a; . ./ibbq.env; set +a
./venv/bin/python ibbq_telegram.py
```
Turn the thermometer on. You should get a "connected" message, then be able to
send `/status`. Ctrl-C to stop once it works.

### 5. Run it always (systemd)
Edit `ibbq-telegram.service` and replace `youruser` with your username, then:
```bash
sudo cp ibbq-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibbq-telegram
journalctl -u ibbq-telegram -f      # watch the logs
```
It now starts on boot and restarts itself if it crashes.

## Bot commands

- `/status` - current probe temps + configured alerts
- `/probeN target=160c low=45c high=102c` - set alerts for probe N (1-4)
  - `target` pings once when the probe reaches that temp
  - `low`/`high` ping when the probe leaves that band
  - units: append `c` or `f` (e.g. `225f`); default is Celsius
- `/probeN clear` - remove that probe's alerts
- `/mute` - silence the thermometer's own buzzer
- `/battery` - ask the device for its battery level
- `/help`

Example: `/probe1 target=203f` then `/probe2 low=104c high=116c`

## Tuning / gotchas

- **Readings 10x off?** iBBQ realtime data is 0.1 C per count, so the code
  divides by 10 (`TEMP_DIVISOR`). If your unit differs, change that constant.
- **One connection at a time.** While this daemon is connected, the Inkbird
  phone app can't connect, and vice versa. That's a BLE limitation, not a bug.
- **Range is short** (~10 m, less through walls). Keep the laptop near the grill.
- **Permissions:** on most distros BlueZ scanning works for a normal user. If
  scanning fails, either run under a user in the `bluetooth` group or grant the
  Python binary BLE caps:
  `sudo setcap 'cap_net_raw,cap_net_admin+eip' $(readlink -f venv/bin/python3)`

## Docker note

BLE from inside a container needs `--net=host` plus D-Bus access
(`-v /var/run/dbus:/var/run/dbus`) and cooperation from the host's BlueZ. It
works but adds friction for no real benefit on a single-device home setup, so
running the daemon on the host via systemd (above) is the recommended path.
