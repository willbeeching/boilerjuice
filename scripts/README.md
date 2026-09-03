# Manual scripts

These are throwaway diagnostic scripts, not tests. They talk to the live
BoilerJuice site (or to a page you saved from it) and print what they find,
so they need real credentials and a network connection. `pytest` never
collects them: they live outside `tests/` and no longer carry `test_` names.

Run them from the repository root with the extra dependencies installed:

```
pip install -r scripts/requirements.txt
```

## `check_live_account.py`

Signs in with the credentials in your `.env` (see `.env.example`) and prints
the tank page it finds. Use it when the integration stops parsing and you
need to see what BoilerJuice is actually serving.

## `check_saved_tank_page.py`

Runs the field-by-field parse over a `tank_page.html` you saved from a
browser, so you can work on parsing without hitting the site. Sanitise the
page before sharing it: it contains your account details.
