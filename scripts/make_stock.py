#!/usr/bin/env python3
"""
Far-Fetched story stock builder.

Sources a big batch of extra stories ahead of time, using the same editor rules as make_daily.py.
It never touches the Real or Fake daily picks (data/<date>.json, data/latest.json, data/bank.json):

  --practice N   N true stories from the last 200 years (found through Wikipedia) plus invented fakes,
                 appended to data/practice.json. Practice mode only: they are stamped with an old date and
                 make_daily.py never reads them as candidates.
  --politics N   N recent odd politics stories (and fakes) saved in data/stock.json for the Politics mode.
  --blank N      N recent stories with a hidden-word headline saved in data/stock.json for Fill the Blank.

make_daily.py draws on data/stock.json for Politics and Fill the Blank only, and removes a story from it once used.

Usage:
  python scripts/make_stock.py --practice 120 --politics 30 --blank 40      (needs ANTHROPIC_API_KEY)
Env: ANTHROPIC_API_KEY, GLOVE (word vectors for Practice's Fill the Blank hints), FF_MODEL, FF_SCREEN_MODEL
"""
import argparse, datetime as dt, json, os, random, re, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_daily as md

DATA = md.DATA
PRACTICE_DAY = "2026-01-01"      # any past date: Practice only skips stories dated today or later
HIST_FROM = 1826                 # "the last 200 years"


def log(*a):
    print(*a, file=sys.stderr)


def load(name, default):
    try:
        return json.load(open(os.path.join(DATA, name)))
    except Exception:
        return default


def save(name, obj, **kw):
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, ensure_ascii=False, **kw)


def today_et():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et = now - dt.timedelta(hours=5)
    return now, et.date().isoformat()


def strip_year(place):
    return re.sub(r"\s*\(\s*\d{3,4}s?\s*\)\s*$", "", place or "").strip()


def check_real(r, historical=False):
    """Same checks as the daily build (the place may carry a '(1932)' suffix on historical stories)."""
    r2 = dict(r, place=strip_year(r.get("place", "")) if historical else r.get("place", ""))
    why = md.valid_real(r2)
    if not why and r2["place"].split(",")[0].strip().lower() in md.COUNTRY_NAMES:
        why = "country-level place"
    return why


def refine_coords(r, wiki_coord=None, geocode=True):
    """Prefer an accurate geocode when it agrees with the editor's own coordinates (same rule as the daily build)."""
    try:
        la, ln = float(r["lat"]), float(r["lng"])
    except Exception:
        return
    if wiki_coord and md.km(wiki_coord[0], wiki_coord[1], la, ln) < 400:
        r["lat"], r["lng"] = wiki_coord
        return
    if geocode and r.get("geocode"):
        g = md.geocode(r["geocode"])
        if g and md.km(g[0], g[1], la, ln) < 400:
            r["lat"], r["lng"] = g


def ask_in_chunks(cands, model, make_prompt, per_chunk, size, avoid=None):
    """One editor call can only return so many stories (the answer has a size limit), so ask in chunks of `size` candidates.
    Returns ([(story, candidate), ...], [fake, ...])."""
    pairs, fakes = [], []
    for start in range(0, len(cands), size):
        chunk = cands[start:start + size]
        log(f"  editor call for candidates {start + 1}-{start + len(chunk)} of {len(cands)}")
        try:
            res = md.ask_claude(chunk, model, make_prompt(per_chunk), avoid=avoid)
        except SystemExit as e:
            log(f"  editor call failed ({e}); continuing with what we have")
            continue
        for r in res.get("real", []):
            i = r.get("id")
            if isinstance(i, int) and 0 <= i < len(chunk):
                pairs.append((r, chunk[i]))
        fakes += res.get("fake", [])
    return pairs, fakes


def quality_gate(r):
    """Reason to leave a story out of Practice or the stock, or None. Stricter than the old quota-driven picking."""
    if not md.fit_to_map(r) or md.eff_strength(r) < 2:
        return f"hard to map or no fair clue (mappable {md.map_score(r)}, clue {md.eff_strength(r)})"
    return None


def clean_real(r, keep):
    return {k: r[k] for k in keep if r.get(k) not in (None, "", [])}


