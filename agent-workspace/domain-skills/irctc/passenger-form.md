# IRCTC — Passenger Details Form

URL: `https://www.irctc.co.in/nget/booking/psgninput`

Reached by clicking **Book Now** on a train card with class+date selected.
If logged out, the page renders but a login modal (a `p-dialog` containing
`input[placeholder="User Name"]`) overlays it. Verify before filling.

Field-tested 2026-05-08.

## Page structure

Each passenger row is one `<app-passenger>` element. The first row is
present on initial render; clicking **+ Add Passenger** appends another.

```
app-passenger          ← one per passenger
  p-autocomplete[formcontrolname="passengerName"]
    input[placeholder="Name", maxlength="16"]   ← name
  input[formcontrolname="passengerAge", type="number", max="125"]   ← age
  select[formcontrolname="passengerGender"]                          ← gender
  select[formcontrolname="passengerNationality"]                     ← nationality
  select[formcontrolname="passengerBerthChoice"]                     ← berth pref
  select[formcontrolname="passengerFoodChoice"]                      ← food pref (optional)
```

## Field-by-field

### Name

Wrapper is a PrimeNG autocomplete (suggestions come from "master passenger
list" — names previously saved on the account). Free-form text is
accepted. Maxlength is 16.

```js
const inp = document.querySelector(
  'app-passenger p-autocomplete[formcontrolname="passengerName"] input');
inp.focus();
```

Then `type_text(name)`. Press Escape afterwards to dismiss the suggestion
panel — without it, the next click can land on a suggestion `li` and
overwrite the typed value.

### Age

Native `<input type="number">`. Bounds: `min=1`, `max=125`. Set via real
typing (`type_text`) so Angular reactive-form validators register it.
Setting `.value =` directly is **not enough** — the form stays `ng-pristine`
and the row fails validation on Continue.

```js
const inp = document.querySelector(
  'app-passenger input[formcontrolname="passengerAge"]');
inp.focus(); inp.value = '';
```

Then `type_text(str(age))`.

### Gender

Native `<select>` with three options:

| Value | Label       |
|-------|-------------|
| `M`   | Male        |
| `F`   | Female      |
| `T`   | Transgender |

Set via value + dispatch `input` and `change` for Angular to pick it up.
Don't try clicking — it's not a `p-dropdown`.

```js
const sel = document.querySelector(
  'app-passenger select[formcontrolname="passengerGender"]');
sel.value = 'M';
sel.dispatchEvent(new Event('input',  {bubbles: true}));
sel.dispatchEvent(new Event('change', {bubbles: true}));
```

### Nationality / Berth / Food

All native `<select>` with sensible defaults (India / No Preference /
default catering). Skip unless the user explicitly asked.

## Login modal detection

The login modal is a `p-dialog` that overlays `/booking/psgninput` when
the session is not authenticated. It has **no stable id** (`#loginModal`
is a stale legacy selector — current build does not use it). Detect by:

```js
const u = document.querySelector('input[placeholder="User Name"]');
const loginVisible = !!(u && u.offsetParent !== null);
```

If `loginVisible` is true on `/booking/psgninput`, do not try to fill the
passenger form — the inputs underneath are inert until the modal closes.

## Confirm dialog after Book Now

Trains whose terminal differs from the searched destination (e.g. Mumbai
Rajdhani lists destination NZM/Hazrat Nizamuddin even when searched for
NDLS/New Delhi) show a `.ui-confirmdialog` between the train list and
`/booking/psgninput`:

> You searched trains from BPL (BHOPAL) to NDLS (NEW DELHI) but booking
> from BPL to NZM. Do you want to continue with it?

Auto-accept by clicking the accept button — no user action needed:

```js
document.querySelector('.ui-confirmdialog button.ui-confirmdialog-acceptbutton').click();
```

## Quirks

- **`<select>` for gender**, not `p-dropdown`. Don't go looking for
  `li.ui-dropdown-item Male` — there isn't one.
- **`maxlength="16"` on Name.** Names longer than 16 chars are silently
  truncated; truncate in the script too.
- **Autocomplete suggestions on Name** can hijack the click that comes
  next. Press Escape after typing.
- **`#loginModal` is gone.** Use `input[placeholder="User Name"]` for the
  visible login modal instead.
- **Don't `goto_url` back to `/train-search` to retry.** A full reload
  to the search route drops the IRCTC session (see search-form.md).
  Recover via SPA navigation (in-page TRAINS link / browser Back) or
  have the user re-login in the same tab.
- **Reactive-form pristine flag.** Setting `.value =` on the age/name
  inputs without firing real key events leaves the form `ng-pristine`,
  which rejects Continue. Always use `type_text` (which dispatches keys
  through the CDP), not raw `.value =`.

## End-to-end

`booking_fsm.py` next to this file packages search → train pick → class +
date → Book Now → passenger fill into one entry point:

```bash
browser-harness -c '
exec(open("agent-workspace/domain-skills/irctc/booking_fsm.py").read())
import json
r = book_ticket("BPL, NDLS, 2026-05-25, Anshul Jain, M, 30, 22691, 3A")
print(json.dumps(r, indent=2))
'
```

Stops after the passenger row is filled — does **not** click Continue.

If a login modal appears after Book Now, the FSM prints a prompt and
blocks on `input()` until the user logs in (in the browser) and presses
Enter once they're on the passenger details page.
