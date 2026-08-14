"""Update tool — bring the running deploy in sync with the repo (Phase 4+).

``update_server`` runs the configured update command (normally
``deploy/update.sh``: git pull, deps, .env reconcile, systemd, restart). It's a
privileged, server-mutating action, so — like the OpenClaw bridge — it's only
offered to the model when ``AMBER_UPDATE_COMMAND`` is set; ``available`` hides it
otherwise, and ``dispatch`` refuses to run it.

**Self-restart caveat.** The update script restarts the amber service, which (under
systemd's default cgroup kill) can take this very process — and the subprocess we
spawned — down with it before the script finishes. Configure ``update_command`` to
run the script *detached* from the service cgroup, e.g.::

    AMBER_UPDATE_COMMAND=sudo systemd-run --collect --unit=amber-update bash /opt/amber/deploy/update.sh

so the update survives the restart. We wait up to ``update_timeout_s`` for output;
if the command outlives that (or restarts us), the detached job keeps going and the
model is simply told the update was kicked off.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.tools.registry import registry

logger = logging.getLogger(__name__)

# Keep the result the model reads short — it's spoken aloud. We return a status
# line plus the tail of the command's output if anything was captured.
_MAX_OUTPUT_CHARS = 1500


def _configured() -> bool:
    """The tool is usable only once an update command is configured."""
    return bool(get_settings().update_command.strip())


@registry.register(
    name="update_server",
    description=(
        "Update Amber itself on the server: pull the latest code, reinstall "
        "dependencies, and restart the service. Use this only when the user "
        "explicitly asks to update, upgrade, or redeploy Amber. The server may "
        "briefly restart as part of this."
    ),
    input_schema={"type": "object", "properties": {}},
    available=_configured,
)
async def update_server() -> str:
    settings = get_settings()
    command = settings.update_command.strip()
    if not command:
        return "Updating isn't configured (set AMBER_UPDATE_COMMAND)."

    logger.info("update_server: running %r", command)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        logger.warning("update_server: failed to launch: %s", exc)
        return f"Couldn't start the update ({exc})."

    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=settings.update_timeout_s
        )
    except asyncio.TimeoutError:
        # The script is likely mid-restart (or genuinely slow). Don't kill it —
        # let the (ideally detached) job finish; just report that it's underway.
        logger.info("update_server: command still running after timeout")
        return "Update started — it's running now and the server may restart."

    output = (stdout or b"").decode("utf-8", "replace").strip()
    tail = output[-_MAX_OUTPUT_CHARS:]
    if proc.returncode == 0:
        logger.info("update_server: completed ok")
        return "Update finished successfully." + (f"\n{tail}" if tail else "")
    logger.warning("update_server: exited %s", proc.returncode)
    return f"Update failed (exit {proc.returncode}).\n{tail}".rstrip()
