# SearXNG image-search fixtures

Captured on 2026-09-08 from the locally deployed `searxng/searxng:latest`
instance (`gslide-ops` compose, `search` profile) with:

```
/search?q=<query>&categories=images&format=json&safesearch=1
```

Each fixture is a sanitized subset of the real response: up to 4 results per
engine, only the fields the adapter consumes, plus the real
`unresponsive_engines` list. Field values (URLs, titles, resolution strings)
are verbatim so parser tests must tolerate the quirks documented below.

## Top-level response

| Key | Meaning |
| --- | --- |
| `query` | Echoed normalized query |
| `results` | Merged, scored results from all engines |
| `unresponsive_engines` | `[name, error]` pairs for engines that failed this query (e.g. `["duckduckgo images", "access denied"]`) — useful for diagnostics, must be tolerated |
| `answers`, `corrections`, `infoboxes`, `suggestions` | Not relevant to image import; tolerate presence |

## Result fields

| Field | Observed meaning | Quirks (all present in fixtures) |
| --- | --- | --- |
| `img_src` | Direct URL to the full image | Flickr returns protocol-relative `//live.staticflickr.com/...`; Wikimedia appends `utm_*` tracking params; icon engines return `.svg` URLs |
| `thumbnail_src` | URL to a small preview | **May be absent** (openverse, artic); pexels/bing/gstatic each have different param conventions |
| `url` | Source page containing the image | For icon engines equals `img_src` |
| `title` | Result title | Always present in captures; treat as optional anyway |
| `engine` | Primary engine that produced the result | Icon engines appear here (`lucide`, `devicons`, `artic`) |
| `engines` | List of all engines returning the same result | Deduplication signal |
| `resolution` | Dimensions string | **Format varies per engine**: `824x464` (google cse images), `1000×431` (bing images, U+00D7), `800 x 609` (flickr, spaced), `1181 x 536` (wikicommons); **may be absent** |
| `width` / `height` | **Never present** in this SearXNG version's JSON | Derive dimensions from `resolution` instead |
| `img_format` | MIME/format hint | `jpeg`, `image/jpeg`, or absent — unreliable, validate real bytes |
| `filesize` | Human-readable size | Rarely present, sometimes empty string — informational only |
| `template` | Result template | `images.html` for **both** photos and icons — cannot be used to filter icons |
| `category` | Result category | `images` |
| `metadata`, `author`, `pubdate`, `views`, `score`, `positions`, `priority`, ... | Present on some results | Adapter ignores them |

## Engine behavior in the `images` category (2026-09-08, default settings)

Responsive photo engines: `bing images`, `google cse images`, `pexels`,
`flickr`, `wikicommons.images`, `pinterest`, `openverse`, `artic`.

**Icon engines also answer `categories=images`**: `lucide`, `devicons` (SVG
icon CDNs, no `resolution`). Neither `template` nor `category` distinguishes
them — filter by engine allow-list (ops config) and/or reject `.svg`/missing
dimensions. This is why the ops phase pins an explicit engine set.

Commonly unresponsive engines (rate limiting / CAPTCHA, from
`unresponsive_engines`): `brave.images` (suspended: too many requests),
`duckduckgo images` (access denied), `startpage images` (suspended: CAPTCHA),
`unsplash` (parsing error). Do not rely on any single engine.