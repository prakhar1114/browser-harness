# Blinkit — Search & Add-to-Cart

Field-tested against blinkit.com on 2026-05-03 using a logged-in Chrome
session with a delivery address already set. Uses only core helpers
(`new_tab`, `goto_url`, `wait_for_load`, `js`, `page_info`, `click_at_xy`,
`cdp`, `capture_screenshot`).

## Direct search URL

```
https://blinkit.com/s/?q={query}
```

Always navigate here directly. The homepage triggers a location-picker
modal whenever no delivery address is set; `/s/?q=` renders results
immediately when an address *is* set, and shows an empty grid when it
isn't — useful signal, no modal to dismiss.

## Empty-results trap

If the parser returns zero rows, the cause is almost always **no delivery
address set on the account**, not a selector regression. The URL stays on
`/s/?q=...` either way, but the DOM has no product cards. Stop and ask the
user to set an address before retrying.

## Hydration wait

`wait_for_load()` returns at `readyState=complete`, but Blinkit hydrates the
listing grid client-side after that. A fixed sleep races slow networks and
makes an empty DOM look like "no results".

Poll instead — count `[role="button"][id]` cards whose `id` is purely numeric
(filters out filter chips and other role=button widgets) and whose innerText
contains `ADD` or `₹`. Cap at ~15s.

```python
import time
deadline = time.time() + 15
while time.time() < deadline:
    n = js(r"""
const cards = document.querySelectorAll('[role="button"][id]');
let n = 0;
cards.forEach(el => {
  if (!/^\d+$/.test(el.id)) return;
  const t = el.innerText || '';
  if (t.includes('ADD') || /₹/.test(t)) n++;
});
return n;
""")
    if n and n > 0:
        break
    time.sleep(0.3)
```

## Lazy-load scroll

Scroll to `document.body.scrollHeight` until the height plateaus or you hit
a cap (~10 iterations works in practice). Page height not changing = end of
results for this query.

```python
last_h = 0
for _ in range(10):
    h = js("return document.body.scrollHeight")
    if h == last_h:
        break
    last_h = h
    js(f"window.scrollTo(0, {h})")
    time.sleep(0.4)
```

## Card parser

Product cards are `[role="button"]` containers whose `id` is the **numeric
Blinkit product id**. Their `innerText` is a newline-joined block. Stable
heuristics:

- Find the `ADD` line — everything before it is the header.
- Discount: line matching `/%\s*OFF/i` (e.g. `"24% OFF"`).
- ETA: line matching `/\bMINS?\b/i` (e.g. `"10 MINS"`).
- Prices: lines starting `^₹`. First is current price; second (if present)
  is the struck-through MRP.
- Name = first remaining line; size = second.
- Dedupe by numeric `id` (cards repeat across stacks).

Avoid CSS-class selectors here — Blinkit ships obfuscated CSS-module class
names that change between deployments. The role=button + innerText approach
has been stable.

```js
const out = [];
document.querySelectorAll('[role="button"][id]').forEach(el => {
  const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (!lines.includes('ADD') && !lines.some(l => /^\d+$/.test(l))) return;
  if (!/^\d+$/.test(el.id)) return;  // numeric id = product id
  const addIdx = lines.indexOf('ADD');
  const head = addIdx >= 0 ? lines.slice(0, addIdx) : lines;
  const discount = head.find(s => /%\s*OFF/i.test(s)) || '';
  const eta = head.find(s => /\bMINS?\b/i.test(s)) || '';
  const prices = head.filter(s => /^₹/.test(s));
  const price = prices[0] || '';
  const mrp = prices[1] || '';
  const meta = new Set([discount, eta, ...prices].filter(Boolean));
  const rest = head.filter(s => !meta.has(s));
  const name = rest[0] || '';
  const size = rest[1] || '';
  if (!name) return;
  out.push({ id: el.id, name, size, price, mrp, discount, eta });
});
const seen = new Set();
return out.filter(r => { if (seen.has(r.id)) return false; seen.add(r.id); return true; });
```

Output schema: `{id, name, size, price, mrp, discount, eta}`.

## Pick the right item before adding

**Never blindly add the first row.** Blinkit's `/s/?q=` returns a noisy mix:
sponsored placements, larger/smaller pack sizes, related-but-not-matching
products, and combo packs all share the listing. The top result is often
*not* what the user asked for.

After parsing, inspect every row and decide:

