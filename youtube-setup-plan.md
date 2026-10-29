# Attaching a YouTube channel to Snipster

Goal: clips produced by the pipeline get posted to a YouTube channel as Shorts
with as little manual work as possible.

## What already exists (don't rebuild this)

The YouTube integration is written and wired end to end:

| Piece | Location |
|---|---|
| "Connect" button | `frontend/src/pages/Settings.tsx:71` → `getGoogleLoginUrl()` |
| OAuth start | `backend/app/api/routes/auth.py:38` `GET /api/auth/google/login` |
| OAuth callback | `backend/app/api/routes/auth.py:52` `GET /api/auth/google/callback` |
| Token storage | encrypted via `encrypt_token()` into `users.yt_access_token` / `yt_refresh_token` |
| Publish endpoint | `backend/app/api/routes/publish.py:20` `POST /api/clips/{id}/publish` |
| Upload worker | `backend/app/workers/tasks.py:356` `publish_clip_task` |
| Upload call | `backend/app/services/youtube.py:26` `YouTubeService.upload()` |

Scopes already requested: `youtube.upload` + `userinfo.email`, with
`access_type=offline` and `prompt=consent` (so Google does return a refresh
token).

Upload metadata already set: title = caption truncated to 100 chars, description
= caption + hashtags + `#Shorts`, category 22 (People & Blogs),
`privacyStatus: "public"`, `selfDeclaredMadeForKids: false`.

**So Phases 1–6 below are pure configuration. Phase 7 is the code work that
stands between you and *automatic* posting.**

---

## Phase 1 — Create the YouTube channel

1. Decide which Google account owns it. Use a **dedicated account**, not your
   personal one — you're handing an API client permanent upload rights.
2. Go to <https://www.youtube.com/> signed in as that account.
3. Profile icon → **Create a channel**.
4. Choose **Use a custom name** if you want a Brand Account (recommended — it can
   be transferred and have multiple managers later). Enter the channel name.
5. Set the handle (`@something`). This is separate from the display name and must
   be globally unique.
6. Go to **YouTube Studio → Settings → Channel → Feature eligibility** and
   complete **phone verification**. Without it you cannot upload videos longer
   than 15 minutes, use custom thumbnails, or appeal Content ID claims. Do this
   now; it takes 2 minutes and is a prerequisite later.
7. Upload one video manually (can be unlisted). A channel with zero uploads and
   no history is more likely to get flagged when an API starts posting to it.
8. Studio → **Customization → Basic info**: set the description/bio, links, and
   contact email. Set banner and avatar.

**Checkpoint:** channel exists, phone-verified, has ≥1 manual upload.

---

## Phase 2 — Google Cloud project

1. Go to <https://console.cloud.google.com/>, signed in as the **same account
   that owns the channel**.
2. **Create a new project** — name it `snipster`. Don't reuse an existing project;
   the YouTube compliance audit in Phase 8 applies to the whole project.
3. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
4. That's the only API you need. Do not enable others — extra scopes make the
   audit harder.

---

## Phase 3 — OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **External**. (Internal is only available with Google Workspace.)
3. App information:
   - App name: `Snipster`
   - User support email: your email
   - App logo: optional now, **required for the Phase 8 audit**
4. App domain — leave blank while testing. Required for audit:
   application home page, privacy policy URL, terms of service URL.
5. Developer contact email: your email.
6. **Scopes** → *Add or remove scopes* → add exactly these two:
   ```
   https://www.googleapis.com/auth/youtube.upload
   https://www.googleapis.com/auth/userinfo.email
   ```
   `youtube.upload` is classed as a **restricted scope**. Google will warn you.
   That warning is the thing Phase 8 resolves.
7. **Test users** → add the Google account that owns the channel.
8. Save. Leave publishing status as **Testing** for now.

> **Testing vs Production — read this before you pick.**
>
> - **Testing:** only listed test users can authorise. Refresh tokens **expire
>   after 7 days**, so you must reconnect the channel weekly. Fine for getting it
>   working, unusable for unattended automation.
> - **In production (unverified):** anyone can authorise, but they see a scary
>   "Google hasn't verified this app" interstitial — you click *Advanced → Go to
>   Snipster (unsafe)* to get past it. Refresh tokens **do not** expire on a
>   7-day clock. For a single-user personal tool this is the practical setting.
>
> Recommendation: get it working in Testing, then flip to **In production** and
> live with the unverified warning, since you're the only user.

---

## Phase 4 — OAuth client credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Name: `snipster-local`.
4. **Authorized redirect URIs** — add this exactly, character for character:
   ```
   http://localhost:8000/api/auth/google/callback
   ```
   It must match `GOOGLE_REDIRECT_URI` in your `.env` exactly, including the
   scheme, port, and no trailing slash. A mismatch gives `redirect_uri_mismatch`.
   Google permits `http://localhost` — you do not need HTTPS for local work.
5. Authorized JavaScript origins: not needed (the flow is server-side), but
   `http://localhost:5173` does no harm.
6. **Create** → copy the **Client ID** and **Client secret**.

---

## Phase 5 — Put the credentials into the app

Edit `backend/.env`:

```
GOOGLE_CLIENT_ID=<the client id>
GOOGLE_CLIENT_SECRET=<the client secret>
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/google/callback
FRONTEND_URL=http://localhost:5173
```

