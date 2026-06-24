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

## Voice self-update (the `update_server` tool)

Amber can run the update above by voice ("update your backend") — but only when
it's wired up, which is **off by default**. Two things are required:

1. **Set `AMBER_UPDATE_COMMAND`** in `.env`. Empty = the tool is hidden from the
   model entirely (so Amber just says it can't), because it's a privileged action.
   Point it at this script, run detached so the restart can't kill it mid-update:

   ```ini
   AMBER_UPDATE_COMMAND=sudo systemd-run --collect --unit=amber-update bash /opt/amber/deploy/update.sh
   ```

   (`update.sh` auto-detects its app dir + the owning user, so this works whether
   the service runs as `amber`, `ubuntu`, or anyone else — just point it at the
   right `deploy/update.sh`.)

2. **Let the service `sudo`.** The command runs as the *service user*, and
   `update.sh` needs root (systemctl, `/etc`). So:
   - the service user needs **passwordless sudo** for it (the cloud `ubuntu` user
     usually already has blanket NOPASSWD; otherwise add a `/etc/sudoers.d` entry), and
   - the unit must **not** set `NoNewPrivileges=true` — that flag makes the kernel
     ignore setuid and `sudo` fails silently. Check and fix:

   ```bash
   systemctl show amber -p NoNewPrivileges   # want: NoNewPrivileges=no
   # if it's yes: set NoNewPrivileges=no in the unit, then:
   sudo systemctl daemon-reload && sudo systemctl restart amber
   ```

Then restart Amber so the tool registers, and "update your backend" works
hands-free. Note `update.sh` will **not** overwrite a customized systemd unit
(different `User`, relaxed hardening, …); it warns and leaves it. Force adopting
the repo's template with `AMBER_FORCE_UNIT=1 sudo bash deploy/update.sh`.

## Notes

- The unit binds to `0.0.0.0:8000`. Front it with nginx/Caddy for TLS (`wss://`)
  before exposing it publicly — browser WebSocket clients on HTTPS pages require
  `wss://`. Terminate TLS at the proxy and reverse-proxy to `127.0.0.1:8000`.
- Set `AMBER_AUTH_SECRET` in `.env` to require `?token=...` on the WS handshake.
- Logs go to the journal (`journalctl -u amber`). Adjust verbosity with
  `AMBER_LOG_LEVEL` (DEBUG/INFO/WARNING).
