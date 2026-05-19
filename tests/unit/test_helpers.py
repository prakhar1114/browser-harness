import os
import sys
import tempfile
import time
import types
from unittest.mock import patch

import pytest
from PIL import Image

from browser_harness import helpers


def _run(fake_png, width, height, **kwargs):
    fake = lambda method, **_: {"data": fake_png(width, height)}
    with patch("browser_harness.helpers.cdp", side_effect=fake), tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "shot.png")
        helpers.capture_screenshot(path, **kwargs)
        return Image.open(path).size


def test_max_dim_downsizes_oversized_image(fake_png):
    assert max(_run(fake_png, 4592, 2286, max_dim=1800)) == 1800


def test_max_dim_skips_when_image_already_small(fake_png):
    assert _run(fake_png, 800, 400, max_dim=1800) == (800, 400)


def test_max_dim_default_is_no_resize(fake_png):
    assert _run(fake_png, 4592, 2286) == (4592, 2286)


def _seed_skill(tmp_path):
    site = tmp_path / "domain-skills" / "example"
    site.mkdir(parents=True)
    (site / "scraping.md").write_text("hi")


def test_goto_url_omits_domain_skills_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("BH_DOMAIN_SKILLS", raising=False)
    monkeypatch.setattr(helpers, "AGENT_WORKSPACE", tmp_path)
    _seed_skill(tmp_path)
    with patch("browser_harness.helpers.cdp", return_value={"frameId": "f"}):
        result = helpers.goto_url("https://www.example.com/")
    assert result == {"frameId": "f"}


def test_goto_url_includes_domain_skills_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_DOMAIN_SKILLS", "1")
    monkeypatch.setattr(helpers, "AGENT_WORKSPACE", tmp_path)
    _seed_skill(tmp_path)
    with patch("browser_harness.helpers.cdp", return_value={"frameId": "f"}):
        result = helpers.goto_url("https://www.example.com/")
    assert result == {"frameId": "f", "domain_skills": ["scraping.md"]}


def test_page_info_raises_clear_error_on_js_exception():
    def fake_send(req):
        return {}

    def fake_cdp(method, **kwargs):
        return {
            "result": {
                "type": "object",
                "subtype": "error",
                "description": "ReferenceError: location is not defined",
            },
            "exceptionDetails": {
                "text": "Uncaught",
                "lineNumber": 0,
                "columnNumber": 16,
            },
        }

    with patch("browser_harness.helpers._send", side_effect=fake_send), \
         patch("browser_harness.helpers.cdp", side_effect=fake_cdp):
        with pytest.raises(RuntimeError, match="ReferenceError"):
            helpers.page_info()


# --- ask_llm / ask_gemini ---

def test_ask_llm_uses_sdk_structured_output_and_parses_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text='{"id":"row-1"}')]
            )

    class FakeLLMClient:
        def __init__(self, api_key):
            seen["api_key"] = api_key
            self.messages = FakeMessages()

    fake_llm_sdk = types.SimpleNamespace(Anthropic=FakeLLMClient)
    monkeypatch.setitem(sys.modules, "anthropic", fake_llm_sdk)

    result = helpers.ask_llm("Pick a row", schema, model="sonnet", thinking="minimal", timeout=3.0)

    assert result == {"id": "row-1"}
    assert seen["api_key"] == "test-key"
    assert seen["model"] == "sonnet"
    assert seen["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "medium",
    }
    assert seen["timeout"] == 3.0
    assert seen["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Pick a row"}]}]


def test_ask_llm_encodes_images_for_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    schema = {"type": "object"}
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text='{"ok":true}')]
            )

    class FakeLLMClient:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeLLMClient))
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(b"fake image")
        f.flush()
        assert helpers.ask_llm("Inspect", schema, images=[f.name]) == {"ok": True}

    image_block = seen["messages"][0]["content"][1]
    assert image_block["type"] == "image"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == "ZmFrZSBpbWFnZQ=="


