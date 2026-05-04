# IRCTC — Train Search "Book Ticket" Form

The Angular/PrimeNG search form on the IRCTC home page. All fields
(From, To, Date, Class, Quota) are PrimeNG widgets — none are plain
`<input>`s the user can `value=` into. The whole form can be filled
**deterministically by selectors** when inputs are normalized to
station codes / class codes / a calendar date — no LLM step needed.
LLM disambiguation is only required when the input is a free-form
station *name* that matches multiple stations.

URL — go here directly:

```
https://www.irctc.co.in/nget/train-search
```

After submit, the page navigates to:

```
https://www.irctc.co.in/nget/booking/train-list
```

## Field map

| Field   | Wrapper                        | Inner input (aria-label)                                         | Widget       |
| ------- | ------------------------------ | ---------------------------------------------------------------- | ------------ |
| From    | `p-autocomplete#origin`        | `Enter From station. Input is Mandatory.`                        | autocomplete |
| To      | `p-autocomplete#destination`   | `Enter To station. Input is Mandatory.`                          | autocomplete |
| Date    | `p-calendar#jDate`             | `Enter Journey Date. Formate D.D./.M.M./.Y.Y.Y.Y. Input is Mandatory.` | calendar     |
| Class   | `p-dropdown#journeyClass`      | aria-label `All Classes` (current selection is the label)        | dropdown     |
| Quota   | `p-dropdown#journeyQuota`      | aria-label `GENERAL` (current selection is the label)            | dropdown     |
| Submit  | `button.search_btn` (also `.train_Search`) | text "Search Trains"                                  | button       |

Quick selectors for `js(...)`:

```js
document.querySelector('p-autocomplete#origin input')
document.querySelector('p-autocomplete#destination input')
document.querySelector('p-calendar#jDate input')
document.querySelector('p-dropdown#journeyClass')
document.querySelector('p-dropdown#journeyQuota')
document.querySelector('button.search_btn')
```

## Filling From / To (autocomplete) — code-anchored, deterministic

**Default path: always normalize to the IR station code (BPL, NDLS,
CSMT, MMCT, RKMP, SBC, …) and pick by code-anchored selector.** No
LLM, no user confirmation. IR station codes are globally unique, so a
word-boundary match on `" - {CODE}"` resolves to exactly one station.

```js
// Type the code (or any string that surfaces this station), then:
const li = Array.from(document.querySelectorAll('li.ui-autocomplete-list-item'))
  .find(el => / - BPL\b/.test(el.innerText));
li.click();
```

Use `\b` (word boundary), **not** `\(`. Two row formats exist:

- `STATION NAME - CODE (CITY)\nSTATE` — when station name ≠ city
  (e.g. `BHOPAL JN - BPL (BHOPAL)\nMADHYA PRADESH`).
