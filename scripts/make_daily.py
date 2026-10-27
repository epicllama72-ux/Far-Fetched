#!/usr/bin/env python3
"""
Far-Fetched daily headline builder.

Pulls weird-news stories from the past week, asks Claude to pick the best ones,
rewrite them as redacted headlines and invent a couple of believable fakes, then
geocodes them and writes data/<YYYY-MM-DD>.json and data/latest.json for the game.

Usage:
  python scripts/make_daily.py                 # normal run (needs ANTHROPIC_API_KEY)
  python scripts/make_daily.py --picks f.json  # skip the Claude call, use a picks file
  python scripts/make_daily.py --date 2026-10-27 --no-geocode

Env:
  ANTHROPIC_API_KEY   required unless --picks is given
  GLOVE               path to glove-wiki-gigaword-50.gz (word vectors for Fill the Blank)
  FF_MODEL            Claude model (default claude-sonnet-5-5)
"""
import argparse, base64, datetime as dt, email.utils, gzip, html, json, math, os, random, re, sys, time
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = "far-fetched-game/1.0 (daily headline builder; github actions)"

FEEDS = [
    ("UPI Odd News", "https://rss.upi.com/news/odd_news.rss"),
    ("Sky News Strange", "https://feeds.skynews.com/feeds/rss/strange.xml"),
    ("NY Post Weird But True", "https://nypost.com/weird-but-true/feed/"),
    ("Oddity Central", "https://www.odditycentral.com/feed"),
]
# Worldwide net: Google News searches for odd-but-true stories from the past 7 days, one per regional edition.
_ODD_Q = urllib.parse.quote('(bizarre OR quirky OR "strange but true" OR unusual OR unlikely OR "world record") (rescued OR escaped OR found OR wins OR discovered OR stuck OR unveiled OR spotted) when:7d')
for _name, _gl, _ceid in [("US", "US", "US:en"), ("UK", "GB", "GB:en"), ("Australia", "AU", "AU:en"), ("Canada", "CA", "CA:en"),
                          ("India", "IN", "IN:en"), ("Ireland", "IE", "IE:en"), ("New Zealand", "NZ", "NZ:en"),
                          ("South Africa", "ZA", "ZA:en"), ("Singapore", "SG", "SG:en"), ("Philippines", "PH", "PH:en")]:
    FEEDS.append((f"Google News odd ({_name})", f"https://news.google.com/rss/search?q={_ODD_Q}&hl=en-{_gl}&gl={_gl}&ceid={_ceid}"))
POL_FEEDS = [  # Google News searches for odd local-government and election stories worldwide
    ("Google News: odd elections", "https://news.google.com/rss/search?q=" + urllib.parse.quote('(election OR candidate OR ballot) (tie OR "coin toss" OR "dog mayor" OR bizarre OR unusual) when:21d') + "&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: odd councils", "https://news.google.com/rss/search?q=" + urllib.parse.quote('(council OR parliament OR lawmakers OR mayor) (bizarre OR quirky OR unusual OR "honorary mayor") when:21d') + "&hl=en-US&gl=US&ceid=US:en"),
    ("UPI Odd News", "https://rss.upi.com/news/odd_news.rss"),
]
MAX_AGE_DAYS = 7
POL_MAX_AGE_DAYS = 45
N_POL_REAL, N_POL_FAKE = 8, 2   # Politics: also exactly 2 fakes
N_CALL_REAL, N_CALL_FAKE, N_BLANK = 8, 2, 10   # Real or Fake always has exactly 2 fakes