def test_ask_llm_falls_back_to_agent_sdk_structured_output(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    seen = {}

    class FakeResultMessage:
        def __init__(self):
            self.is_error = False
            self.structured_output = {"id": "row-2"}

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    async def fake_query(prompt, options):
        seen["prompt"] = prompt
        yield FakeResultMessage()

    fake_sdk = types.SimpleNamespace(
        ClaudeAgentOptions=FakeClaudeAgentOptions,
        ResultMessage=FakeResultMessage,
        query=fake_query,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    assert helpers.ask_llm("Pick a row", schema, model="sonnet", thinking="low") == {"id": "row-2"}
    assert seen["prompt"] == "Pick a row"
    assert seen["model"] == "sonnet"
    assert seen["effort"] == "medium"
    assert seen["tools"] == []
    assert seen["allowed_tools"] == []
    assert seen["output_format"] == {"type": "json_schema", "schema": schema}


def test_ask_gemini_alias_delegates_to_ask_llm(monkeypatch):
    seen = {}

    def fake_ask_llm(prompt, schema, images=None, model="sonnet", thinking="low", timeout=30.0):
        seen.update({
            "prompt": prompt,
            "schema": schema,
            "images": images,
            "model": model,
            "thinking": thinking,
            "timeout": timeout,
        })
        return {"id": "row-3"}

    monkeypatch.setattr(helpers, "ask_llm", fake_ask_llm)
    schema = {"type": "object"}
    assert helpers.ask_gemini("Pick a row", schema, images=["x.png"], model="claude-test", timeout=5.0) == {"id": "row-3"}
    assert seen == {
        "prompt": "Pick a row",
        "schema": schema,
        "images": ["x.png"],
        "model": "claude-test",
        "thinking": "low",
        "timeout": 5.0,
    }


def test_ask_llm_rejects_text_only_malformed_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    schema = {"type": "object"}

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="not json")]
            )

    class FakeLLMClient:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeLLMClient))

    with pytest.raises(RuntimeError, match="ask_llm: response not valid JSON"):
        helpers.ask_llm("Pick a row", schema)


# --- fill_input ---

def test_fill_input_focuses_types_and_fires_events():
    cdp_calls = []
    js_calls = []

    def fake_cdp(method, **kwargs):
        cdp_calls.append((method, kwargs))
        return {}

    def fake_js(expr, **kwargs):
        js_calls.append(expr)
        return True  # focus call must return True (element found)

    with patch("browser_harness.helpers.cdp", side_effect=fake_cdp), \
         patch("browser_harness.helpers.js", side_effect=fake_js):
        helpers.fill_input("#my-input", "hello")

    assert any("#my-input" in e for e in js_calls)
    key_downs = [m for m, _ in cdp_calls if m == "Input.dispatchKeyEvent"]
    assert len(key_downs) > 0
    assert any("input" in e and "change" in e for e in js_calls)


def test_fill_input_raises_when_element_not_found():
    def fake_js(expr, **kwargs):
        return False  # element not found

    with patch("browser_harness.helpers.js", side_effect=fake_js):
        with pytest.raises(RuntimeError, match="element not found"):
            helpers.fill_input("#missing", "hello")


def test_fill_input_clear_first_sends_select_all_then_backspace():
    import sys

    key_events = []

    def fake_cdp(method, **kwargs):
        if method == "Input.dispatchKeyEvent":
            key_events.append(kwargs)
        return {}

    def fake_js(expr, **kwargs):
        return True  # element found

    with patch("browser_harness.helpers.cdp", side_effect=fake_cdp), \
         patch("browser_harness.helpers.js", side_effect=fake_js):
        helpers.fill_input("#inp", "x", clear_first=True)

    # The "a" must be dispatched with the platform-correct modifier (Meta=4 on
    # macOS, Ctrl=2 elsewhere). Without the modifier, the field would never get
    # selected — it would just receive a literal "a".
    expected_mod = 4 if sys.platform == "darwin" else 2
    a_events = [e for e in key_events if e.get("key") == "a"]
    assert a_events, "expected an 'a' key event for select-all"
    assert all(e.get("modifiers") == expected_mod for e in a_events), \
        f"select-all 'a' must carry modifiers={expected_mod}; got {[e.get('modifiers') for e in a_events]}"

    # Crucial: no `char` event for the "a" — emitting one makes Chrome treat
    # Cmd/Ctrl+A as a printable letter instead of a shortcut.
    assert not any(e.get("type") == "char" and e.get("text") == "a" for e in key_events), \
        "select-all must not emit a 'char' event with text='a' (would cancel the shortcut)"

    # Backspace still fires (via press_key, which uses keyDown).
    keys_down = [e.get("key") for e in key_events if e.get("type") in ("keyDown", "rawKeyDown")]
    assert "Backspace" in keys_down


def test_fill_input_no_clear_skips_ctrl_a():
    key_events = []

    def fake_cdp(method, **kwargs):
        if method == "Input.dispatchKeyEvent":
            key_events.append(kwargs)
        return {}

    def fake_js(expr, **kwargs):
        return True  # element found

    with patch("browser_harness.helpers.cdp", side_effect=fake_cdp), \
         patch("browser_harness.helpers.js", side_effect=fake_js):
        helpers.fill_input("#inp", "x", clear_first=False)

    keys_seen = [e.get("key") for e in key_events if e.get("type") == "keyDown"]
    assert "Backspace" not in keys_seen


# --- wait_for_element ---

def test_wait_for_element_returns_true_when_found_immediately():
    def fake_js(expr, **kwargs):
        return True

    with patch("browser_harness.helpers.js", side_effect=fake_js):
        assert helpers.wait_for_element("#target", timeout=2.0) is True


