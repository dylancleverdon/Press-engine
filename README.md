# Press Engine

A personal CRM for finding, pitching and keeping track of music press, reviewers, playlist curators and creators, built from [the spec](docs/spec.md). It does two jobs:

1. **Finds ~20 well-matched contacts a day.** It expands your seed artists into similar artists, searches the web, YouTube, TikTok (via web search) and Spotify playlists for people who cover them, reads each page with a small local model, finds a contact email, removes duplicates and ranks everyone by taste.
2. **Sends ~20 cold emails a day from your own Gmail.** Sends are spaced at random through your sending hours, with a warm-up ramp, cooldowns and permanent opt-outs. Replies are detected and pushed to your phone.

It has five parts:

| Part | What it is |
|---|---|
| [`desktop/`](desktop) | The Mac app (Tauri). Runs everything in the background, shows the dashboard, updates itself |
| [`backend/`](backend) | Python (FastAPI + SQLite). The discovery pipeline, scheduler, Gmail drip, JSON API and web dashboard |
| [`extension/`](extension) | Chrome extension (Manifest V3). Adds a "Save to CRM" button on TikTok, Instagram and YouTube |
| [`mobile/`](mobile) | Expo (React Native) app. Shows replies, today's sends and the DM queue, lets you reply, and receives shares from the TikTok/Instagram share sheet |
| [`docs/spec.md`](docs/spec.md) | The original build spec |

## Status

| Spec phase | State |
|---|---|
| 1. Discovery MVP (Last.fm → web search → extraction → contacts, CSV export) | Built, tested with mocked APIs |
| 2. Scoring + dedupe + daily top-20 queue | Built, tested |
| 3. Email drip (Gmail, spacing, warm-up, opt-outs, reply detection) | Built, tested against a fake Gmail API |
| 4. Creator engine (YouTube, Spotify playlists, TikTok via search) | Built, tested with mocked APIs |
| 5. Capture tools (Chrome extension, share sheet) | Extension built and tested end to end in headless Chromium; share sheet is in the mobile app |
| 6. Mobile app (notifications, replies, drafts) | Built; typechecks and bundles for Android, **not yet run on a phone** |
| 7. Calendar | Built (Google Calendar events with optional Meet link, tied to contacts). Leaderboard and Android bubble not built |

None of this has run against the real APIs yet (Last.fm, Brave/SerpAPI, YouTube, Spotify, Gmail, Calendar, Ollama), because this build had no keys. All tests use recorded-shape fakes. Treat the first real runs as the "check lead quality by hand" step from phase 1.

---

## Mac app

