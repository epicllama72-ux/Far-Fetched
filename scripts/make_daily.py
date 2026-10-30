#!/usr/bin/env python3
"""
Far-Fetched daily headline builder.

Pulls weird-news stories from the past month (English outlets, Reddit, Google News in
several languages), has a cheap model screen them all, fetches the article text for the
best ~50, asks Claude to pick and rewrite the best as redacted headlines plus a couple of
believable fakes, fact-checks the rewrites against the articles, then geocodes them and
writes data/<YYYY-MM-DD>.json and data/latest.json for the game. Good stories that are not
used today go into data/bank.json and fill in on thin days.

Usage:
  python scripts/make_daily.py                 # normal run (needs ANTHROPIC_API_KEY)
  python scripts/make_daily.py --picks f.json  # skip the Claude call, use a picks file
  python scripts/make_daily.py --date 2026-10-27 --no-geocode

Env:
  ANTHROPIC_API_KEY   required unless --picks is given
  GLOVE               path to glove-wiki-gigaword-50.gz (word vectors for Fill the Blank)
  FF_MODEL            Claude model for picking/writing/fact-checking (default claude-sonnet-5-5)
  FF_SCREEN_MODEL     cheap model that scores every candidate (default claude-haiku-5-5)
"""
import argparse, base64, concurrent.futures, datetime as dt, email.utils, gzip, html, json, math, os, random, re, sys, time
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = "far-fetched-game/1.0 (daily headline builder; github actions)"

FEEDS = [
    ("UPI Odd News", "https://rss.upi.com/news/odd_news.rss"),
    ("Sky News Strange", "https://feeds.skynews.com/feeds/rss/strange.xml"),
    ("NY Post Weird But True", "https://nypost.com/weird-but-true/feed/"),
    ("Oddity Central", "https://www.odditycentral.com/feed"),
    ("HuffPost Weird News", "https://www.huffpost.com/section/weird-news/feed"),
    ("Metro Weird", "https://metro.co.uk/news/weird/feed/"),
    ("Mirror Weird News", "https://www.mirror.co.uk/news/weird-news/?service=rss"),
    # Reddit: the community upvotes the funniest real stories; the entry links to the article.
    ("Reddit nottheonion", "https://www.reddit.com/r/nottheonion/top/.rss?t=month&limit=100"),
    ("Reddit NewsOfTheWeird", "https://www.reddit.com/r/NewsOfTheWeird/top/.rss?t=month&limit=100"),
]
def _gn(q, hl="en-US", gl="US", ceid="US:en"):
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q + ' when:30d')}&hl={hl}&gl={gl}&ceid={ceid}"
# Weird-news sections reached through Google News (works even when an outlet blocks direct feed requests)
FEEDS += [
    ("Google News site: HuffPost weird", _gn("site:huffpost.com weird")),
    ("Google News site: Metro weird", _gn("site:metro.co.uk weird OR bizarre", "en-GB", "GB", "GB:en")),
    ("Google News site: Mirror weird", _gn("site:mirror.co.uk weird OR bizarre", "en-GB", "GB", "GB:en")),
    ("Google News site: Times of India offbeat", _gn("site:timesofindia.indiatimes.com offbeat OR bizarre OR viral", "en-IN", "IN", "IN:en")),
    ("Google News site: ABC Australia", _gn("site:abc.net.au bizarre OR quirky OR unusual", "en-AU", "AU", "AU:en")),
    ("Google News site: Stuff NZ", _gn("site:stuff.co.nz quirky OR bizarre OR odd", "en-NZ", "NZ", "NZ:en")),
    ("Google News site: AP oddities", _gn("site:apnews.com oddities OR bizarre OR unusual")),
]
# Non-English editions: stories English outlets miss (Claude translates them).
FEEDS += [
    ("Google News es-MX", _gn("insólito OR curioso OR surrealista OR increíble", "es-419", "MX", "MX:es-419")),
    ("Google News es-ES", _gn("insólito OR surrealista OR curioso", "es", "ES", "ES:es")),
    ("Google News fr", _gn("insolite OR improbable OR cocasse", "fr", "FR", "FR:fr")),
    ("Google News de", _gn("kurios OR skurril OR verrückt OR \"kaum zu glauben\"", "de", "DE", "DE:de")),
    ("Google News pt-BR", _gn("inusitado OR curioso OR bizarro", "pt-BR", "BR", "BR:pt-419")),
    ("Google News it", _gn("curioso OR surreale OR incredibile OR bizzarro", "it", "IT", "IT:it")),
    ("Google News ja", _gn("珍事 OR 珍騒動 OR まさか", "ja", "JP", "JP:ja")),
    ("Google News nl", _gn("opmerkelijk OR bizar OR curieus", "nl", "NL", "NL:nl")),
    ("Google News es-AR", _gn("insólito OR increíble OR curioso", "es-419", "AR", "AR:es-419")),
    ("Google News es-CO", _gn("insólito OR curioso OR increíble", "es-419", "CO", "CO:es-419")),
    ("Google News pt-PT", _gn("insólito OR caricato OR curioso", "pt-PT", "PT", "PT:pt-150")),
    ("Google News ko", _gn("황당 OR 기막힌 OR 진기한", "ko", "KR", "KR:ko")),
    ("Google News zh-TW", _gn("離奇 OR 荒謬 OR 奇聞", "zh-TW", "TW", "TW:zh-Hant")),
    ("Google News id", _gn("unik OR aneh OR nyeleneh OR \"tak biasa\"", "id", "ID", "ID:id")),
    ("Google News tr", _gn("ilginç OR tuhaf OR \"görenler şaşkına döndü\"", "tr", "TR", "TR:tr")),
    ("Google News pl", _gn("kuriozalny OR absurdalny OR nietypowy", "pl", "PL", "PL:pl")),
    ("Google News sv", _gn("bisarr OR märklig OR kuriös", "sv", "SE", "SE:sv")),
    ("Google News ar", _gn("غريب OR طريف OR \"واقعة غريبة\"", "ar", "EG", "EG:ar")),
    ("Google News he", _gn("מוזר OR הזוי OR משעשע", "he", "IL", "IL:he")),
]
# Worldwide net: Google News searches for odd-but-true stories from the past 30 days, one per regional edition.
_ODD_Q = urllib.parse.quote('(bizarre OR quirky OR "strange but true" OR unusual OR unlikely OR "world record") (rescued OR escaped OR found OR wins OR discovered OR stuck OR unveiled OR spotted) when:30d')
_ODD_Q3 = urllib.parse.quote('(arrested OR stole OR stolen OR burglar OR thief OR "pulled over" OR caught) (bizarre OR ridiculous OR hilarious OR strange OR "of all things" OR absurd) when:30d')
_ODD_Q2 = urllib.parse.quote('(ridiculous OR hilarious OR absurd OR baffling OR "only in") (council OR police OR residents OR town OR village OR farmer OR zoo) when:30d')
for _name, _gl, _ceid in [("US", "US", "US:en"), ("UK", "GB", "GB:en"), ("Australia", "AU", "AU:en"), ("Canada", "CA", "CA:en"),
                          ("India", "IN", "IN:en"), ("Ireland", "IE", "IE:en"), ("New Zealand", "NZ", "NZ:en"),
                          ("South Africa", "ZA", "ZA:en"), ("Singapore", "SG", "SG:en"), ("Philippines", "PH", "PH:en"),
                          ("Nigeria", "NG", "NG:en"), ("Kenya", "KE", "KE:en"), ("Ghana", "GH", "GH:en"),
                          ("Pakistan", "PK", "PK:en"), ("Malaysia", "MY", "MY:en")]:
    FEEDS.append((f"Google News odd ({_name})", f"https://news.google.com/rss/search?q={_ODD_Q}&hl=en-{_gl}&gl={_gl}&ceid={_ceid}"))
    FEEDS.append((f"Google News crime ({_name})", f"https://news.google.com/rss/search?q={_ODD_Q3}&hl=en-{_gl}&gl={_gl}&ceid={_ceid}"))
    FEEDS.append((f"Google News absurd ({_name})", f"https://news.google.com/rss/search?q={_ODD_Q2}&hl=en-{_gl}&gl={_gl}&ceid={_ceid}"))
POL_FEEDS = [  # Google News searches for odd local-government and election stories worldwide
    ("Google News: odd elections", "https://news.google.com/rss/search?q=" + urllib.parse.quote('(election OR candidate OR ballot) (tie OR "coin toss" OR "dog mayor" OR bizarre OR unusual) when:21d') + "&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: odd councils", "https://news.google.com/rss/search?q=" + urllib.parse.quote('(council OR parliament OR lawmakers OR mayor) (bizarre OR quirky OR unusual OR "honorary mayor") when:21d') + "&hl=en-US&gl=US&ceid=US:en"),
    ("UPI Odd News", "https://rss.upi.com/news/odd_news.rss"),
]
MAX_AGE_DAYS = 30
POL_MAX_AGE_DAYS = 45
N_POL_REAL, N_POL_FAKE = 5, 2   # Politics: 5 true + exactly 2 fakes
N_CALL_REAL, N_CALL_FAKE, N_BLANK = 5, 2, 7   # Daily Dig: 5 true + exactly 2 fakes
SCREEN_TOP = 50
SCREEN_PER_COUNTRY = 7  # at most this many shortlisted stories from one country
# how many screened candidates get their article fetched and go to the editor
BANK_DAYS, BANK_KEEP, BANK_MAX = 30, 365, 400   # Real or Fake takes banked stories up to 30 days old; Fill the Blank any up to a year
RULES = 2              # bump when the picking rules change: banked stories chosen under older rules are dropped   # banked stories expire after this many days; keep at most this many


