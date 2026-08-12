# Security Policy

## Supported versions
The latest `0.1.x` release receives security fixes.

## Reporting a vulnerability
Please **do not** open a public issue for security problems. Instead email the
maintainers or use GitHub's private vulnerability reporting for this repository.

Include:
- A description of the issue and its impact.
- Steps to reproduce (or a PoC).
- Affected version(s).

We aim to acknowledge reports within a few days and will coordinate a fix and
disclosure timeline with you.

## Known trust boundaries
- Your Telegram `api_id`/`api_hash` and session file are stored **locally only**
  (see [PRIVACY.md](PRIVACY.md)); they are never transmitted to anyone except
  Telegram's own API.
- The streaming HTTP server binds to `127.0.0.1` only and is token-addressed;
  it is not reachable from the network.
