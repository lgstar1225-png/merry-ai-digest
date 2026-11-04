# MERRY AI DIGEST (test repository)

Independent from `ai-secretary` and `써보니`.

## Setup
1. Create a new public GitHub repository named `merry-ai-digest` (do not modify existing repos).
2. Upload these files, preserving `.github/workflows/digest.yml`.
3. GitHub repository Settings → Secrets and variables → Actions → New repository secret: `GEMINI_API_KEY`.
4. Actions → MERRY AI DIGEST → Run workflow → `DIRECT_URL` → enter a YouTube URL with available subtitles.
5. Download the run's artifact. It contains six PNGs, `sources.json`, `digest.json`, and `merry_ai_digest.zip`.

## Notes
- YouTube subtitle access from GitHub-hosted runners may be blocked; failure is reported, not fabricated.
- `LATEST_AI_YOUTUBE` is an initial verified-channel subset, not yet the requested full ten.
- `GITHUB` uses repositories created in the last 7 days ranked by current stars. It is **not** official weekly trending.
- Public repository workflow source is visible. Never commit API keys.
- GitHub Actions and Gemini API quotas/pricing must be checked on the actual accounts.
- This code has not been run against live external services in this environment.