# ---------------------------------------------------------------- feeds
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=500):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def parse_feed(xml_bytes, source):
    items = []
    root = ET.fromstring(xml_bytes)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):
        items.append({
            "title": re.sub(r"\s+-\s+[^-]{2,40}$", "", clean(it.findtext("title"), 200)) if source.startswith("Google News") else clean(it.findtext("title"), 200),
            "link": (it.findtext("link") or "").strip(),
            "summary": clean(it.findtext("description")),
            "date": it.findtext("pubDate") or "",
            "source": source,
        })
    for it in root.findall(".//atom:entry", ns):
        link = it.find("atom:link", ns)
        items.append({
            "title": clean(it.findtext("atom:title", default="", namespaces=ns), 200),
            "link": link.get("href") if link is not None else "",
            "summary": clean(it.findtext("atom:summary", default="", namespaces=ns)),
            "date": it.findtext("atom:updated", default="", namespaces=ns),
            "source": source,
        })
    return items


def parse_date(s):
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def gather(now, used_links, feeds=FEEDS, max_age=MAX_AGE_DAYS):
    out, seen = [], set()
    for name, url in feeds:
        try:
            items = parse_feed(http_get(url), name)
            print(f"  {name}: {len(items)} items", file=sys.stderr)
        except Exception as e:
            print(f"  {name}: failed ({e})", file=sys.stderr)
            continue
        for it in items:
            d = parse_date(it["date"])
            if not d or (now - d).days >= max_age or not it["title"] or not it["link"]:
                continue
            key = re.sub(r"\W+", "", it["title"].lower())[:60]
            if it["link"] in used_links or key in seen:
                continue
            seen.add(key)
            it["published"] = d.date().isoformat()
            out.append(it)
    return out


