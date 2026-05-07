# Blinkit — Cart Panel

Field-tested against blinkit.com on 2026-05-04 using a logged-in Chrome
session with a delivery address set. Covers opening the cart, reading line
items, bumping quantity, and removing items — all through selectors +
`getBoundingClientRect()`, no screenshot/pixel reads.

## Opening the cart

The cart is a side panel modal, opened from the green "N items / ₹X" pill
in the top-right of every page header. There's no dedicated `/cart` URL
that works as a deep link — you have to click the pill.

The pill's text strictly matches `/^\d+\s+items?$/` (e.g. `"7 items"`).
That exact match is the disambiguator — Blinkit also renders strings like
`"Shipment of 6 items"` *inside* the cart modal once it's open, so scope to
elements that aren't descendants of the modal portal:

```python
target = js(r"""
const modal = document.querySelector('.ReactModalPortal, .cart-modal-rn');
const cands = [...document.querySelectorAll('div, button, a, span')]
  .filter(el => /^\d+\s+items?$/i.test((el.innerText || '').trim())
              && (!modal || !modal.contains(el)));
if (!cands.length) return null;
cands.sort((a,b) => (a.innerText||'').length - (b.innerText||'').length);  // innermost
const r = cands[0].getBoundingClientRect();
return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
""")
click_at_xy(target["x"], target["y"])
import time; time.sleep(1.5)
```

If the cart is empty the pill says "My Cart" and there's nothing to read —
detect that case before trying to extract rows.

## Closing the cart

The cart is rendered as a `ReactModal` with a dimmed overlay. Clicking
**anywhere outside the panel** (on the overlay) closes it — there is no
visible × button you need to find. Derive the click point from the panel's
own rect so it works at any viewport width:

```python
import time
geom = js(r"""
const p = document.querySelector('.cart-modal-rn');
if (!p) return null;
const r = p.getBoundingClientRect();
return {left: r.left, top: r.top, bottom: r.bottom, vw: window.innerWidth, vh: window.innerHeight};
""")
if geom:
    click_x = max(10, int(geom["left"] / 2))           # midpoint of dim area
    click_y = int((geom["top"] + geom["bottom"]) / 2)  # vertical middle of panel
    click_at_xy(click_x, click_y)
    time.sleep(0.8)
# verify
assert not js("return !!document.querySelector('.cart-modal-rn');")
```

**Always close the cart before interacting with anything on the main page.**
While the modal is open, the dimmed overlay sits on top of the rest of the
page and intercepts every click — `click_at_xy` against a product card or
the search bar will land on the overlay (and close the modal) instead of
hitting your target. Treat "cart open" as a modal state and exit it
explicitly before the next action.

## Cart panel structure

| Selector | Role |
|---|---|
| `.cart-modal-rn` | scrollable container — set `scrollTop` here, not on rows |
| `div[class*="AddToCart___StyledDiv-"]` | minus / remove button (one per row, in order) |
| `div[class*="AddToCart___StyledDiv2-"]` | plus button (one per row, in order) |

The minus and plus glyphs are `CustomFont` icon characters — `textContent`
reads them as `"U"` (minus) and `"5"` (plus). Don't search by those
characters; use the wrapper class names above.

The number of `StyledDiv` and `StyledDiv2` matches the number of cart line
items, in DOM order (which is the same as the visible order).

**Note:** these selectors are *cart-page-specific*. The search-results
stepper uses `.icon-plus` and a different class tree (see
`search-and-cart.md`). Don't mix them.

## Reading line items

Walk up from each minus button until you find an ancestor row that contains
both the product name and the stepper text. The price/stepper sub-divs
have multi-line text but no name, so you have to keep climbing until a
`/[A-Za-z]{4,}/` line shows up alongside the `U` and `5` glyphs.

