# Far-Fetched

A daily weird-news game. **Real or Fake:** spot the invented headline, then pin where the real ones happened. **Fill the Blank:** guess the hidden word in a real headline within 15 seconds.

## Files
- `index.html`: the game
- `world.json`: country and state outlines for the map
- `words.json`: word list for Fill the Blank (loaded only when that mode opens)
- `data/`: the daily headline sets (`latest.json` plus one file per day)
- `scripts/make_daily.py`: builds each day's set from the past week's odd news
- `.github/workflows/daily.yml`: runs the builder every day and publishes the site

## Put it online (GitHub Pages, free)
1. Create a new GitHub repository and upload everything in this folder, including the hidden `.github` folder.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `ANTHROPIC_API_KEY` and paste an API key from console.anthropic.com. A daily run costs a few cents.
3. Go to **Settings → Pages** and set **Source** to **GitHub Actions**.
4. Go to **Actions → Daily headlines + deploy → Run workflow** to publish right away. After that it runs by itself every night.

The site will be at `https://<your-username>.github.io/<repo-name>/`.

## How the daily set is made
The builder reads odd-news RSS feeds (UPI Odd News, Sky News Strange, NY Post Weird But True, Oddity Central). It keeps stories from the last 7 days that haven't been used before, and asks Claude to:
- pick light-hearted ones tied to one specific place
- rewrite them with the place redacted (Real or Fake) and with the subject word redacted (Fill the Blank)
- write believable fake headlines

Places are geocoded with OpenStreetMap Nominatim. If a day's run fails, the game keeps showing the last good set.
