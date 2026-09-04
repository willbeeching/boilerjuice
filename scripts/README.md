# Manual scripts

Throwaway diagnostics, not tests. They talk to the live BoilerJuice site (or
to a page you saved from it), so they need real credentials and a network
connection. `pytest` never collects them: they live outside `tests/` and do
not carry `test_` names.

Both drive the integration's own client and parser rather than a second copy
of them, so what they report is what Home Assistant would see. That means
they need Home Assistant installed, which the test environment already has:

```bash
.venv/bin/python scripts/check_live_account.py
```

Output is redacted by default - which fields parsed, not what they contain -
so it can be pasted into a public issue. `--show-values` prints the readings
themselves and warns you first.

## `check_live_account.py`

Signs in with the credentials in your `.env` (see `.env.example`) and reports
what the integration can read from each tank on the account. Use it when
readings stop and you need to know which field the site stopped serving.

Because it uses the real client, it inherits the same protections: explicit
timeouts, and a sign-in that refuses to follow a redirect off
boilerjuice.com with your password attached.

## `check_saved_tank_page.py`

Runs the real parser over a `tank_page.html` you saved from a browser, so you
can work on parsing without hitting the site.

Do not attach that saved page to an issue. It contains your account details.

## `check_versions.py` and `check_archive.py`

Used by CI rather than by hand. The first checks that the manifest, the HACS
floor and the CI matrix agree; the second checks a built release archive
against the working tree before anything is published.
