# Press CRM — Build Spec

A personal CRM for finding, pitching and managing relationships with music press, reviewers, playlist curators and creators. Built for D and the band first. A label version may come later but is out of scope for now.

---

## 1. Goals

The app has to nail two jobs:

1. **Find ~20 new, well-matched contacts a day** (press and creators).
2. **Send ~20 cold emails a day automatically**, spaced out across reasonable hours so Gmail's spam filters aren't triggered.

Everything else (replies, calendar, mobile) supports those two.

### Out of scope (for now)
- Multi-band / label features
- Automated scraping of TikTok or Instagram (against their terms; see §4.3)
- Fully automated replies (D always writes or edits replies himself)

---

## 2. Users

- **D** (owner): runs outreach, replies, sets the seed artists and the band's sound profile.
- **Bandmates**: add creators through the capture tools (§4.3). Every lead is tagged with who found it.

---

## 3. System overview

```
Seed artists ──► Seed expansion ──► Discovery engines ──► Extraction ──► Contact resolution
                                     (press + creator)     (local LLM)    (find email)
                                                                               │
Manual capture (extension / share sheet) ──────────────────────────────────────┤
                                                                               ▼
                                         Dedupe ──► Taste scoring ──► Daily queue
                                                                        │       │
                                                              Email drip     DM queue
                                                               (Gmail)       (manual)
                                                                        │
                                                         Reply detection ──► Mobile app
                                                                              (notify + reply)
```

---

## 4. Components

### 4.1 Seed expansion
- Input: a list of comparable artists. First seed: **Athletic Progression**.
- Expand each seed into ~20–30 similar artists using **Last.fm** (free). Spotify's related-artists endpoint isn't available to new apps, so don't rely on it.
- D can add or remove seed artists at any time.

### 4.2 Press discovery engine
- For each artist, search the web for recent reviews, interviews, premieres and features.
  - Web search API (Brave, SerpAPI or similar; small paid tier).
- **Global**: include non-English outlets (Danish, German, Dutch, Japanese, etc.). Translate articles for scoring; pitches go out in English.
- Fetch each result and pass it to extraction (§4.5).

### 4.3 Creator discovery engine
Automated sources:
- **YouTube Data API** (free, daily quota): reviewers, reaction channels, mixes using similar artists.
- **Playlist curators**: playlists containing similar artists.
- **TikTok sound pages**: found through regular web search (e.g. `site:tiktok.com` + track name). Lists creators who used similar artists' tracks.
- Optional paid data providers (Chartmetric, Modash, etc.). Evaluate cost later.

Manual capture (the main TikTok/Instagram route):
- **Chrome extension** (Manifest V3): adds a "Save to CRM" button on tiktok.com profiles and videos. Sends handle, profile URL and bio link to the backend.
- **Mobile share sheet**: in TikTok or Instagram, tap Share → app → creator is saved. Works on iOS and Android.
- **Android floating bubble** (optional, later): overlay over TikTok. Android only; iOS doesn't allow it.
- Every capture records **who saved it**.

### 4.4 Contact resolution
For each person, look for a contact email before falling back to DMs:
1. Outlet contact page / author bio
2. YouTube "About" business email
3. Linktree or other link-in-bio pages
4. Instagram business contact
5. Personal website

If no email is found, the creator goes into the **DM queue** (§4.8).

### 4.5 Extraction (LLM)
- Pull structured data from each page: name, outlet, role, socials, email, artists covered, article URL, date, language.
- Use a **local Qwen 3B** for this step. Keep prompts tight and require JSON output; validate against a schema and discard invalid results.
- Expect occasional made-up details from the small model. Never send to an email that wasn't found verbatim on a fetched page.

### 4.6 Dedupe
One person can appear several times across press and creator sources (personal email, outlet email, YouTube channel...). Prevent multiple pitches:
1. **Link following**: if profiles link to each other (Linktree, author bio, YouTube About), merge them into one person.
2. **Fuzzy matching**: same or similar names with overlapping outlets or handles get flagged as "probably the same"; D confirms with one tap.
3. **Cooldowns**: no repeat pitch to the same person or outlet within 30 days, even if a match slips through.