# ---------------------------------------------------------------- historical stories for Practice
PROPOSE_PROMPT = """You are helping the editor of "Far-Fetched", a guessing game built on weird-but-true stories: players guess whether a headline is real, then deduce where in the world it happened.

List {n} REAL events from {y0} to {y1} that would make great stories for the game. Each must have its own English Wikipedia article (give the exact article title). Choose events that are absurd, funny, baffling or delightful but TRUE and well documented; light-hearted and non-violent. Never pick wars, massacres, disasters with deaths, crimes against people, abuse, or anything tragic. Each must be tied to ONE specific town, city or county (not a country), and its details should let a well-read player deduce the region (local wildlife, climate, currency, customs, institutions, industries). Spread them across the world and across the decades (at most 3 from any one country and at least half from outside the US, UK, Canada, Australia, New Zealand and Ireland) and across topics (animals, food, sport, science mishaps, odd laws, mix-ups, inventions, feuds, records). Include about {npol} that are odd POLITICAL or government happenings (strange elections, tiebreaks, joke or animal candidates, unusual laws or votes), strictly non-partisan, and mark those with "pol": true.

Do NOT include any of these (already used):
{avoid}

Reply with ONE JSON object only: {{"s":[{{"title":"Wikipedia article title","year":1932,"pol":false}}]}}
"""


def wiki_lookup(titles):
    """title -> {'title','url','extract','coord'} for every title that resolves to a real article."""
    out = {}
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        url = ("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "redirects": 1, "prop": "extracts|coordinates|info", "inprop": "url",
            "exintro": 1, "explaintext": 1, "exlimit": "max", "colimit": "max", "titles": "|".join(chunk)}))
        try:
            data = json.loads(md.http_get(url))
        except Exception as e:
            log(f"  wikipedia lookup failed: {e}")
            continue
        q = data.get("query", {})
        redirect = {}
        for lst in (q.get("normalized", []), q.get("redirects", [])):
            for m in lst:
                redirect[m["from"]] = m["to"]
        by_title = {p.get("title"): p for p in q.get("pages", {}).values() if "missing" not in p}
        for t in chunk:
            name = t
            for _ in range(3):
                name = redirect.get(name, name)
            p = by_title.get(name)
            if not p or not p.get("extract"):
                continue
            co = (p.get("coordinates") or [None])[0]
            out[t] = {"title": p["title"], "url": p.get("fullurl") or "https://en.wikipedia.org/wiki/" + urllib.parse.quote(p["title"].replace(" ", "_")),
                      "extract": p["extract"], "coord": (co["lat"], co["lon"]) if co else None}
        time.sleep(0.3)
    return out


LOOSE_QUOTA = ("You MUST return at least 14; if clue-rich stories run short, fill the rest with the most surprising remaining ones "
               "(low clue_strength is fine), never return fewer than 14 (rank by clue_strength plus unbelievable).")
STRICT = ("Be SELECTIVE: return only stories that are genuinely funny or astonishing, make complete sense from the headline alone, can be pinned to one "
          "specific place and carry a fair regional clue. Returning only a few stories, or none, is better than padding with weak ones (rank by mappable, clue_strength and unbelievable).")


def strict_prompt(p, n):
    """The daily editor prompt, set to pick up to n and to prefer fewer, better stories over a quota."""
    a = "pick 20 of the best stories for the game, best first. " + LOOSE_QUOTA
    assert a in p, "daily prompt changed; update make_stock.py"
    return p.replace(a, f"pick up to {n} of the best stories for the game, best first. " + STRICT, 1)


def hist_prompt(n, nfake):
    """The daily editor prompt, adapted for historical Wikipedia summaries."""
    p = md.PROMPT
    def sub(a, b):
        nonlocal p
        assert a in p, f"daily prompt changed; update make_stock.py (missing: {a[:50]!r})"
        p = p.replace(a, b, 1)
    sub("Below are candidate news stories from the past month (JSON).",
        "Below are Wikipedia summaries of true historical events from the last 200 years (JSON); \"published\" holds the year. Write every field as for a news story, in English.")
    p = strict_prompt(p, n)
    sub("write 8 invented headlines", f"write {nfake} invented headlines")
    sub('"place": display name like "Appleton, Wisconsin" or "Mykolaiv, Ukraine".',
        '"place": display name like "Campion, Western Australia (1932)": the specific place, then the YEAR in parentheses. Never just a country.')
    sub('"clue": at most 80 characters,', '"pol": true when the story is an odd political or government happening, otherwise false.\n   - "clue": at most 80 characters,')
    return p + "\n"


