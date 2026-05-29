"""Blinkit grocery-cart FSM. Domain-skill scoped — load with:

    browser-harness -c '
    exec(open("agent-workspace/domain-skills/blinkit/cart_fsm.py").read())
    result = add_groceries("tomato, potato, amul masti 1L")
    print(result)
    '

Core helpers (js, cdp, ask_llm, new_tab, goto_url, wait_for_load) come from
the harness globals via exec — this file does not import them.

Returns a progress dict: {"status": "success"|"failed", "progress": [...], "error": str?}.
On failure the loop aborts; un-reached items stay status="pending" so a follow-up
agent can re-invoke add_groceries with just those items.
"""

import json as _json
import os as _os
import random as _random
import time as _time
import urllib.parse as _urlparse


# FSM states (also used as the `state` field on each progress entry)
_S_SEARCH = "SEARCH"
_S_WAIT = "WAIT_RESULTS"
_S_PARSE = "PARSE"
_S_DECIDE = "DECIDE"
_S_LOCATE = "LOCATE"
_S_CLICK = "CLICK"
_S_VERIFY = "VERIFY"
_S_DONE = "DONE"


_CARD_PARSER_JS = r"""
const out = [];
document.querySelectorAll('[role="button"][id]').forEach(el => {
  if (!/^\d+$/.test(el.id)) return;
  const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (!lines.includes('ADD') && !lines.some(l => /^\d+$/.test(l))) return;
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
"""


_LOCATE_ADD_JS = r"""
const card = document.getElementById(__PRODUCT_ID__);
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
"""


def _wait_for_results(timeout=15.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
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
            return n
        _time.sleep(0.3)
    return 0


def _parse_search_cards():
    return js(_CARD_PARSER_JS) or []


def _slow_click(x, y, jitter=5, steps=3):
    tx = int(x + _random.uniform(-jitter, jitter))
    ty = int(y + _random.uniform(-jitter, jitter))
    sx = tx + _random.randint(-120, 120)
    sy = ty + _random.randint(-120, 120)
    for i in range(1, steps + 1):
        t = i / steps
        ix = int(sx + (tx - sx) * t + _random.uniform(-2, 2))
        iy = int(sy + (ty - sy) * t + _random.uniform(-2, 2))
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=ix, y=iy)
        _time.sleep(_random.uniform(0.02, 0.08))
    cdp("Input.dispatchMouseEvent", type="mouseMoved", x=tx, y=ty)
    _time.sleep(_random.uniform(0.04, 0.12))
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=tx, y=ty, button="left", clickCount=1)
    _time.sleep(_random.uniform(0.04, 0.12))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=tx, y=ty, button="left", clickCount=1)


