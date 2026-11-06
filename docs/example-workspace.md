# Example workspace

A minimal, fake workspace that shows the layout the console expects. Every
address here is documentation-only (`192.0.2.0/24`, TEST-NET-1); nothing will
be reachable.

Copy it, or generate the same layout with `./hlc init --example ~/homelab/workspace`:

```text
workspace/
├── scripts/hosts.json
├── stacks/example/whoami/docker-compose.yml
└── .gitignore
```

Then point the console at it from the homelab checkout:

```sh
# use this example folder in place
HLC_REPO="$PWD/examples/workspace" ./hlc

# or, if you copied the folder somewhere else
HLC_REPO="$HOME/homelab/workspace" ./hlc
```

For the native app, use **Homelab Console → Choose Workspace…** and select this
folder. Replace `example` with your own host once you have one.
