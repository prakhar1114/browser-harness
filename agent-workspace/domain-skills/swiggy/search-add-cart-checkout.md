# Swiggy — Search, Add to Cart, Checkout

Field-tested against swiggy.com on 2026-05-17 in a logged-in Chrome session
with a saved delivery address (`A2`). Uses only core helpers
(`new_tab`, `goto_url`, `wait_for_load`, `js`, `click_at_xy`, `type_text`,
`press_key`, `capture_screenshot`).

## Preconditions

- **User must be logged in.** Check via cookies: `document.cookie` contains
  `_is_logged_in=1` and `deliveryAddressId=<n>`. If not, stop and ask —
  never type credentials.
- A saved address (e.g. `A2`) exists on the account.
- The homepage forces a location modal if no `deliveryAddressId` cookie is
  present; skip it entirely by jumping straight to `/search`.

## Direct entrypoints

| Goal | URL |
|------|-----|
| Global restaurant/dish search | `https://www.swiggy.com/search` |
| Restaurant menu | `https://www.swiggy.com/city/<city>/<slug>-rest<id>` |
| Checkout | `https://www.swiggy.com/checkout` (empty cart → "Your cart is empty") |

Don't load `/`. It can prompt for location even when you already have an
address; `/search` renders immediately for logged-in users.

## Cart state — single source of truth

Read the header link, NOT visual state. Cart icon refreshes synchronously
after every successful add.

```js
const a = document.querySelector('a[href="/checkout"]');
return a ? a.innerText : null;   // "0\nCart", "1\nCart", ...
```

Note: `a[href=/checkout]` is **not** a valid CSS selector — the `/` must be
inside quotes (`a[href="/checkout"]`).

## The two-ADD trap (most important)

Each menu/result card renders **two `<button>` elements with innerText
`ADD`** stacked at nearly the same x. From `getBoundingClientRect()`:

- **Upper button** (class `... erTKTR`): rect e.g. `y:197.8–237.8`. **Covered
  by the product `<img class="_3XS7H">` overlay**, so a real mouse click at
  its geometric center hits the IMG, which opens the **item-detail modal**
  (image + description + a non-functional ghost ADD inside). Adding nothing
  to cart.
- **Lower button** (class `... add-button-center-container`): rect
  `y:237.8–275.8`. This is the real interactive control. Clicking it either
  adds the item directly OR opens the customisation wizard
  ("Customise as per your taste") for `Customisable` items.

Always pick the **lower-y candidate**, and verify with `elementFromPoint`
before clicking:

```js
const card = /* parent card element */;
const btns = [...card.querySelectorAll('button')]
  .filter(b => (b.innerText||'').trim() === 'ADD' && b.offsetWidth > 0);
const btn = btns[btns.length - 1];                  // lower one
const r = btn.getBoundingClientRect();
const cx = r.left + r.width/2, cy = r.top + r.height/2;
const hit = document.elementFromPoint(cx, cy);
return {ok: hit.tagName === 'BUTTON', cx, cy};
```

If `ok` is false (hit is an IMG or wrapper DIV), nudge `cy` downward in 4px
steps until `elementFromPoint` returns the `<button>` — that's your click
point.

### Why the item-detail modal looks deceptive

When the wrong ADD is clicked, the resulting modal contains its own
`<button>ADD</button>` and a fake `− 1 +` stepper. None of them mutate the
cart — the modal is read-only (its ancestor is `aria-hidden="true"`).
Clicking these buttons does nothing observable; the cart header stays at
`0\nCart`. Don't keep retrying inside the modal — close it
(`press_key("Escape")` or click the `×` at the top-right of the dialog)
and target the underlying card's lower ADD.

## Search flow

```python
new_tab("https://www.swiggy.com/search")
wait_for_load()

# Type into the first text input on the page (placeholder
# "Search for restaurants and food"). Focus first to avoid mis-routing keys.
js("document.querySelector('input').focus()")
type_text(restaurant_name)
```

Results auto-render under the "Restaurants" tab. Pick the top card whose
text starts with the queried name plus a locality/ETA tag (e.g. `30–35
MINS`, `Frazer Town`, `km`) — that disambiguates from dish results in the
same listing.

Clicking the restaurant card navigates to
`/city/<city>/<slug>-rest<id>`. Save that URL for re-entry.

## Restaurant page → menu search