def source_practice(n, now, day, geocode):
    arch = load("practice.json", {"real": [], "fake": []})
    have = {x.get("src") or x.get("h") or x.get("h2") for x in arch["real"]} | {x.get("h") for x in arch["fake"]}
    used_titles = sorted({urllib.parse.unquote(x["src"].rsplit("/", 1)[-1]).replace("_", " ") for x in arch["real"] if "wikipedia.org" in (x.get("src") or "")})
    model = os.environ.get("FF_MODEL", "claude-sonnet-5-5")
    ask = min(300, int(n * 3))      # the editor keeps only a fraction, so propose plenty
    # One call can only list so many titles before it hits the output limit, so go era by era, a few dozen at a time.
    this_year = dt.date.today().year
    eras = [(1826, 1860), (1861, 1890), (1891, 1915), (1916, 1940), (1941, 1960), (1961, 1980), (1981, 2000), (2001, this_year)]
    per_era = -(-ask // len(eras))
    log(f"Practice: asking for about {ask} historical events across {len(eras)} eras…")
    seen, titles, meta, empty = set(), [], {}, 0
    # Titles are only a shortlist (Wikipedia and the editor check them later), so the cheap model with a forced structured
    # answer does it: the big model wrote long deliberations here and ran out of output room.
    propose_model = os.environ.get("FF_SCREEN_MODEL", "claude-haiku-5-5")
    for y0, y1 in eras:
        if empty >= 2 and not titles:
            log("  proposals keep failing; giving up before spending more")
            break
        avoid = used_titles[:300] + titles[-200:]
        try:
            res = md.call_claude(PROPOSE_PROMPT.format(n=per_era, y0=y0, y1=y1, npol=max(2, per_era // 6),
                                                       avoid="\n".join("- " + t for t in avoid) or "(none yet)"),
                                 propose_model, max_tokens=4000, keys=["s"], fatal=False)
        except SystemExit:
            res = None
        empty += 0 if (res or {}).get("s") else 1
        for p in (res or {}).get("s", []):
            if not isinstance(p, dict) or not p.get("title"):
                continue
            t = str(p["title"]).strip()
            if t.lower() in seen or t in used_titles:
                continue
            seen.add(t.lower()); titles.append(t); meta[t] = p
        log(f"  {y0}-{y1}: {len(titles)} titles so far")
    log(f"  {len(titles)} proposed titles; checking Wikipedia…")
    wiki = wiki_lookup(titles)
    log(f"  {len(wiki)} resolve to real articles")
    cands, wiki_by_idx = [], {}
    for t in titles:
        w = wiki.get(t)
        if not w:
            continue
        try:
            yr = int(meta[t].get("year"))
        except Exception:
            yr = 0
        if not (HIST_FROM <= yr <= dt.date.today().year):
            continue
        wiki_by_idx[len(cands)] = w
        cands.append({"title": w["title"], "summary": w["extract"][:900], "source": "Wikipedia", "published": str(yr),
                      "link": w["url"], "text": w["extract"][:2000], "pol": bool(meta[t].get("pol"))})
    if len(cands) < 6:
        log("  too few grounded stories; nothing added to Practice")
        return 0
    cands = cands[:ask]
    pairs, fakes = ask_in_chunks(cands, model, lambda k: hist_prompt(k, 4), 9, 24)
    prior = md.prior_stories(day, days=3650)
    good = []
    for r, c in pairs:
        r["src"], r["published"] = c["link"], c["published"]
        r["_title"], r["_summary"], r["_text"] = c["title"], c["summary"], c["text"]
        why = check_real(r, historical=True)
        if not why and not re.search(r"\(\s*\d{3,4}\s*\)\s*$", r.get("place", "")):
            r["place"] = f"{r.get('place', '').strip()} ({c['published']})"
        why = why or quality_gate(r)
        if why or r["src"] in have:
            log(f"  drop ({why or 'already in Practice'}): {r.get('h')}")
            continue
        if md.seen_before(r, prior):
            log(f"  drop (same story as one already shown): {r.get('h')}")
            continue
        good.append((r, c))
        prior.append(((r.get("place") or "").split(",")[0].strip().lower(), md.words((r.get("h") or "") + " " + " ".join(r.get("ans") or []))))
    reals = md.fact_check([r for r, _ in good], model)
    keep_ids = {id(r) for r in reals}
    good = sorted([(r, c) for r, c in good if id(r) in keep_ids], key=lambda rc: md.merit(rc[0]), reverse=True)[:n]    # the best n, not the first n
    vecs = {}
    if os.environ.get("GLOVE"):
        wanted = [a for r, _ in good for a in (r.get("ans") or [])]
        wanted += [w for r, _ in good for w in re.findall(r"[a-z]+", (r.get("h2") or "").lower())]
        vecs = md.load_vectors(os.environ["GLOVE"], wanted)
    added = 0
    for r, c in good:
        refine_coords(r, wiki_by_idx.get(cands.index(c), {}).get("coord"), geocode)
        x = {"real": True, "day": PRACTICE_DAY}
        x.update(clean_real(r, ("h", "place", "src", "clue")))
        x["lat"], x["lng"] = round(float(r["lat"]), 4), round(float(r["lng"]), 4)
        if c.get("pol") or r.get("pol"):
            x["cat"] = "pol"
        tmp = dict(r, _title="")          # a Wikipedia title is Title Case, which would make every noun look like a name
        if r.get("h2") and md.valid_blank(tmp):
            x["h2"], x["ans"] = tmp["h2"], tmp["ans"]
            a = next((a for a in tmp["ans"] if a in vecs), None)
            if a:
                x["vec"] = md.pack_vec(vecs[a])
        arch["real"].append(x)
        have.add(x["src"])
        added += 1
    nf = 0
    for f in fakes:
        if md.valid_fake(f) and f["h"] not in have:
            have.add(f["h"])
            arch["fake"].append({"real": False, "h": f["h"], "tell": (f.get("tell") or "")[:160], "day": PRACTICE_DAY})
            nf += 1
    arch["real"], arch["fake"] = arch["real"][-3000:], arch["fake"][-800:]
    save("practice.json", arch, separators=(",", ":"))
    log(f"Practice: added {added} historical stories and {nf} fakes ({len(arch['real'])} / {len(arch['fake'])} in the archive)")
    return added


# ---------------------------------------------------------------- recent stories for Politics and Fill the Blank
def used_links():
    return {l for links in load("history.json", {}).values() for l in links}


def stock_headlines(stock):
    out = []
    for k in ("pol", "blank"):
        for r in stock.get(k, []):
            out.append((r.get("h") or (r.get("h2") or "").replace("[]", (r.get("ans") or ["…"])[0]))[:140])
    return out


def source_politics(n, now, day, geocode):
    stock = load("stock.json", {})
    model = os.environ.get("FF_MODEL", "claude-sonnet-5-5")
    pc = md.gather(now, used_links(), md.POL_FEEDS, 60)
    have_src = {r.get("src") for r in stock.get("pol", [])}
    pc = [c for c in pc if c["link"] not in have_src]
    log(f"Politics: {len(pc)} candidates")
    if len(pc) < 8:
        log("  too few politics candidates; skipping")
        return 0
    random.Random(day).shuffle(pc)
    pc = pc[:90]
    md.add_text(pc)
    prompt = md.POL_PROMPT
    assert "pick up to 8" in prompt and "write 6 invented" in prompt, "politics prompt changed; update make_stock.py"
    prompt = prompt.replace("pick up to 8", f"pick up to {n}").replace("at most 3 from one country", "at most 4 from one country").replace("write 6 invented", f"write {max(6, n // 2)} invented")
    avoid = md.recent_headlines(day) + stock_headlines(stock)
    pairs, pfakes = ask_in_chunks(pc, model, lambda k: prompt.replace(f"pick up to {n}", f"pick up to {k}"), max(8, n // 2), 45, avoid)
    prior = md.prior_stories(day)
    seen_place = {(r.get("place") or "").split(",")[0].strip().lower() for r in stock.get("pol", [])}
    good = []
    for r, c in pairs:
        r["src"], r["published"] = c["link"], c["published"]
        r["_title"], r["_summary"], r["_text"] = c["title"], c["summary"], c.get("text", "")
        why = check_real(r)
        key = (r.get("place") or "").split(",")[0].strip().lower()
        if not why and key in seen_place:
            why = "same place as another stock story"
        if not why and md.seen_before(r, prior):
            why = "already shown"
        if why:
            log(f"  drop politics ({why}): {r.get('h')}")
            continue
        seen_place.add(key)
        good.append(r)
    good = md.fact_check(good, model)[:n]
    added = []
    for r in good:
        refine_coords(r, None, geocode)
        x = clean_real(r, ("h", "place", "src", "published", "clue", "country", "cc"))
        x.update(lat=round(float(r["lat"]), 4), lng=round(float(r["lng"]), 4), cat="pol", banked=day)
        added.append(x)
    fakes = [dict(f, banked=day) for f in pfakes if md.valid_fake(f)]
    have_f = {f.get("h") for f in stock.get("polfake", [])}
    fakes = [{"h": f["h"], "banked": day} for f in fakes if f["h"] not in have_f]
    stock["pol"] = stock.get("pol", []) + added
    stock["polfake"] = stock.get("polfake", []) + fakes
    save("stock.json", stock, indent=1)
    log(f"Politics: added {len(added)} stories and {len(fakes)} fakes to the stock")
    return len(added)


def source_blank(n, now, day, geocode):
    stock = load("stock.json", {})
    model = os.environ.get("FF_MODEL", "claude-sonnet-5-5")
    cands = md.gather(now, used_links())
    have_src = {r.get("src") for r in stock.get("blank", [])}
    cands = [c for c in cands if c["link"] not in have_src]
    log(f"Fill the Blank: {len(cands)} candidates")
    if len(cands) < 20:
        log("  too few candidates; skipping")
        return 0
    random.Random(day + "blank").shuffle(cands)
    cands = md.screen(cands, os.environ.get("FF_SCREEN_MODEL", "claude-haiku-5-5"), top=min(90, max(40, n * 2)))
    md.add_text(cands)
    prompt = md.PROMPT.replace("write 8 invented headlines", "write 2 invented headlines", 1)
    avoid = md.recent_headlines(day) + stock_headlines(stock)
    pairs, _ = ask_in_chunks(cands, model, lambda k: strict_prompt(prompt, k), 8, 30, avoid)
    prior = md.prior_stories(day)
    seen_place = {(r.get("place") or "").split(",")[0].strip().lower() for r in stock.get("blank", [])}
    good = []
    for r, c in pairs:
        r["src"], r["published"] = c["link"], c["published"]
        r["_title"], r["_summary"], r["_text"] = c["title"], c["summary"], c.get("text", "")
        why = check_real(r)
        key = (r.get("place") or "").split(",")[0].strip().lower()
        why = why or quality_gate(r)
        if not why and not md.valid_blank(r):
            why = "bad hidden word"
        if not why and key in seen_place:
            why = "same place as another stock story"
        if not why and md.seen_before(r, prior):
            why = "already shown"
        if why:
            log(f"  drop blank ({why}): {r.get('h2') or r.get('h')}")
            continue
        seen_place.add(key)
        good.append(r)
    good = sorted(md.fact_check(good, model), key=md.merit, reverse=True)[:n]
    added = []
    for r in good:
        refine_coords(r, None, geocode)
        x = clean_real(r, ("h", "h2", "ans", "place", "src", "published", "clue", "country", "cc", "clue_strength", "unbelievable", "theme"))
        x.update(lat=round(float(r["lat"]), 4), lng=round(float(r["lng"]), 4), banked=day, rules=md.RULES)
        added.append(x)
    stock["blank"] = stock.get("blank", []) + added
    save("stock.json", stock, indent=1)
    log(f"Fill the Blank: added {len(added)} stories to the stock")
    return len(added)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--practice", type=int, default=0, help="historical stories to add to Practice (last 200 years)")
    ap.add_argument("--politics", type=int, default=0, help="recent politics stories to stock")
    ap.add_argument("--blank", type=int, default=0, help="recent Fill the Blank stories to stock")
    ap.add_argument("--no-geocode", action="store_true")
    args = ap.parse_args()
    if not (args.practice or args.politics or args.blank):
        ap.error("pick at least one of --practice, --politics, --blank")
    os.makedirs(DATA, exist_ok=True)
    now, day = today_et()
    geocode = not args.no_geocode
    total = 0
    for fn, n in ((source_practice, args.practice), (source_politics, args.politics), (source_blank, args.blank)):
        if not n:
            continue
        try:
            total += fn(n, now, day, geocode)
        except SystemExit as e:                  # a failed Claude call must not lose the other jobs
            log(f"{fn.__name__} stopped: {e}")
    log(f"Estimated Claude cost this run: ${md.SPENT[0]:.2f}; {total} stories added")


if __name__ == "__main__":
    main()