# ---------------------------------------------------------------- feeds
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=500):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"[\ud800-\udfff\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)   # lone surrogates / control chars make the API reject the request
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
        href = link.get("href") if link is not None else ""
        content = it.findtext("atom:content", default="", namespaces=ns) or ""
        summary = clean(it.findtext("atom:summary", default="", namespaces=ns))
        if source.startswith("Reddit"):
            m = re.search(r'<a href="(https?://[^"]+)">\[link\]', content)
            if not m or "reddit.com" in m.group(1) or "redd.it" in m.group(1):
                continue                         # self posts and image posts are not news stories
            href, summary = html.unescape(m.group(1)), ""
        items.append({
            "title": clean(it.findtext("atom:title", default="", namespaces=ns), 200),
            "link": href,
            "summary": summary,
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

Below are candidate news stories from the past month (JSON). Some include "text", the opening of the article: use it for the facts and the punchline. Some are not in English: write everything you output in English. Do two jobs and reply with ONE JSON object only, no prose.

1) "real": pick 20 of the best stories for the game, best first. You MUST return at least 14; if clue-rich stories run short, fill the rest with the most surprising remaining ones (low clue_strength is fine), never return fewer than 14 (rank by clue_strength plus unbelievable). Rank by how HARD TO BELIEVE the story is: favour the ones a reader would assume are made up (absurd specifics, improbable coincidences, odd rules, unlikely animals or objects) but that really happened. Strongly prefer stories where the event is linked to the place it happened, so a player can DEDUCE the location from the headline instead of guessing: local wildlife or climate (a kangaroo, a moose, monsoon flooding, snow in summer), regional industry, food, sport or customs, a local law or institution type, driving side, currency, units or typical local jobs. Stories that could have happened anywhere rank lower but are still acceptable fillers. The game should be FUNNY, EDUCATIONAL and reward broad general knowledge: favour stories that make a reader laugh out loud or are so ridiculous they feel like something out of the movie Idiocracy (absurd decisions, baffling rules, dumbfounding mix-ups, spectacularly dumb but harmless events), and whose clues connect to things a well-read person could know about the place (geography, climate, wildlife, languages, currencies, laws, food, history, sport, industries), so that general knowledge lets them reason to the right region, not luck. Good picks are surprising, funny or delightful, happened in ONE specific identifiable town/city/county, and are light-hearted. Every pick must make sense on its own: a reader must understand exactly what happened from the headline alone (no missing context, no confusing or contradictory details), and the hidden place must be where it actually happened. Skip BORING stories: a rare animal spotted, a nice rescue, a big vegetable, a quiet record, a mild oddity with no real twist. Every pick needs a real punchline, an absurd mistake or a jaw-dropping detail. STRANGE, FUNNY CRIME stories are welcome and wanted (dumb criminals, bizarre thefts, absurd arrests, getaway fails, odd things stolen or smuggled, ridiculous excuses) as long as they are non-violent and nobody is seriously hurt. Never pick stories about death, serious injury, violent crimes against people, abuse, sexual content, children in danger, war, politics, disasters or tragedy. That includes floods, storms, wildfires, quakes, heatwaves and outbreaks, even when the story itself is light-hearted (a man swimming down a flooded street is out), because they are often tied to real tragedies. Write headlines in plain words a general reader knows: no local weather jargon or acronyms (never 'DANA', 'derecho', 'haboob' and the like). Avoid near-duplicates (e.g. several lottery wins) - at most one lottery story. Spread your picks around the WORLD: at most 3 from any one country, and at least half from outside the US, UK, Canada, Australia, New Zealand and Ireland whenever the candidates allow it (Latin America, Europe, Asia, Africa and the Middle East are all welcome). Include at least one story from Africa, one from South America and one from Oceania or the Pacific whenever the candidates have a usable one, and never more than two stories about the same kind of thing (dogs, goats, cheese and so on). For each pick give:
   - "id": the candidate id.
   - "h": a punchy wire-style headline, max 110 characters, keeping every fact of the story exactly as reported (never change what happened, who or what was involved, numbers or outcome). You MAY reword and add short descriptive wording that makes the region easier to deduce, as long as it is generically true of that place and does not alter or add to the story: e.g. local terms and institutions ("the county sheriff", "council", "constabulary", "a roo", "monsoon rains", "rupees", "kilometres", "on the ski slopes"), local climate, wildlife or customs, or spelling and units typical of the country. Replace the place with the literal token {} exactly once (e.g. "... in {}"). Remove every EXPLICIT location clue: no state/country/city names, demonyms, local team, landmark, university or company names that reveal the place (landmarks and nearby cities belong in "hint", not here), no "Florida man". But KEEP the implicit, deducible clues that are part of the story (the animal, the weather, the local custom, currency or units, the type of institution), so a sharp player can reason their way to the right region. Keep the oddity intact. The clues do NOT have to come from the original headline: the article text ("text") often holds the best ones (a currency, a unit, a local institution, an animal, the weather, a local custom or law, a sport, a typical job). If a great story's clues sit in the body, WRITE them into "h" in your own words, as long as they are facts the text states or are generically true of that place, so the game headline can differ a lot from the source headline. A story whose body gives a fair regional clue is better than one with a punchier headline and nothing to deduce from.
   - "h2": the same story as a headline that DOES name the place (city + state/country), with the main subject noun of the story (the animal, object or kind of person it is about, e.g. "kangaroo", "pelicans", "kitten") replaced by the literal token [] exactly once. The hidden word must be an ordinary common noun that is written in lowercase in normal text: never a name, brand, product, model, company, team, place or nationality (not "Maserati" or "Vegemite" - use "car" or "spread" instead, or pick another noun). The rest of h2 must NOT contain the hidden word, a synonym or a close variant of it (if the hidden word is "toilets", h2 must not say "potties" or "loo"), and give each pick a different hidden word than the other picks (not both "burglar" and "thief").
   - "ans": 4-14 lowercase single-word answers that should all COUNT as correct for [] - the exact hidden word first, then its singular/plural, then every reasonable synonym, informal word, regional variant or obvious broader word a player might type for it (e.g. "boa" -> ["boa","boas","snake","snakes","serpent","constrictor"]; "police" -> ["police","officers","officer","cops","cop","policemen","policeman","constables"]; "sofa" -> ["sofa","sofas","couch","couches","settee"]). Be generous, but never include a word that means something clearly different (not "lizard" for snake). Letters only.
   - "place": display name like "Appleton, Wisconsin" or "Mykolaiv, Ukraine".
   - "geocode": a precise query for a geocoder, like "Appleton, Wisconsin, USA".
   - "lat", "lng": your best coordinates for the place (decimal degrees).
   - "country": the country name.
   - "cc": the ISO 3166-1 alpha-2 country code ("US", "GB" for anywhere in the UK, "JP", ...).
   - "hint": at most 60 characters, a short phrase that continues the headline after "in {}", shown to the player ONLY after they have called the story real or fake, to help them place it. It is a KNOWLEDGE clue, so the game rewards what a well-read person knows: a landmark the place is known for, a historical fact, its geography or surroundings (a nearby famous city, coast, mountains, lake, climate), a political or institutional fact, or a media reference (a film, show or book set there). Give one even when the source never mentions it, as long as it is TRUE of that place; it describes the PLACE, never the event. It must be a hint, not an answer: never write the hidden place, its state/region/country or a nationality, though a famous landmark or a different, nearby big city is fine. Good examples: "home of the Royal Pavilion", "near the theme parks", "a short train ride from Glasgow", "the planned capital beside a man-made lake", "a factory town east of the capital". Use "" if you are not sure of a true one.
   - "clue": at most 80 characters, written for the PLAYER after the reveal: the regional hints that point to the place, e.g. "moose + wire fencing = northern US/Canada", "monsoon flooding = South Asia", "metres and rupees". It may draw on details from the story itself, not only words in the headline. Use "" if there is none.
   - "clue_strength": 0-3. 3 = the headline strongly points to a region; 2 = a fair hint that a well-read general player could use; 1 = a weak hint; 0 = could be anywhere, OR the hint is misleading (an animal or object that is out of place, such as a kangaroo in Tennessee, is a surprise, not a clue: give it 0). JARGON-ONLY CLUES DO NOT COUNT: if the only regional hint is a local term, acronym or specialist word that a general player would not know (e.g. "cold-drop DANA", a regional dish or agency name nobody outside the country has heard of), set "jargon_only" to true and give clue_strength 0 or 1. A clue must work by general knowledge: currency, units, driving side, climate, wildlife, institution types, sport, language of well-known words.
   - "mappable": 0-3, how sensibly a player can drop a pin for this story. 3 = it happened at ONE specific town, city or county and the headline's clues point to that region; 2 = one specific place but the clues are thin; 1 = the place is only approximate (a region, a journey, or the HQ or court that reported it rather than where it happened); 0 = a pin makes no sense (online, nationwide, several places, a place unrelated to the event). Be strict: when in doubt, score lower.
   - "unbelievable": 1-5, how likely a reader is to assume the story is made up (5 = sounds like a joke but is true).
   - "theme": one lowercase word for the topic (e.g. "lottery", "escaped-animal", "record"). Never pick two stories with the same theme.
   Keep the source's own units and currency; NEVER convert them (a "4.7 miles" story from South Africa must read in kilometres if the source said kilometres), because units are clues.

2) "fake": write 8 invented headlines in exactly the same style, each with {} for the redacted place. Like the real ones, give each fake implicit regional clues a player could use (a local animal, climate, custom or unit) without naming the place. They should pass at first glance (specific numbers, ordinary local institutions, plausible animals and objects) but be SLIGHTLY easier to spot than the real ones: give each one small tell, and VARY the kind of tell: an over-tidy cause and effect, a detail slightly wrong for the region, an unlikely coincidence, a plausible-sounding but slightly off local rule. NO quotes from officials or spokespeople, no puns, no punchlines - those are tells players learn fast. Do NOT end with an absurd official reaction (a fine, a ban, a decree, a new rule, an apology): at most ONE fake in the set may do that. Vary how the fakes finish: some stop at the plain odd event, some end on a number, some on a consequence for ordinary people. Keep each fake at most 95 characters (no longer than a typical real headline), start each one differently (never two starting with the same two words, never "Council in"), and give each a different topic from each other and from the real picks (if a real story is about a kangaroo, no fake about kangaroos, wallabies or koalas). NEVER use the word "exactly", round numbers (100, 500, 1,000...) or "to the second" - players learn those tells within a week; use ordinary specific numbers like 37 or 1,260. Make the fakes as odd as the real stories, not odder: the real stories are routine local news with one strange twist, so the fakes must not be the only headlines that sound like jokes. Never cartoonish. Not about real people, and not resembling any candidate story. Strange non-violent crime (dumb criminals, bizarre thefts) is fine for fakes too. Do not write about death, injury, violence or tragedy. Give each as {"h": "...", "tell": "one sentence of at most 18 words for the PLAYER explaining why it should have smelled fake, e.g. 'councils fine overgrown hedges, not ones that look too lifelike'", "obvious": 1-5, "theme": "one lowercase word for the topic, same vocabulary as the real picks' theme"} where obvious rates how easy it is to spot as invented (aim for 2 or 3: slightly easier to spot than a real story, never a giveaway).