The in-page dish search is a separate route, not a focusable input on the
menu page. Trigger it by clicking the visible **"Search for dishes"** pill
(it's a plain `<div>`, not a button). After click, the URL/UI flips to a
search header with a real input — placeholder `Search in <Restaurant>` —
which is already focused. `type_text(menu_search_query)` works immediately;
no Enter needed.

## Customisation wizard

Triggered by clicking the (lower) ADD on a `Customisable` card. Modal
title: **`Customise as per your taste`**.

- Header counter `Step N/M` for each required-choice step.
- Required radios per step (`Choose Your Crust`, `Choose Size`,
  `Choose Your Pizza Base`, …). Cheapest option is usually pre-selected;
  verify with a screenshot. Custom-styled, not `input[type=radio]`.
- Footer button: `Continue` while there are more required steps.
- After the last required step, the modal continues into an unnumbered
  add-ons screen (`Extra Cheese Topping`, `Make it a Meal (0/5)`,
  toppings checkboxes, …). Footer text flips to **`Add Item to cart`**.
- Add-ons are optional — **leave unchecked for the cheapest order.**

Driver — poll the footer button text, not the step counter:

```python
import time
while True:
    btn = js("""
    const b = [...document.querySelectorAll('button')]
      .find(e => e.offsetWidth>0 &&
                 /^(Continue|Add Item to cart)$/.test((e.innerText||'').trim()));
    if (!b) return null;
    const r = b.getBoundingClientRect();
    return {t: b.innerText.trim(),
            x: Math.round(r.x + r.width/2),
            y: Math.round(r.y + r.height/2)};
    """)
    if not btn:
        break
    click_at_xy(btn["x"], btn["y"])
    time.sleep(2)
    if btn["t"] == "Add Item to cart":
        break
```

The button's y-coordinate **changes between steps** as the modal grows.
Requery every iteration.

## Confirm the add

After `Add Item to cart` (or a non-customisable direct ADD), expect:

- Cart header → `"1\nCart"` (or N+1).
- A fixed green bottom bar appears: `1 item added ... VIEW CART`.
- Card flips from `ADD` to `− 1 +` stepper.

If the cart header is still `"0\nCart"` after clicking and the modal is
gone, you clicked the wrong ADD (the image overlay swallowed it). Reopen
the card, probe with `elementFromPoint`, and retry on the lower button.

## VIEW CART

`VIEW CART` text lives inside a `<span>` (the wrapping `<div>` covers the
whole viewport — don't click by `endsWith('VIEW CART')` on arbitrary
elements; you'll get a 1470×800 rectangle anchored off-screen). Filter to
the small visible span:

```js
const el = [...document.querySelectorAll('*')]
  .find(e => e.offsetWidth>0 && e.offsetWidth<200 &&
             (e.innerText||'').trim() === 'VIEW CART');
const r = el.getBoundingClientRect();
return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
```

Click → navigates to `/checkout`.

## Address selection

Saved addresses render as cards labelled `A1`, `A2`, … each with a green
**`DELIVER HERE`** action. The action is a `<div>`, not a `<button>` — a
generic `button,a` selector misses it.

```js
const want = "A2";
const hit = [...document.querySelectorAll('*')]
  .filter(e => e.offsetWidth>0 && (e.innerText||'').trim() === 'DELIVER HERE')
  .find(e => {
    let p = e;
    for (let i=0;i<6;i++) {
      p = p.parentElement; if (!p) break;
      if ((p.innerText||'').includes(want)) return true;
    }
    return false;
  });
const r = hit.getBoundingClientRect();
return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
```

Success state: the address card collapses into a confirmed strip with a
`CHANGE` link, and a `Choose payment method` panel with a green
`PROCEED TO PAY` button appears. **Stop here** — handing off payment is
the user's call.

## Gotchas / Traps

- **CSS selector with `/`** — `a[href="/checkout"]` not `a[href=/checkout]`;
  JS throws otherwise.
- **`document.elementFromPoint`** is the cheapest way to detect overlay
  interception. Use it before any "this click looks right but nothing
  happens" theory.
- **Modal ancestor `aria-hidden="true"`** marks read-only preview dialogs
  whose buttons fire no handlers. Don't waste retries clicking them.
- **`VIEW CART`** and **`DELIVER HERE`** are non-`<button>` elements. Width
  filter by `offsetWidth < 200` to avoid catching whole-page wrapper divs.
- **"Next available at HH:MM"** items show a ghost ADD that has no handler
  — pre-order window not open. Stop and ask the user.
- **Image DPR**: page is 1470 CSS px wide; screenshots are 2× Retina.
  Always click using `getBoundingClientRect()` CSS coords, never pixels
  measured off the rendered image.
- **Don't reuse coordinates between actions.** Customisation steps,
  scroll, and modal open/close all shift y. Requery every time.

## Not covered

- Login (OTP / password) — stop and ask the user.
- Adding a new delivery address from checkout.
- Coupon application, payment selection, order placement.
- Dineout flow (`Order Online` tab is the food-delivery path used here).
