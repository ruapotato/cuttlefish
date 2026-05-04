# TLS

> **Status: planned, not implemented.** Cuttlefish currently runs over plain
> HTTP. This document captures the design so the implementation lands cleanly.

## Goals

- Real, browser-trusted certificates (Let's Encrypt).
- Works for users on **any DNS host**, not just Cloudflare. Cloudflare is the
  documented default; other providers are a config swap.
- Fully automated renewal — set it up once, never think about it again.
- Doesn't require an open inbound port for the ACME challenge (so home
  servers behind NAT or carrier-grade NAT still work).

## Approach: Let's Encrypt + ACME DNS-01 via certbot

We shell out to [`certbot`](https://certbot.eff.org/) with the appropriate
[`certbot-dns-*`](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins)
plugin. Certbot handles ACME, the plugin handles writing the challenge TXT
record to your DNS provider.

**Why DNS-01 over HTTP-01:** doesn't need port 80 open, works behind NAT,
and supports wildcard certificates if you ever want them.

## Configuration sketch

```toml
# cuttlefish.toml
[tls]
enabled = true
domain = "media.example.com"
email = "you@example.com"          # for LE expiry warnings

dns_provider = "cloudflare"        # or route53, digitalocean, gandi, ...
dns_credentials_file = "/etc/cuttlefish/dns-credentials.ini"

cert_dir = "/etc/letsencrypt/live/media.example.com"
renew_check_interval_hours = 24
```

The format of `dns_credentials_file` is whatever certbot's chosen plugin
expects — see the plugin's docs.

## What cuttlefish will do

On startup with `tls.enabled = true`:

1. Check `cert_dir/fullchain.pem` exists and isn't expiring within 30 days.
2. If missing or near-expiry, run:
   ```
   certbot certonly --non-interactive --agree-tos \
       --email "$email" \
       --dns-$provider \
       --dns-$provider-credentials "$creds_file" \
       -d "$domain"
   ```
3. Configure uvicorn (or the fronting process) with the cert + key.
4. Schedule a daily check (in-process or as a systemd timer) and reload the
   TLS context when the cert file changes — uvicorn picks up cert reloads on
   `SIGHUP`.

## Supported DNS providers

Anything with a [certbot DNS plugin](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins).
Cuttlefish doesn't ship its own provider integrations; we delegate.

Common picks:

| `dns_provider` | install hint |
|---|---|
| `cloudflare` | `pip install certbot-dns-cloudflare` |
| `route53` | `pip install certbot-dns-route53` |
| `digitalocean` | `pip install certbot-dns-digitalocean` |
| `gandi` | `pip install certbot-plugin-gandi` |
| `linode` | `pip install certbot-dns-linode` |

## Why we picked this over Cloudflare Origin CA

We considered three options:

1. **Cloudflare Origin CA** — 15-year cert, only valid through Cloudflare's
   proxy. Gets you free TLS + IP hiding without ACME.
2. **Let's Encrypt + DNS-01 via certbot.** ← what we picked.
3. **Caddy as a reverse proxy** — handles ACME automatically. Adds a process
   to deploy.

We picked (2) because the user wanted "the one that's really easy for anyone
even on non-Cloudflare control." Origin CA locks you into Cloudflare; option
(2) works everywhere and is still single-process.

## Local dev / unconfigured behavior

When `tls.enabled = false` (the default), cuttlefish serves plain HTTP on
the configured port. Recommended for dev. Production should always set
`tls.enabled = true`.