Output format:
{"real":[{"id":0,"h":"...","h2":"...","ans":["..."],"place":"...","geocode":"...","lat":0,"lng":0,"country":"...","cc":"..","hint":"...","clue":"...","clue_strength":0,"jargon_only":false,"mappable":0,"unbelievable":1,"theme":"..."}], "fake":[{"h":"...","tell":"...","obvious":3,"theme":"..."}]}

Candidates:
"""


POL_PROMPT = """You are the editor of the Politics mode of "Far-Fetched", a daily guessing game built on hard-to-believe but true news.

Below are candidate stories (JSON). Reply with ONE JSON object only.

1) "real": pick up to 8 stories about hard-to-believe POLITICAL or GOVERNMENT happenings anywhere in the world: odd election results and tiebreaks, animal or joke candidates, strange council votes, unusual laws, quirky parliamentary moments. Keep it light and strictly non-partisan. Never pick stories about violence, death, scandals, allegations, crimes, wars, protests, or anything that mocks or praises a party, ideology or named politician. Skip BORING ones: every pick needs a real punchline or a ridiculous, Idiocracy-style detail (a plain tied vote or a routine procedural story is not enough), and pick at most ONE tie or tiebreak story. Never pick two stories about the same event or the same place. Spread picks around the world, at most 3 from one country. Prefer the hardest-to-believe ones whose details hint at the region (local customs, institutions, offices, voting rules) so a player can deduce where it happened. Each must be tied to one specific place where it actually happened, and must make complete sense from the headline alone. Pick at most ONE story about animal or dog candidates, and make sure the set is not all the same kind of story. "place" must be a specific town, city or county (with its region or country after a comma), NEVER just a country. For each give "id", "h" (wire-style headline, max 100 chars, only facts from the candidate, the place replaced by the literal token {} exactly once, no other location clues, and no names of real politicians or parties), "place", "geocode", "lat", "lng", and "clue" (at most 80 characters, written for the PLAYER after the reveal: the regional hints that point to the place, drawn from the story, e.g. "mayor + council + wards = Canada or Commonwealth"; never empty).

2) "fake": write 6 invented headlines in the same style, each with {} for the place, about councils, parliaments, mayors, elections or laws. Each must pass at first glance but carry one small tell, varied across the six (over-tidy cause and effect, a detail slightly wrong for the region, an unlikely coincidence). No quotes from officials, no puns or punchlines, at most 95 characters, each starting differently (never "Council in"). Never use the word "exactly" or round numbers. NEVER name or hint at a real person, party, politician or real event, and never take a side. Give each as {"h": "..."}.

Output format:
{"real":[{"id":0,"h":"...","place":"...","geocode":"...","lat":0,"lng":0,"clue":"..."}], "fake":[{"h":"..."}]}

