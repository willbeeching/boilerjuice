# Security policy

## Supported versions

Only the latest release gets fixes. If you are on an older one, upgrade
first and check whether the problem is still there.

## Reporting a vulnerability

Report privately through GitHub's
[security advisories](https://github.com/willbeeching/boilerjuice/security/advisories/new)
rather than opening an issue. Include what an attacker can do, not just what
looks wrong, and give a way to reproduce it.

You should get an acknowledgement within a week. This is a hobby project
maintained by one person, so there is no formal fix timeline: expect a fix
proportional to the severity, and credit in the release notes if you want it.

## What this integration does with your credentials

BoilerJuice has no public API, so the integration signs into your account
with your email and password and reads the tank page.

- Your credentials are stored by Home Assistant in its config entry store,
  the same as every other integration that needs a login. That store is
  plain JSON on disk: anyone who can read your Home Assistant configuration
  directory can read them.
- They are sent to `https://www.boilerjuice.com` over HTTPS, and nowhere
  else.
- Each configured account gets its own session and its own cookie jar. They
  are never shared with another account or with another integration.
- Nothing in the integration logs a password, an email address, a tank ID, a
  CSRF token, a cookie or page HTML, at any log level.
- Diagnostics downloads are written to contain none of those either, so they
  are safe to attach to a public issue. There is a test that fails if any of
  them appears in the output.

If you would rather not store the password at all, this integration cannot
help: there is no token or API key to use instead.

## Scope

In scope: anything that leaks credentials, sends them somewhere other than
BoilerJuice, or lets a crafted BoilerJuice response run code or corrupt
another account's stored data.

Out of scope: BoilerJuice's own website, and the fact that Home Assistant
stores integration credentials unencrypted, which is a Home Assistant design
decision rather than something this integration can change.
