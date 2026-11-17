# Porchlight

[![CI](https://github.com/tachyonFlux/porchlight-hoa/actions/workflows/ci.yml/badge.svg)](https://github.com/tachyonFlux/porchlight-hoa/actions/workflows/ci.yml)

Self-hosted, open-source management for self-managed homeowners associations. It runs in
Docker on a small server or NAS (a Synology works well), and residents use it from their
phones.

- **Homes and residents:** a directory with privacy controls, invitation links, and CSV import.
- **Dues:** invoices with PDF and QR codes, payments by check or cash, late fees, reminders,
  and a ledger for every home.
- **Meetings and polls:** one vote per home, calendar feeds.
- **Exterior change requests:** photos, board review, a decision record.
- **Courtesy notices, announcements,** and a private **Board Room** for board discussion.
- **Compliance calendar and document library:** secure, expiring share links (for
  example, for a home sale).
- **Bank import:** CSV, OFX or QFX files, with optional read-only SimpleFIN sync. Includes
  categorizing rules, deposit matching, and a tax ledger.
- **Budget planning:** look back at past years, project forward, plan reserve funding, use
  a dues calculator, compare scenarios side by side, and publish the adopted budget to residents.
- **Form 1120-H worksheet:** a draft for your tax preparer.

> Porchlight helps volunteers keep good records. It is not legal, accounting or tax advice.
> Have your governing documents, your accountant and your tax preparer check anything important.

---

## Contents

1. [What you need](#what-you-need)
2. [Install on a Synology NAS](#install-on-a-synology-nas-container-manager)
3. [Install on any Linux server](#install-on-any-linux-server)
4. [First-run setup](#first-run-setup)
5. [Email](#email)
6. [Backups and restoring](#backups-and-restoring)
7. [Upgrading](#upgrading)
8. [Configuration reference](#configuration-reference)
9. [Optional: PostgreSQL](#optional-postgresql)
10. [Optional: SimpleFIN bank sync](#optional-simplefin-bank-sync)
11. [Security notes](#security-notes)
12. [Troubleshooting](#troubleshooting)
13. [Development](#development)
14. [Contributing](#contributing)

---

## What you need

- A machine that runs Docker, for example a Synology NAS with **Container Manager** (DSM 7.2
  or later), a small Linux server, or a Raspberry Pi 4/5. Images are built for x86-64 and ARM64.
- A domain or subdomain for the site, such as `hoa.example.com`, that points at your home or
  office IP. A dynamic-DNS name works, including Synology's free `*.synology.me`.
- An SMTP account for sending email. Your email provider's SMTP settings, or a service such
  as Postmark, Fastmail or Amazon SES, will do.

Porchlight uses SQLite by default: a single file that needs no separate database server.
That's plenty for a neighborhood.

## Install on a Synology NAS (Container Manager)

These steps use DSM 7.2. Porchlight's files go in `/volume1/docker/porchlight`.

### 1. Install Container Manager and create the folder

1. Open **Package Center** and install **Container Manager**. This creates a shared folder
   named `docker`.
2. In **File Station**, open `docker` and create a folder named `porchlight`. Inside it,
   create a folder named `data`.

### 2. Create the `.env` settings file

1. On your computer, download [`.env.example`](.env.example) and open it in a text editor.
2. Fill in at least the following:
   - `PORCHLIGHT_SECRET_KEY`: a long random string. One way to make one is to run
     `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`, or use a password
     manager to generate 50+ characters.
   - `PORCHLIGHT_BASE_URL`: your site address, for example `https://hoa.example.com`.
   - `PORCHLIGHT_TIME_ZONE`, such as `America/New_York`.
   - The `EMAIL_...` lines and `DEFAULT_FROM_EMAIL` (see [Email](#email)).
   - `PUID` and `PGID`: the user that will own the files. On most Synology systems the first
     admin user is `1026` and the group `users` is `100`. To check, connect over SSH and run
     `id`.
3. Save it as `env.txt`, upload it to `docker/porchlight` with File Station, then right-click
   it, choose **Rename**, and rename it to `.env`.

The `.env` file holds your secret key and email password. Don't share it or put it in a
shared folder others can read.

### 3. Create the project

1. Open **Container Manager → Project → Create**.
2. Fill in:
   - **Project name:** `porchlight`
   - **Path:** `/docker/porchlight`
   - **Source:** *Create docker-compose.yml*. Paste the contents of
     [`docker-compose.yml`](docker-compose.yml).
3. Leave the Web Station portal option unticked. Click **Next**, then **Done**.

Container Manager downloads the image and starts two containers:

- **web**: the website, on port 8000. Only the NAS itself can reach that port; the reverse
  proxy in the next step makes it available over HTTPS.
- **scheduler**: runs daily invoices, reminders, cleanups and backups.

On first start the web container sets up the database and prints a **setup token** in its
log. You'll need this token in [First-run setup](#first-run-setup).

### 4. HTTPS with the DSM reverse proxy and Let's Encrypt

1. **Router:** forward ports **80** and **443** to your NAS. Let's Encrypt uses port 80 to
   verify your domain.
2. **Certificate:** go to **Control Panel → Security → Certificate → Add → Add a new
   certificate → Get a certificate from Let's Encrypt**. Enter your domain
   (`hoa.example.com`) and your email address.
3. **Reverse proxy:** go to **Control Panel → Login Portal → Advanced → Reverse Proxy →
   Create** and fill in:
   - **Source:** protocol `HTTPS`, hostname `hoa.example.com`, port `443`. Tick **Enable HSTS**.
   - **Destination:** protocol `HTTP`, hostname `localhost`, port `8000`.
4. **Assign the certificate:** go back to **Certificate → Settings** and choose the new
   certificate for the `hoa.example.com` reverse-proxy entry.
5. Keep `PORCHLIGHT_BEHIND_PROXY=1` and `PORCHLIGHT_TRUSTED_PROXY_COUNT=1` in `.env`. If
   Cloudflare's proxy also sits in front of the NAS, use `2`.

Only expose Porchlight through the reverse proxy. Don't forward port 8000 on your router. If
Cloudflare's proxy is in front, accept connections to the NAS from Cloudflare's addresses
only; otherwise visitors can bypass it and fake their IP address.

**Uploads:** the reverse proxy must accept requests as large as `PORCHLIGHT_MAX_REQUEST_MB`
(default 60 MB, enough for several phone photos at once). If large uploads fail with
*413 Request Entity Too Large*, the proxy's limit is too low. Either raise the proxy's limit
or lower `PORCHLIGHT_MAX_UPLOAD_MB` and `PORCHLIGHT_MAX_REQUEST_MB`.

## Install on any Linux server

```sh
mkdir -p /opt/porchlight && cd /opt/porchlight
curl -LO https://raw.githubusercontent.com/tachyonFlux/porchlight-hoa/main/docker-compose.yml
curl -L -o .env https://raw.githubusercontent.com/tachyonFlux/porchlight-hoa/main/.env.example
chmod 600 .env
nano .env                 # fill in the required settings
docker compose up -d
docker compose logs web   # shows the first-run setup token
```

Put a reverse proxy with HTTPS in front of port 8000, for example Caddy, nginx or Traefik.
With Caddy, two lines are enough:

```
hoa.example.com {
    reverse_proxy localhost:8000
}
```

## First-run setup

1. Open `https://hoa.example.com`. The setup wizard appears.
2. It asks for the **setup token**, which stops a stranger who finds your new site from
   claiming it. To find the token, do one of the following:
   - On a Synology, go to **Container Manager → Container → porchlight-web-1 → Log**.
   - On Linux, run `docker compose logs web`.
   - Open `data/setup-token.txt`.
3. Enter your HOA's details and create the first admin account. The token stops working
   once setup is finished.
4. Add homes, either one at a time or by CSV import. Then invite residents with each home's
   invitation link or QR code.
5. Board members should turn on two-step sign-in from their account page. An admin can
   require it for everyone on the board.

## Email

Residents sign in with emailed links. Invoices, reminders and notices are also sent by
email. Use the SMTP settings from your email provider:

```
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=hoa@example.com
EMAIL_HOST_PASSWORD=app-password-here
EMAIL_USE_TLS=1
DEFAULT_FROM_EMAIL=Maple Ridge HOA <hoa@example.com>
```

Gmail and Outlook need an *app password*, not your normal password. Porchlight limits bulk
email to keep you inside typical sending limits.

## Backups and restoring

Everything Porchlight stores is in the `data` folder:

| Path | What it is |
| --- | --- |
| `data/db.sqlite3` (plus `-wal`, `-shm`) | The live database. Don't copy it while running. |
| `data/backups/` | Consistent copies of the database: `db-…` daily ones from the scheduler (the newest 14 are kept) and `manual-…` ones you make, which are never deleted automatically. |
| `data/media/` | Uploaded documents, photos and attachments. |
| `.env` | Your settings and secrets. |

**On a Synology:** in **Hyper Backup**, create a backup task for the shared folder
`docker/porchlight`, ideally to another device or a cloud destination, running daily after
`PORCHLIGHT_DAILY_TASKS_HOUR`. This captures `data/backups`, `data/media` and `.env`.
Restore from `data/backups`, not from a copy of the live `db.sqlite3`.

**Make a backup now,** for example before an upgrade:

- **Without a terminal:** stop the project (Container Manager → Project → porchlight →
  **Stop**). While it's stopped, the live files are safe to copy: in File Station, copy
  `data/db.sqlite3` (and `-wal`, `-shm` if present) somewhere else. Then start it again.
- **With a terminal:** `docker compose run --rm web manage backup` writes a
  `manual-…` copy to `data/backups`. On a Synology, run it over SSH with `sudo`, in
  `/volume1/docker/porchlight`.

Always run commands through `manage …` as above. A plain `python manage.py …` in a
container terminal runs as root, and the files it creates can't be written by Porchlight
until the next restart fixes their owner.

**Restore:**

1. Stop the project (Container Manager → Project → porchlight → **Stop**, or
   `docker compose down`).
2. Copy the backup you want to `data/db.sqlite3`, and delete `data/db.sqlite3-wal` and
   `data/db.sqlite3-shm` if they exist.
3. If needed, restore `data/media` from the same date.
4. Start the project again.

Keep a copy of `.env`. If you use SimpleFIN, the saved bank connection can only be read with
`PORCHLIGHT_ENCRYPTION_KEY`.

## Upgrading

New versions apply their database changes automatically when the web container starts.

1. Make a backup (see above).
2. **On a Synology:** go to **Container Manager → Project → porchlight → Action → Stop**.
   Then go to **Image**, select `tachyonflux/porchlight` and choose **Update** (shown when
   a newer version exists). Back in **Project**, choose **Action → Build** to recreate the
   containers with it. If the version in the web log hasn't changed, use **Action → Clean**
   and then **Build**; your `data` folder isn't touched.
   **On Linux:** run `docker compose pull && docker compose up -d`.
3. Check the web container's log for errors, then sign in.

To stay on a minor version, set `PORCHLIGHT_IMAGE` in `.env`. For example,
`PORCHLIGHT_IMAGE=tachyonflux/porchlight:1.2` gets fixes but no new features. Read the
release notes before moving to a new minor version. The same images are also published as
`ghcr.io/tachyonflux/porchlight`.

## Configuration reference

All settings are environment variables in `.env`. [`.env.example`](.env.example) lists every
one with an explanation. The most important ones:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORCHLIGHT_SECRET_KEY` | *(required)* | Signs sessions and links. Keep it secret; changing it signs everyone out. |
| `PORCHLIGHT_BASE_URL` | `http://localhost:8000` | Public address, used in emails and links. Its host is allowed automatically. |
| `PORCHLIGHT_TIME_ZONE` | `America/New_York` | Local time for due dates and daily tasks. |
| `PORCHLIGHT_BEHIND_PROXY` / `PORCHLIGHT_TRUSTED_PROXY_COUNT` | `0` / `1` | Trust `X-Forwarded-*` headers from this many proxies. |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | none | SMTP settings for outgoing email. |
| `PUID` / `PGID` | `1000` / `1000` | Owner of the files in `data`. |
| `PORCHLIGHT_DAILY_TASKS_HOUR` | `6` | When invoices, reminders, cleanups and backups run. |
| `PORCHLIGHT_BACKUP_KEEP` | `14` | Daily database copies to keep (`0` turns it off). |
| `PORCHLIGHT_MAX_UPLOAD_MB` / `PORCHLIGHT_MAX_REQUEST_MB` | `15` / `60` | Upload limits. |
| `DATABASE_URL` | SQLite in `data/` | Set it to use PostgreSQL. |
| `PORCHLIGHT_SIMPLEFIN_ENABLED` / `PORCHLIGHT_ENCRYPTION_KEY` | off | Optional bank sync. |

## Optional: PostgreSQL

SQLite suits one neighborhood. If you prefer PostgreSQL, set these two lines in `.env`, using
the same password in both:

```
POSTGRES_PASSWORD=a-long-random-password
DATABASE_URL=postgres://porchlight:a-long-random-password@db:5432/porchlight
```

Also add `COMPOSE_PROFILES=postgres` to `.env`, so the database container starts with the
others (this works in Container Manager too). Its data lives in `./postgres`. The daily SQLite copies don't apply to PostgreSQL. Back it up with
`docker compose exec db pg_dump -U porchlight porchlight > porchlight.sql`, for example from a
scheduled task, and include that file in Hyper Backup.

## Optional: SimpleFIN bank sync

Bank access in Porchlight is **read-only**. The main route is downloading CSV, OFX or QFX
files from your bank's website and importing them. Porchlight never asks for or stores bank
usernames or passwords.

To have transactions pull in automatically, you can use
[SimpleFIN Bridge](https://bridge.simplefin.org), a paid third-party service:

1. Sign in at SimpleFIN Bridge and check that **your bank is supported**. The list is only
   visible after signing in.
2. Generate an encryption key and add both lines to `.env`:
   ```sh
   python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```
   ```
   PORCHLIGHT_SIMPLEFIN_ENABLED=1
   PORCHLIGHT_ENCRYPTION_KEY=the-key-you-generated
   ```
3. Restart. The treasurer then pastes a SimpleFIN *setup token* under **Finances → SimpleFIN**
   and chooses which Porchlight bank account each SimpleFIN account feeds.

The saved connection is encrypted with that key. Keep a copy of the key with your backups.

## Security notes

- **Keep the database on the NAS's own disk.** `data` must be on a local volume such as
  `/volume1`, not a network share (SMB/NFS). SQLite can corrupt data on network file systems.
- **The port is private by default.** It only listens on the NAS itself
  (`PORCHLIGHT_BIND=127.0.0.1`). Anyone who could reach it directly could skip the proxy
  and fake their IP address, so open it to your network only for testing.
- **Data files are private.** Files in `data` can only be read by the `PUID` user, even
  though Synology's `users` group (`PGID=100`) includes every account on the NAS.
- **Sign-in and share links are hidden in logs.** The access log shows them as `…`.
- **The setup token appears in logs.** It only matters until setup is finished, after which
  it's deleted. Anyone who can read your container logs before then could use it.
- **The proxy count matters.** `PORCHLIGHT_TRUSTED_PROXY_COUNT` must match the number of
  proxies in front of Porchlight. If it's too high, visitors can fake their IP address,
  which weakens sign-in rate limits.
- **Photo metadata.** JPEG, PNG and WebP uploads have location and camera data removed.
  HEIC photos (the iPhone default format) are stored as uploaded, so their metadata stays.
  To avoid this, set iPhones to *Most Compatible* (Settings → Camera → Formats), or share
  photos as JPEG.
- **Two-step sign-in** is strongly recommended for board members, who can see finances and
  personal details.
- **Never commit secrets.** Never commit or share `.env`, the `data` folder, or exported CSV
  files.
- **The Django admin is off by default.** If you turn it on with
  `PORCHLIGHT_ENABLE_DJANGO_ADMIN=1`, it still requires two-step sign-in.

## Troubleshooting

- **"Bad Request (400)":** the address you used isn't allowed. Check `PORCHLIGHT_BASE_URL`.
  To use the NAS's IP address directly (for testing), set `PORCHLIGHT_BIND=0.0.0.0` and add
  the address to `PORCHLIGHT_ALLOWED_HOSTS`.
- **The scheduler shows "unhealthy":** check its log. It waits for the web container to
  finish setting up the database, so start the web container first.
- **"CSRF verification failed" when submitting forms:** `PORCHLIGHT_BASE_URL` must match the
  address in your browser exactly, including `https://`. If your proxy doesn't send
  `X-Forwarded-Proto`, add it as a custom header (in DSM: reverse-proxy entry → **Custom
  Header** → Create, name `X-Forwarded-Proto`, value `$scheme`).
- **You can't stay signed in over plain `http://` while testing on your network:** set
  `PORCHLIGHT_SECURE_COOKIES=0` for testing only.
- **No emails arrive:** check the web container log for SMTP errors, and check spam folders.
  Many providers require `DEFAULT_FROM_EMAIL` to use your account's own address.
- **Daily tasks didn't run:** check the scheduler container's log. It runs once a day after
  `PORCHLIGHT_DAILY_TASKS_HOUR`, retrying up to three times.

## Development

You need [uv](https://docs.astral.sh/uv/) and Python 3.12. PDF generation also needs
WeasyPrint's system libraries (Pango).

```sh
uv sync
export PORCHLIGHT_DEBUG=1
uv run python manage.py migrate
uv run python manage.py seed_demo      # optional: a made-up neighborhood
uv run python manage.py runserver
```

Open http://localhost:8000. With demo data, sign in with a password at `/login/password/` as
`board@example.com` / `porchlight-demo`. Emails are printed to the console in development.

Run the tests and the linter:

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

To build and run the Docker image from source instead of pulling it:

```sh
cp .env.example .env    # fill it in; for http://localhost testing also set PORCHLIGHT_SECURE_COOKIES=0
docker compose -f docker-compose.dev.yml up --build
```

## Contributing

Bug reports, ideas and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
Please report security problems privately, as described in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