Candidates:
"""


COUNTRY_NAMES = set(['afghanistan', 'albania', 'algeria', 'andorra', 'angola', 'argentina', 'armenia', 'australia', 'austria', 'azerbaijan', 'bahamas', 'bahrain', 'bangladesh', 'barbados', 'belarus', 'belgium', 'belize', 'benin', 'bhutan', 'bolivia', 'bosnia', 'botswana', 'brazil', 'brunei', 'bulgaria', 'burundi', 'cambodia', 'cameroon', 'canada', 'chad', 'chile', 'china', 'colombia', 'congo', 'croatia', 'cuba', 'cyprus', 'czechia', 'denmark', 'djibouti', 'ecuador', 'egypt', 'eritrea', 'estonia', 'eswatini', 'ethiopia', 'fiji', 'finland', 'france', 'gabon', 'gambia', 'germany', 'ghana', 'greece', 'guatemala', 'guyana', 'haiti', 'honduras', 'hungary', 'iceland', 'india', 'indonesia', 'iran', 'iraq', 'ireland', 'israel', 'italy', 'jamaica', 'japan', 'jordan', 'kazakhstan', 'kenya', 'kosovo', 'kuwait', 'kyrgyzstan', 'laos', 'latvia', 'lebanon', 'lesotho', 'liberia', 'libya', 'lithuania', 'luxembourg', 'madagascar', 'malawi', 'malaysia', 'maldives', 'mali', 'malta', 'mauritania', 'mauritius', 'mexico', 'moldova', 'monaco', 'mongolia', 'montenegro', 'morocco', 'mozambique', 'myanmar', 'namibia', 'nepal', 'netherlands', 'nicaragua', 'niger', 'nigeria', 'norway', 'oman', 'pakistan', 'panama', 'paraguay', 'peru', 'philippines', 'poland', 'portugal', 'qatar', 'romania', 'russia', 'rwanda', 'samoa', 'senegal', 'serbia', 'singapore', 'slovakia', 'slovenia', 'somalia', 'spain', 'suriname', 'sweden', 'switzerland', 'syria', 'taiwan', 'tajikistan', 'tanzania', 'thailand', 'togo', 'tonga', 'trinidad', 'tunisia', 'turkey', 'turkmenistan', 'uae', 'uganda', 'uk', 'ukraine', 'uruguay', 'usa', 'uzbekistan', 'vanuatu', 'venezuela', 'vietnam', 'yemen', 'zambia', 'zimbabwe']) | set(['burkina faso', 'costa rica', 'dominican republic', 'el salvador', 'new zealand', 'north korea', 'north macedonia', 'papua new guinea', 'saudi arabia', 'sierra leone', 'south africa', 'south korea', 'south sudan', 'sri lanka', 'united arab emirates', 'united kingdom', 'united states'])


def ask_claude(cands, model, prompt=None, avoid=None):
    payload = []
    for i, c in enumerate(cands):
        p = {"id": i, "title": c["title"], "summary": c["summary"], "source": c["source"], "published": c["published"]}
        if c.get("text"):
            p["text"] = c["text"]
        payload.append(p)
    extra = ""
    if avoid:
        extra = ("\n\nALREADY USED in earlier games. Never pick any of these stories again, even if a candidate tells it "
                 "with different words or comes from a different outlet:\n" + "\n".join("- " + a for a in avoid) + "\n")
    return call_claude((prompt or PROMPT) + json.dumps(payload, ensure_ascii=False) + extra, model, max_tokens=16000, keys=["real", "fake"])


PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.0, 10.0), "opus": (5.0, 25.0)}   # USD per million tokens (in, out)
SPENT = [0.0]


def note_cost(model, usage):
    tier = next((k for k in PRICES if k in model), "sonnet")
    pin, pout = PRICES[tier]
    if tier == "haiku" and usage.get("input_tokens", 0) > 100_000:
        pin, pout = 0.50, 2.50
    c = usage.get("input_tokens", 0) / 1e6 * pin + usage.get("output_tokens", 0) / 1e6 * pout
    SPENT[0] += c
    return c


def call_claude(content, model, max_tokens=12000, fatal=True, keys=None):
    """One Claude call. The answer comes back through a forced tool call, so the API hands us
    already-parsed JSON (no broken quotes or stray prose). `keys` are the top-level arrays expected;
    by default they are read from the {"key": ...} names in the prompt's output example."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set (or pass --picks).")
    if keys is None:
        keys = [k for k in ("real", "fake", "s", "checks") if f'{{"{k}":' in content or f'"{k}":[' in content] or ["real", "fake"]
    schema = {"type": "object", "properties": {k: {"type": "array", "items": {}} for k in keys}, "required": keys}
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "tools": [{"name": "submit", "description": "Submit your answer as structured data.", "input_schema": schema}],
        # Haiku accepts a forced tool call; Sonnet 5.5 refuses forcing, so it gets "auto" plus an instruction
        "tool_choice": {"type": "tool", "name": "submit"} if "haiku" in model else {"type": "auto"},
        "messages": [{"role": "user", "content": content + "\n\n(You MUST submit your answer by calling the submit tool once, using exactly the JSON structure described above. Do not write the answer as text.)"}],
    }
    body["messages"][0]["content"] = re.sub(r"[\ud800-\udfff]", "", body["messages"][0]["content"])
    last = None
    for attempt in range(4):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.loads(r.read())
            note_cost(model, res.get("usage") or {})
            if res.get("stop_reason") == "max_tokens":
                print("  Claude hit the output limit; the answer may be cut short", file=sys.stderr)
            for b in res.get("content", []):
                if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                    out = b["input"]
                    for k in keys:                      # some models send a nested array as a JSON string
                        if isinstance(out.get(k), str):
                            out[k] = json.loads(out[k])
                    return out
            text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf8", "replace")[:500]
            last = f"HTTP {e.code}: {detail}"
            print(f"  Claude attempt {attempt+1} failed: {last}", file=sys.stderr)
            if e.code == 400:                     # the request itself was refused (free): adjust it, don't just repeat it
                if "tool_choice" in detail and body.get("tool_choice", {}).get("type") != "auto":
                    body["tool_choice"] = {"type": "auto"}
                elif body["max_tokens"] > 12000:
                    body["max_tokens"] = 12000
                elif "tools" in body:
                    body.pop("tools"); body.pop("tool_choice")
                    print("  retrying without structured output", file=sys.stderr)
                continue
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            last = e
            print(f"  Claude attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    if fatal:
        sys.exit(f"Claude call failed: {last}")
    return None


# ---------------------------------------------------------------- pass 1: screen everything cheaply
SCREEN_PROMPT = """You screen candidate stories for "Far-Fetched", a daily game where players guess whether a weird news headline is real and then deduce where in the world it happened.

Score EVERY candidate 0-10 for how good it would be in the game:
- 8-10: laugh-out-loud funny or so ridiculous it feels like the movie Idiocracy (absurd decisions, baffling rules, dumbfounding mix-ups, dumb criminals, bizarre thefts, getaway fails), with a real punchline, happening in one specific place, ideally with details a well-read person could use to deduce the region (wildlife, climate, customs, institutions, currency, units, local industry).
- 4-7: genuinely odd with a twist, but less funny or the place is harder to deduce.
- 0-3: boring or mild (a rare animal spotted, a nice rescue, a big vegetable, a quiet record), vague, could be anywhere with no twist, not a single news event (listicle, roundup, opinion, advert, quiz, horoscope), or an unsafe topic: death, serious injury, violent crime, abuse, sexual content, children in danger, war, partisan politics, disaster or tragedy.
Strange NON-violent crime is welcome. Candidates in any language are fine: judge the story itself.
The game wants stories from ALL over the world, so do not favour the US, UK or other English-speaking countries: a story from Brazil, Japan, Kenya or Poland is worth exactly as much as one from Ohio.

For each candidate also give the ISO two-letter code of the country where the story happened ("?" if unclear).
Reply with ONE JSON object only: {"s":[[id,score,"CC"],[id,score,"CC"],...]} covering every id.

Candidates:
"""


def screen(cands, model, top=SCREEN_TOP):
    """Score every candidate with a cheap model; return the best `top` (falls back to a random slice)."""
    scores = {}
    for start in range(0, len(cands), 120):
        chunk = [{"id": i, "title": c["title"], "summary": c["summary"][:220], "source": c["source"]}
                 for i, c in enumerate(cands) if start <= i < start + 120]
        res = call_claude(SCREEN_PROMPT + json.dumps(chunk, ensure_ascii=False), model, max_tokens=4000, fatal=False, keys=["s"])
        for pair in (res or {}).get("s", []):
            try:
                i, sc = int(pair[0]), float(pair[1])
                if 0 <= i < len(cands):
                    scores[i] = sc
                    cands[i]["cc"] = (str(pair[2]).upper()[:2] if len(pair) > 2 else "?") or "?"
            except Exception:
                pass
    if len(scores) < min(len(cands), top) // 2:
        print(f"  screening returned {len(scores)} scores; using a random slice instead", file=sys.stderr)
        return cands[:top]
    order = sorted(range(len(cands)), key=lambda i: scores.get(i, -1), reverse=True)
    # Spread the shortlist over the world: at most SCREEN_PER_COUNTRY stories from any one country.
    best, per = [], {}
    for i in order:
        cc = cands[i].get("cc", "?")
        if cc != "?" and per.get(cc, 0) >= SCREEN_PER_COUNTRY:
            continue
        per[cc] = per.get(cc, 0) + 1
        cands[i]["screen"] = scores.get(i, -1)
        best.append(cands[i])
        if len(best) == top:
            break
    if len(best) < top:                                   # not enough variety: top up with the best of the rest
        for i in order:
            if cands[i] not in best:
                cands[i]["screen"] = scores.get(i, -1)
                best.append(cands[i])
                if len(best) == top:
                    break
    spread = ", ".join(f"{k} {v}" for k, v in sorted(per.items(), key=lambda kv: -kv[1]))
    print(f"  screened {len(scores)} candidates; kept top {len(best)} (scores {best[0]['screen']:.0f} to {best[-1]['screen']:.0f}); countries: {spread}", file=sys.stderr)
    return best


# ---------------------------------------------------------------- article text
def gnews_decode(url):
    """Google News RSS links are redirects that need JavaScript; resolve them to the publisher's URL."""
    m = re.search(r"news\.google\.com/(?:rss/)?articles/([^?/]+)", url)
    if not m:
        return url
    art = m.group(1)
    page = http_get(f"https://news.google.com/rss/articles/{art}", timeout=12).decode("utf8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return url
    inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                                       "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], art, int(ts.group(1)), sg.group(1)])
    form = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
    req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=form,
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=12) as r:
        body = r.read().decode("utf8", "replace")
    for line in body.splitlines():
        if line.startswith('[["wrb.fr"'):
            try:
                inner_res = json.loads(json.loads(line)[0][2])
                if inner_res[0] == "garturlres" and str(inner_res[1]).startswith("http"):
                    return inner_res[1]
            except Exception:
                pass
    return url


def article_text(url, limit=1500):
    """Opening paragraphs of an article (JSON-LD articleBody, else <p> text). Empty string on failure."""
    try:
        if "news.google.com" in url:
            url = gnews_decode(url)
            if "news.google.com" in url:
                return ""
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; far-fetched-game/1.0)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            page = r.read(800_000).decode("utf8", "replace")
    except Exception:
        return ""
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        m = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', blob)
        if m:
            try:
                return clean(json.loads('"' + m.group(1) + '"'), limit)
            except Exception:
                pass
    paras = [clean(p, 800) for p in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S)]
    paras = [p for p in paras if len(p) > 60 and not re.search(r"cookie|subscribe|newsletter|sign up|all rights reserved|javascript", p, re.I)]
    return clean(" ".join(paras), limit)


def add_text(cands):
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(lambda c: article_text(c["link"]), cands))
    for c, t in zip(cands, texts):
        if t:
            c["text"] = t
    print(f"  fetched article text for {sum(1 for t in texts if t)}/{len(cands)}", file=sys.stderr)


# ---------------------------------------------------------------- pass 3: fact-check the rewrites
CHECK_PROMPT = """You fact-check headlines for "Far-Fetched", a game built on TRUE weird news. For each item compare the game's headlines with the source material (title, summary and article text; the source may be in another language).

ok=false if:
- "h" or "h2" states something the source does not support: a changed event, outcome, number, animal/object or who did what, or an invented detail;
- the place is wrong, or the source does not support it;
- "h" names or explicitly reveals the place ("{}" stands for the hidden place);
- the headline is confusing, contradicts itself, or only makes sense with context it does not give;
- the named place is not where the event actually happened (a company's headquarters, the court or capital that reported it, or just one of several places involved).
ALLOWED, not a reason to fail: rewording, and short added wording that is generically true of that place (local terms like "county sheriff" or "constabulary", local units or currency, climate, wildlife, customs).
Each item may also carry a "hint" that is shown to players after they call the story. It is NOT part of the story, so it need not appear in the source, but ok=false if the hint is false or doubtful for that place, is about the event instead of the place, or names the hidden place, its state/region/country or a nationality. A landmark the place is known for, a historical or geographic fact, a different nearby famous city and a film/show/book set there are all fine when true.
If the source material is too thin to judge but nothing contradicts the headline, ok=true.

Reply with ONE JSON object only: {"checks":[{"id":0,"ok":true,"why":"at most 10 words"}]}

Items:
"""


SHARPEN_PROMPT = """You edit hints for "Far-Fetched", a daily KNOWLEDGE game. Players read a TRUE weird-news headline with the place hidden as {}, call it real or fake, and then see a short HINT before they pin where it happened on a world map. They see ONLY the headline and the hint: the "clue" and "text" below are for you, not them.

For each story give a "hint": a short phrase that continues the headline after "in {}", shown to the player ONLY after they have called the story real or fake, to help them place it. It is a KNOWLEDGE clue, so the game rewards what a well-read person knows: a landmark the place is known for, a historical fact, its geography or surroundings (a nearby famous city, coast, mountains, lake, climate), a political or institutional fact, or a media reference (a film, show or book set there). Give one even when the source never mentions it, as long as it is TRUE of that place; it describes the PLACE, never the event. It must be a hint, not an answer: never write the hidden place, its state/region/country or a nationality, though a famous landmark or a different, nearby big city is fine. Good examples: "home of the Royal Pavilion", "near the theme parks", "a short train ride from Glasgow", "the planned capital beside a man-made lake", "a factory town east of the capital". Use "" if you are not sure of a true one.

If the story already has a "hint" that is true, short and fits those rules, keep it unchanged. If it is false, doubtful, names the place or the country, or is about the event, replace it. The headline itself stays as it is.

Then rate the headline PLUS hint as the player will have them when they pin:
- "clue_strength": 0-3. 3 = strongly points to a region; 2 = a fair hint a well-read general player could use; 1 = a weak hint; 0 = could be anywhere, or the hint is misleading.
- "jargon_only": true when the only help is a local term or acronym a general player would not know.
Be honest: a low rating is better than an invented hint.

Reply with ONE JSON object only: {"real":[{"id":0,"hint":"...","clue_strength":2,"jargon_only":false}]}

Stories:
"""


