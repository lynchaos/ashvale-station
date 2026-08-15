# Security Policy

## Supported versions

The latest release on `main` is the supported version.

## Reporting a vulnerability

Please email **support@yaylali.uk** rather than opening a public issue.

Include what you found, how to reproduce it, and what an attacker could achieve.
You should get an acknowledgement within a few days. This is a personal project
maintained in spare time, so please be patient with fix timelines.

## Deployment note worth reading

Ashvale Station ships **no authentication and no TLS**. It is designed to sit on
a trusted home network, and the default bind address is `0.0.0.0`, meaning
anything on your LAN can reach it.

Do not port-forward it to the open internet. If you want remote access, put it
behind a reverse proxy that terminates TLS and handles authentication, or reach
it over a VPN or a WireGuard tunnel. The API includes endpoints that mutate model
state (`/api/train`, `/api/calibrate`, `/api/label`), so an exposed instance is a
system a stranger can degrade.

To restrict it to the local machine only:

```yaml
server:
  host: 127.0.0.1
```
