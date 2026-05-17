"""IRCTC end-to-end booking FSM. Domain-skill scoped — load with:

    browser-harness -c '
    exec(open("agent-workspace/domain-skills/irctc/booking_fsm.py").read())
    result = book_ticket("BPL, NDLS, 2026-05-25, Anshul Jain, M, 30, 12951, 3A")
    print(result)
    '

Core helpers (js, cdp, type_text, press_key, new_tab, goto_url, wait_for_load,
page_info, list_tabs, switch_tab, ask_gemini) come from harness globals via
exec — this file does not import them.

Input CSV: from, to, date, passenger_name, sex, age, train, class
All 8 comma-separated fields required, but `train` and/or `class` may be
left empty — when missing, after the train list renders the FSM calls
`ask_gemini` to make a schema-constrained selection from the live options.
Requires `GEMINI_API_KEY` in env. Quota always GENERAL.

    "BPL, NDLS, 2026-05-25, Anshul Jain, M, 30, , 3A"   # infer train
    "BPL, NDLS, 2026-05-25, Anshul Jain, M, 30, 12951, " # infer class
    "BPL, NDLS, 2026-05-25, Anshul Jain, M, 30, , "      # infer both

Multiple passengers: pipe-separate the name/sex/age fields. The three
lists must be equal length, max 6 (IRCTC General-quota cap). Example —
two passengers in one call:

    "BPL, NDLS, 2026-07-01, Prakhar Jain|Anshul Jain, M|M, 28|30, 12001, CC"

If a login modal pops up after Book Now, the script prints a prompt and
blocks on `input()` until the user logs in (in the browser) and presses
Enter on the passenger page.

Session invariants (see search-form.md): IRCTC stores auth in per-tab
sessionStorage, so the FSM (a) never opens a new tab — caller must open
IRCTC and log in manually first, (b) pins to the IRCTC tab whose body
shows "Welcome <name>", and (c) skips `goto_url` to /train-search when
already on that route, since a full reload there drops the login.

Returns:
    {"status": "success"|"failed",
     "state": <FSM state>, "error"?: str, "details": {...}}
"""

import json as _json
import re as _re
import time as _time
from datetime import date as _date


# FSM states
_S_PARSE = "PARSE"
_S_SEARCH = "SEARCH_FORM"
_S_WAIT = "WAIT_RESULTS"
_S_PICK_TRAIN = "PICK_TRAIN"
_S_PICK_CLASS = "PICK_CLASS"
_S_CLICK_DATE = "CLICK_DATE"
_S_BOOK_NOW = "BOOK_NOW"
_S_LOGIN = "LOGIN_CHECK"
_S_PSGN = "PASSENGER_FORM"
_S_DONE = "DONE"


_MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# Lifted from search-form.md — defaults for city-name inputs.
_CITY_DEFAULTS = {
    "bhopal": "BPL",
    "delhi": "NDLS", "new delhi": "NDLS",
    "mumbai": "CSMT", "mumbai cst": "CSMT", "mumbai vt": "CSMT",
    "mumbai central": "MMCT",
    "bangalore": "SBC", "bengaluru": "SBC",
    "chennai": "MAS",
    "kolkata": "HWH",
    "hyderabad": "HYB",
    "pune": "PUNE",
    "ahmedabad": "ADI",
}


_VALID_CLASS_CODES = {"EA", "1A", "EC", "2A", "FC", "3A", "3E", "CC", "SL", "2S"}