def hint_ok(hint, place):
    """A hint is shown after the call, so it must be short, must not name the hidden place or its country, and must not carry the {} token."""
    hint = (hint or "").strip()
    return bool(hint) and len(hint) <= 70 and "{}" not in hint and not leaks(hint, place)


def sharpen(stories, model, texts):
    """Give every Daily Dig story a true knowledge hint (shown after the real-or-fake call) and rate how well headline plus hint point to a region."""
    items = [{"id": i, "h": r.get("h", ""), "hint": r.get("hint", ""), "place": r.get("place", ""), "clue": r.get("clue", ""),
              "text": texts.get(r.get("src"), "")} for i, r in enumerate(stories)]
    res = call_claude(SHARPEN_PROMPT + json.dumps(items, ensure_ascii=False), model, max_tokens=3000, fatal=False, keys=["real"])
    if not res or "real" not in res:
        print("  hint pass unavailable; keeping the stories as they are", file=sys.stderr)
        return
    for x in res["real"]:
        try:
            r = stories[int(x["id"])]
        except Exception:
            continue
        hint = (x.get("hint") or "").strip()
        if hint and not hint_ok(hint, r.get("place", "")):
            print(f"  hint rejected (names the place, too long or malformed), dropped: {hint}", file=sys.stderr)
            hint = ""
        if hint != (r.get("hint") or ""):
            print(f"  hint: {r.get('h', '')[:60]}  ->  {hint or '(none)'}", file=sys.stderr)
        r["hint"] = hint
        try:
            cs = max(0, min(3, int(x.get("clue_strength"))))
        except Exception:
            cs = None
        if cs is not None:
            if hint and cs >= 2 and map_score(r) == 2:
                r["mappable"] = 3                      # one specific place, and the headline plus hint point to its region
            r["clue_strength"], r["jargon_only"] = cs, bool(x.get("jargon_only"))


def fact_check(reals, model):
    items = [{"id": i, "h": r.get("h", ""), "h2": r.get("h2", ""), "hint": r.get("hint", ""), "place": r.get("place", ""),
              "source": {"title": r.get("_title", ""), "summary": r.get("_summary", ""), "text": r.get("_text", "")}}
             for i, r in enumerate(reals)]
    res = call_claude(CHECK_PROMPT + json.dumps(items, ensure_ascii=False), model, max_tokens=4000, fatal=False, keys=["checks"])
    if not res or "checks" not in res:
        print("  fact-check unavailable; keeping all picks", file=sys.stderr)
        return reals
    bad = {}
    for ch in res["checks"]:
        try:
            if ch.get("ok") is False:
                bad[int(ch["id"])] = ch.get("why", "")
        except Exception:
            pass
    for i, why in bad.items():
        if 0 <= i < len(reals):
            print(f"  drop real (fact-check: {why}): {reals[i].get('h')}", file=sys.stderr)
    return [r for i, r in enumerate(reals) if i not in bad]


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
    if r.get("hint") and not hint_ok(r["hint"], r.get("place", "")):
        r["hint"] = ""
    if h.count("{}") != 1 or len(h) > 130:
        return "bad h"
    if leaks(h.replace("{}", ""), r.get("place", "")):
        return "place leaks into h"
    try:
        float(r["lat"]); float(r["lng"])
    except Exception:
        return "bad coordinates"
    return None


ANGLO = {"US", "GB", "CA", "AU", "NZ", "IE"}
_CC_NAMES = {"united states": "US", "usa": "US", "united kingdom": "GB", "uk": "GB", "england": "GB", "wales": "GB",
             "scotland": "GB", "northern ireland": "GB", "canada": "CA", "australia": "AU", "new zealand": "NZ",
             "ireland": "IE", "india": "IN", "japan": "JP", "germany": "DE", "france": "FR", "brazil": "BR",
             "south africa": "ZA", "philippines": "PH", "singapore": "SG", "nigeria": "NG", "kenya": "KE"}


def ccode(r):
    """ISO code for a story's country; England, Wales, Scotland and Northern Ireland all count as GB."""
    cc = str(r.get("cc") or "").upper().strip()
    if cc == "UK":
        cc = "GB"
    if len(cc) != 2 or not cc.isalpha():
        cc = _CC_NAMES.get(str(r.get("country") or "").lower().strip(), "?")
    return cc


def cos(a, b):
    na = math.sqrt(sum(x * x for x in a)) or 1
    nb = math.sqrt(sum(x * x for x in b)) or 1
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def valid_fake(f):
    """Fakes must not lean on the tells players learn fastest: "exactly", round numbers, "to the second"."""
    h = f.get("h", "")
    if h.count("{}") != 1 or len(h) > 130:
        return False
    if re.search(r"\bexactly\b|to the second|on the dot", h, re.I):
        return False
    for n in re.findall(r"(?<![$£€¥₹\d,])\b\d[\d,]*\b(?!\s*(?:dollars|pounds|euros|yen|rupees|pesos|rand))", h):   # prices are naturally round
        try:
            v = int(n.replace(",", ""))
        except ValueError:
            continue
        if v >= 100 and v % 100 == 0 and not 1900 <= v <= 2100:
            return False
    return True


_STOP = {"with", "from", "after", "into", "their", "that", "this", "have", "were", "been", "over", "when", "about",
         "than", "they", "them", "what", "while", "says", "said", "just", "only", "more", "most", "some"}