# ---------------------------------------------------------------- Claude
PROMPT = """You are the editor of "Far-Fetched", a daily guessing game built on weird-but-true news.

Below are candidate news stories from the past week (JSON). Do two jobs and reply with ONE JSON object only, no prose.

1) "real": pick 20 of the best stories for the game, best first. You MUST return at least 14; if clue-rich stories run short, fill the rest with the most surprising remaining ones (low clue_strength is fine), never return fewer than 14 (rank by clue_strength plus unbelievable). Rank by how HARD TO BELIEVE the story is: favour the ones a reader would assume are made up (absurd specifics, improbable coincidences, odd rules, unlikely animals or objects) but that really happened. Strongly prefer stories where the event is linked to the place it happened, so a player can DEDUCE the location from the headline instead of guessing: local wildlife or climate (a kangaroo, a moose, monsoon flooding, snow in summer), regional industry, food, sport or customs, a local law or institution type, driving side, currency, units or typical local jobs. Stories that could have happened anywhere rank lower but are still acceptable fillers. The game should be FUNNY, EDUCATIONAL and reward broad general knowledge: favour stories that make a reader laugh out loud or are so ridiculous they feel like something out of the movie Idiocracy (absurd decisions, baffling rules, dumbfounding mix-ups, spectacularly dumb but harmless events), and whose clues connect to things a well-read person could know about the place (geography, climate, wildlife, languages, currencies, laws, food, history, sport, industries), so that general knowledge lets them reason to the right region, not luck. Good picks are surprising, funny or delightful, happened in ONE specific identifiable town/city/county, and are light-hearted. Never pick stories about death, serious injury, crimes against people, abuse, sexual content, children in danger, war, politics, disasters or tragedy. Avoid near-duplicates (e.g. several lottery wins) - at most one lottery story. For each pick give:
   - "id": the candidate id.
   - "h": a punchy wire-style headline, max 100 characters, keeping every fact of the story exactly as reported (never change what happened, who or what was involved, numbers or outcome). You MAY reword and add short descriptive wording that makes the region easier to deduce, as long as it is generically true of that place and does not alter or add to the story: e.g. local terms and institutions ("the county sheriff", "council", "constabulary", "a roo", "monsoon rains", "rupees", "kilometres", "on the ski slopes"), local climate, wildlife or customs, or spelling and units typical of the country. Replace the place with the literal token {} exactly once (e.g. "... in {}"). Remove every EXPLICIT location clue: no state/country/city names, demonyms, local team, landmark, university or company names that reveal the place, no "Florida man". But KEEP the implicit, deducible clues that are part of the story (the animal, the weather, the local custom, currency or units, the type of institution), so a sharp player can reason their way to the right region. Keep the oddity intact.
   - "h2": the same story as a headline that DOES name the place (city + state/country), with the main subject noun of the story (the animal, object or kind of person it is about, e.g. "kangaroo", "pelicans", "kitten") replaced by the literal token [] exactly once. Not a name, not a place.
   - "ans": 1-3 lowercase single-word answers for [] - the exact hidden word first, then singular/plural variants. Letters only.
   - "place": display name like "Appleton, Wisconsin" or "Mykolaiv, Ukraine".
   - "geocode": a precise query for a geocoder, like "Appleton, Wisconsin, USA".
   - "lat", "lng": your best coordinates for the place (decimal degrees).
   - "country": the country name.
   - "clue": at most 60 characters naming the implicit regional hint a sharp player could use to deduce the place (for example "moose + wire fencing = northern US/Canada", "monsoon flooding = South Asia", "metres and rupees"). Use "" if there is none.
   - "clue_strength": 0-3. 3 = the headline strongly points to a region; 2 = a fair hint; 1 = a weak hint; 0 = could be anywhere, OR the hint is misleading (an animal or object that is out of place, such as a kangaroo in Tennessee, is a surprise, not a clue: give it 0).
   - "unbelievable": 1-5, how likely a reader is to assume the story is made up (5 = sounds like a joke but is true).
   - "theme": one lowercase word for the topic (e.g. "lottery", "escaped-animal", "record"). Never pick two stories with the same theme.
   Keep the source's own units and currency; NEVER convert them (a "4.7 miles" story from South Africa must read in kilometres if the source said kilometres), because units are clues.

2) "fake": write 6 invented headlines in exactly the same style, each with {} for the redacted place. Like the real ones, give each fake implicit regional clues a player could use (a local animal, climate, custom or unit) without naming the place. They should pass at first glance (specific numbers, ordinary local institutions, plausible animals and objects) but be SLIGHTLY easier to spot than the real ones: give each one small tell, a detail that is a little too neat, too cute or too convenient (a perfectly round number, an unlikely coincidence, a punchline-like flourish). Make the fakes as odd as the real stories, not odder: the real stories are routine local news with one strange twist, so the fakes must not be the only headlines that sound like jokes. Never cartoonish. Not about real people, and not resembling any candidate story. Do not write about death, injury, crime against people or tragedy. Give each as {"h": "...", "tell": "the tell in at most 8 words", "obvious": 1-5} where obvious rates how easy it is to spot as invented (aim for 2 or 3: slightly easier to spot than a real story, never a giveaway).

Output format:
{"real":[{"id":0,"h":"...","h2":"...","ans":["..."],"place":"...","geocode":"...","lat":0,"lng":0,"country":"...","clue":"...","clue_strength":0,"unbelievable":1,"theme":"..."}], "fake":[{"h":"...","tell":"...","obvious":3}]}

Candidates:
"""


POL_PROMPT = """You are the editor of the Politics mode of "Far-Fetched", a daily guessing game built on hard-to-believe but true news.

Below are candidate stories (JSON). Reply with ONE JSON object only.

1) "real": pick up to 8 stories about hard-to-believe POLITICAL or GOVERNMENT happenings anywhere in the world: odd election results and tiebreaks, animal or joke candidates, strange council votes, unusual laws, quirky parliamentary moments. Keep it light and strictly non-partisan. Never pick stories about violence, death, scandals, allegations, crimes, wars, protests, or anything that mocks or praises a party, ideology or named politician. Prefer the hardest-to-believe ones whose details hint at the region (local customs, institutions, offices, voting rules) so a player can deduce where it happened. Each must be tied to one specific place. For each give "id", "h" (wire-style headline, max 100 chars, only facts from the candidate, the place replaced by the literal token {} exactly once, no other location clues, and no names of real politicians or parties), "place", "geocode", "lat", "lng".

2) "fake": write 6 invented headlines in the same style, each with {} for the place, about councils, parliaments, mayors, elections or laws. Each must pass at first glance but carry one small tell (too neat, too cute, too convenient). NEVER name or hint at a real person, party, politician or real event, and never take a side. Give each as {"h": "..."}.

Output format:
{"real":[{"id":0,"h":"...","place":"...","geocode":"...","lat":0,"lng":0}], "fake":[{"h":"..."}]}

Candidates:
"""