Confirm `TOKEN_ENCRYPTION_KEY` is set and **never changes** — it is the Fernet
key used by `encrypt_token()`. If you rotate it, every stored YouTube token
becomes undecryptable and you must reconnect.

Restart the worker and the API so both pick up the new env (on Vercel: add the variables in the project settings and redeploy; on your PC: restart the worker).

---

## Phase 6 — Connect the channel and do one real test

1. Open <http://localhost:5173/settings>.
2. YouTube card → **Connect**. You'll be sent to Google.
3. Sign in as the channel-owning account. If the account has multiple channels,
   Google shows a channel picker — **pick the right one**, this is the step that
   decides where clips land.
4. Grant "Manage your YouTube videos". You'll be redirected back to
   `/settings?yt=connected` and the card should read `✓ <channel name>`.
5. Process a video, approve one clip, hit **Shorts**.
6. Watch it:
   Watch the worker terminal for lines containing `PUBLISH` or `YOUTUBE`.
   Success looks like `[YOUTUBE] Published: https://www.youtube.com/shorts/<id>`.
7. **Open that URL and check the privacy setting in Studio.** See Phase 8 — an
   unaudited project's uploads are expected to come out **private** even though
   the code asks for `public`. Confirm this empirically rather than assuming.

**Checkpoint:** one clip is on the channel.

---

## Phase 7 — Code changes required for *automatic* posting

These are real gaps, in priority order. None are hypothetical.

### 7.1 — Token refresh is not implemented (**blocker**)

`backend/app/services/youtube.py:21`:

```python
def _get_valid_token(self, user) -> str:
    access_token = decrypt_token(user.yt_access_token)
    # TODO: check expiry and refresh if needed
    return access_token
```

Google access tokens live **one hour**. The refresh token is already being
captured and stored — it is simply never used. So publishing works for the first
hour after you connect, then fails with `401` until you reconnect by hand.

Needs: store `yt_token_expires_at`, and on each upload, if the token is expired
or near expiry, POST to `https://oauth2.googleapis.com/token` with
`grant_type=refresh_token` and persist the new access token. This requires a
migration to add the expiry column.

### 7.2 — Nothing triggers publishing (**blocker for "automatic"**)

The pipeline stops at `READY`. A human must click Approve, then Publish. There is
no auto-publish path anywhere in `workers/pipeline.py` or `tasks.py`.

Needs a decision from you:
- **Option A (safe):** keep manual approval. "Automatic" means one click, not
  zero. Recommended while the LLM's clip picks are unproven.
- **Option B (fully hands-off):** add an `AUTO_PUBLISH` setting; when true, have
  `generate_caption_task` chain into `publish_clip_task` once render + caption
  are both done. You will be posting unreviewed AI-selected clips to a public
  channel — a bad clip or a misread transcript goes out under your name.

### 7.3 — Scheduled publishing silently does nothing

`backend/app/api/routes/publish.py:59` sets `PublishStatus.SCHEDULED` when
`scheduled_at` is given, and then **never queues a task** — the `.delay()` call
is inside `if not payload.scheduled_at`. There is no Celery beat schedule in the
project. A scheduled job sits in the DB forever.

Needs: either a beat task that sweeps due `SCHEDULED` jobs, or use
`publish_clip_task.apply_async(eta=...)`, or remove the feature from the UI.

### 7.4 — Quota ceiling: 6 uploads per day

YouTube Data API v3 gives a default **10,000 units/day**, and `videos.insert`
costs **1,600 units**. That is **6 uploads per day**, hard stop; the 7th returns
`quotaExceeded`. Quota resets at midnight Pacific.

Your pipeline produced 4 clips from one 7-minute video. Two source videos a day
will hit the wall.

Needs: either accept the limit and only publish your best picks, or apply for a
quota increase (a separate form from the audit, and it expects the audit to be
done first). At minimum, `publish_clip_task` should detect `quotaExceeded` and
stop retrying — right now it retries with exponential backoff against a quota
that won't reset for hours.

---

## Phase 8 — Getting uploads to actually be public

Until the Google Cloud project passes YouTube's **API Services compliance
audit**, videos uploaded through the API are expected to be forced to **private**
regardless of the `privacyStatus: "public"` your code sends. You can then flip
each one to public manually in Studio, which defeats the automation.

To apply you need, at minimum:
- a public home page for the app
- a published privacy policy and terms of service
- the OAuth consent screen fully filled in with a logo
- a demo video showing the OAuth flow and how the scope is used
- the audit form: <https://support.google.com/youtube/contact/yt_api_form>

Turnaround is typically weeks and it is a real review.

**Be honest with yourself about which you want:**
- *Personal tool, you flip clips to public by hand* → skip the audit. Keep the
  project in "In production/unverified". Fix 7.1 and you're done.
- *Truly unattended public posting* → the audit is mandatory, and you should do
  Phase 8 before investing in 7.2 Option B.

---

## Order of work

1. Phases 1–6 (configuration) — an evening.
2. **7.1 token refresh** — without it nothing survives an hour.
3. Test for a week with manual approval.
4. Then decide on 7.2, 7.4, and whether Phase 8 is worth it.
