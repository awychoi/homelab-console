"""Safe starter templates for new stacks.

Every template publishes on loopback only (rule 6), pins an image tag, and
leaves the front door to the host's Caddy.  They are starting points, not
magic: the user can edit everything before deploying.
"""

from __future__ import annotations

from typing import Any

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "blank",
        "name": "Blank stack",
        "description": "An empty compose file with the fleet conventions in comments.",
        "compose": """# New stack — a file in this repo is the source of truth.
# deploy.py / Homelab Console push this to /opt/stacks/<name>/.
#
# Exposure rule: publish on loopback only; put a vhost in this host's
# Caddyfile if humans need a name for it.
services:
  example:
    image: nginx:1.29.0
    container_name: example
    restart: unless-stopped
    ports:
      - "127.0.0.1:8088:80"
""",
        "env": "",
    },
    {
        "id": "whoami",
        "name": "Whoami (diagnostic)",
        "description": "Tiny HTTP echo service — useful to prove push + recreate end to end.",
        "compose": """# whoami — disposable diagnostic endpoint on loopback :9180.
services:
  whoami:
    image: traefik/whoami:v1.11.0
    container_name: whoami
    restart: unless-stopped
    ports:
      - "127.0.0.1:9180:80"
""",
        "env": "",
    },
    {
        "id": "uptime-kuma",
        "name": "Uptime Kuma",
        "description": "Self-hosted uptime monitor. Data lives at /srv/uptime-kuma.",
        "compose": """# Uptime Kuma — loopback :3001, front it with Caddy if wanted.
services:
  uptime-kuma:
    image: louislam/uptime-kuma:1.23.16
    container_name: uptime-kuma
    restart: unless-stopped
    ports:
      - "127.0.0.1:3001:3001"
    volumes:
      - /srv/uptime-kuma:/app/data
""",
        "env": "",
    },
    {
        "id": "dozzle",
        "name": "Dozzle (log viewer)",
        "description": "Container log viewer. Read-only docker socket mount; loopback :9999.",
        "compose": """# Dozzle — loopback :9999. The docker socket is powerful: keep this local.
services:
  dozzle:
    image: amir20/dozzle:v8.12.8
    container_name: dozzle
    restart: unless-stopped
    ports:
      - "127.0.0.1:9999:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
""",
        "env": "",
    },
]
