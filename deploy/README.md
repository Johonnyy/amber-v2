# Deploying Amber to the OVH VPS (systemd)

First-time setup. Run as root (or with `sudo`) on the VPS.

```bash
# 1. System deps
apt update && apt install -y python3.11 python3.11-venv git

# 2. Dedicated user + app dir
useradd --system --create-home --home-dir /opt/amber amber

# 3. Get the code (clone your repo, or rsync from your machine)
sudo -u amber git clone <your-repo-url> /opt/amber
cd /opt/amber

# 4. Virtualenv + deps
sudo -u amber python3.11 -m venv .venv
sudo -u amber .venv/bin/pip install --upgrade pip
sudo -u amber .venv/bin/pip install -e .

# 5. Config — copy the example and fill in the OpenAI key
sudo -u amber cp .env.example .env
sudo -u amber nano .env            # set AMBER_OPENAI_API_KEY (and AMBER_AUTH_SECRET)

# 6. Install + start the service
cp deploy/amber.service /etc/systemd/system/amber.service
systemctl daemon-reload
systemctl enable --now amber

# 7. Verify
systemctl status amber
curl http://127.0.0.1:8000/health
journalctl -u amber -f          # live logs
```

## Updating after a code change

```bash
cd /opt/amber
sudo -u amber git pull
sudo -u amber .venv/bin/pip install -e .   # only if deps changed
systemctl restart amber
```

## Notes

- The unit binds to `0.0.0.0:8000`. Front it with nginx/Caddy for TLS (`wss://`)
  before exposing it publicly — browser WebSocket clients on HTTPS pages require
  `wss://`. Terminate TLS at the proxy and reverse-proxy to `127.0.0.1:8000`.
- Set `AMBER_AUTH_SECRET` in `.env` to require `?token=...` on the WS handshake.
- Logs go to the journal (`journalctl -u amber`). Adjust verbosity with
  `AMBER_LOG_LEVEL` (DEBUG/INFO/WARNING).
