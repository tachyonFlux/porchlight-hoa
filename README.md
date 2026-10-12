# Porchlight

Self-hosted, open-source management for self-managed HOAs: dues tracking, meeting polls,
resident requests, documents, compliance deadlines, bank import, and budget planning.

> Work in progress. Full deployment docs (Docker, Synology) are coming in a later release.

## Development quick start

```sh
uv sync
export PORCHLIGHT_DEBUG=1
uv run python manage.py migrate
uv run python manage.py seed_demo      # optional: fake neighborhood
uv run python manage.py runserver
```

Open http://localhost:8000. With demo data, sign in with password at `/login/password/`
as `board@example.com` / `porchlight-demo`. Without demo data, the setup wizard runs first.
Emails (sign-in links, claim confirmations) print to the console in development.

Run the tests and linter:

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## License

MIT. See [LICENSE](LICENSE).
