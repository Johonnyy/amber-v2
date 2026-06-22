# Deploying Amber to the OVH VPS (systemd)

## First-time setup (scripted)

[`setup.sh`](setup.sh) does the whole first-time install and **prompts you** for
the settings that can't be guessed (OpenAI key, auth secret, port, etc.). It is
idempotent — safe to re-run. Run as root on the VPS:

```bash
# Bootstrap straight from GitHub (no clone needed first):
curl -fsSL https://raw.githubusercontent.com/Johonnyy/amber-v2/main/deploy/setup.sh | sudo bash

# ...or, if you've already cloned the repo to /opt/amber:
sudo bash /opt/amber/deploy/setup.sh
```

It will: install `python3.11`/`venv`/`git`, create the `amber` system user, clone
`https://github.com/Johonnyy/amber-v2` into `/opt/amber`, build the virtualenv and
install deps, write `/opt/amber/.env` (chmod 600) from your answers, install +
enable the systemd unit, and run a health check.

## Updating after a code change

[`update.sh`](update.sh) is the **one command** to bring a deploy fully in sync
with the repo — you should never have to hand-edit `.env` or touch systemd. Run as
root:

```bash
sudo bash /opt/amber/deploy/update.sh
```

It reconciles everything, each step idempotent:

- **code** — `git pull --ff-only` (clear error if the checkout diverged).
- **python/venv** — rebuilds `.venv` if its interpreter went missing or dropped
  below 3.11 (e.g. the OS upgraded Python out from under it).
- **deps** — reinstalls when a dependency file changed in the pull *or* the venv
  was rebuilt.
- **`.env`** — adds any new keys that appeared in `.env.example` (carrying their
  defaults), then **prompts** for any required secret still unset or left as a
  `...` placeholder (`AMBER_OPENAI_API_KEY`, and `AMBER_ANTHROPIC_API_KEY` unless
  `AMBER_FEATURE_LLM=false`). Existing values and optional empties (e.g.
  `AMBER_AUTH_SECRET`) are never touched. If a required secret is missing and
  there's no terminal (cron), it warns and exits non-zero instead of starting a
  broken service.
- **systemd** — reinstalls the unit whenever the installed copy differs from the
  repo's, then `daemon-reload`.
- **restart + health check** on the port from `.env`.

<details>
<summary>Manual steps (if you'd rather not use the scripts)</summary>

```bash
# First-time setup
apt update && apt install -y python3.11 python3.11-venv git
useradd --system --create-home --home-dir /opt/amber amber
sudo -u amber git clone https://github.com/Johonnyy/amber-v2 /opt/amber
cd /opt/amber
sudo -u amber python3.11 -m venv .venv
sudo -u amber .venv/bin/pip install --upgrade pip
sudo -u amber .venv/bin/pip install -e .
sudo -u amber cp .env.example .env
sudo -u amber nano .env            # set AMBER_OPENAI_API_KEY (and AMBER_AUTH_SECRET)
cp deploy/amber.service /etc/systemd/system/amber.service
systemctl daemon-reload
systemctl enable --now amber
systemctl status amber
curl http://127.0.0.1:8000/health
journalctl -u amber -f          # live logs

# Update after a code change
cd /opt/amber
sudo -u amber git pull
sudo -u amber .venv/bin/pip install -e .   # only if deps changed
systemctl restart amber
```

</details>

## Notes

- The unit binds to `0.0.0.0:8000`. Front it with nginx/Caddy for TLS (`wss://`)
  before exposing it publicly — browser WebSocket clients on HTTPS pages require
  `wss://`. Terminate TLS at the proxy and reverse-proxy to `127.0.0.1:8000`.
- Set `AMBER_AUTH_SECRET` in `.env` to require `?token=...` on the WS handshake.
- Logs go to the journal (`journalctl -u amber`). Adjust verbosity with
  `AMBER_LOG_LEVEL` (DEBUG/INFO/WARNING).
