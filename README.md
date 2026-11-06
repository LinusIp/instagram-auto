# Instagram autopost

Drop videos and images into `queue/`. Three times a day GitHub takes the next one, adds the
series title and English subtitles (videos), writes a caption with hashtags, and posts it.
Everything runs on GitHub's servers, so you don't need your own computer.

| You put in `queue/` | It posts |
|---|---|
| `001_baku.mp4` (any .mp4 .mov .m4v .mkv .webm) | Reel with the series title + burned-in subtitles |
| `002_sunset.jpg` (.jpg .png .webp) | Photo post |
| folder `003_trip/` with 2–10 images | Carousel |
| `001_baku.txt` (optional, same name) | Per-post settings, see below |

Items are posted in **name order**, so number them. Posted items move to `posted/`
(with `posted/log.json`). Anything that fails moves to `failed/` with an error file, and
GitHub emails you; the queue keeps going.

### The optional .txt file
```
TITLE: Songs for a rainy day
Filmed in Baku at sunrise, song is my favourite this week
CAPTION:
My own caption here #baku
```
- `TITLE:` changes the on-screen title for this video only (`TITLE: none` = no title)
- `CAPTION:` everything after it is used as the caption exactly
- any other lines are notes the AI uses when writing the caption

## Look and series settings: `config.json`
| Setting | What it does |
|---|---|
| `title` | Title shown at the top of every video. `{n}` = post number, `{total}` = series length, `\n` = new line. `""` = no title |
| `series_start` / `series_total` | First number and the "/30" part. Numbering continues automatically |
| `title_color` | Title colour, e.g. `"#FFD60A"` (yellow) |
| `subtitles` | `false` = no subtitles |
| `subtitle_style` | `"clean"` (small white text, like your example) or `"bold"` (big outlined) |
| `skip_silence` | Set `false` if your videos are songs and some sung lines are missing |

## One-time setup (about 30 minutes)

### 1. Instagram account
Switch the account to **Professional → Creator or Business** (Instagram settings). No Facebook Page needed.

### 2. Get the access token (no Facebook Page needed)
1. Go to <https://developers.facebook.com> → **My Apps → Create app**, and pick the Instagram use case
   (**"Instagram API with Instagram Login"**).
2. In the app go to **Instagram → API setup with Instagram login → Generate access tokens → Add account**,
   log in to Instagram, then click **Generate token** next to your account.
3. Copy the token (it starts with `IG`) into the GitHub secret `IG_ACCESS_TOKEN`. `IG_USER_ID` is
   optional with this token type; the script looks it up.
4. The token lasts 60 days. Generate a new one in the same place and update the secret before then.

(The older route with a Facebook Page and a Facebook Login token also still works.)

### 3. Caption key (optional, about 1–2 cents per post)
Create an API key at <https://console.anthropic.com>. Without it, posts use your `.txt` notes,
the title, or the transcript as the caption.

### 4. Create the GitHub repo
1. New repository → **Public** (see "Good to know") → upload all these files,
   including the hidden `.github` folder.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   `IG_ACCESS_TOKEN` (and optionally `ANTHROPIC_API_KEY`).
3. **Settings → Actions → General → Workflow permissions** → "Read and write permissions".

### 5. Test it
1. Upload a video into `queue/` (on github.com open `queue/` → **Add file → Upload files**;
   works from your phone).
2. **Actions → Instagram autopost → Run workflow** with "Preview only" ticked.
3. When it finishes, download **preview** at the bottom of the run page and check the video
   and `caption.txt`.
4. Run it once with "Preview only" unticked to post for real. After that it runs by itself.

## Changing things
- **Times / posts per day:** the `cron` line in `.github/workflows/autopost.yml` (UTC; Tashkent = UTC+5).
- **More than one post per run:** add a repository *variable* `POSTS_PER_RUN`.
- **More accurate subtitles:** add a variable `WHISPER_MODEL` = `medium.en` (slower).

## Good to know
- **Keep the queue full.** 3 posts a day = about 90 items a month. When the queue is empty, runs
  just do nothing.
- **Music:** songs from Spotify/YouTube added in an editor can get the Reel muted or blocked,
  and the API can't pick songs from Instagram's own music library. Use royalty-free music or
  your own audio for auto-posted reels.
- **Subtitles on songs** are less accurate than on speech. Check them with a preview run first.
- **Privacy:** images are posted by letting Instagram download them from the repo, which is why
  it's public. Files in `queue/` are visible on GitHub until they're posted.
- **Size:** GitHub allows 100 MB per file. Export reels at 1080p, under 60 seconds. Every video
  stays in the repo's history, so after a few GB start a fresh repo.
- **Cost:** GitHub Actions is free for public repos and the Instagram API is free; only AI
  captions cost cents.
- GitHub pauses scheduled runs after 60 days with no commits. The bot commits on every post,
  so this only happens if the queue sits empty that long. Re-enable it in the Actions tab.
