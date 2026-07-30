# Deep dive — Rank & Rent SEO (The Edward Show E1117, Ippei Kanehara)

- **Video**: [Rank & Rent SEO: How Ippei Built 130 Sites - and Made $240K From One 15-Hour Website](https://www.youtube.com/watch?v=muwrS4Dh_R8)
- **Channel**: Edward Sturm — [@buildinpublic](https://www.youtube.com/@buildinpublic) (channel ID `UCDxhBSgPvrFvyXLdpmygDhg`), "The Edward Show" daily SEO podcast
- **Published**: 2026-07-26 · ~9.9K views at capture · length ~1:31:00 · 32 chapters
- **Guest**: Ippei Kanehara ([ippei.com](https://ippei.com/)) — 11+ years building rank-and-rent local lead-gen sites; built 130 new sites in the last year to test what currently works; one early ~15-hour site has grossed $240K+ lifetime and still pays monthly.
- **Captured**: 2026-07-30 (metadata via Innertube; transcript 0:00–30:00 via TurboScribe PDF upload).

## Provenance and confidence

| Layer | Source | Confidence |
|---|---|---|
| Metadata, full description, 32 chapters, channel catalog | YouTube Innertube API — verbatim in `source/` | Verbatim |
| **0:00 → ~28:22** | TurboScribe transcript (`source/transcript-0000-3000.txt`) | **Verbatim** |
| **~57:59 → 1:31:00** (GBP boost, video verification, lead-value math, contracts, mistakes, rollup, why-fail) | Ryan's raw auto-transcript paste (`source/transcript-remainder-paste.txt`) | **Verbatim** |
| **~28:22 → ~57:59** | Chapter titles + description topic list, cross-checked against ippei.com published guides | Outline only — **[transcript-needed]** |

**Transcript status**: only the middle block still lacks spoken detail — must-have pages
tail (28:22), internal linking (31:36), doorway avoidance (36:31), AI content workflow
(39:44), keyword research + GSC + page splits (43:34–48:31), **first-30-days blueprint
(51:09)**, site structure strategy (54:38), image sourcing (56:35), and the reviews/
conversions segment (1:05:52).
(Transcription note: "Ipe/eBay/eay/epay.com" in the raw transcripts = Ippei / ippei.com.)

---

## 1. The model in one paragraph

Build your OWN local-service website under your OWN invented brand (e.g. "Glendale Quality
Drywall"), rank it for bottom-of-funnel "{service} {city}" searches, put a **tracking phone
number** on it, and rent the ringing phone to a local operator — commission first, then flat
monthly. Unlike agency SEO you keep the asset, the brand, the domain, and the phone number;
if a tenant leaves, the site re-rents. Ippei's proof: a tree-care site in Grand Rapids, MI,
built in ~15 hours 11 years ago, has paid ~$2K/month ever since — $240K+ lifetime.

## 2. The secrets (verbatim-grounded, first 30 minutes)

**The machine: tracking number + whisper message**
1. **The whisper message is the sales engine.** Call-tracking software plays "this lead was
   sent to you by [your name]" to the tenant before every connected call. The business owner
   hears your name on every job that rings — so when you finally call them, you're not a
   cold pitch, you're "the guy who's been sending you those leads."
2. **Free leads → commission → flat rent ladder.** Rank first, route free leads for a few
   days, then ask "want more of these customers?" Start on commission (removes all tenant
   risk), and after 1–2 months of trust convert to flat rental at $1,000–$2,000/month.
3. **Bill weekly, not monthly.** Card on file in Stripe, billed weekly — $250/week never
   reads like a "big fat bill" the way $1,000/month does. Ippei has tenants he hasn't
   spoken to in years who are still paying. No reports, no handholding: "I'm getting you
   the leads, you're paying me for the leads."
4. **Top-end monetization is CRM-integrated commission**: his call tracking is
   API-connected to a large LA construction company's CRM (7 niches, dozens of sites) and
   he takes straight commission on every closed job. Flat fee = predictable; integrated
   commission = lucrative with a trusted whale tenant.

**Why the niche buys**
5. **Small contractors are pre-burned on SEO.** They can't afford $1.5–2K/month real
   agencies, so they've tried $500/month junk agencies, got nothing, and now distrust SEO.
   Rank-and-rent sidesteps the entire objection because you deliver the end product —
   customers — before asking for anything.
6. **Churn is structurally lower than agency work**: an agency client can fire you no
   matter how good the work; a tenant cancelling gives up a working phone line, and the
   asset just re-rents.

**Market selection (the filter, with numbers)**
7. **Niche filter**: phone-driven home-construction services with **average customer value
   ≥ $2–3K** — drywall, concrete, painting, custom cabinets, countertops, metal
   gates/fences, foundation repair, tree care. Sites can also catch commercial jobs (a
   tenant closed a **$200K commercial roofing job** off one).
8. **What to avoid** — three distinct failure modes: low-ticket commoditized (carpet
   cleaning — TaskRabbit ate it), over-saturated (plumbing/HVAC — too many companies),
   and high-ticket-slow-close (kitchen/bath remodel, ADU construction — price shoppers,
   months to close; leads come but don't convert).
9. **Sub-niches are the cheat code**: obscure services with real volume (epoxy flooring,
   garage storage systems) rank trivially easily, especially when you already have a
   tenant who wants those jobs.
10. **City size**: minimum ~80K population; **sweet spot 100–300K**. (His published guides
    say 75–250K; the episode number supersedes.)
11. **Ignore keyword tools for local volume** — "drywall Glendale CA" shows zero monthly
    volume yet the site produces steady leads. Demand proxy: search the keyword and count
    **10–15 businesses in the Google Maps pack** → demand exists once you rank.
12. **Competition is read manually, page by page — never by DR/KD alone.** Do the actual
    local searches, open the top 3–5 sites and audit like an agency would: heading
    structure (H1/H2/H3), indexed page count (`site:` — most local sites index only ~10
    pages), keyword targeting mistakes, domain type. Avoid SERPs where the top 3–4 sites
    hold DR 30+ (personal-injury law); most home-construction SERPs are DR ≤5 or 0.
    "Pick and choose your battles" — saturation is SERP-level, not niche-level.
13. **The topical-authority bet**: against a 10-page weak site, indexing/ranking **30–40
    targeted pages** wins **without backlinks**. Most local sites ranking for lucrative BOF
    keywords rank *accidentally* — merely targeting the keyword on purpose beats them.

**Asset build (structure chapter, verbatim)**
14. **Brand = partial exact-match domain.** True EMDs (glendaledrywall.com) are usually
    taken; insert a quality word: "Glendale Quality Drywall" → glendalequalitydrywall.com.
    City + service in the brand name is an edge most real businesses don't have.
15. **On-page formula for the homepage/hub**: H1 starts with the brand name, then main
    keywords; page title likewise brand-first; **first 1–2 paragraphs must define the
    entity** — repeat the brand and enumerate the exact services ("Glendale Quality Drywall
    has 20 years of experience doing X, Y, Z"). Then **load industry entities** — material
    names, engineering terms, expertise vocabulary that isn't necessarily searched — to
    raise Google's quality score for the page.
16. **Write as the brand, for no tenant in particular.** You don't know who'll rent the site
    when you build it. The tenant ends up operating **two brands** — theirs plus yours —
    "like a franchise, but better." Your brand outranks theirs because it has keywords in
    the name and real SEO behind it.
17. **The expansion play**: service businesses travel a 20–30 mile radius, so a happy $500/
    month tenant becomes a $2–3K/month tenant as you build sibling sites for surrounding
    cities and hand them the whole cluster.
18. **GBP is optional, not dead** — the model's biggest misconception. Video verification
    still gets profiles ("a bit more gray hat"); only **30–40% of his current 130 sites**
    have a GBP; a student clears $15–20K/month with none. Niche-dependent: towing is
    urgent/GBP-driven (nobody browses a towing site), custom cabinets is
    consideration/website-driven — and most local sites are so ugly that good design alone
    converts. People who called the model dead were the ones who only ever farmed GBPs.
19. **Update-resilience is proven in his own portfolio**: ippei.com (content blog, was
    $50K/month in content spend, 3,500 clicks/day) got hit repeatedly by core updates down
    to ~200–300 clicks/day, while 11-year-old rank-and-rent sites never moved. "Bottom of
    the funnel doesn't die."

**Origin story (context)**
20. Started 2014 at 24, from a $35K/yr job ($2,200/month take-home, $60/month raise), via
    Dan Klein's "Job Killing" program — ugly Weebly limo/tree/towing sites in Michigan;
    $3–4K/month in 7 months → quit; $25–30K/month within 2–3 years; later Dan's business
    partner; launched his own rank-and-rent community in the past year. Key lesson credited
    to Dan: sales, positioning, and deal structure matter more than SEO technique.

**GBP video verification (57:59–1:05:52 — framed on-air as "what other people have done", explicit don't-do-this disclaimer)**
21. **Vehicle-only verification video**: today's service-business GBP verification wants a
    phone-shot video proving the business is real. The pattern that passes: a vehicle with
    a **~$60 car magnet** (Home Depot), film yourself opening/entering/starting the
    vehicle, show **$10–20 business cards** with logo + name + address + phone (NAP) held
    clearly so the AI can read them. Do NOT walk into a house/home-office — it confuses
    the classifier into treating you as a location-based business instead of service-area.
22. **Turn precise location OFF** while shooting (there's IP/location tracking in the
    flow). If verification fails: **delete the listing and redo it with the exact same
    name/address** — it frequently passes on the retry.
23. **Trust stack**: Google Workspace email on the domain, plus Search Console and
    Analytics connected to the site, as ownership proof signals.
24. **The call-tracking stack is ~$10/month**: records every call, and the whisper message
    is just typed into settings and read by an AI voice before each call connects.
25. **GBP-less compensation**: if you skip GBP, build MORE sites per client — sibling
    cities and hyper-specific sub-niche sites (his example: a whole site for **lime-wash
    painting** ranks in Los Angeles without any GBP because it's so targeted).

**Sales mechanics & leverage (1:08:23–1:17:00)**
26. **Qualification question**: "If I sent you 10 leads, how many could you close?" Most
    industries should close ~**30%**; an answer of 10% = not a good fit, walk.
27. **Lead value calculator**: avg customer value × close rate × ~7% commission = per-lead
    value (e.g. **$60/lead**); at 10–15 leads/month that prices the site's rent and makes
    the flat-fee conversation trivial ("this site is easily worth $X/month").
28. **Why he mostly doesn't need contracts**: he owns the site, the phone line, and the
    routing — non-payment is answered by pointing the leads at a competitor, which "forces
    their hand" faster than any contract would. When contracts appear, it's usually the
    TENANT wanting exclusivity (stop you working with their competitor). Leverage grows
    with every week of delivered leads — tenants fear losing you to a rival.

**Ops doctrine (1:18:14–end)**
29. **Per-site done-criterion**: once the homepage ranks top of page one for the main
    "{service} {city}" keyword, leave the site alone and build the next one.
30. **His two most expensive mistakes**: (a) lazy "shotgun" sites with thin, undetailed
    content — they never rank durably; (b) **automated backlink/link-generation services**
    — rankings spiked then crashed. Conclusion: no automation on links; on-page topical
    authority + genuinely structured content (H2/H3s that go deeper, not walls of text)
    ranks in **3–4 months with zero links**.
31. **Untested idea he wants to run**: 10–20 YouTube videos per site targeting the same
    "{service} {city}" keywords, linking back — one video per site already works.
32. **The 10-site rule (why projects fail)**: the #1 failure cause is building ONE site,
    getting a mediocre result, and quitting. ~10 hours per site × 10 sites = 100 hours;
    "I've never seen anybody build 10 rank-and-rent sites and not be making money."
    Consistency is "the mother of all skills."
33. **Mega-brand rollup** (Edward's riff, Ippei's amendment): Edward — 301-redirect a
    ranking portfolio into one national-sounding brand, press-release the "acquisition,"
    add active social for top-of-mind awareness; a real brand ranks faster for high-volume
    terms and opens adjacent niches. Ippei — the national moat is **branded search
    signals** (Roto-Rooter effect), and without an ad budget you build them by shipping a
    **utility** that earns repeat visits (e.g. photo-upload digital quoting instead of an
    in-home estimator; directories that win long-term all have one).

**[transcript-needed] middle block (~28:22–57:59)** — must-have pages list, internal
linking, doorway avoidance, AI content workflow, autocomplete/GSC keyword mining,
page-split rule, first-30-days blueprint, site-structure strategy, image sourcing; plus
the reviews/conversions segment (1:05:52).

## 3. Step-by-step formula (operator sequence)

All steps verbatim-grounded except the bracketed middle-block details.

1. **Pick the niche**: phone-driven home-construction service, avg job ≥$2–3K, fast close.
   Kill list: carpet cleaning (commoditized), plumbing/HVAC (saturated), remodels/ADU
   (slow close). Hunt sub-niches (epoxy floors, garage storage).
2. **Pick the city**: 80K minimum, 100–300K sweet spot. Validate demand by Maps-pack count
   (10–15 businesses), not keyword-tool volume.
3. **Vet the SERP manually**: top 3–5 sites — DR (want ≤5), indexed pages (want ~10),
   heading structure, accidental-vs-intentional keyword targeting. Enter only where weak.
4. **Mint the brand**: "{City} {Quality-word} {Service}" partial-EMD; register matching
   domain.
5. **Build the hub page**: brand-first H1 + title; first 2 paragraphs define the entity and
   enumerate services; entity-load with industry/materials vocabulary; write AS the brand.
6. **Out-structure the incumbents**: 30–40 targeted BOF pages (service pages + sub-city
   pages) beats a 10-page site with no backlinks — topical authority over links.
7. **Wire the phone**: tracking number with whisper message ("lead sent by …"), card-on-file
   weekly billing rails ready (Stripe).
8. **Run the first-30-days sequence** after launch *[transcript-needed — 51:09]*.
9. **Mine and split keywords**: Autocomplete + GSC; new-page-vs-expand rule
   *[transcript-needed — 43:34–48:31]*. No automated link building ever — spike-and-crash;
   quality on-page ranks in 3–4 months with zero links.
10. **(Optional) GBP layer**: vehicle-only verification video (car magnet + NAP business
    cards, precise location off, delete-and-retry on failure), Workspace email + GSC +
    Analytics as trust stack. Skipping GBP? Compensate with more sites and sub-niche
    hyper-targeting per client.
11. **Monetize the ring**: free leads → qualification ("10 leads — how many close?",
    expect ~30%) → commission → price via the lead-value calculator (value × close rate ×
    ~7% ≈ $/lead) → $1–2K/month flat rent billed weekly → (whale tenants) CRM-integrated
    commission. Contracts optional: owning site + number + routing IS the leverage.
12. **Ship the next site once the homepage hits top of page one** — and build 10 sites
    (~100 hours) before judging the model. Scale: sibling-city sites for proven tenants
    ($500 → $2–3K/month per client); long-term, consider the mega-brand rollup (301s into
    a national brand + branded-search moat via a utility feature).

## 4. Fit to our system (ryan-super-affiliate)

- **SCAN**: the niche filter (phone-driven, ≥$2–3K ticket, low loyalty, fast close,
  Maps-pack-count demand proxy) is directly reusable as market-engine pack criteria.
- **VET**: his manual SERP audit (DR ≤5, ~10 indexed pages, accidental targeting) is a
  ready-made competition gate — sharper than our current "landing page audit" lens for
  local plays.
- **ROUTE**: a ranked lead-gen site is a permanently-owned BRIDGE with a phone number; the
  whisper-message + free-leads close maps cleanly onto pay-per-call affiliate offers.
- **BUILD**: hub/service/sub-city BOF architecture + entity-loading maps onto
  02-page-builder; brand-first on-page formula is a checklist item.
- **LEARN**: weekly billing as a churn hack and the update-resilience thesis (weight the
  portfolio toward commercial intent) both generalize beyond local SEO.

## 5. Open items

- F1 — transcript for the middle block **~28:22–57:59** only (must-have pages, internal
  linking, doorway avoidance, AI workflow, keyword research/GSC/splits, first-30-days
  blueprint, structure strategy, image sourcing) + reviews segment (1:05:52). Everything
  else is now verbatim-covered.
- F2 — channel-wide scan of The Edward Show queued in `intake/channel-scan-queue/QUEUE.yaml`.
- F3 — port `strategy.yaml` + `obsidian-note.md` to their destinations once write access exists.

## Links from the episode

- Ippei's blog: https://ippei.com/ · dashboard/call-tracking: https://ippei.com/dashboard/
- Ippei on YouTube: @ippeiseo
- Edward Sturm's SEO course: https://compactkeywords.com/ (BOF landing pages, avg 415 words)
- Show: https://edwardsturm.com/the-edward-show/