- `STATION NAME - CODE\nSTATE` — when station name effectively matches
  the city (e.g. `KSR BENGALURU - SBC\nKARNATAKA`,
  `NEW DELHI - NDLS (NEW DELHI)` — the latter still has parens, but
  many city-name stations don't). A `\(` regex misses these.

Why a regex on `" - CODE\b"` and not `.startsWith(code)` or first():

- The panel can include a `----- Journeys -----` section (recent
  searches) **above** `----- Stations -----`. Picking the literal first
  `li` can land on a journey row, which fills *both* From and To.
- The headers (`----- Stations -----`, `----- Journeys -----`) are
  themselves `li` elements.
- Substring matches: typing `DLI` returns `OLD DELHI - DLI`, `NDLS`,
  others. Codes don't usually collide as substrings, but the
  code-anchored regex is safe even when they do.

When the user gave a city without specifying the station (e.g.
"Bhopal" → BPL **or** RKMP, "Delhi" → NDLS, DLI, NZM, ANVT), pick the
main junction by default (BPL, NDLS) and proceed. Do **not** ask the
user when there is only one obvious station for the city. Only ask
when the user-provided text is genuinely ambiguous *and* the
candidates are materially different (e.g. `MUMBAI CST` → CSMT vs MMCT,
which serve different rail lines).

City → default code (use without confirmation):

| City                  | Default code | Notes                                       |
| --------------------- | ------------ | ------------------------------------------- |
| Bhopal                | BPL          | RKMP also serves Bhopal; pick BPL by default |
| Delhi / New Delhi     | NDLS         | DLI/NZM/ANVT exist but NDLS is the default  |
| Mumbai (CST / VT)     | CSMT         | Central Rly terminus                        |
| Mumbai Central        | MMCT         | Western Rly terminus                        |
| Bangalore / Bengaluru | SBC          | KSR Bengaluru; YPR (Yesvantpur) for some trains |
| Chennai               | MAS          | Chennai Central                             |
| Kolkata               | HWH          | Howrah; SDAH/KOAA also serve Kolkata        |
| Hyderabad             | HYB          |                                             |
| Pune                  | PUNE         |                                             |
| Ahmedabad             | ADI          |                                             |

LLM-only fallback: if the user gave a free-form name and no obvious
single code matches (e.g. an unusual or misspelled town), fall back to
typing the name and reading the panel — but log this as the exception
path, not the default.

Reading the panel for inspection:

```js
Array.from(document.querySelectorAll('li.ui-autocomplete-list-item'))
  .map(el => el.innerText)
```

## Filling Date (PrimeNG calendar) — deterministic, no LLM

Click the inner `<input>` of `p-calendar#jDate` to open the popup.

- Header: `.ui-datepicker-title` — text is `MonthYYYY` (e.g.
  `May2026`, no space).
- Month nav: `.ui-datepicker-prev` / `.ui-datepicker-next` (anchors).
- Day cells: `.ui-datepicker-calendar td a` — each anchor's `innerText`
  is the day-of-month. Match by text, not by grid position.

Deterministic flow for an absolute date `(year, month, day)`:

```python
import time
MONTHS = ["January","February","March","April","May","June",
          "July","August","September","October","November","December"]
target_header = f"{MONTHS[month-1]}{year}"   # e.g. "May2026"

js("document.querySelector('p-calendar#jDate input').click()")
time.sleep(0.3)

for _ in range(14):                          # ARP is ~60 days; cap loops
    header = js("return document.querySelector('.ui-datepicker-title').innerText")
    if header.replace(" ", "") == target_header:
        break
    js("document.querySelector('.ui-datepicker-next').click()")
    time.sleep(0.2)
else:
    raise RuntimeError(f"date {target_header} beyond ARP horizon")

js(f"""(() => {{
  const cell = Array.from(document.querySelectorAll('.ui-datepicker-calendar td a'))
    .find(a => a.innerText.trim() === '{day}');
  cell.click();
}})()""")
```

Don't type into the calendar input directly — the widget re-parses on
every keystroke and rejects partial values.

## Filling Class / Quota (PrimeNG dropdown) — deterministic, no LLM

`p-dropdown` is a custom select. Click the wrapper to open
`.ui-dropdown-panel`, then click the `li.ui-dropdown-item` whose
`innerText` matches the desired option. The reliable match is on the
**class code in parens** — that string is unique per row.

Class options (label / code):

| Label                  | Code |
| ---------------------- | ---- |
| All Classes            | —    |
| Anubhuti Class         | EA   |
| AC First Class         | 1A   |
| Exec. Chair Car        | EC   |
| AC 2 Tier              | 2A   |
| First Class            | FC   |
| AC 3 Tier              | 3A   |
| AC 3 Economy           | 3E   |
| AC Chair car           | CC   |
| Sleeper                | SL   |
| Second Sitting         | 2S   |

Quota options: `GENERAL`, `LADIES`, `LOWER BERTH/SR.CITIZEN`,
`PERSON WITH DISABILITY`, `DUTY PASS`, `TATKAL`, `PREMIUM TATKAL`.

Defaults when the user did not specify:

- **Class → "All Classes"** (already the form default; usually no click
  needed — verify via `document.querySelector('p-dropdown#journeyClass
  .ui-dropdown-label').innerText`).
- **Quota → `GENERAL`** (already the form default).

Set explicitly only when the user provided a value:

```js
// Class — match by code in parens, e.g. "(SL)"
(() => {
  document.querySelector('p-dropdown#journeyClass').click();
})()
```
```js
(() => {
  const li = Array.from(document.querySelectorAll('p-dropdown#journeyClass li.ui-dropdown-item'))
    .find(el => / \(SL\)$/.test(el.innerText.trim()));
  li.click();
})()
```

For "All Classes" (no code in parens) match by exact label:
```js
(() => {
  const li = Array.from(document.querySelectorAll('p-dropdown#journeyClass li.ui-dropdown-item'))
    .find(el => el.innerText.trim() === 'All Classes');
  li.click();
})()
```

Quota uses the same pattern with exact label match (labels are unique
on their own).

## Submit & verify

```js
document.querySelector('button.search_btn').click();
```

Then `wait_for_load()` and confirm `page_info().url` ends in
`/booking/train-list`. Result cards are `app-train-avl-enq`; each
card's `innerText` starts with `TRAIN NAME (NUMBER)` then `Runs On:
MTWTFSS` then dep/arr rows.

## End-to-end (deterministic, no LLM, no coordinate clicks)

```python
import time
new_tab("https://www.irctc.co.in/nget/train-search"); wait_for_load()

# From — type code, pick by code-anchored regex
js("document.querySelector('p-autocomplete#origin input').focus()")
type_text("BPL"); time.sleep(0.8)
js("""(() => {
  const li = Array.from(document.querySelectorAll('li.ui-autocomplete-list-item'))
    .find(el => / - BPL\\b/.test(el.innerText));
  li.click();
})()""")

# To
js("document.querySelector('p-autocomplete#destination input').focus()")
type_text("NDLS"); time.sleep(0.8)
js("""(() => {
  const li = Array.from(document.querySelectorAll('li.ui-autocomplete-list-item'))
    .find(el => / - NDLS\\b/.test(el.innerText));
  li.click();
})()""")

# Date — see "Filling Date" section for the month-navigation loop

# Class / Quota — only set if user specified; defaults are All Classes / GENERAL

# Submit
js("document.querySelector('button.search_btn').click()")
wait_for_load()
```

## Quirks & traps

- **PrimeNG, not vanilla.** No `<select>`, no `value=`. Every widget
  opens + click-on-list-item.
- **`js()` scope persists across calls.** `const`/`let` declarations
  leak between invocations and throw "already declared". Wrap snippets
  in `(() => { ... })()`.
- **Autocomplete panel has Journeys + Stations sections.** Section
  headers (`----- Journeys -----`, `----- Stations -----`) and recent
  journey rows are also `li.ui-autocomplete-list-item`. Always select
  with the code-anchored regex `/ - CODE\b/`, never by index.
- **Login modal hijack.** Logged-out → submit pops `#loginModal`
  instead of navigating. Stop and ask the user to log in; do not type
  credentials from screenshots.
- **Mumbai terminals.** `CSMT` (Central Rly, ex-VT) ≠ `MMCT/BCT`
  (Western Rly). Wrong terminus → "no direct trains".
- **`MMCT` and `BCT` are the same physical station** under two codes;
  either works for Mumbai Central trains, but they're listed
  separately in the autocomplete.
- **Calendar input is write-only via the popup.** Don't type the date
  into the `<input>`.
- **Booking horizon (ARP).** ~60 days. Months past ARP never appear;
  cap the next-month loop and raise.
- **Calendar header has no space:** `May2026`, not `May 2026`. Strip
  whitespace before comparing.
- **`Runs On: MTWTFSS` mask.** A train listed for the searched date
  may not run that weekday — verify before recommending.