def ask_claude(cands, model, prompt=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set (or pass --picks).")
    payload = [{"id": i, "title": c["title"], "summary": c["summary"], "source": c["source"],
                "published": c["published"]} for i, c in enumerate(cands)]
    body = {
        "model": model,
        "max_tokens": 12000,
        "messages": [{"role": "user", "content": (prompt or PROMPT) + json.dumps(payload, ensure_ascii=False)}],
    }
    last = None
    for attempt in range(3):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.loads(r.read())
            text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0))
        except Exception as e:
            last = e
            print(f"  Claude attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    sys.exit(f"Claude call failed: {last}")


# ---------------------------------------------------------------- geocoding
def km(a, b, c, d):
    t = math.radians
    h = math.sin(t(c - a) / 2) ** 2 + math.cos(t(a)) * math.cos(t(c)) * math.sin(t(d - b) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def geocode(q):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1})
    try:
        res = json.loads(http_get(url))
        time.sleep(1.1)  # Nominatim usage policy: max 1 request/second
        if res:
            return float(res[0]["lat"]), float(res[0]["lon"])
    except Exception as e:
        print(f"  geocode failed for {q!r}: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- word vectors
def load_vectors(path, words):
    want, out = set(words), {}
    if not path or not os.path.exists(path):
        return out
    with gzip.open(path, "rt", encoding="utf8") as f:
        for line in f:
            w, rest = line.split(" ", 1)
            if w in want:
                out[w] = [float(x) for x in rest.split()]
                if len(out) == len(want):
                    break
    return out


def pack_vec(v):
    n = math.sqrt(sum(x * x for x in v)) or 1
    m = max(abs(x / n) for x in v) or 1
    q = bytes((round(x / n / m * 127) & 0xFF) for x in v)
    return base64.b64encode(q).decode()


# ---------------------------------------------------------------- validation
GENERIC = {"county", "city", "town", "state", "north", "south", "east", "west", "united", "states",
           "kingdom", "republic", "island", "islands", "province", "district", "lake", "port", "saint", "new"}


def leaks(h, place):
    low = h.lower()
    for tok in re.split(r"[\s,.\-']+", place.lower()):
        if len(tok) >= 4 and tok not in GENERIC and re.search(r"\b" + re.escape(tok) + r"\b", low):
            return tok
    return None


def valid_real(r):
    h, h2 = r.get("h", ""), r.get("h2", "")
    if h.count("{}") != 1 or len(h) > 130:
        return "bad h"
    if leaks(h.replace("{}", ""), r.get("place", "")):
        return "place leaks into h"
    try:
        float(r["lat"]); float(r["lng"])
    except Exception:
        return "bad coordinates"
    return None


def valid_blank(r):
    h2, ans = r.get("h2", ""), r.get("ans") or []
    if h2.count("[]") != 1 or not ans or not all(re.fullmatch(r"[a-z]+", a) for a in ans):
        return False
    low = h2.lower()
    return not any(re.search(r"\b" + a + r"\b", low) for a in ans)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date to build (default today)")
    ap.add_argument("--picks", help="use this JSON (Claude-format output plus candidate fields) instead of calling Claude")
    ap.add_argument("--no-geocode", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    day = args.date or now.date().isoformat()
    os.makedirs(DATA, exist_ok=True)
    hist_path = os.path.join(DATA, "history.json")
    history = json.load(open(hist_path)) if os.path.exists(hist_path) else {}
    # links from earlier days are "used"; today's own entry doesn't count, so a re-run rebuilds today's set
    used = {link for d, links in history.items() if d != day for link in links}

    if args.picks:
        picks = json.load(open(args.picks))
        cands = picks.get("candidates", [])
    else:
        print("Fetching feeds…", file=sys.stderr)
        cands = gather(now, used)
        if len(cands) < 25:                      # thin news week: widen the window before giving up
            print(f"  only {len(cands)} candidates; widening to 10 days", file=sys.stderr)
            cands = gather(now, used, max_age=10)
        print(f"{len(cands)} fresh candidates", file=sys.stderr)
        if len(cands) < 8:
            print("Not enough fresh stories today; keeping yesterday's set.", file=sys.stderr)
            sys.exit(0)
        random.Random(day).shuffle(cands)
        cands = cands[:130]
        picks = ask_claude(cands, os.environ.get("FF_MODEL", "claude-sonnet-5-5"))

    reals = []
    for r in picks.get("real", []):
        c = cands[r["id"]] if isinstance(r.get("id"), int) and r["id"] < len(cands) else r
        r["src"] = r.get("src") or c.get("link", "")
        r["published"] = r.get("published") or c.get("published", "")
        why = valid_real(r)
        if why:
            print(f"  drop real ({why}): {r.get('h')}", file=sys.stderr)
            continue
        if not args.no_geocode and r.get("geocode"):
            g = geocode(r["geocode"])
            if g and km(g[0], g[1], float(r["lat"]), float(r["lng"])) < 400:
                r["lat"], r["lng"] = g
            elif g:
                print(f"  geocoder disagrees for {r['place']} (likely a same-name town elsewhere); keeping the editor's coordinates", file=sys.stderr)
        reals.append(r)

    fakes = [f for f in picks.get("fake", []) if f.get("h", "").count("{}") == 1]
    if len(reals) < N_CALL_REAL or len(fakes) < N_CALL_FAKE:
        print(f"Only {len(reals)} usable real / {len(fakes)} fake headlines; keeping yesterday's set.", file=sys.stderr)
        sys.exit(0)

    # Real or Fake wants stories whose place can be DEDUCED, and a spread of countries and themes.
    def merit(r):
        try:
            return 2 * int(r.get("clue_strength", 0)) + int(r.get("unbelievable", 1))
        except Exception:
            return 1
    seen_theme, uniq = set(), []
    for r in sorted(reals, key=merit, reverse=True):
        th = (r.get("theme") or "").strip().lower()
        if th and th in seen_theme:
            continue
        seen_theme.add(th)
        uniq.append(r)
    if len(uniq) >= N_CALL_REAL:
        reals = uniq
    else:
        reals = uniq + [r for r in reals if r not in uniq]
    call_pick, per_country = [], {}
    for r in reals:
        c = (r.get("country") or "?").lower()
        if c != "?" and per_country.get(c, 0) >= 4:
            continue
        per_country[c] = per_country.get(c, 0) + 1
        call_pick.append(r)
        if len(call_pick) == N_CALL_REAL:
            break
    call_pick += [r for r in reals if r not in call_pick][:N_CALL_REAL - len(call_pick)]
    strong = sum(1 for r in call_pick if int(r.get("clue_strength", 0) or 0) >= 2)
    print(f"  Real or Fake: {strong}/{len(call_pick)} stories with a usable regional clue; countries {per_country}", file=sys.stderr)

    # Fill the Blank: real stories whose answer has a word vector
    vecs = load_vectors(os.environ.get("GLOVE"), [a for r in reals for a in (r.get("ans") or [])])
    blank_pool = []
    for r in reals:
        if not valid_blank(r):
            continue
        a = next((a for a in r["ans"] if a in vecs), None)
        if a is None and vecs:
            continue
        r["ans"] = [a] + [x for x in r["ans"] if x != a] if a else r["ans"]
        if a:
            r["vec"] = pack_vec(vecs[a])
        blank_pool.append(r)

    rng = random.Random("far-fetched-" + day)
    # Fill the Blank takes what Real or Fake doesn't need, so the modes don't spoil each other;
    # on a thin news week they share stories rather than shrink.
    call_real = call_pick
    blank = [r for r in blank_pool if r not in call_real][:N_BLANK]
    if len(blank) < N_BLANK:                       # thin week: the modes share a few stories rather than shrink
        blank += [r for r in blank_pool if r not in blank][:N_BLANK - len(blank)]

    def real_call(r):
        return {"real": True, "h": r["h"], "lat": round(float(r["lat"]), 4), "lng": round(float(r["lng"]), 4),
                "place": r["place"], "src": r["src"], "published": r["published"], "clue": (r.get("clue") or "")[:80]}

    def real_blank(r):
        out = {"real": True, "h2": r["h2"], "ans": r["ans"], "place": r["place"], "src": r["src"],
               "published": r["published"]}
        if "vec" in r:
            out["vec"] = r["vec"]
        return out

    def fake_rank(f):
        try:
            return abs(float(f.get("obvious", 2.5)) - 2.5)      # aim for "slightly easier than the real ones"
        except Exception:
            return 1.0
    fakes = sorted(fakes, key=fake_rank)
    call = [real_call(r) for r in call_real] + [{"real": False, "h": f["h"], "tell": (f.get("tell") or "")[:80]} for f in fakes[:N_CALL_FAKE]]
    rng.shuffle(call)
    rng.shuffle(blank)
    result = {"date": day, "generated": now.isoformat(timespec="seconds"),
              "call": call, "blank": [real_blank(r) for r in blank]}

    # Politics mode (Real or Fake with political headlines). Skipped on a thin news week.
    pol = picks.get("politics") if args.picks else None
    if pol is None and not args.picks:
        try:
            pc = gather(now, used, POL_FEEDS, POL_MAX_AGE_DAYS)
            print(f"{len(pc)} politics candidates", file=sys.stderr)
            if len(pc) >= 6:
                pc = pc[:60]
                pol = ask_claude(pc, os.environ.get("FF_MODEL", "claude-sonnet-5-5"), POL_PROMPT)
                pol["candidates"] = pc
        except SystemExit as e:
            print(f"  politics skipped: {e}", file=sys.stderr)
    if pol:
        pcands = pol.get("candidates", [])
        preal = []
        for r in pol.get("real", []):
            c = pcands[r["id"]] if isinstance(r.get("id"), int) and r["id"] < len(pcands) else r
            r["src"] = r.get("src") or c.get("link", "")
            r["published"] = r.get("published") or c.get("published", "")
            if valid_real(r):
                continue
            if not args.no_geocode and r.get("geocode"):
                gg = geocode(r["geocode"])
                if gg:
                    try:
                        near = km(gg[0], gg[1], float(r["lat"]), float(r["lng"])) < 400
                    except Exception:
                        near = True
                    if near:
                        r["lat"], r["lng"] = gg
            preal.append(r)
        pfake = [f for f in pol.get("fake", []) if f.get("h", "").count("{}") == 1]
        preal = preal[:N_POL_REAL]
        if len(preal) >= 4 and len(pfake) >= N_POL_FAKE:
            items = [dict(real_call(r), cat="pol") for r in preal] + \
                    [{"real": False, "h": f["h"], "cat": "pol"} for f in pfake[:N_POL_FAKE]]
            rng.shuffle(items)
            result["politics"] = items
            call_real = call_real + preal

    for name in (f"{day}.json", "latest.json"):
        with open(os.path.join(DATA, name), "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)

    history[day] = sorted({r["src"] for r in call_real + blank})
    cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=60)).isoformat()
    history = {k: v for k, v in history.items() if k >= cutoff}
    with open(hist_path, "w") as f:
        json.dump(history, f, indent=1)
    print(f"Wrote data/{day}.json: {len(call)} Real-or-Fake, {len(result['blank'])} Fill-the-Blank", file=sys.stderr)


if __name__ == "__main__":
    main()
