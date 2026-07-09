# spool-notify

`spool-notify` sends a message to an ntfy server; if the send fails (early
boot before the uplink is up, endpoint unreachable) the message is spooled
locally and `spool-notify-drain` — run from a systemd timer — redelivers it
later. sbfd-ctl's notifications (`notify.py`) shell out to it with
`NOTIFY_TOPIC` set, so alerts survive exactly the network trouble they
report on.

- With `NOTIFY_TOPIC` unset, the topic comes from a shared JSON config
  (`NOTIFY_CONFIG`, key `.ntfy_topic`), so several services on one box can
  publish to a common topic.
- Spool files carry an optional `TOPIC=` header so delayed redelivery goes
  to the right topic; files without the header drain to the config topic.

Install:

    sudo install -m 0755 deploy/spool-notify/spool-notify /usr/local/sbin/spool-notify
    sudo install -m 0755 deploy/spool-notify/spool-notify-drain /usr/local/sbin/spool-notify-drain

Auth lives in `/etc/spool-notify.auth` (shell vars `NTFY_BASE`, `NTFY_USER`,
`NTFY_PASS`).

## Access for the sbfd-ctl service user

`sbfd-ctl.service` runs as the unprivileged `sbfd-ctl` user under
`ProtectSystem=strict`, so spool-notify's root-owned auth file and spool dir
are unreachable from inside the service by default. Grant access via a
shared group (this is why `spool()` chmods the dir 770, not 700 — a
root-side spool must not re-lock the group out):

    sudo groupadd -f spool-notify
    sudo chgrp spool-notify /etc/spool-notify.auth && sudo chmod 640 /etc/spool-notify.auth
    sudo mkdir -p /var/spool/spool-notify
    sudo chgrp spool-notify /var/spool/spool-notify && sudo chmod 770 /var/spool/spool-notify

and a drop-in at `/etc/systemd/system/sbfd-ctl.service.d/notifications.conf`:

    [Service]
    SupplementaryGroups=spool-notify
    ReadWritePaths=/var/spool/spool-notify

then `sudo systemctl daemon-reload && sudo systemctl restart sbfd-ctl`.

Deployment-specific choices (existing group names, an already-installed
notify helper under another name, shared auth/spool paths) belong on the
box — point the service at them with `Environment=NOTIFY_AUTH=...` /
`NOTIFY_SPOOL=...` lines in the same drop-in rather than editing the
scripts.

Tests: `tests/test_spool_notify.py` (fake curl; uses the scripts' env hooks).