def _parse_input(s):
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 8:
        raise ValueError(
            f"expected 8 csv fields (from,to,date,name,sex,age,train,class), got {len(parts)}"
        )
    frm, to, date_s, name, sex, age_s, train, klass = parts
    for label, val in [("from", frm), ("to", to), ("date", date_s),
                       ("passenger_name", name), ("sex", sex), ("age", age_s)]:
        if not val:
            raise ValueError(f"empty field: {label}")

    # Date — accept YYYY-MM-DD or DD/MM/YYYY
    d = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            from datetime import datetime
            d = datetime.strptime(date_s, fmt).date()
            break
        except ValueError:
            continue
    if d is None:
        raise ValueError(f"date {date_s!r}: use YYYY-MM-DD or DD/MM/YYYY")

    # Passengers — name/sex/age may be pipe-separated for multiple riders.
    # The three lists must be equal length and ≤ 6 (IRCTC General-quota cap).
    sex_map = {"m": "M", "male": "M", "f": "F", "female": "F",
               "t": "T", "trans": "T", "transgender": "T"}
    names = [p.strip() for p in name.split("|")]
    sexes_raw = [p.strip() for p in sex.split("|")]
    ages_raw = [p.strip() for p in age_s.split("|")]
    if not (len(names) == len(sexes_raw) == len(ages_raw)):
        raise ValueError(
            f"passenger lists must be equal length: "
            f"names={len(names)} sexes={len(sexes_raw)} ages={len(ages_raw)}"
        )
    if len(names) > 6:
        raise ValueError(f"too many passengers ({len(names)}): IRCTC caps at 6")
    passengers = []
    for i, (nm, sx_raw, ag_raw) in enumerate(zip(names, sexes_raw, ages_raw)):
        if not nm:
            raise ValueError(f"passenger #{i+1}: empty name")
        sx = sx_raw.lower()
        if sx not in sex_map:
            raise ValueError(
                f"passenger #{i+1} sex {sx_raw!r}: expected M/F/T (or male/female/trans)"
            )
        try:
            ag = int(ag_raw)
        except ValueError:
            raise ValueError(f"passenger #{i+1} age {ag_raw!r}: not an integer")
        if ag < 1 or ag > 125:
            raise ValueError(f"passenger #{i+1} age {ag!r}: out of range")
        passengers.append({"name": nm, "sex": sex_map[sx], "age": ag})

    # Class — empty means ask_gemini will choose from the rendered train list.
    klass_u = klass.strip().upper()
    if klass_u and klass_u not in _VALID_CLASS_CODES:
        raise ValueError(
            f"class {klass!r}: expected one of {sorted(_VALID_CLASS_CODES)}"
        )

    # Stations — resolve city → code if a known city was given.
    def _norm_station(x):
        u = x.strip().upper()
        # Already looks like a code? (2-5 letters, all caps after upper())
        if _re.fullmatch(r"[A-Z]{2,5}", u):
            return u, "code"
        lower = x.strip().lower()
        if lower in _CITY_DEFAULTS:
            return _CITY_DEFAULTS[lower], "city"
        return x.strip(), "freeform"

    frm_code, frm_kind = _norm_station(frm)
    to_code, to_kind = _norm_station(to)

    return {
        "from": frm_code, "from_kind": frm_kind,
        "to": to_code, "to_kind": to_kind,
        "date": d,
        "passengers": passengers,
        "train": train,
        "class": klass_u,
    }


def _autocomplete_pick(field_id, query, kind):
    """Type into a p-autocomplete and pick the right li."""
    js(f"(() => {{ document.querySelector('p-autocomplete#{field_id} input').focus(); }})()")
    js(f"(() => {{ document.querySelector('p-autocomplete#{field_id} input').value = ''; }})()")
    type_text(query)
    _time.sleep(0.9)
    if kind in ("code", "city"):
        ok = js(r"""(() => {
const code = __CODE__;
const items = Array.from(document.querySelectorAll('li.ui-autocomplete-list-item'));
const re = new RegExp(' - ' + code + '\\b');
const li = items.find(el => re.test(el.innerText));
if (li) { li.click(); return true; }
return false;
})()""".replace("__CODE__", _json.dumps(query)))
        if not ok:
            raise RuntimeError(f"no autocomplete row matched code {query!r}")
    else:
        ok = js(r"""(() => {
const items = Array.from(document.querySelectorAll('li.ui-autocomplete-list-item'));
let inStations = false;
for (const el of items) {
  const t = (el.innerText || '').trim();
  if (/-+\s*Stations\s*-+/i.test(t)) { inStations = true; continue; }
  if (/-+\s*Journeys\s*-+/i.test(t)) { inStations = false; continue; }
  if (inStations && t) { el.click(); return true; }
}
return false;
})()""")
        if not ok:
            raise RuntimeError(f"no station row for {query!r}")