```js
const panel = document.querySelector('.cart-modal-rn');
const minusDivs = [...panel.querySelectorAll('div[class*="AddToCart___StyledDiv-"]')];
const plusDivs  = [...panel.querySelectorAll('div[class*="AddToCart___StyledDiv2-"]')];
return minusDivs.map((minus, i) => {
  const plus = plusDivs[i];
  let row = minus.parentElement, chosen = null;
  for (let k = 0; k < 12; k++) {
    if (!row) break;
    const lines = (row.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const hasName = lines.some(l => /[A-Za-z]{4,}/.test(l) && !/^(ADD|U|5)$/.test(l));
    const hasStepper = lines.includes('U') && lines.includes('5');
    if (hasName && hasStepper) { chosen = row; break; }
    row = row.parentElement;
  }
  if (!chosen) return null;
  const lines = (chosen.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  const name = lines.find(l => /[A-Za-z]{4,}/.test(l) && !/^(ADD|U|5)$/.test(l)) || '';
  const size = lines[lines.indexOf(name) + 1] || '';
  const uIdx = lines.indexOf('U');
  const qty = uIdx >= 0 && /^\d+$/.test(lines[uIdx + 1] || '')
    ? parseInt(lines[uIdx + 1], 10) : null;
  const mr = minus.getBoundingClientRect();
  const pr = plus.getBoundingClientRect();
  return {
    name, size, qty,
    minus: {x: Math.round(mr.left + mr.width/2), y: Math.round(mr.top + mr.height/2)},
    plus:  {x: Math.round(pr.left + pr.width/2), y: Math.round(pr.top + pr.height/2)},
  };
}).filter(Boolean);
```

Output schema per row: `{name, size, qty, minus: {x,y}, plus: {x,y}}`.

## Editing quantity (the click pattern)

For both `+` and `−`:

1. Scroll the row into the viewport (see scroll quirk below).
2. **Re-read the row** to get fresh `getBoundingClientRect()` coordinates.
3. `click_at_xy(stepper.x, stepper.y)`.
4. `time.sleep(~1s)` for the panel to re-render.
5. **Re-read the whole cart** before the next action — rows shift up when
   items are removed, and the previously-read coordinates go stale.

Don't try `el.click()` from JS first — the existing search-and-cart skill
documents that Blinkit watches CDP mouse fingerprints, so prefer real
coordinate clicks (or the slow-click recipe in `search-and-cart.md` for
the most sensitive interactions).

### Bump quantity

Find the row by name substring, click `plus`, verify qty incremented:

```python
items = read_cart_items()           # the JS above, wrapped in js(...)
it = next(i for i in items if "Ooty Carrot".lower() in i["name"].lower())
click_at_xy(it["plus"]["x"], it["plus"]["y"])
time.sleep(1.0)
new_qty = next(i for i in read_cart_items() if "Ooty Carrot" in i["name"])["qty"]
```

### Remove an item

A single click on the minus glyph at `qty=1` removes the row — there is
**no separate trash icon** and no confirm dialog. The "U" glyph stays the
same regardless of qty.

For higher quantities, click minus `qty` times, re-reading between clicks.
The row disappears from the cart panel after the last decrement.

```python
while True:
    items = read_cart_items()
    it = next((i for i in items if "Coriander Powder" in i["name"]), None)
    if not it:
        break  # gone
    scroll_cart_to_row(it)          # see below
    it = next(i for i in read_cart_items() if "Coriander Powder" in i["name"])
    click_at_xy(it["minus"]["x"], it["minus"]["y"])
    time.sleep(1.0)
```

## Quirks

- **`row.scrollIntoView()` does not reliably scroll the cart panel.** Even
  walking up to the row's ancestors, calling `scrollIntoView({block: 'center'})`
  often leaves the row off-screen (the click then lands at y≈16, on the
  panel header, and silently does nothing). Drive the scroll directly:

  ```js
  const panel = document.querySelector('.cart-modal-rn');
  panel.scrollTop = 0;                                          // top
  // or center a known minus button inside the panel's own viewport:
  const pr = panel.getBoundingClientRect();
  panel.scrollTop += (minusY - (pr.top + pr.height / 2));
  ```

  Then re-read coordinates. The panel is the only scroll container that
  matters; ignore the body scroll behind the modal.