def test_wait_for_element_returns_false_on_timeout():
    def fake_js(expr, **kwargs):
        return False

    with patch("browser_harness.helpers.js", side_effect=fake_js), \
         patch("browser_harness.helpers.time") as mock_time:
        # simulate time advancing past the deadline immediately
        start = time.time()
        mock_time.time.side_effect = [start, start + 5.0]
        mock_time.sleep = lambda _: None
        assert helpers.wait_for_element("#missing", timeout=1.0) is False


def test_wait_for_element_visible_uses_check_visibility():
    js_exprs = []

    def fake_js(expr, **kwargs):
        js_exprs.append(expr)
        return True

    with patch("browser_harness.helpers.js", side_effect=fake_js):
        helpers.wait_for_element("#btn", visible=True)

    # Prefers checkVisibility (walks ancestor chain) with a computed-style
    # fallback for older Chrome.
    assert any("checkVisibility" in e for e in js_exprs)
    assert any("getComputedStyle" in e for e in js_exprs)
    # must NOT use offsetParent (fails for position:fixed elements)
    assert not any("offsetParent" in e for e in js_exprs)


def test_wait_for_element_non_visible_uses_simple_check():
    js_exprs = []

    def fake_js(expr, **kwargs):
        js_exprs.append(expr)
        return True

    with patch("browser_harness.helpers.js", side_effect=fake_js):
        helpers.wait_for_element("#btn", visible=False)

    assert any("querySelector" in e and "offsetParent" not in e for e in js_exprs)


# --- wait_for_network_idle ---

def test_wait_for_network_idle_returns_true_when_no_events():
    call_count = 0

    def fake_send(req):
        nonlocal call_count
        call_count += 1
        return {"events": []}

    with patch("browser_harness.helpers._send", side_effect=fake_send), \
         patch("browser_harness.helpers.time") as mock_time:
        start = 1000.0
        # first call: not idle yet; second call: idle window elapsed
        mock_time.time.side_effect = [start, start, start, start + 0.6, start + 0.6]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=5.0, idle_ms=500)

    assert result is True


def test_wait_for_network_idle_waits_for_inflight_request():
    # Verifies inflight tracking: must not return True until loadingFinished,
    # even though >idle_ms elapses between requestWillBeSent and loadingFinished.
    # An event-silence-only implementation would return True at iter2 (wrong).
    events_seq = [
        [{"method": "Network.requestWillBeSent", "params": {"requestId": "req1"}}],
        [],   # >500ms elapsed — old impl returns True here; new must NOT
        [{"method": "Network.loadingFinished",   "params": {"requestId": "req1"}}],
        [],   # idle_ms after loadingFinished → return True
    ]
    idx = 0

    def fake_send(req):
        nonlocal idx
        evs = events_seq[min(idx, len(events_seq) - 1)]
        idx += 1
        return {"events": evs}

    with patch("browser_harness.helpers._send", side_effect=fake_send), \
         patch("browser_harness.helpers.time") as mock_time:
        start = 1000.0
        # inflight non-empty → short-circuit skips time.time() in idle check for iter1/iter2
        mock_time.time.side_effect = [
            start, start,       # deadline + last_activity init
            start + 0.1,        # iter1 while-check
            start + 0.1,        # iter1 rWS last_activity update
                                # iter1 idle-check: inflight non-empty → short-circuit
            start + 0.7,        # iter2 while-check (>500ms since rWS but request still in flight)
                                # iter2 idle-check: inflight non-empty → short-circuit
            start + 0.8,        # iter3 while-check
            start + 0.8,        # iter3 lF last_activity update
            start + 0.8,        # iter3 idle-check: 0ms < 500 → not idle
            start + 1.4,        # iter4 while-check
            start + 1.4,        # iter4 idle-check: 600ms >= 500 → True
        ]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=5.0, idle_ms=500)

    assert result is True
    assert idx == 4  # did not short-circuit at iter2 despite silence > idle_ms


def test_wait_for_network_idle_returns_false_on_timeout():
    # Continuous rWS keeps inflight non-empty → idle check short-circuits every iteration.
    # time.time() is only called for while-check and rWS last_activity (not idle check).
    def fake_send(req):
        return {"events": [{"method": "Network.requestWillBeSent", "params": {"requestId": "r"}}]}

    with patch("browser_harness.helpers._send", side_effect=fake_send), \
         patch("browser_harness.helpers.time") as mock_time:
        start = 1000.0
        mock_time.time.side_effect = [
            start, start,       # deadline + last_activity init
            start + 0.1,        # iter1 while-check (in deadline)
            start + 0.1,        # iter1 rWS last_activity update
                                # iter1 idle-check: inflight non-empty → short-circuit
            start + 20.0,       # iter2 while-check (past deadline → exit)
        ]
        mock_time.sleep = lambda _: None
        result = helpers.wait_for_network_idle(timeout=10.0, idle_ms=500)

    assert result is False