def _open_calendar_and_pick(d):
    js("document.querySelector('p-calendar#jDate input').click()")
    _time.sleep(0.4)
    target = f"{_MONTHS[d.month - 1]}{d.year}"  # e.g. "May2026"
    for _ in range(14):
        header = js("return (document.querySelector('.ui-datepicker-title')||{}).innerText || ''")
        if header.replace(" ", "") == target:
            break
        js("document.querySelector('.ui-datepicker-next').click()")
        _time.sleep(0.25)
    else:
        raise RuntimeError(f"date {target} beyond IRCTC ARP horizon")
    ok = js(r"""
const day = __DAY__;
const cell = Array.from(document.querySelectorAll('.ui-datepicker-calendar td a'))
  .find(a => a.innerText.trim() === String(day));
if (cell) { cell.click(); return true; }
return false;
""".replace("__DAY__", str(d.day)))
    if not ok:
        raise RuntimeError(f"day {d.day} not found in calendar grid")


def _wait_for_train_cards(timeout=15.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        n = js("return document.querySelectorAll('app-train-avl-enq').length")
        if n and n > 0:
            return n
        _time.sleep(0.4)
    return 0


_PARSE_TRAINS_JS = r"""
const out = [];
document.querySelectorAll('app-train-avl-enq').forEach((card, idx) => {
  const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (!lines.length) return;
  // First line: "TRAIN NAME (NUMBER)"
  const head = lines[0];
  const m = head.match(/^(.*)\s*\((\d{4,5})\)\s*$/);
  const name = m ? m[1].trim() : head;
  const number = m ? m[2] : '';
  // Runs On line
  const runs = (lines.find(l => /^Runs On:/i.test(l)) || '').replace(/^Runs On:\s*/i, '');
  // Departure / Arrival / Duration — lifted from sequential lines around dep/arr times.
  const timeRe = /^\d{1,2}:\d{2}/;
  const times = lines.filter(l => timeRe.test(l));
  const dep = times[0] || '';
  const arr = times[1] || '';
  const durLine = lines.find(l => /\bh\b/i.test(l) && /\bm\b/i.test(l)) || '';
  // Class boxes: each div.pre-avl directly inside the card has "Label (CODE)\nFare\nAvail"
  const classes = [];
  card.querySelectorAll('div.pre-avl').forEach(box => {
    const t = (box.innerText || '').trim();
    const cm = t.match(/\(([A-Z0-9]{2,3})\)/);
    if (!cm) return;  // skip date cells which don't have (CODE)
    const code = cm[1];
    const parts = t.split('\n').map(s => s.trim()).filter(Boolean);
    const fare = parts.find(p => /^₹/.test(p)) || '';
    const avail = parts.find(p => /^(AVL|RAC|WL|GNWL|PQWL|RLWL|REGRET|TRAIN\s+CANCELLED|NOT\s+AVAILABLE|BOOKING\s+CLOSED|CHART\s+PREPARED|RUNNING\s+STATUS)/i.test(p)) || '';
    classes.push({code, fare, avail});
  });
  out.push({index: idx, number, name, dep, arr, duration: durLine, runs_on: runs, classes});
});
return out;
"""


def _parse_train_cards():
    return js(_PARSE_TRAINS_JS) or []


def _match_train(cards, train_arg):
    # Number match preferred.
    num_matches = [c for c in cards if c["number"] == train_arg.strip()]
    if num_matches:
        return num_matches
    needle = train_arg.strip().lower()
    return [c for c in cards if needle in c["name"].lower()]


def _pick_train_with_gemini(cards, cfg):
    """Use ask_gemini to choose which train to book from the live options.

    Returns the chosen train_number (string). Raises RuntimeError if the
    model returns a number that isn't on any card.
    """
    options = [{"number": c["number"], "name": c["name"], "dep": c["dep"],
                "arr": c["arr"], "duration": c["duration"], "runs_on": c["runs_on"],
                "classes": c["classes"]}
               for c in cards]
    prompt = (
        f"The user wants to book a ticket from {cfg['from']} to {cfg['to']} on "
        f"{cfg['date'].isoformat()} but did NOT specify which train. "
        "Below is the live list of trains parsed from IRCTC for this route+date. "
        "Choose the most appropriate train from the provided options using only "
        "the route, date, schedule, availability, and fare data available here. "
        "Return the matching train_number exactly as it appears in the options. "
        "If the available data does not justify a defensible choice, return an "
        "empty string and explain why in reason.\n\n"
        f"Options (JSON):\n{_json.dumps(options, indent=2)}"
    )
    schema = {"type": "object",
              "properties": {"train_number": {"type": "string"},
                             "reason": {"type": "string"}},
              "required": ["train_number", "reason"]}
    pick = ask_gemini(prompt, schema, thinking="low")
    num = (pick.get("train_number") or "").strip()
    if not any(c["number"] == num for c in cards):
        raise RuntimeError(
            f"gemini returned train_number {num!r} which is not on the train list"
        )
    return num, pick.get("reason", "")


def _pick_class_with_gemini(chosen_card, cfg):
    """Use ask_gemini to choose a class on the already-chosen train.

    Returns the chosen class_code. Raises RuntimeError if the model returns
    a code that isn't offered on this card or isn't a valid IRCTC class.
    """
    classes = chosen_card["classes"]
    if not classes:
        raise RuntimeError(
            f"train {chosen_card['number']} has no class boxes on the card"
        )
    prompt = (
        f"The user wants to book {chosen_card['number']} ({chosen_card['name']}) "
        f"from {cfg['from']} to {cfg['to']} on {cfg['date'].isoformat()} but did "
        "NOT specify which class. Choose the most appropriate class from the "
        "provided options using only class code, fare, and availability. Return "
        "the matching class_code exactly as it appears in the options. If the "
        "available data does not justify a defensible choice, return an empty "
        "string and explain why in reason.\n\n"
        f"Classes (JSON):\n{_json.dumps(classes, indent=2)}"
    )
    schema = {"type": "object",
              "properties": {"class_code": {"type": "string"},
                             "reason": {"type": "string"}},
              "required": ["class_code", "reason"]}
    pick = ask_gemini(prompt, schema, thinking="low")
    code = (pick.get("class_code") or "").strip().upper()
    if code not in _VALID_CLASS_CODES:
        raise RuntimeError(
            f"gemini returned class_code {code!r} which is not a valid IRCTC class"
        )
    if not any(c["code"] == code for c in classes):
        raise RuntimeError(
            f"gemini returned class_code {code!r} which is not on train "
            f"{chosen_card['number']} (offered: {[c['code'] for c in classes]})"
        )
    return code, pick.get("reason", "")


def _click_class_box(card_index, class_code):
    return js(r"""
const card = document.querySelectorAll('app-train-avl-enq')[__IDX__];
if (!card) return {ok: false, reason: 'card not found'};
const re = new RegExp('\\(' + __CODE_JSON__ + '\\)');
const box = Array.from(card.querySelectorAll('div.pre-avl'))
  .find(d => re.test(d.innerText));
if (!box) {
  const codes = Array.from(card.querySelectorAll('div.pre-avl'))
    .map(d => (d.innerText.match(/\(([A-Z0-9]{2,3})\)/) || [,''])[1])
    .filter(Boolean);
  return {ok: false, reason: 'class not on card', available: codes};
}
box.scrollIntoView({block: 'center'});
box.click();
return {ok: true};
""".replace("__IDX__", str(card_index)).replace("__CODE_JSON__", _json.dumps(class_code)))


def _wait_date_row(card_index, timeout=8.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        n = js(f"""
const card = document.querySelectorAll('app-train-avl-enq')[{card_index}];
if (!card) return 0;
return card.querySelectorAll('td.link.ng-star-inserted').length;
""")
        if n and n > 0:
            return n
        _time.sleep(0.3)
    return 0


def _click_date_cell(card_index, d):
    label = d.strftime("%a, %d %b")  # e.g. "Sun, 25 May"
    # Strip leading zero on day for resilience (some locales differ).
    alt_label = f"{d.strftime('%a')}, {d.day} {d.strftime('%b')}"
    res = js(r"""
const card = document.querySelectorAll('app-train-avl-enq')[__IDX__];
if (!card) return {ok: false, reason: 'card gone'};
const labels = __LABELS__;
const tds = Array.from(card.querySelectorAll('td.link.ng-star-inserted'));
const td = tds.find(t => labels.some(l => t.innerText.includes(l)));
if (!td) {
  return {ok: false, reason: 'date cell not found',
          available: tds.map(t => t.innerText.split('\n')[0].trim())};
}
const inner = td.querySelector('div.pre-avl');
if (!inner) return {ok: false, reason: 'inner pre-avl missing'};
inner.scrollIntoView({block: 'center'});
inner.click();
return {ok: true, selected: inner.classList.contains('selected-class')};
""".replace("__IDX__", str(card_index)).replace("__LABELS__", _json.dumps([label, alt_label])))
    return res


def _book_now(card_index, timeout=5.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        ready = js(f"""
const card = document.querySelectorAll('app-train-avl-enq')[{card_index}];
if (!card) return false;
const btn = card.querySelector('button.train_Search');
return !!btn && !btn.classList.contains('disable-book');
""")
        if ready:
            break
        _time.sleep(0.3)
    else:
        return False
    js(f"""
const card = document.querySelectorAll('app-train-avl-enq')[{card_index}];
const btn = card.querySelector('button.train_Search');
btn.scrollIntoView({{block: 'center'}});
btn.click();
return null;
""")
    return True


def _peek_state():
    return js(r"""
const url = location.href;
const userInput = document.querySelector('input[placeholder="User Name"]');
const loginVisible = !!(userInput && userInput.offsetParent !== null);
const confirmBtn = document.querySelector('.ui-confirmdialog button.ui-confirmdialog-acceptbutton');
const confirmVisible = !!(confirmBtn && confirmBtn.offsetParent !== null);
return {url, loginVisible, confirmVisible};
""")


def _accept_confirm_if_present():
    state = _peek_state()
    if state.get("confirmVisible"):
        js("(()=>{document.querySelector('.ui-confirmdialog button.ui-confirmdialog-acceptbutton').click();})()")
        _time.sleep(0.8)
        return True
    return False


def _post_booknow_outcome(timeout=20.0):
    """After Book Now, drive the page to /booking/psgninput.

    IRCTC transitions after Book Now:
      - logged out → login modal pops over train-list.
      - logged in, same terminal → /booking/psgninput.
      - logged in, terminal mismatch → confirm dialog → Yes →
        /booking/psgninput.

    This call:
      - Auto-accepts the confirm dialog if it appears.
      - If a login modal appears, prints a prompt and blocks on `input()`
        until the user logs in and presses Enter on the passenger page.
      - Returns "psgn" when on /booking/psgninput, or "timeout" otherwise.
    """
    deadline = _time.time() + timeout
    login_handled = False
    while _time.time() < deadline:
        state = _peek_state()
        if "/booking/psgninput" in (state.get("url") or "") and not state.get("loginVisible"):
            return "psgn"
        if state.get("confirmVisible"):
            _accept_confirm_if_present()
            continue
        if state.get("loginVisible") and not login_handled:
            print(
                "[login] IRCTC login modal opened.\n"
                "        Please log in in the browser. Once you reach the\n"
                "        passenger details page, return here and press Enter."
            )
            try:
                input()
            except EOFError:
                pass
            login_handled = True
            # Reset the deadline — user just took some time to log in.
            deadline = _time.time() + timeout
            # Some logins land on train-list with the confirm dialog still
            # pending; loop will catch that on the next pass.
            continue
        _time.sleep(0.4)
    return "timeout"


def _add_passenger_row():
    """Click the `+ Add Passenger` anchor on /booking/psgninput.

    The anchor has no stable id/class — match by visible text, scoped to
    `app-passenger-input`. Returns True if a new `app-passenger` rendered,
    False if the click was a no-op (likely the 6-passenger cap).
    See passenger-form.md > "Adding more passengers".
    """
    before = js("return document.querySelectorAll('app-passenger').length")
    js(r"""(() => {
  const a = Array.from(document.querySelectorAll('app-passenger-input a'))
    .find(el => /^\+\s*Add Passenger\b/i.test((el.innerText||'').trim())
                 && el.offsetParent !== null);
  if (a) a.click();
})()""")
    deadline = _time.time() + 3
    while _time.time() < deadline:
        n = js("return document.querySelectorAll('app-passenger').length")
        if n > before:
            return True
        _time.sleep(0.2)
    return False


def _fill_passenger_row(idx, name, sex, age):
    """Fill app-passenger[idx]. Selectors per passenger-form.md."""
    js(r"""
const rows = document.querySelectorAll('app-passenger');
const row = rows[__IDX__];
if (!row) return false;
const inp = row.querySelector('p-autocomplete[formcontrolname="passengerName"] input');
inp.scrollIntoView({block: 'center'});
inp.focus(); inp.value = '';
return true;
""".replace("__IDX__", str(idx)))
    _time.sleep(0.2)
    type_text(name[:16])  # respect maxlength
    _time.sleep(0.4)
    press_key("Escape")
    _time.sleep(0.2)

    js(r"""
const row = document.querySelectorAll('app-passenger')[__IDX__];
const inp = row.querySelector('input[formcontrolname="passengerAge"]');
inp.scrollIntoView({block: 'center'});
inp.focus(); inp.value = '';
return null;
""".replace("__IDX__", str(idx)))
    _time.sleep(0.2)
    type_text(str(age))
    _time.sleep(0.2)

    ok = js(r"""
const row = document.querySelectorAll('app-passenger')[__IDX__];
const sel = row.querySelector('select[formcontrolname="passengerGender"]');
if (!sel) return false;
sel.scrollIntoView({block: 'center'});
sel.value = __VAL__;
sel.dispatchEvent(new Event('input',  {bubbles: true}));
sel.dispatchEvent(new Event('change', {bubbles: true}));
return sel.value === __VAL__;
""".replace("__IDX__", str(idx)).replace("__VAL__", _json.dumps(sex)))
    if not ok:
        raise RuntimeError(f"row {idx}: gender select rejected value {sex!r}")


def _fill_passenger_form(passengers):
    """Fill all passenger rows on /booking/psgninput.

    `passengers` is a list of {name, sex, age} dicts (1–6 entries). Adds
    rows via `+ Add Passenger` as needed; raises if IRCTC caps the click.
    """
    deadline = _time.time() + 15
    while _time.time() < deadline:
        if js("return !!document.querySelector('app-passenger')"):
            break
        _time.sleep(0.3)
    else:
        raise RuntimeError("passenger form never rendered")

    for i, p in enumerate(passengers):
        if i > 0:
            if not _add_passenger_row():
                raise RuntimeError(
                    f"+ Add Passenger click did not append row {i+1} "
                    f"(IRCTC caps at 6 per booking)"
                )
        _fill_passenger_row(i, p["name"], p["sex"], p["age"])


def book_ticket(input_csv):
    """Search IRCTC for the given route+date, pick a specific train+class,
    click Book Now, and (if logged in) fill the first passenger row.

    Returns:
        {"status": "success"|"failed"|"login_required",
         "state": <state>, "details": {...}, "error"?: str}
    """
    details = {}
    try:
        cfg = _parse_input(input_csv)
        details["parsed"] = {**cfg, "date": cfg["date"].isoformat()}
    except Exception as e:
        return {"status": "failed", "state": _S_PARSE, "error": str(e)}

    state = _S_SEARCH
    try:
        state = _S_SEARCH
        # IRCTC binds login to a single tab (sessionStorage-based). Never open
        # a new tab and never switch between IRCTC tabs mid-flow — both drop
        # the login. Find the one IRCTC tab with an active session and pin
        # everything to it.
        url = "https://www.irctc.co.in/nget/train-search"
        irctc_tabs = [t for t in (list_tabs() or [])
                      if "irctc.co.in" in (t.get("url") or "")]
        if not irctc_tabs:
            return {"status": "failed", "state": state,
                    "error": "No IRCTC tab open. Open https://www.irctc.co.in/nget/train-search "
                             "and log in manually before running this script.",
                    "details": details}

        logged_in_tab = None
        for t in irctc_tabs:
            switch_tab(t["targetId"])
            _time.sleep(0.3)
            li = js("return !!(document.body && /Welcome\\s+\\S/.test(document.body.innerText) "
                    "&& !/LOGIN\\/SIGN UP/i.test(document.body.innerText))")
            if li:
                logged_in_tab = t
                break
        if not logged_in_tab:
            return {"status": "failed", "state": state,
                    "error": "Found IRCTC tab(s) but none are logged in. Log in in the browser "
                             "(do not open extra tabs — IRCTC drops session across tabs) and retry.",
                    "details": {**details, "irctc_tabs": [t["url"] for t in irctc_tabs]}}

        # IRCTC drops login on full reloads (Page.navigate). Only navigate if
        # we are not already on /train-search; in that case fill the existing
        # form in place. Reset From/To inputs so stale values from a previous
        # run don't bleed in.
        cur_url = (page_info() or {}).get("url", "")
        if "/nget/train-search" not in cur_url:
            goto_url(url)
            wait_for_load()
            _time.sleep(1.0)
            still_in = js("return !!(document.body && /Welcome\\s+\\S/.test(document.body.innerText) "
                          "&& !/LOGIN\\/SIGN UP/i.test(document.body.innerText))")
            if not still_in:
                return {"status": "failed", "state": state,
                        "error": "Logged out after navigating to /train-search. "
                                 "Log in again in the same tab and retry.",
                        "details": details}
        else:
            # Clear any stale From/To text so the autocomplete starts fresh.
            js("(()=>{['origin','destination'].forEach(id=>{const i=document.querySelector('p-autocomplete#'+id+' input');if(i){i.focus();i.value='';i.dispatchEvent(new Event('input',{bubbles:true}));}});})()")
            _time.sleep(0.2)

        _autocomplete_pick("origin", cfg["from"], cfg["from_kind"])
        _time.sleep(0.4)
        _autocomplete_pick("destination", cfg["to"], cfg["to_kind"])
        _time.sleep(0.4)
        _open_calendar_and_pick(cfg["date"])
        _time.sleep(0.4)

        # Class & Quota stay at defaults (All Classes, GENERAL).
        js("document.querySelector('button.search_btn').click()")
        wait_for_load()
        _time.sleep(1.5)

        cur_url = (page_info() or {}).get("url", "")
        if "/booking/train-list" not in cur_url:
            return {"status": "failed", "state": state,
                    "error": f"submit did not navigate to train-list (url={cur_url})",
                    "details": details}

        state = _S_WAIT
        n = _wait_for_train_cards(15.0)
        if n == 0:
            return {"status": "failed", "state": state,
                    "error": "no train cards rendered in 15s",
                    "details": details}

        state = _S_PICK_TRAIN
        cards = _parse_train_cards()
        if not cards:
            return {"status": "failed", "state": state,
                    "error": "parser returned 0 cards", "details": details}
        if not cfg["train"]:
            picked_num, reason = _pick_train_with_gemini(cards, cfg)
            cfg["train"] = picked_num
            details["chosen_via_gemini"] = {"train_number": picked_num, "reason": reason}
        matches = _match_train(cards, cfg["train"])
        if len(matches) == 0:
            return {"status": "failed", "state": state,
                    "error": f"no train matched {cfg['train']!r}",
                    "details": {**details,
                                "candidates": [{"number": c["number"], "name": c["name"]}
                                               for c in cards]}}
        if len(matches) > 1:
            return {"status": "failed", "state": state,
                    "error": f"{len(matches)} trains matched {cfg['train']!r} — pass an exact number",
                    "details": {**details,
                                "candidates": [{"number": c["number"], "name": c["name"]}
                                               for c in matches]}}
        chosen = matches[0]
        details["chosen_train"] = {"number": chosen["number"], "name": chosen["name"],
                                   "index": chosen["index"]}

        state = _S_PICK_CLASS
        if not cfg["class"]:
            picked_class, reason = _pick_class_with_gemini(chosen, cfg)
            cfg["class"] = picked_class
            details.setdefault("chosen_via_gemini", {})["class_code"] = picked_class
            details["chosen_via_gemini"]["class_reason"] = reason
        res = _click_class_box(chosen["index"], cfg["class"])
        if not res.get("ok"):
            return {"status": "failed", "state": state,
                    "error": res.get("reason", "class click failed"),
                    "details": {**details,
                                "available_classes": res.get("available", [])}}
        _time.sleep(0.7)

        state = _S_CLICK_DATE
        if _wait_date_row(chosen["index"]) == 0:
            return {"status": "failed", "state": state,
                    "error": "date row never appeared after class click",
                    "details": details}
        dr = _click_date_cell(chosen["index"], cfg["date"])
        if not dr.get("ok"):
            return {"status": "failed", "state": state,
                    "error": dr.get("reason", "date click failed"),
                    "details": {**details, "available_dates": dr.get("available", [])}}
        _time.sleep(0.6)

        state = _S_BOOK_NOW
        if not _book_now(chosen["index"]):
            return {"status": "failed", "state": state,
                    "error": "Book Now button stayed disabled",
                    "details": details}
        _time.sleep(1.5)

        state = _S_LOGIN
        outcome = _post_booknow_outcome(timeout=20.0)
        if outcome != "psgn":
            return {"status": "failed", "state": state,
                    "error": f"unexpected post-Book-Now state (url={(page_info() or {}).get('url','')})",
                    "details": details}

        state = _S_PSGN
        _fill_passenger_form(cfg["passengers"])

        return {"status": "success", "state": "PASSENGER_FORM_FILLED",
                "details": details}

    except Exception as e:
        return {"status": "failed", "state": state,
                "error": f"{type(e).__name__}: {e}", "details": details}