def words(text):
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def recent_headlines(day, days=45, limit=600):
    """Plain list of the headlines shown recently (newest first), so the editor can avoid retelling them."""
    out, lo = [], (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()
    for fn in sorted(os.listdir(DATA), reverse=True):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", fn)
        if not m or m.group(1) < lo:
            continue
        try:
            d = json.load(open(os.path.join(DATA, fn)))
        except Exception:
            continue
        for x in (d.get("call") or []) + (d.get("blank") or []) + (d.get("politics") or []):
            if x.get("real"):
                t = (x.get("h") or (x.get("h2") or "").replace("[]", (x.get("ans") or ["…"])[0])).replace("{}", x.get("place") or "?")
                out.append(t[:140] + (" (" + x["place"] + ")" if x.get("place") and x.get("h2") and not x.get("h") else ""))
    return out[:limit]


def prior_stories(day, days=120):
    """(city, words) for every real story shown on earlier days, read from the saved day files,
    so the same story can't come back through a different outlet's link."""
    out = []
    lo = (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()
    for fn in os.listdir(DATA):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", fn)
        if not m or m.group(1) < lo:          # today's own earlier set counts too: players may have played it
            continue
        try:
            d = json.load(open(os.path.join(DATA, fn)))
        except Exception:
            continue
        for x in (d.get("call") or []) + (d.get("blank") or []) + (d.get("politics") or []):
            if x.get("real"):
                out.append(((x.get("place") or "").split(",")[0].strip().lower(),
                            words((x.get("h") or "") + " " + (x.get("h2") or "").replace("[]", " ".join(x.get("ans") or [])))))
    try:                                           # stories kept for Practice count too, so a daily never retells one
        for x in json.load(open(os.path.join(DATA, "practice.json"))).get("real", []):
            out.append(((x.get("place") or "").split(",")[0].strip().lower(),
                        words((x.get("h") or "") + " " + (x.get("h2") or "").replace("[]", " ".join(x.get("ans") or [])))))
    except Exception:
        pass
    return out


def seen_before(r, prior):
    city = (r.get("place") or "").split(",")[0].strip().lower()
    w = words((r.get("h") or "") + " " + (r.get("h2") or "").replace("[]", " ".join(r.get("ans") or [])))
    if not w:
        return False
    for pc, pw in prior:
        if not pw:
            continue
        j = len(w & pw) / len(w | pw)
        if j >= 0.45 or (city and city == pc and j >= 0.15):
            return True
    return False


def proper_noun(word, text):
    """True if `word` only ever appears capitalised mid-sentence in `text` (a name or brand, e.g. Maserati)."""
    hits = [m for m in re.finditer(r"\b" + re.escape(word) + r"\b", text or "", re.I)]
    if not hits:
        return False
    mid = [m for m in hits if m.start() > 0 and not re.search(r"[.!?:]\s*$", text[:m.start()])]
    return bool(mid) and all(m.group(0)[0].isupper() for m in mid)


def opener(h):
    w = [x for x in re.findall(r"[a-z']+", (h or "").lower())][:2]
    return " ".join(w)


QUOTE_TELL = re.compile(r"(spokes|official|mayor|council|police|sheriff|says|said|told)[^.]{0,40}[:,;]\s*['\"‘“]", re.I)


def recent_fake_openers(day, days=14):
    out, lo = set(), (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()
    for fn in os.listdir(DATA):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", fn)
        if not m or m.group(1) < lo or m.group(1) == day:
            continue
        try:
            d = json.load(open(os.path.join(DATA, fn)))
        except Exception:
            continue
        for x in (d.get("call") or []) + (d.get("politics") or []):
            if not x.get("real"):
                out.add(opener(x.get("h")))
    return out


def pick_fakes(fakes, reals, day, n, rank):
    """Choose n fakes without the tells regulars learn: longer than the real headlines, a quoted official,
    an opening players have seen recently, or a topic that clashes with another story today."""
    max_len = max([len(r.get("h", "")) for r in reals] + [60])
    real_open = {opener(r.get("h")) for r in reals}
    real_theme = {(r.get("theme") or "").lower() for r in reals} - {""}
    recent = recent_fake_openers(day)
    ordered = sorted(fakes, key=rank)
    for strict in (3, 2, 1, 0):          # loosen the softer rules only if there aren't enough fakes
        chosen, opens, themes = [], set(), set()
        for f in ordered:
            h, op, th = f.get("h", ""), opener(f.get("h")), (f.get("theme") or "").lower()
            if len(h) > max(max_len, 70) or QUOTE_TELL.search(h) or op == "council in":
                continue
            if op in opens or (strict >= 1 and op in real_open) or (strict >= 3 and op in recent):
                continue
            if th and (th in themes or (strict >= 2 and th in real_theme)):
                continue
            chosen.append(f); opens.add(op); themes.add(th)
            if len(chosen) == n:
                return chosen
    return chosen


# A clue only counts when general knowledge can use it: jargon-only hints (like "cold-drop DANA") are downgraded.
JARGON = re.compile(r"\b(dana|gota fr[ií]a)\b", re.I)


def eff_strength(r):
    try:
        cs = int(r.get("clue_strength", 0) or 0)
    except Exception:
        cs = 0
    if r.get("jargon_only") or JARGON.search(r.get("h") or "") or JARGON.search(r.get("clue") or ""):
        cs = min(cs, 1)
    return cs


def map_score(r):
    """The editor's 0-3 rating of how sensibly the story can be pinned (2 when an older banked story has none)."""
    try:
        return int(r.get("mappable", 2))
    except Exception:
        return 2


def merit(r):
    try:
        return 2 * eff_strength(r) + 2 * map_score(r) + int(r.get("unbelievable", 1)) + r.get("_fresh", 0)
    except Exception:
        return 1


def fit_to_map(r):
    """Good enough to be a Real or Fake / Practice story: it can be pinned and carries at least a thin regional clue."""
    return map_score(r) >= 2 and eff_strength(r) >= 1


STOCK_KEEP = 120   # days a story sourced by make_stock.py stays eligible


def load_stock(day, used, prior):
    """Extra Politics and Fill the Blank stories sourced ahead of time by scripts/make_stock.py (data/stock.json).
    They feed those two modes, and top up the Daily Dig only on a day too thin to fill it otherwise. Returns (the whole file, the usable stories)."""
    try:
        st = json.load(open(os.path.join(DATA, "stock.json")))
    except Exception:
        return {}, {"pol": [], "polfake": [], "blank": []}
    keep_from = (dt.date.fromisoformat(day) - dt.timedelta(days=STOCK_KEEP)).isoformat()
    ok = lambda r: r.get("banked", "") >= keep_from and r.get("src") not in used and not valid_real(r) and not seen_before(r, prior)
    return st, {"pol": [r for r in st.get("pol", []) if ok(r)],
                "polfake": [f for f in st.get("polfake", []) if f.get("banked", "") >= keep_from and valid_fake(f)],
                "blank": [r for r in st.get("blank", []) if ok(r) and r.get("h2")]}


def valid_blank(r):
    h2, ans = r.get("h2", ""), r.get("ans") or []
    if h2.count("[]") != 1 or not ans or not all(re.fullmatch(r"[a-z]+", a) for a in ans):
        return False
    if proper_noun(ans[0], r.get("h", "")) or proper_noun(ans[0], r.get("_title", "")):
        print(f"  drop blank (answer '{ans[0]}' is a name or brand): {h2}", file=sys.stderr)
        return False
    low = h2.lower()
    if re.search(r"\b" + ans[0] + r"s?\b", low):        # the hidden word itself can't appear elsewhere in the headline
        return False
    # synonyms that happen to appear elsewhere in the headline are simply dropped from the accepted list
    r["ans"] = [ans[0]] + [a for a in dict.fromkeys(ans[1:]) if a != ans[0] and not re.search(r"\b" + a + r"\b", low)][:15]
    return True


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date to build (default today)")
    ap.add_argument("--picks", help="use this JSON (Claude-format output plus candidate fields) instead of calling Claude")
    ap.add_argument("--no-geocode", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    # Editions are US Eastern days and come out at midnight Eastern. The evening run (about 9 pm Eastern)
    # prints tomorrow's paper ahead of time; a run earlier in the day rebuilds today's.
    try:
        from zoneinfo import ZoneInfo
        et_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et_now = now - dt.timedelta(hours=5)
    day = args.date or (et_now + dt.timedelta(hours=4)).date().isoformat()
    os.makedirs(DATA, exist_ok=True)
    hist_path = os.path.join(DATA, "history.json")
    history = json.load(open(hist_path)) if os.path.exists(hist_path) else {}
    # links from earlier days are "used"; today's own entry doesn't count, so a re-run rebuilds today's set
    used = {link for d, links in history.items() if d != day for link in links}

    model = os.environ.get("FF_MODEL", "claude-sonnet-5-5")
    picks, cands = {}, []
    if args.picks:
        picks = json.load(open(args.picks))
        cands = picks.get("candidates", [])
    else:
        print("Fetching feeds…", file=sys.stderr)
        cands = gather(now, used)
        if len(cands) < 40:                      # thin stretch: widen the window before giving up
            print(f"  only {len(cands)} candidates; widening to 60 days", file=sys.stderr)
            cands = gather(now, used, max_age=60)
        print(f"{len(cands)} fresh candidates", file=sys.stderr)
        if len(cands) >= 8:
            random.Random(day).shuffle(cands)    # tie-break order only; screening decides what goes on
            cands = screen(cands, os.environ.get("FF_SCREEN_MODEL", "claude-haiku-5-5"))
            add_text(cands)
            try:
                picks = ask_claude(cands, model, avoid=recent_headlines(day)) or {}
            except SystemExit as e:
                print(f"  editor call failed ({e}); relying on the bank", file=sys.stderr)
                picks = {}
        else:
            print("  too few fresh stories to ask the editor; relying on the bank", file=sys.stderr)

    fresh, src_text = [], {}
    for r in picks.get("real", []):
        c = cands[r["id"]] if isinstance(r.get("id"), int) and r["id"] < len(cands) else r
        r["src"] = r.get("src") or c.get("link", "")
        r["published"] = r.get("published") or c.get("published", "")
        r["_title"], r["_summary"], r["_text"] = c.get("title", ""), c.get("summary", ""), c.get("text", "")
        if not r.get("cc") and c.get("cc") and c.get("cc") != "?":
            r["cc"] = c["cc"]
        why = valid_real(r)
        if why:
            print(f"  drop real ({why}): {r.get('h')}", file=sys.stderr)
            continue
        fresh.append(r)
    if fresh and not args.picks:
        fresh = fact_check(fresh, model)
    for r in fresh:
        if not args.no_geocode and r.get("geocode"):
            g = geocode(r["geocode"])
            if g and km(g[0], g[1], float(r["lat"]), float(r["lng"])) < 400:
                r["lat"], r["lng"] = g
            elif g:
                print(f"  geocoder disagrees for {r['place']} (likely a same-name town elsewhere); keeping the editor's coordinates", file=sys.stderr)
        r["banked"], r["rules"] = day, RULES
        src_text[r["src"]] = (r.get("_text") or r.get("_summary") or "")[:1500]   # kept for the headline sharpening pass
        for k in ("_title", "_summary", "_text"):
            r.pop(k, None)
    fresh_fakes = []
    for f in picks.get("fake", []):
        if valid_fake(f):
            fresh_fakes.append(f)
        else:
            print(f"  drop fake (tell too obvious): {f.get('h')}", file=sys.stderr)
    for f in fresh_fakes:
        f["banked"], f["rules"] = day, RULES

    # The bank: good stories (and fakes) from earlier runs that were never used.
    bank_path = os.path.join(DATA, "bank.json")
    bank = json.load(open(bank_path)) if os.path.exists(bank_path) else {}
    oldest = (dt.date.fromisoformat(day) - dt.timedelta(days=BANK_DAYS)).isoformat()
    fresh_src = {r["src"] for r in fresh}
    bank_real = [r for r in bank.get("real", []) if r.get("rules", 1) >= RULES and r.get("banked", "") >= oldest and r.get("banked") != day
                 and r.get("src") not in used and r.get("src") not in fresh_src and not valid_real(r)]
    keep_from = (dt.date.fromisoformat(day) - dt.timedelta(days=BANK_KEEP)).isoformat()
    bank_old = [r for r in bank.get("real", []) if r.get("rules", 1) >= RULES and keep_from <= r.get("banked", "") < oldest
                and r.get("src") not in used and r.get("src") not in fresh_src and r.get("h2")]
    bank_fake = [f for f in bank.get("fake", []) if f.get("rules", 1) >= RULES and f.get("banked", "") >= oldest and f.get("banked") != day and valid_fake(f)]
    prior = prior_stories(day)
    for lst, name in ((fresh, "fresh"), (bank_real, "banked"), (bank_old, "older banked")):
        dup = [r for r in lst if seen_before(r, prior)]
        for r in dup:
            print(f"  drop real (already shown on an earlier day): {r.get('h')}", file=sys.stderr)
            lst.remove(r)
    stock_all, stock = load_stock(day, used, prior)
    print(f"  stock: {len(stock['pol'])} politics, {len(stock['polfake'])} politics fakes, {len(stock['blank'])} Fill the Blank", file=sys.stderr)
    for r in fresh:
        r["_fresh"] = 1
    reals = fresh + bank_real
    fakes = fresh_fakes + bank_fake
    print(f"  {len(fresh)} fresh real stories + {len(bank_real)} from the bank; {len(fresh_fakes)} fresh fakes + {len(bank_fake)} banked", file=sys.stderr)
    # A thin day must still print a full Daily Dig: top up with older banked stories, then recent stock stories, mappable ones only
    if len([r for r in reals if fit_to_map(r)]) < N_CALL_REAL + 3:
        extra = sorted([r for r in bank_old + stock["blank"] if r not in reals and fit_to_map(r) and not valid_real(r)], key=merit, reverse=True)
        if extra:
            print(f"  thin day: {len(extra)} mappable stories available from the older bank and the stock to top up", file=sys.stderr)
        reals = reals + extra
    if len(fakes) < N_CALL_FAKE:
        fakes = fakes + [f for f in bank.get("fake", []) if f.get("rules", 1) >= RULES and f.get("banked") != day and valid_fake(f) and f not in fakes]
    if len(reals) < N_CALL_REAL or len(fakes) < N_CALL_FAKE:
        # fail loudly: the run goes red (GitHub emails the owner) and the next scheduled attempt tries again
        print(f"ERROR: only {len(reals)} usable real / {len(fakes)} fake headlines even with the bank and stock; no new paper printed.", file=sys.stderr)
        sys.exit(1)

    # Real or Fake wants stories whose place can be DEDUCED, and a spread of countries and themes.
    # Daily Dig only takes stories a player can sensibly pin: when the pool is deep enough, anything the editor rates as hard to map
    # (or with no usable regional clue at all) stays out; a thin day relaxes this rather than printing a short round.
    # Before choosing: give great stories with a weak headline clue a chance, by rewriting them with a clue from the article
    if not args.picks:
        weak = [r for r in reals if eff_strength(r) < 2 and map_score(r) >= 2]
        weak = sorted(weak, key=lambda r: (merit(r) - 2 * eff_strength(r), r.get("_fresh", 0)), reverse=True)[:15]   # the most surprising first, clue aside
        if weak:
            before = sum(1 for r in reals if eff_strength(r) >= 2 and map_score(r) >= 2)
            sharpen(weak, model, src_text)
            after = sum(1 for r in reals if eff_strength(r) >= 2 and map_score(r) >= 2)
            print(f"  sharpening pass: {len(weak)} weak-clue stories reviewed; strong, pinnable stories {before} -> {after}", file=sys.stderr)
    fit = [r for r in reals if fit_to_map(r)]
    dropped_unfit = [r for r in reals if r not in fit]   # not good enough for today's map, but still worth keeping in the bank
    if len(fit) >= N_CALL_REAL + 3:
        print(f"  {len(reals) - len(fit)} stories left out of Real or Fake: hard to pin or no regional clue", file=sys.stderr)
        reals = fit
    else:
        print(f"  only {len(fit)} easily mapped stories; Real or Fake keeps the weaker ones as fillers", file=sys.stderr)
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
    # Spread over the world: at most 2 per country and at most half from the English-speaking six.
    # The pool already includes the bank, so banked stories fill gaps before any limit is loosened.
    def pick_call(cap, anglo_cap):
        out, per, ang, cities = [], {}, 0, set()
        for r in reals:
            cc = ccode(r)
            city = (r.get("place") or "").split(",")[0].strip().lower()
            if city and city in cities:      # one story per city, always
                continue
            if cc != "?" and per.get(cc, 0) >= cap:
                continue
            if cc in ANGLO and ang >= anglo_cap:
                continue
            per[cc] = per.get(cc, 0) + 1
            ang += cc in ANGLO
            cities.add(city)
            out.append(r)
            if len(out) == N_CALL_REAL:
                break
        return out, per
    reals = sorted(reals, key=lambda r: not r.get("priority"))   # stories hand-picked into the bank ("priority") go first; the country spread still applies
    for cap, anglo_cap in ((1, N_CALL_REAL // 2), (1, N_CALL_REAL), (2, N_CALL_REAL // 2), (2, N_CALL_REAL), (3, N_CALL_REAL), (N_CALL_REAL, N_CALL_REAL)):
        call_pick, per_country = pick_call(cap, anglo_cap)
        if len(call_pick) == N_CALL_REAL:
            break
        print(f"  only {len(call_pick)} stories fit the country spread (max {cap}/country, {anglo_cap} English-speaking); loosening", file=sys.stderr)
    # at least 4 of the 5 true stories need a fair regional clue and a sensible pin: swap weak picks for stronger ones from the pool
    MIN_STRONG = N_CALL_REAL - 1
    strong_n = lambda lst: sum(1 for r in lst if eff_strength(r) >= 2 and map_score(r) >= 2)
    if len(call_pick) == N_CALL_REAL and strong_n(call_pick) < MIN_STRONG:
        weak = sorted([r for r in call_pick if eff_strength(r) < 2 or map_score(r) < 2], key=merit)
        for cand in [r for r in reals if r not in call_pick and eff_strength(r) >= 2 and map_score(r) >= 2]:
            if strong_n(call_pick) >= MIN_STRONG or not weak:
                break
            cc = ccode(cand)
            if cc != "?" and sum(1 for r in call_pick if ccode(r) == cc) >= 3:
                continue
            call_pick[call_pick.index(weak.pop(0))] = cand
        per_country = {}
        for r in call_pick:
            per_country[ccode(r)] = per_country.get(ccode(r), 0) + 1
        if strong_n(call_pick) < MIN_STRONG:
            print(f"  WARNING: only {strong_n(call_pick)} of {len(call_pick)} true stories have a fair regional clue and a sensible pin (wanted {MIN_STRONG})", file=sys.stderr)
    if not args.picks:
        sharpen(call_pick, model, src_text)
    strong = strong_n(call_pick)
    print(f"  Real or Fake: {strong}/{len(call_pick)} stories with a usable regional clue; countries {per_country}", file=sys.stderr)

    # Fill the Blank: real stories whose answer has a word vector
    # Fill the Blank isn't tied to the news cycle: older banked stories (up to a year) are fair game too.
    blank_src = sorted(reals + stock["blank"], key=merit, reverse=True) + sorted(bank_old, key=merit, reverse=True)
    blank_src = [r for i, r in enumerate(blank_src) if not any(r is q for q in blank_src[:i])]   # a topped-up story can sit in two lists
    h2words = {w for r in blank_src for w in re.findall(r"[a-z]+", (r.get("h2") or "").lower())}
    vecs = load_vectors(os.environ.get("GLOVE"), [a for r in blank_src for a in (r.get("ans") or [])] + sorted(h2words))
    blank_pool, used_ans = [], []
    for r in blank_src:
        if r in call_pick or not valid_blank(r):
            continue
        a = next((a for a in r["ans"] if a in vecs), None)
        if a is None and vecs:
            continue
        if a:
            words = set(re.findall(r"[a-z]+", r["h2"].lower())) - {"the", "a", "an", "of", "in", "on", "for", "to", "and"}
            leak = next((w for w in words if w in vecs and w != a and cos(vecs[w], vecs[a]) > 0.72), None)
            if leak:                                   # "six of eight [] fail: 'only two potties'" gives the answer away
                print(f"  drop blank (h2 says '{leak}', too close to '{a}'): {r['h2']}", file=sys.stderr)
                continue
            if any(a == u or (u in vecs and cos(vecs[a], vecs[u]) > 0.7) for u in used_ans):
                continue                               # one burglar per day, not burglar, burglar and thief
            used_ans.append(a)
        # keep the hidden word first (it's what the reveal shows); the vector just needs some accepted word
        if a:
            r["vec"] = pack_vec(vecs[a])
        blank_pool.append(r)

    rng = random.Random("far-fetched-" + day)
    # Fill the Blank only takes what Real or Fake doesn't use, so a story never appears in two modes
    # (on a thin day the round is a little shorter instead).
    call_real = call_pick
    blank = [r for r in blank_pool if r not in call_real][:N_BLANK]
    if len(blank) < N_BLANK:
        print(f"  Fill the Blank has {len(blank)} stories today (no sharing with Real or Fake)", file=sys.stderr)

    def real_call(r):
        return {"real": True, "h": r["h"], "lat": round(float(r["lat"]), 4), "lng": round(float(r["lng"]), 4),
                "place": r["place"], "src": r["src"], "published": r["published"], "clue": (r.get("clue") or "")[:80], "hint": (r.get("hint") or "")[:70]}

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
    chosen_fakes = pick_fakes(fakes, call_real, day, N_CALL_FAKE, lambda f: fake_rank(f) + (0 if f.get("banked") == day else 0.3))
    if len(chosen_fakes) < N_CALL_FAKE:
        print(f"  only {len(chosen_fakes)} fakes pass the tell checks; keeping yesterday's set.", file=sys.stderr)
        sys.exit(0)
    fakes = chosen_fakes + [f for f in fakes if f not in chosen_fakes]       # the rest go back to the bank
    call = [real_call(r) for r in call_real] + [{"real": False, "h": f["h"], "tell": (f.get("tell") or "")[:160]} for f in fakes[:N_CALL_FAKE]]
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
            if len(pc) < 6:
                print("  politics: too few candidates; using the politics bank", file=sys.stderr)
            else:
                pc = pc[:60]
                pol = ask_claude(pc, os.environ.get("FF_MODEL", "claude-sonnet-5-5"), POL_PROMPT, avoid=recent_headlines(day))
                pol["candidates"] = pc
        except SystemExit as e:
            print(f"  politics skipped: {e}", file=sys.stderr)
    pol = pol or {}
    bank_pol = [r for r in bank.get("pol", []) if r.get("banked", "") >= oldest and r.get("banked") != day
                and r.get("src") not in used and not valid_real(r) and not seen_before(r, prior)]
    bank_polfake = [f for f in bank.get("polfake", []) if f.get("banked", "") >= oldest and f.get("banked") != day and valid_fake(f)]
    keep_pol, keep_polfake = bank_pol, bank_polfake
    if True:
        pcands = pol.get("candidates", [])
        preal = []
        for r in pol.get("real", []):
            c = pcands[r["id"]] if isinstance(r.get("id"), int) and r["id"] < len(pcands) else r
            r["src"] = r.get("src") or c.get("link", "")
            r["published"] = r.get("published") or c.get("published", "")
            why = valid_real(r)
            if not why and (r.get("place") or "").split(",")[0].strip().lower() in COUNTRY_NAMES:
                why = "country-level place"
            if why:
                print(f"  drop politics ({why}): {r.get('h')}", file=sys.stderr)
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
        pfake = [dict(f, banked=day) for f in pol.get("fake", []) if valid_fake(f)] + stock["polfake"] + bank_polfake
        pchosen = pick_fakes(pfake, [r for r in pol.get("real", []) if r.get("h")], day, N_POL_FAKE, lambda f: 0 if f.get("banked") == day else 1)
        pfake = pchosen + [f for f in pfake if f not in pchosen]
        for r in preal:
            r["banked"] = day
        seen_place, uniq_pol = set(), []
        for r in preal + stock["pol"] + bank_pol:                     # fresh first, then banked politics stories                                # one story per place: no two takes on the same dog-mayor race
            key = (r.get("place") or "").split(",")[0].strip().lower()
            if key and key in seen_place:
                continue
            seen_place.add(key)
            uniq_pol.append(r)
        pool_pol = [r for r in uniq_pol if not seen_before(r, prior)]
        preal = pool_pol[:N_POL_REAL]
        keep_pol, keep_polfake = pool_pol[N_POL_REAL:], pfake[N_POL_FAKE:]
        print(f"  politics: {len(preal)} real (from {len(pol.get('real', []))} picked + {len(bank_pol)} banked), {len(pfake)} fakes", file=sys.stderr)
        if len(preal) < 4 or len(pfake) < N_POL_FAKE:
            print("  politics: not enough for a round today; Politics mode falls back", file=sys.stderr)
            keep_pol, keep_polfake = pool_pol, pfake
        else:
            items = [dict(real_call(r), cat="pol") for r in preal] + \
                    [{"real": False, "h": f["h"], "cat": "pol"} for f in pfake[:N_POL_FAKE]]
            rng.shuffle(items)
            result["politics"] = items
            call_real = call_real + preal
    pol_src = {r["src"] for r in preal} if result.get("politics") else set()
    stock_pol_src, stock_pf = {r.get("src") for r in stock["pol"]}, {f.get("h") for f in stock["polfake"]}
    keep_pol = [r for r in keep_pol if r.get("src") not in stock_pol_src]        # stock stories stay in data/stock.json, not the bank
    keep_polfake = [f for f in keep_polfake if f.get("h") not in stock_pf]

    for name in (f"{day}.json", "latest.json"):
        with open(os.path.join(DATA, name), "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)

    used_today = {r["src"] for r in call_real + blank}
    if stock_all:       # stories from data/stock.json that ran today leave it; very old ones expire
        keep_from = (dt.date.fromisoformat(day) - dt.timedelta(days=STOCK_KEEP)).isoformat()
        used_pol = {f["h"] for f in pfake[:N_POL_FAKE]} if result.get("politics") else set()
        stock_all["pol"] = [r for r in stock_all.get("pol", []) if r.get("src") not in used_today and r.get("banked", "") >= keep_from]
        stock_all["blank"] = [r for r in stock_all.get("blank", []) if r.get("src") not in used_today and r.get("banked", "") >= keep_from]
        stock_all["polfake"] = [f for f in stock_all.get("polfake", []) if f.get("h") not in used_pol and f.get("banked", "") >= keep_from]
        with open(os.path.join(DATA, "stock.json"), "w") as f:
            json.dump(stock_all, f, ensure_ascii=False, indent=1)
    history[day] = sorted(used_today)
    keep_real, seen_src = [], set()
    for r in sorted(reals + dropped_unfit + bank_old, key=lambda r: r.get("banked", ""), reverse=True):
        if r["src"] in used_today or r["src"] in seen_src:
            continue
        seen_src.add(r["src"])
        keep_real.append({k: v for k, v in r.items() if k not in ("_fresh", "vec")})
    used_fake = {f["h"] for f in fakes[:N_CALL_FAKE]}
    keep_fake = [f for f in fakes if f["h"] not in used_fake]
    with open(bank_path, "w") as f:
        json.dump({"real": keep_real[:BANK_MAX], "fake": keep_fake[:30],
                   "pol": [r for r in keep_pol if r.get("src") not in pol_src][:60], "polfake": keep_polfake[:20]},
                  f, ensure_ascii=False, indent=1)
    print(f"  bank now holds {min(len(keep_real), BANK_MAX)} stories and {min(len(keep_fake), 30)} fakes", file=sys.stderr)

    # Practice archive: every story the daily modes have shown, from any day, so Practice has a deep pool.
    arch_path = os.path.join(DATA, "practice.json")
    arch = json.load(open(arch_path)) if os.path.exists(arch_path) else {"real": [], "fake": []}
    have = {x.get("src") or x.get("h") or x.get("h2") for x in arch["real"]} | {x.get("h") for x in arch["fake"]}
    pol_src = {r.get("src") for r in result.get("politics", []) if r.get("real")}

    def add_real(r, d, is_pol=False):
        key = r.get("src") or r.get("h")
        if not key or key in have:
            return
        have.add(key)
        x = {"real": True, "day": d}
        for k in ("h", "h2", "ans", "vec", "place", "src", "published", "clue", "hint"):
            if r.get(k):
                x[k] = r[k]
        try:
            x["lat"], x["lng"] = round(float(r["lat"]), 4), round(float(r["lng"]), 4)
        except Exception:
            x.pop("h", None)                           # no coordinates: Fill the Blank only
        if is_pol or r.get("src") in pol_src or r.get("cat") == "pol":
            x["cat"] = "pol"
        if x.get("h") or (x.get("h2") and x.get("ans")):
            arch["real"].append(x)

    def add_fake(f, d):
        if f.get("h") and f["h"] not in have:
            have.add(f["h"])
            arch["fake"].append(dict(f, day=d))

    # Back-fill from every earlier edition on file, stamped with that edition's own day (never today's).
    ed_day = {}
    for fn in sorted(os.listdir(DATA)):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", fn)
        if not m or m.group(1) >= day:
            continue
        try:
            d = json.load(open(os.path.join(DATA, fn)))
        except Exception:
            continue
        for key, pol in (("call", False), ("blank", False), ("politics", True)):
            for r in d.get(key) or []:
                if r.get("src"):
                    ed_day.setdefault(r["src"], m.group(1))
                if r.get("real"):
                    add_real(r, m.group(1), pol)
                elif key != "blank":
                    add_fake(r, m.group(1))
    # Entries wrongly stamped with today's date (or later) that were not in today's edition can be practised: restamp them.
    todays = {r.get("src") for r in call_real + blank if r.get("src")} | {r.get("src") for r in result.get("politics", []) if r.get("src")}
    todays_fake = {c.get("h") for c in call + result.get("politics", []) if not c.get("real")}
    yesterday = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    for x in arch["real"]:
        if x.get("day", "") >= day and x.get("src") not in todays:
            x["day"] = ed_day.get(x.get("src"), yesterday)
    for x in arch["fake"]:
        if x.get("day", "") >= day and x.get("h") not in todays_fake:
            x["day"] = yesterday

    for r in call_real + blank:
        add_real(r, day)
    for r in result.get("politics", []):
        if r.get("real"):
            add_real(r, day, True)
    for f in [c for c in call if not c["real"]] + [c for c in result.get("politics", []) if not c["real"]]:
        add_fake(f, day)
    arch["real"], arch["fake"] = arch["real"][-3000:], arch["fake"][-800:]
    with open(arch_path, "w") as f:
        json.dump(arch, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  practice archive: {len(arch['real'])} real, {len(arch['fake'])} fake", file=sys.stderr)

    # Back issues: an index of every edition on file, newest first
    issues = []
    for fn in sorted(os.listdir(DATA), reverse=True):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", fn)
        if not m:
            continue
        try:
            d = json.load(open(os.path.join(DATA, fn)))
        except Exception:
            continue
        issues.append({"date": m.group(1), "call": len(d.get("call") or []), "blank": len(d.get("blank") or []),
                       "politics": len(d.get("politics") or [])})
    with open(os.path.join(DATA, "issues.json"), "w") as f:
        json.dump(issues, f, separators=(",", ":"))
    cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=120)).isoformat()
    history = {k: v for k, v in history.items() if k >= cutoff}
    with open(hist_path, "w") as f:
        json.dump(history, f, indent=1)
    print(f"  estimated Claude cost this run: ${SPENT[0]:.2f}", file=sys.stderr)
    print(f"Wrote data/{day}.json: {len(call)} Real-or-Fake, {len(result['blank'])} Fill-the-Blank", file=sys.stderr)


if __name__ == "__main__":
    main()