### 4.7 Taste scoring
- **Main metric: how close the writer's or creator's taste is to the band's.**
- Build a taste profile per contact from everything they've covered.
- Score against the band's sound profile (to be written; see Open questions).
- Contacts who've covered several of the seed or similar artists rank highest.
- Recency and outlet size are secondary signals only.
- The local 3B model probably isn't good enough for this. Plan: run the 3B for bulk extraction and pass only the top candidates (~20–50/day) to a larger model (bigger local model or a paid API) for scoring and translation.

### 4.8 Outreach
**Email drip**
- One shared queue fed by press contacts and creators who have emails.
- **~20 emails/day**, spaced out with randomised gaps across reasonable sending hours.
- Sent through D's Gmail via the **Gmail API** (OAuth), so emails come from his real address.
- Start lower and ramp up over the first couple of weeks to warm up sending.
- Personalisation: still being designed (see Open questions). Current idea: an opening line referencing a recent piece the contact wrote about a similar band, then a short template written once by D (who we are, one track link, one simple ask).
- Include a simple opt-out line and honour opt-outs permanently (anti-spam laws like CAN-SPAM and GDPR apply, especially with global outreach).

**DM queue**
- Creators without emails go into a daily list in the app.
- D or bandmates send DMs manually; the app suggests a message and marks it as sent.

### 4.9 Reply handling
- Watch the Gmail thread of every sent pitch for replies.
- On reply: update the contact's status and push a notification to the mobile app.

### 4.10 Calendar
- **Google Calendar** integration for booking interviews, calls and meetings with contacts.
- Meetings are linked to the contact's record.

### 4.11 Mobile companion app
A simpler app for monitoring and replying on the go:
- Push notifications when someone replies
- Today's sends, new leads and the DM queue at a glance
- **Reply from inside the app** (sends through Gmail, so it looks like a normal email from D)
- **Suggested reply drafts**, which D edits or rewrites in his own voice
- Share sheet target for capturing creators (§4.3)

---

## 5. Data model (draft)

- **Person**: id, name, merged identities, taste profile, taste score, status, cooldown until, opted out, found by
- **Identity**: person id, type (email, outlet, YouTube, TikTok, Instagram, site), value, source URL, verified
- **Outlet**: name, URL, country, language
- **Coverage**: person id, artist, article URL, date, summary
- **SeedArtist**: name, source (manual / expanded)
- **Outreach**: person id, channel (email / DM), status (queued, sent, replied, bounced), scheduled at, sent at, Gmail thread id
- **Meeting**: person id, calendar event id, time, notes
- **User**: D and bandmates

Contact statuses: `new → queued → pitched → replied → in conversation → covered / not interested`

---

## 6. Suggested tech stack (open to change)

- **Backend**: Python (FastAPI), runs the discovery pipeline and scheduler
- **Database**: SQLite to start, Postgres if it grows
- **Local LLM**: Qwen 3B (via Ollama or llama.cpp) for extraction
- **Scoring/translation**: larger model for top candidates
- **Web dashboard**: simple web UI for the queue, contacts and settings
- **Chrome extension**: Manifest V3
- **Mobile**: React Native / Expo (iOS + Android, share sheet, push notifications)
- **Integrations**: Gmail API, Google Calendar API, YouTube Data API, Last.fm API, a web search API

### Rough running costs
Mostly free tiers. Web search is the main paid piece; expect roughly tens of dollars a month at this volume. Check current API pricing before committing.

---

## 7. Build phases

1. **Discovery MVP**: seed expansion → web search → extraction → contact list in a database. No sending yet. Check lead quality by hand.
2. **Scoring + dedupe**: taste scoring, identity merging, daily top-20 queue.
3. **Email drip**: Gmail integration, spaced sending, warm-up, opt-outs, reply detection.
4. **Creator engine**: YouTube, playlists, TikTok sound pages via search.
5. **Capture tools**: Chrome extension, then the share sheet.
6. **Mobile app**: notifications, reply from phone, suggested drafts.
7. **Calendar** and nice-to-haves (bandmate leaderboard, Android bubble).

---

## 8. Open questions

- **Band sound profile**: a short description of the band's sound to score against.
- **Personalisation**: exact pitch template and how much the opening line is auto-written vs. hand-written.
- **Model size**: can D's machine run a 7B/8B, or is the 3B the ceiling? Which larger model handles scoring?
- **Phones**: do the bandmates use iPhone or Android? (Decides whether the Android bubble is worth building.)
- **Sending hours**: D's time zone or the recipient's?
- **More seed artists** beyond Athletic Progression.
- **Paid data providers**: worth it, or skip?