def _llm_pick(item, candidates):
    schema = {
        "type": "object",
        "properties": {
            "chosen_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["chosen_id", "reason"],
    }
    prompt = (
        f"User wants to buy: {item!r}.\n"
        "Pick the single best matching product id from the candidates below. "
        "If none is a reasonable match, return the string 'none' as chosen_id. "
        "Prefer common brands, sensible pack size matching what the user asked for, "
        "and avoid combos/multipacks unless the user explicitly asked for one. "
        "Return the candidate's `id` field exactly as given.\n\n"
        f"Candidates (JSON):\n{_json.dumps(candidates, indent=2)}"
    )
    res = ask_llm(prompt, schema)
    chosen = (res.get("chosen_id") or "").strip()
    if chosen.lower() in ("", "none", "null"):
        return None, res.get("reason", "")
    return chosen, res.get("reason", "")


def _is_in_cart(product_id):
    return bool(js(f"""
const card = document.getElementById({_json.dumps(str(product_id))});
if (!card) return false;
return !!card.querySelector('.icon-plus');
"""))


def _locate_add(product_id):
    return js(_LOCATE_ADD_JS.replace("__PRODUCT_ID__", _json.dumps(str(product_id))))


def add_groceries(items_csv):
    """Search Blinkit for each item, AI-pick the best match, add it to the cart.

    Assumes Blinkit is logged in with a delivery address set.

    Returns:
        {
          "status": "success" | "failed",
          "progress": [{"item", "status", "state", ...}, ...],
          "error": str (only when status == "failed"),
        }
    """
    items = [s.strip() for s in items_csv.split(",") if s.strip()]
    progress = [{"item": it, "status": "pending", "state": _S_SEARCH} for it in items]

    if not _os.environ.get("GEMINI_API_KEY"):
        return {
            "status": "failed",
            "progress": progress,
            "error": "GEMINI_API_KEY not set in env",
        }

    first_error = None
    first = True

    def fail(entry, state, msg):
        entry["status"] = "failed"
        entry["state"] = state
        entry["error"] = msg

    for entry in progress:
        item = entry["item"]
        try:
            # SEARCH
            entry["state"] = _S_SEARCH
            url = f"https://blinkit.com/s/?q={_urlparse.quote(item)}"
            print(f"[search] {item} -> {url}")
            if first:
                new_tab(url)
                first = False
            else:
                goto_url(url)
            wait_for_load()
            _time.sleep(1.5)

            # WAIT_RESULTS
            entry["state"] = _S_WAIT
            n = _wait_for_results(timeout=15.0)
            if n == 0:
                fail(entry, _S_WAIT, "no results — delivery address not set or hydration timeout")
                first_error = first_error or f"{item} failed at {_S_WAIT}: {entry['error']}"
                break

            js("window.scrollTo(0, 600)")
            _time.sleep(_random.uniform(0.4, 0.8))
            js("window.scrollTo(0, 0)")
            _time.sleep(_random.uniform(0.4, 0.8))

            # PARSE
            entry["state"] = _S_PARSE
            candidates = _parse_search_cards()
            if not candidates:
                fail(entry, _S_PARSE, "parser returned 0 cards despite results visible")
                first_error = first_error or f"{item} failed at {_S_PARSE}: {entry['error']}"
                break
            candidates = candidates[:20]

            # DECIDE
            entry["state"] = _S_DECIDE
            try:
                chosen_id, reason = _llm_pick(item, candidates)
            except Exception as e:
                fail(entry, _S_DECIDE, f"gemini error: {e}")
                first_error = first_error or f"{item} failed at {_S_DECIDE}: {entry['error']}"
                break

            if chosen_id is None:
                entry["status"] = "skipped"
                entry["state"] = _S_DECIDE
                entry["reason"] = reason or "no good match"
                print(f"[skip] {item}: {entry['reason']}")
                continue

            if not any(c["id"] == chosen_id for c in candidates):
                fail(entry, _S_DECIDE, f"gemini returned id {chosen_id!r} not in candidate list")
                first_error = first_error or f"{item} failed at {_S_DECIDE}: {entry['error']}"
                break

            entry["chosen_id"] = chosen_id
            print(f"[pick] {item} -> id={chosen_id} ({reason})")

            if _is_in_cart(chosen_id):
                entry["status"] = "done"
                entry["state"] = _S_DONE
                print(f"[ok] {item}: already in cart")
                continue

            # LOCATE → CLICK → VERIFY (up to 3 tries)
            ok = False
            last_err = None
            for attempt in range(3):
                entry["state"] = _S_LOCATE
                loc = _locate_add(chosen_id)
                if not loc or not loc.get("found"):
                    last_err = f"locate failed: {loc}"
                    print(f"[retry {attempt}] {item}: {last_err}")
                    _time.sleep(_random.uniform(0.6, 1.4))
                    continue
                _time.sleep(_random.uniform(0.4, 0.8))
                entry["state"] = _S_CLICK
                _slow_click(loc["x"], loc["y"])
                _time.sleep(_random.uniform(1.6, 3.2))
                entry["state"] = _S_VERIFY
                if _is_in_cart(chosen_id):
                    ok = True
                    break
                last_err = "stepper did not appear after click"
                print(f"[retry {attempt}] {item}: {last_err}")
                _time.sleep(_random.uniform(0.6, 1.4))

            if ok:
                entry["status"] = "done"
                entry["state"] = _S_DONE
                print(f"[added] {item}")
                _time.sleep(_random.uniform(0.6, 1.4))
            else:
                fail(entry, entry["state"], last_err or "add failed after 3 attempts")
                first_error = first_error or f"{item} failed at {entry['state']}: {entry['error']}"
                break

        except Exception as e:
            fail(entry, entry["state"], f"{type(e).__name__}: {e}")
            first_error = first_error or f"{item} failed at {entry['state']}: {entry['error']}"
            break

    has_failure = any(p["status"] == "failed" for p in progress)
    return {
        "status": "failed" if has_failure else "success",
        "progress": progress,
        **({"error": first_error} if has_failure else {}),
    }