**Download: [Press-Engine-mac.dmg](https://github.com/dylancleverdon/Press-engine/releases/latest/download/Press-Engine-mac.dmg)** (works on Apple Silicon and Intel Macs, macOS 12 or later)

1. Open the DMG and drag **Press Engine** into Applications.
2. Open it. The app isn't notarized by Apple yet, so the first time macOS says it can't check it for malware. Open **System Settings → Privacy & Security**, scroll down to the message about Press Engine, click **Open Anyway** and confirm. You only do this once.
3. The first launch takes a minute or two and needs internet: the app downloads its own copy of Python and the libraries it needs. Nothing else on your Mac is touched.
4. A welcome page asks for your name, band, time zone and seed artists, then shows the token for the Chrome extension and phone app.
5. On **Connections**, add your API keys, upload your Google OAuth client and click **Connect Google** (see [Google setup](#google-setup-gmail--calendar)). If Ollama is installed, you can download the extraction model from the same page.

Day to day:

- **Closing the window doesn't stop Press Engine.** It keeps running from the menu bar, so discovery and the email drip carry on. Quit from the menu bar icon.
- **It opens at login** by default, in the background, so sends continue after a restart. You can turn that off on Connections.
- **To let the phone app connect**, tick "Let my phone connect" on Connections. The page then shows the address to use.

### Automatic updates

- **Checking.** The app checks for a new release every 6 hours, and when you choose **Check for Updates…** in the menu bar.
- **Installing.** A new version is installed next to the current one, and Press Engine restarts with it in a few seconds. It always waits for an email that's mid-send.
- **Rollback.** If the new version doesn't start, the app switches back to the previous one and never retries that version.
- **Turning it off.** Automatic updates can be switched off on Connections → Updates.
- **Updates to the app itself** (the window and menu bar part) are rare. When there is one, a banner links to the new DMG.
- **Your data stays put** through updates, and even if you delete the app. Everything lives in `~/Library/Application Support/com.dylancleverdon.pressengine/`:
  - `data/`: the database, Google token and API keys
  - `logs/backend.log`: the log

### Publishing a new version

1. Bump `__version__` in `backend/press_engine/__init__.py`.
2. Push a tag, for example `v0.2.1`:

```bash
git tag v0.2.1 && git push origin v0.2.1
```

[`.github/workflows/release-mac.yml`](.github/workflows/release-mac.yml) then:
1. Runs the tests, including the app's update and rollback test on macOS.
2. Builds the universal DMG.
3. Publishes a GitHub release with the DMG, the backend wheel and `desktop-version.txt`.

Installed apps pick it up within 6 hours.

Bump the version in `desktop/src-tauri/tauri.conf.json` (and `Cargo.toml`) only when you change the app shell itself. That turns on the "new Mac app" banner.

To get rid of the first-launch warning, sign and notarize the app with an Apple Developer ID ($99 a year). Add your certificate and Apple ID as repository secrets, and add Tauri's signing and notarization environment variables to the build step.

---

## Running without the Mac app (developers, other platforms)

You need Python 3.11+ and, for extraction, [Ollama](https://ollama.com).

```bash
cd backend
```

```bash
python -m venv .venv && . .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

Add whichever API keys you have to `backend/.env` (see [API keys](#api-keys)); anything missing is skipped. Then:

```bash
press-engine init --name D --band "Your Band" --timezone Europe/Copenhagen
```

`init` creates the database, your owner account and the first seed artist (Athletic Progression), and **prints your access token once**. Keep it; it's how you log in to the dashboard, the extension and the phone app.

```bash
ollama pull qwen2.5:3b
```

```bash
press-engine google-auth
```

```bash
press-engine serve
```

Open http://localhost:8000 and paste your token. `serve` also runs the scheduler: discovery nightly, the queue each morning, the drip every minute and reply checks every few minutes. Use `--no-scheduler` to turn that off.

Other useful commands: `press-engine status`, `press-engine discover`, `press-engine queue`, `press-engine export contacts.csv`, `press-engine user add Sofie` (bandmate token), `press-engine seed add "Mammal Hands"`. Run `press-engine --help` for the full list.

### API keys

| Variable | Used for | Notes |
|---|---|---|
| `PRESS_LASTFM_API_KEY` | Seed expansion (§4.1) | Free |
| `PRESS_BRAVE_API_KEY` or `PRESS_SERPAPI_API_KEY` (+ `PRESS_SEARCH_PROVIDER`) | Press discovery and TikTok-via-search | The main paid piece. The daily budget is a setting (default 60 press + 10 TikTok searches). Check current pricing |
| `PRESS_YOUTUBE_API_KEY` | YouTube reviewers/reaction/mix channels | Free quota (search costs 100 units; default 30 searches/day) |
| `PRESS_SPOTIFY_CLIENT_ID` / `_SECRET` | Playlist curators | Client-credentials flow. Spotify keeps restricting its API for new apps, so check this still works for yours |
| `PRESS_EXTRACT_LLM_*` | Local extraction model | Default `ollama` + `qwen2.5:3b`. `openai` works with any OpenAI-compatible server (llama.cpp, LM Studio, vLLM). `none` falls back to a metadata/keyword heuristic |
| `PRESS_SCORE_LLM_*` | Bigger model for taste scoring, translation, openers, reply drafts | Off by default (heuristic scoring only). Options: `ollama` with a bigger local model, `openai`, or `anthropic` (see below) |

**Using Claude for scoring.** Run `pip install -e ".[anthropic]"`, then set `PRESS_SCORE_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. The model defaults to `claude-opus-5-5`; set `PRESS_SCORE_LLM_MODEL` to change it and `PRESS_SCORE_LLM_EFFORT=low|medium|high` to trade depth for cost. Replies use structured JSON output. Server-side refusal fallbacks (`fallbacks: "default"`) are switched on, so a request Claude declines is retried on Anthropic's recommended fallback model. Only the top ~40 contacts a day are sent to it (`llm_score_top_n`).

### Google setup (Gmail + Calendar)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API** and **Google Calendar API**.
2. Configure the **OAuth consent screen** with user type *External* and add your Gmail address as a user.
3. Create an **OAuth client ID** of type *Desktop app* and download the JSON to `backend/data/google_client_secret.json`.
4. Run `press-engine google-auth` and approve in the browser. The token is saved to `backend/data/google_token.json`.

It asks for the scopes `gmail.send`, `gmail.readonly` (for reply detection) and `calendar.events`.

> **Gotcha:** while the consent screen is in **Testing** status, Google expires refresh tokens after 7 days, and sending would stop with an auth error. For personal use, set the publishing status to **In production**. You'll see an "unverified app" warning when you connect; that's expected for your own app.

---

## How a day works

| When (your time zone) | What happens |
|---|---|
| 03:00 | **Discovery.** Seeds are re-expanded weekly. Then press search runs in English plus Danish, German, Dutch, Swedish, Japanese and more, followed by YouTube, TikTok via search and Spotify playlists. After that: contact resolution, dedupe and scoring |
| 07:30 | **Queue.** Today's best email contacts are picked, up to the warm-up cap. Contacts without email go to the DM queue |
| You | **Approve.** On the dashboard's Today page you edit and approve each email, or "Approve all". Turn off *I approve each email* in Settings to make it fully automatic |
| 09:30–16:30 | **Drip.** Approved emails go out one at a time at random, stratified times at least 8 minutes apart, Mon–Fri. Weekends and overflow roll to the next send day |
| Every 5 min | **Replies.** Gmail is checked for replies, out-of-office auto-replies (not counted as replies), bounces and opt-out requests. A reply triggers a push notification |

All of these times and limits are in **Settings**.

### Rules from the spec, and where they're enforced

- **Never send to an email that wasn't found verbatim on a fetched page** (§4.5). The model's emails are dropped unless the exact address is in the page's HTML/text, a `mailto:` link, or Cloudflare's email protection, which is what a reader's browser shows. Forms like `name [at] site [dot] com` are kept as *unverified* and need a one-click confirm on the contact page. Author names and artists the model returns must also appear on the page. The address is checked again right before each send (`outreach/sender.py: final_check`).
- **One pitch per person and outlet** (§4.6). Shared emails, channels or handles merge people automatically: following Linktree, author pages and YouTube descriptions links profiles together. Similar names with a shared outlet or handle are flagged for one-tap review on the *Duplicates* page. Both halves of a probable duplicate are never queued on the same day.
- **30-day cooldowns** apply per person, per outlet and per email address, even when a duplicate slips through. There's also at most one pitch per outlet per day. Skipping a queued contact snoozes them for the cooldown period.
- **Opt-outs are permanent** (§4.8). A reply containing "unsubscribe", "remove me" and similar (including Danish, German and Dutch forms), or a manual opt-out, adds every address and handle of that person to a suppression list. That list survives deleting or re-discovering the person.
- **Spam safety.** Sends are plain text, one per tick, randomly spaced, and limited by a warm-up ramp (5/day rising to 20/day over 14 send days). After the machine has been asleep, overdue sends are re-planned rather than fired in a burst.
- **No TikTok/Instagram scraping** (§1, §4.3). TikTok creators come only from web-search result URLs; tiktok.com is never fetched. TikTok sound pages found that way are listed on the DM page so someone can browse them with the extension. The extension reads only the page the user is looking at, when they click.
- **D writes the replies** (§1). Suggested drafts only fill the text box; nothing is sent until you press Send.

### Compliance note

The default template ends with an opt-out line, and opt-outs are honoured permanently. Anti-spam rules differ by country, and some laws (for example CAN-SPAM in the US) can require a postal address in commercial email. If that applies to you, add one to the signature in Settings. This isn't legal advice; check what applies to you and your recipients.

---

## Chrome extension

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the `extension/` folder.
2. Click the extension's toolbar icon to open its options. Enter the backend address (`http://localhost:8000`, or your tunnel URL) and your token, then click **Test connection**.
3. On a TikTok, Instagram or YouTube profile or video, click **Save to CRM**. The ✎ button saves with a note, or with a business email you can see on the page.

Each capture records who saved it. Bandmates get their own token from Settings → People.

The extension test runs it against fake TikTok pages and needs a running backend:

```bash
PRESS_URL=http://localhost:8000 PRESS_TOKEN=pe_... node extension/test/smoke.mjs
```

## Mobile app

The app is built on Expo SDK 58 with Expo Router. Because of the share extension and push notifications it needs a **development build**; Expo Go won't work. `expo-dev-client` and an `eas.json` with a `development` profile are already set up.

```bash
cd mobile
```

```bash
npm install
```

```bash
npx eas-cli@latest init
```

```bash
npx eas-cli@latest build --profile development --platform ios
```

Use `--platform android` for Android builds.

Before the first build:

- Change `com.example.pressengine` in `mobile/app.json` to your own bundle ID or package name.
- `eas init` fills in `extra.eas.projectId`, which push notifications need.
- On iOS the share extension uses the App Group `group.<bundle id>`; EAS sets it up during the build.
- The phone must be able to reach the backend. Easiest options: [Tailscale](https://tailscale.com) on the computer and phone, or a tunnel such as `cloudflared`. Then put that address in the app's Settings with your token.

Screens:
- **Today**: stats, approve or skip, scheduled and sent emails.
- **Replies**: thread view, "Suggest a draft", edit, send from Gmail.
- **DMs**: copy the message, open the profile, mark as sent.
- **Settings**: backend address, token, reply notifications.

Sharing a TikTok or Instagram link to the app opens **Save creator**.

Check the app with `npx tsc --noEmit` and `npx expo export --platform android`. Both pass.

---

## Open questions from the spec: what I picked and where to change it

| Question (§8) | Default | Where |
|---|---|---|
| Band sound profile | Empty. Scoring uses the seed artists until you write it; the dashboard reminds you | Settings → Band |
| Personalisation | Template opener ("I really enjoyed your review of X in Y"), then your template. Switch to `llm` to use the bigger model's line (it must reference a real piece of theirs), or `manual` to write each opener yourself | Settings → Pitch |
| Model size | `qwen2.5:3b` for extraction; scoring is heuristic until you configure a bigger model | `.env` |
| Phones | iOS and Android both supported. The Android floating bubble isn't built | — |
| Sending hours | Your time zone, 09:30–16:30, Mon–Fri. "Recipient" mode guesses each contact's time zone from country or language | Settings → Sending |
| More seed artists | Athletic Progression | Seed artists page / `press-engine seed add` |
| Paid data providers | Not used | — |

## Development

```bash
cd backend && . .venv/bin/activate
```

```bash
pytest
```

There are 55 backend tests covering discovery, extraction guards, resolution, dedupe and merges, scoring, queue and scheduling, Gmail sending, reply/bounce/opt-out detection, capture, calendar, the API, the dashboard and the scheduler.

The Mac app's logic is tested with Rust unit tests. A separate end-to-end test covers first-run install, restart, promoting an update and rolling back a broken one, using the real backend wheel and uv. It needs network:

```bash
cd desktop/src-tauri && cargo test --lib
```

```bash
E2E_WHEEL=$PWD/../../dist/press_engine-0.2.0-py3-none-any.whl E2E_UV=$(which uv) cargo test --test update_protocol -- --ignored
```

Mac app (`desktop/`):
- `src-tauri/src/backend.rs`: first-run install, starting and watching the backend, updates and rollback.
- `src-tauri/src/lib.rs`: window, menu bar item, launch at login, opening links in the browser.
- `src/index.html`: the loading and error screen.
- `scripts/prepare-resources.sh`: puts uv and the backend wheel into the bundle.

Code map (`backend/press_engine/`):
- `discovery/`: Last.fm, search, page fetching and parsing, extraction, the press/YouTube/TikTok/playlist engines.
- `contacts/`: ingest, resolution, dedupe.
- `scoring.py`
- `outreach/`: templates, queue, schedule, sender, Gmail, replies.
- `calendar.py`, `capture.py`, `jobs.py`, `scheduler.py`
- `updater.py`, `runtime.py`: self-updates and restarts inside the Mac app.
- `api/`: JSON API and dashboard.
- `cli.py`

## Known gaps and next steps

- **Schema migrations.** New tables and new columns are added automatically on startup, so updates can extend the schema. Renaming or dropping columns still needs a hand-written migration (Alembic).
- **Email sources the APIs don't expose.** YouTube's "business email" button and Instagram business emails aren't available through any API. They're read from the channel description, or typed in through the extension's ✎ button or the app's capture screen.
- **Selectors drift.** TikTok and Instagram change their page markup often. The extension's selectors live in `extension/collect.js`.
- **Reply detection covers the last 90 days of sent threads.** Replies that arrive as a new thread are found by sender address within 21 days.
- **One process.** The scheduler runs inside `press-engine serve` (or `press-engine worker`). Don't run both against the same database, or the Mac app and a terminal copy against the same data folder.