- **Name match** — does `name` actually correspond to what the user asked
  for? "milk" can return curd, buttermilk, or flavored variants.
- **Brand** — if the user named a brand, filter to it; otherwise pick the
  brand they usually buy (check order history) or the cheapest per-unit.
- **Size** — match the requested pack size. `size` is the printed pack
  ("500 g", "1 L", "6 x 200 g"). For "1 litre milk", a 500ml pouch is wrong
  even if it's the cheapest row.
- **Price vs MRP** — `price` is what you'll be charged; `mrp` is the
  list price. A row with no `mrp` is at full price.
- **ETA** — out-of-stock items still appear, often with no `ADD` control or
  a longer ETA. Skip rows where the ADD locator below returns
  `found: false`.

When the right choice is ambiguous (multiple plausible matches, brand not
specified, sizes don't line up with what the user said), stop and ask the
user rather than guessing.

## Add to cart — two modes

A product card has two states:

1. **Not in cart** — shows an `ADD` text label.
2. **Already in cart** — shows a `− N +` stepper; the increment control is
   `.icon-plus` inside the card.

Locate the right control inside the card with the numeric product id, scroll
it into view, then read `getBoundingClientRect()` at click time. Never store
coordinates between calls.

```js
const card = document.getElementById(String(productId));
if (!card) return {found: false, reason: 'card not on page'};
card.scrollIntoView({block: 'center', behavior: 'instant'});
const plus = card.querySelector('.icon-plus');
const addEl = [...card.querySelectorAll('div, button, span')]
  .find(e => (e.textContent || '').trim() === 'ADD');
const target = plus || addEl;
if (!target) return {found: false, reason: 'no ADD or + control'};
const r = target.getBoundingClientRect();
return {
  found: true,
  mode: plus ? 'increment' : 'add',
  x: Math.round(r.left + r.width/2),
  y: Math.round(r.top + r.height/2),
};
```

Let smooth-scroll settle (~0.4–0.8s) before clicking. Then click — see the
anti-bot section below for *how*.

Confirm by re-reading the cart-item count from the bottom bar:

```js
const m = (document.body.innerText || '').match(/(\d+)\s+items?/i);
return m ? parseInt(m[1], 10) : null;
```

## Anti-bot click technique

Blinkit watches for obvious automation signatures. `click_at_xy(x, y)` (core
helper) works for most UI but a bare `Input.dispatchMouseEvent mousePressed`
with no preceding `mouseMoved` is a known CDP fingerprint. For ADD / stepper
clicks specifically, use a slow-click that:

1. Applies ±5px jitter to the landing point.
2. Sends 3 `mouseMoved` events along a short curved path with ±2px wobble
   and 20–80ms gaps.
3. Pauses 40–120ms before `mousePressed`, and 40–120ms before
   `mouseReleased`.

```python
import random, time

def slow_click(x, y, jitter=5, steps=3):
    tx = int(x + random.uniform(-jitter, jitter))
    ty = int(y + random.uniform(-jitter, jitter))
    sx = tx + random.randint(-120, 120)
    sy = ty + random.randint(-120, 120)
    for i in range(1, steps + 1):
        t = i / steps
        ix = int(sx + (tx - sx) * t + random.uniform(-2, 2))
        iy = int(sy + (ty - sy) * t + random.uniform(-2, 2))
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=ix, y=iy)
        time.sleep(random.uniform(0.02, 0.08))
    cdp("Input.dispatchMouseEvent", type="mouseMoved", x=tx, y=ty)
    time.sleep(random.uniform(0.04, 0.12))
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=tx, y=ty,
        button="left", clickCount=1)
    time.sleep(random.uniform(0.04, 0.12))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=tx, y=ty,
        button="left", clickCount=1)
```

Between consecutive interactions, sleep `random.uniform(0.6, 1.4)` — back-
to-back zero-jitter actions are themselves a fingerprint. Use longer
ranges (1.6–3.2s, 3.5–6.5s) when the next action depends on a fresh
network round-trip.

For passive scrolling, plain `js("window.scrollTo(...)")` is fine; if you
need to look like a real user scrolling (e.g. while browsing), break the
distance into uneven chunks with 0.15–0.45s pauses between.

## Not covered

Login, delivery-address setting, product detail pages, checkout, and
category browsing/filter UI are not field-tested here. Add a sibling skill
file when you've verified one of those flows.
