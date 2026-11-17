# Contributing to Porchlight

Thanks for helping. Porchlight is built for small, volunteer-run homeowners associations,
so changes should keep it simple to install, simple to use, and safe with residents' data.

## Before you start

- **Bugs:** open an issue with steps to reproduce it. Leave out real names, addresses and
  bank details.
- **Security problems:** report them privately (see [SECURITY.md](SECURITY.md)), not in an
  issue.
- **Bigger changes or new features:** open an issue first so we can agree on the approach
  before you spend time on it.

## Ground rules

- **Your own work only.** Don't copy code, text, screenshots or branding from commercial HOA
  software or other products. Contributions are licensed under the project's
  [MIT license](LICENSE).
- **No real data.** Never commit `.env` files, databases, uploads, bank exports (CSV, OFX,
  QFX) or anything with real people's details. Test fixtures must be made up.
- **Bank access stays read-only.** Porchlight imports statement files and can optionally read
  from SimpleFIN. It must never store bank usernames or passwords, or move money.
- **Tax features are worksheets, not advice.** Anything tax-related has to say so, and tax
  numbers need a source (usually that year's IRS instructions).

## Setting up

You need [uv](https://docs.astral.sh/uv/), Python 3.12, and WeasyPrint's system libraries
(Pango) for PDFs. On Debian or Ubuntu:
`sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`.

```sh
uv sync
export PORCHLIGHT_DEBUG=1
uv run python manage.py migrate
uv run python manage.py seed_demo      # a made-up neighborhood to click around in
uv run python manage.py runserver
```

Sign in at http://localhost:8000/login/password/ as `board@example.com` /
`porchlight-demo`.

## Making a change

1. Branch from `main`.
2. Keep each change focused. Model changes need a migration
   (`uv run python manage.py makemigrations`).
3. Add tests next to the app you changed (`apps/<app>/tests/`). Test permissions as well
   as the happy path: what a resident, a board member and a treasurer can each see and do.
4. Run the checks CI runs:

   ```sh
   uv run ruff check . && uv run ruff format --check .
   uv run pytest
   ```

5. Update the README or `.env.example` if you changed setup or added a setting.
6. Open a pull request and fill in the checklist.

## Code conventions

- Money is stored as integer cents; use `apps.core.money` for parsing and display.
- Put logic in `services.py` and keep views thin.
- Check permissions in every view with the decorators in `apps.core.permissions` (for example
  `board_required`, `treasurer_required`).
- Record changes to important records with `apps.core.audit.record`.
- Write interface text in plain language for volunteers, not accountants or programmers.

## Releases

Maintainers release by bumping `version` in `pyproject.toml`, committing, and pushing a
matching tag (`git tag v1.2.3 && git push origin v1.2.3`). The release workflow runs the
tests, publishes the multi-architecture image and creates the GitHub release.