- **Visibility check before clicking.** Test the stepper rect against the
  *panel's* rect, not the window viewport — the panel has its own header
  and footer (the "Proceed To Pay" bar) that overlap rows at the edges:

  ```js
  const pr = panel.getBoundingClientRect();
  const visible = mr.top > pr.top + 60 && mr.bottom < pr.bottom - 60;
  ```

  The 60px padding accounts for the sticky panel header and footer; both
  are part of the panel's own layout and scale with its rendered size,
  not the window. If `visible` is false, scroll first, re-read, then click.

- **Layout shifts after every click.** Removing an item moves later rows
  up by one slot; bumping a quantity can change the row height (price
  text reflows). Always re-read `read_cart_items()` between actions
  rather than caching the original list.

- **Different stepper than search results.** The `.icon-plus`/`.icon-minus`
  classes used on product cards (search-and-cart.md) **do not exist**
  inside the cart panel. Don't share selectors between the two flows.

- **Glyphs are `CustomFont`, not text.** `textContent === 'U'` /
  `=== '5'` are font-rendered minus/plus icons. They're stable enough to
  use for parsing (locating the qty digit between them), but never as a
  click target on their own — go through the `StyledDiv` wrappers.

- **No deep link to the cart.** The cart only opens from the header pill;
  there's no `/cart` URL, and reloading the page closes the modal.

## Tab reuse and pacing (anti-bot)

Blinkit watches navigation patterns. **Don't open new tabs for each
action** — a real user clicks through one tab. Default to `goto_url(...)`
on the existing Blinkit tab and only fall back to `new_tab(...)` when the
current tab is on a different origin entirely:

```python
cur = (page_info() or {}).get("url", "")
if "blinkit.com" in cur:
    goto_url(url)         # reuses the open tab
else:
    new_tab(url)
```

Between consecutive interactions (clicks, scrolls, navigations), insert
`time.sleep(random.uniform(0.5, 1.0))`. Back-to-back zero-jitter actions
are themselves a fingerprint, and the cart panel needs that long to
re-render after each stepper click anyway. For a fresh page load, allow
1.5–2s after `wait_for_load()` before reading state — see the hydration
notes in `search-and-cart.md` and `orders.md`.

This applies across the whole site, not just the cart: search, order
history, and order detail pages should all be visited in the same tab
when navigating between them.

## Programmatic add-to-cart (cart_fsm.py)

`cart_fsm.py` next to this file packages the search → AI-pick → add flow
as one function. It is **not** auto-loaded — exec the file inside a
`browser-harness -c` block when you actually need it:

```bash
browser-harness -c '
exec(open("agent-workspace/domain-skills/blinkit/cart_fsm.py").read())
result = add_groceries("tomato, potato, amul masti 1L")
import json; print(json.dumps(result, indent=2))
'
```

`add_groceries(items_csv)` requires `GEMINI_API_KEY` in env and assumes
Blinkit is logged in with a delivery address set.

Return shape:

```python
{
  "status": "success" | "failed",
  "progress": [
    {"item": "tomato", "status": "done",    "state": "DONE",   "chosen_id": "366032"},
    {"item": "potato", "status": "skipped", "state": "DECIDE", "reason": "no good match"},
    {"item": "x",      "status": "failed",  "state": "VERIFY", "error": "stepper did not appear"},
    {"item": "y",      "status": "pending", "state": "SEARCH"},   # un-reached after a failure
  ],
  "error": "x failed at VERIFY: stepper did not appear",  # only when status=failed
}
```

`status: "success"` iff every item ended `done` or `skipped`. On the first
`failed` item the loop aborts and remaining items stay `pending` — a
follow-up agent can re-invoke `add_groceries` with just the un-done items
(everything not `done`/`skipped`) to continue. FSM states the entry can
be parked at: `SEARCH`, `WAIT_RESULTS`, `PARSE`, `DECIDE`, `LOCATE`,
`CLICK`, `VERIFY`, `DONE`.

## Not covered

Checkout (`Proceed To Pay`), tip selection, donation toggle, address
change, and the "Save for later" flow are not field-tested. Add sibling
sections when verified.
