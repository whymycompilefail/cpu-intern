#!/usr/bin/env python3
"""Text-only bridge to a web chat UI over the Chrome DevTools Protocol.

What it does
------------
Send plain text into a web chat, wait for the answer, pull the answer text
back out, write it to a local file. Nothing else:

  - no tool execution, no mouse/keyboard simulation, no screenshots
  - model output is NEVER executed - it is only written to disk

Usage
-----
  python cdp_bridge.py --list              # list open tabs
  python cdp_bridge.py --probe             # dump candidate inputs/buttons
  python cdp_bridge.py --ask --text-file Q.txt --out A.txt

Design notes
------------
* Zero third-party deps: HTTP via urllib, WebSocket via ./cdp_ws.py
* The local proxy MUST be bypassed for 127.0.0.1 (ProxyHandler({})),
  otherwise every CDP HTTP call is swallowed by the proxy.
* Selectors live in sites.json and are meant to be discovered with --probe.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITES_PATH = HERE / "sites.json"

# Make `import cdp_ws` work no matter where this is invoked from.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from cdp_ws import WS  # noqa: E402


class BridgeError(RuntimeError):
    pass


# --------------------------------------------------------------------- HTTP
def http_json(url: str, timeout: float = 5.0):
    """GET JSON from the CDP HTTP endpoint.

    urllib honours http_proxy/https_proxy from the environment, and this
    machine injects a local proxy that swallows 127.0.0.1 requests
    ("upstream connect failed"). ProxyHandler({}) disables that.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------- CDP
class CDP:
    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws = WS(ws_url, timeout=timeout).connect()
        self._id = 0
        self.timeout = timeout

    def call(self, method: str, params=None, timeout: float | None = None):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while True:
            if time.time() > deadline:
                raise BridgeError("CDP timeout waiting for %s" % method)
            raw = self.ws.recv(timeout=max(1.0, deadline - time.time()))
            msg = json.loads(raw)
            if msg.get("id") != msg_id:
                continue  # event, not our response
            if "error" in msg:
                raise BridgeError("CDP %s -> %s" % (method, msg["error"]))
            return msg.get("result", {})

    def evaluate(self, expr: str, timeout: float | None = None):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            raise BridgeError("JS exception: %s" % res["exceptionDetails"])
        return res.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ------------------------------------------------------------------- helpers
def find_tab(port: int, url_contains: str | None, timeout: float = 5.0):
    tabs = [t for t in http_json("http://127.0.0.1:%d/json/list" % port, timeout)
            if t.get("type") == "page"]
    if not url_contains:
        if not tabs:
            raise BridgeError("no page targets")
        return tabs[0]
    for t in tabs:
        if url_contains in (t.get("url") or ""):
            return t
    raise BridgeError(
        "no tab matching %r; open tabs:\n  %s"
        % (url_contains, "\n  ".join((t.get("url") or "")[:100] for t in tabs))
    )


PROBE_JS = r"""
(() => {
  const desc = el => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      id: el.id || null,
      cls: String(el.className || '').slice(0, 90) || null,
      aria: el.getAttribute('aria-label'),
      ph: el.getAttribute('placeholder'),
      role: el.getAttribute('role'),
      testid: el.getAttribute('data-testid'),
      ce: el.isContentEditable || null,
      disabled: !!el.disabled || null,
      visible: r.width > 0 && r.height > 0,
      text: (el.innerText || el.value || '').trim().slice(0, 40) || null
    };
  };
  const uniq = arr => Array.from(new Set(arr));
  return JSON.stringify({
    url: location.href,
    title: document.title,
    inputs: uniq([...document.querySelectorAll(
      'textarea, input[type=text], input[type=search], [contenteditable], [role=textbox]'
    )]).map(desc),
    buttons: uniq([...document.querySelectorAll('button, [role=button]')]).map(desc).slice(0, 60)
  }, null, 1);
})()
"""


def probe(cdp: CDP, site: str) -> dict:
    raw = cdp.evaluate(PROBE_JS)
    try:
        data = json.loads(raw)
    except Exception as e:
        raise BridgeError("probe returned non-JSON: %r (%s)" % (raw, e))
    print("site=%s url=%s" % (site, data.get("url")))
    print("title=%s" % data.get("title"))
    print("\n--- INPUT CANDIDATES ---")
    for i, el in enumerate(data.get("inputs", [])):
        print("[%d] %s" % (i, json.dumps(el, ensure_ascii=False)))
    print("\n--- BUTTON CANDIDATES ---")
    for i, el in enumerate(data.get("buttons", [])):
        print("[%d] %s" % (i, json.dumps(el, ensure_ascii=False)))
    return data


INJECT_JS = r"""
(sel, text) => {
  const el = document.querySelector(sel);
  if (!el) return JSON.stringify({ok:false, err:'no element for selector: ' + sel});
  el.focus();
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'INPUT') {
    // React overrides the `value` setter; assigning directly is invisible to
    // the framework. Go through the prototype's native setter instead.
    const proto = tag === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                     : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, text);
  } else {
    el.textContent = text;
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  const got = (el.value != null) ? el.value : el.textContent;
  return JSON.stringify({ok:true, tag: tag, len: (got || '').length,
                         head: (got || '').slice(0, 60)});
}
"""


def inject(cdp: CDP, selector: str, text: str) -> dict:
    """Put text into the input box WITHOUT submitting.

    Focus the element, then use CDP `Input.insertText` — a *trusted* input event
    that React's onChange picks up reliably. The old approach (set `.value` via
    the prototype setter + dispatch a synthetic `input` event) stopped working
    once DeepSeek gated its textarea on IME composition / trusted input; the value
    would show in the box but React state never updated, so submit read empty.
    `Input.insertText` goes through the browser's real input path and updates
    React state.
    """
    cdp.evaluate("(() => { const el = document.querySelector(%s); if (el) el.focus(); })()"
                 % json.dumps(selector))
    cdp.call("Input.insertText", {"text": text})
    got = cdp.evaluate(
        "(() => { const t = document.querySelector(%s);"
        " return t ? (t.value != null ? t.value : t.textContent) : ''; })()"
        % json.dumps(selector))
    return {"ok": True, "len": len(got or ""), "head": (got or "")[:60]}


def click_send(cdp: CDP) -> bool:
    """Submit by clicking the send button.

    On the current DeepSeek web UI, neither CDP `Input.dispatchKeyEvent` (Enter)
    nor synthetic `Input.dispatchMouseEvent` clicks trigger submission — the
    send button's React `onClick` only fires from a real DOM click. We locate the
    send button (the `div.ds-button--filled` / primary button in the composer)
    and call `.click()` on it. Returns True if a button was clicked.
    """
    js = r"""
    (() => {
      const ta = document.querySelector('textarea')
              || document.querySelector('[contenteditable="true"]');
      if (!ta) return JSON.stringify({ok:false, err:'no input'});
      let node = ta, container = null;
      for (let i = 0; i < 10; i++) {
        node = node.parentElement;
        if (!node) break;
        if (node.querySelector('div.ds-button--filled, div.ds-button--primary, button[type=submit]')) {
          container = node; break;
        }
      }
      const cands = container
        ? [...container.querySelectorAll('div.ds-button--filled, div.ds-button--primary')]
        : [...document.querySelectorAll('div.ds-button--filled, div.ds-button--primary')];
      if (!cands.length) return JSON.stringify({ok:false, err:'no send button'});
      const btn = cands.find(x => String(x.className||'').includes('filled'))
                  || cands[cands.length - 1];
      btn.click();
      return JSON.stringify({ok:true, cls: String(btn.className||'').slice(0,40)});
    })()
    """
    res = cdp.evaluate(js)
    try:
        return bool(json.loads(res).get("ok"))
    except Exception:
        return False


def press_enter(cdp: CDP) -> None:
    """DEPRECATED submit path (kept for reference). Use send_enter instead."""
    for t in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", {
            "type": t,
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
            "text": "\r" if t == "keyDown" else "",
        })


def send_enter(cdp: CDP, selector: str | None = None) -> bool:
    """Submit by a *trusted* Enter key event.

    This is the robust send path that works on modern chat SPAs (Kimi et al.):
    type via trusted `Input.insertText` (focus first), then send with a trusted
    Enter. Synthetic value-set + button clicks were unreliable -- the input
    value would update but the message never reached the backend. Trusted
    keyboard input goes through the real editing pipeline the app expects.
    """
    if selector:
        cdp.evaluate(
            "(() => { const el = document.querySelector(%s); if (el) el.focus(); })()"
            % json.dumps(selector))
    for t in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", {
            "type": t,
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        })
    return True


def focus_page(cdp: CDP) -> None:
    """Make the target deliver trusted keyboard/mouse input.

    Empirical finding (2026-10-20, Edge 155 + DeepSeek): a tab that is not the
    active foreground tab has its keyboard events dropped by the renderer. Every
    injection method appeared to work (DOM value and React's `_valueTracker`
    both updated) yet Enter did nothing and a user bubble never appeared --
    because the Enter never reached the page at all.

    Real keystrokes landing 0 characters was the tell: not a selector problem,
    not a React-state problem, just an inactive tab. `Page.bringToFront` +
    `Emulation.setFocusEmulationEnabled` fixes both; after that a plain
    insertText + Enter submits on DeepSeek and Kimi alike.
    """
    try:
        cdp.call("Page.enable")
        cdp.call("Page.bringToFront")
    except Exception:
        pass
    try:
        cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception:
        pass


DUMP_REPLY_CANDIDATES_JS = r"""
(() => {
  const hits = [];
  const push = (el, why) => {
    const t = (el.innerText || '').trim();
    if (!t) return;
    hits.push({
      why: why,
      tag: el.tagName,
      cls: String(el.className || '').slice(0, 160) || null,
      testid: el.getAttribute('data-testid'),
      len: t.length,
      text: t.slice(0, 200)
    });
  };
  const sel = "div[class*='markdown'], div[class*='message'], div[class*='answer'],"
            + "div[class*='reply'], div[class*='chat'], div[class*='content'],"
            + "[data-testid*='message'], [data-testid*='answer']";
  document.querySelectorAll(sel).forEach(el => push(el, 'css'));
  // also: any element that literally contains a code block
  document.querySelectorAll('pre').forEach(p => {
    const host = p.closest('div');
    if (host) push(host, 'has-pre');
  });
  // de-duplicate by text prefix
  const seen = new Set();
  const out = hits.filter(h => {
    const k = h.text.slice(0, 60);
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
  return JSON.stringify(out.slice(-25), null, 1);
})()
"""


def dump_reply_candidates(cdp: CDP) -> list:
    raw = cdp.evaluate(DUMP_REPLY_CANDIDATES_JS)
    try:
        return json.loads(raw)
    except Exception:
        return []


EXTRACT_JS = r"""
(sel) => {
  const els = document.querySelectorAll(sel);
  if (!els.length) return JSON.stringify({ok: false, err: 'no element: ' + sel});
  const el = els[els.length - 1];

  // innerText collapses leading whitespace, which DESTROYS Python indentation.
  // But textContent drops line breaks. So walk the tree and use:
  //   <pre>  -> textContent (indentation preserved verbatim)
  //   block  -> recurse, then newline
  //   text   -> nodeValue
  const BLOCK = new Set(['DIV','P','PRE','BR','LI','TR','H1','H2','H3','H4',
                         'H5','H6','BLOCKQUOTE','SECTION','ARTICLE']);
  let out = '';
  const walk = node => {
    for (const ch of node.childNodes) {
      if (ch.nodeType === Node.TEXT_NODE) {
        out += ch.nodeValue;
      } else if (ch.nodeType === Node.ELEMENT_NODE) {
        const tag = ch.tagName;
        if (tag === 'PRE') { out += '\n' + ch.textContent + '\n'; continue; }
        if (tag === 'BR') { out += '\n'; continue; }
        walk(ch);
        if (BLOCK.has(tag)) out += '\n';
      }
    }
  };
  walk(el);

  const preEls = [...el.querySelectorAll('pre')];
  const preCount = preEls.length;
  // preText is UI-noise-free: the code block toolbar (language label,
  // "copy", "download") lives outside <pre>, so it never lands in here.
  const preText = preEls.map(p => p.textContent).join('\n\n').trim();
  // Kept separately: a chatty model sometimes emits several fences (it echoes
  // the input first, then answers). Concatenating them yields invalid source,
  // so the caller can pick just the last block.
  const preTexts = preEls.map(p => p.textContent);
  const text = out.replace(/\n{3,}/g, '\n\n').trim();
  return JSON.stringify({
    ok: true, text: text, preText: preText, preTexts: preTexts,
    len: text.length, n: els.length,
    preCount: preCount,
    warn: preCount === 0
        ? 'no <pre> block: model did not fence its code, indentation NOT preserved'
        : null
  });
}
"""

# Marks the protocol block lives inside a fenced code block (as comments),
# so `preText` alone is a complete, clean answer.
_SENTINELS = ("<<<FIXED>>>", "<<<LOCATION>>>")


def extract_last(cdp: CDP, selector: str) -> dict:
    raw = cdp.evaluate("(%s)(%s)" % (EXTRACT_JS, json.dumps(selector)))
    got = json.loads(raw)
    pre = (got.get("preText") or "").strip()
    if pre and any(s in pre for s in _SENTINELS):
        # Sentinels are inside the fence -> preText is the whole answer,
        # free of the code-block toolbar noise.
        got["text"] = pre
        got["len"] = len(pre)
        got["source"] = "pre"
    else:
        got["source"] = "mixed"
    return got


def click_selector(cdp: CDP, selector: str) -> bool:
    """Click a specific element (JS `.click()`). Used as a submit fallback."""
    js = ("(() => { const b = document.querySelector(%s);"
          " if (!b) return JSON.stringify({ok:false,err:'not found'});"
          " b.click(); return JSON.stringify({ok:true}); })()" % json.dumps(selector))
    try:
        return bool(json.loads(cdp.evaluate(js)).get("ok"))
    except Exception:
        return False


def count_nodes(cdp: CDP, selector: str) -> int:
    try:
        return int(cdp.evaluate("document.querySelectorAll(%s).length"
                                % json.dumps(selector)) or 0)
    except Exception:
        return 0


def wait_reply_start(cdp: CDP, reply_sel: str, before_n: int,
                     timeout: float = 12.0) -> bool:
    """Did the submit actually produce a reply bubble?

    This is the only trustworthy submit signal. Length-based heuristics lie:
    pressing Enter on a page that does not bind it still mutates the DOM (a
    newline lands in the composer, the body length changes by a few chars),
    which made an earlier version believe the message had been sent and skip
    the send-button fallback.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_nodes(cdp, reply_sel) > before_n:
            return True
        time.sleep(0.5)
    return False


def new_chat(cdp: CDP, url: str, selectors: list[str] | None = None,
             timeout: float = 60.0) -> bool:
    """Start a fresh conversation.

    A benchmark run MUST NOT share one thread: later problems would be
    answered with earlier ones in context, which both contaminates the
    measurement and inflates the token budget.

    Readiness is "the composer exists", so the caller passes the site's own
    input selectors: Kimi's composer is a contenteditable div, not a textarea,
    and waiting on `textarea` alone made every new-chat attempt time out.
    """
    sels = [s for s in (selectors or ["textarea"]) if s]
    ready_js = " || ".join("!!document.querySelector(%s)" % json.dumps(s) for s in sels)
    cdp.call("Page.enable")
    cdp.call("Page.navigate", {"url": url})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cdp.evaluate(ready_js, timeout=10):
                time.sleep(1.5)  # let the UI settle after navigation
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def wait_reply(cdp: CDP, reply_sel: str, timeout: float,
               stable_for: float = 8.0, poll: float = 1.0) -> int:
    """Wait until the LAST assistant message stops growing; return its length.

    Watching `document.body.innerText.length` is not enough: the body keeps
    changing while the page shows a "thinking" placeholder, and it also counts
    the user's own bubble. Tracking the last assistant container means we
    extract only after the actual answer has settled.
    """
    js = ("(() => { const els = document.querySelectorAll(%s);"
          " if (!els.length) return 0;"
          " return (els[els.length - 1].innerText || '').length; })()"
          % json.dumps(reply_sel))
    return wait_stable(cdp, js, timeout, stable_for=stable_for, poll=poll)


def wait_stable(cdp: CDP, js_len: str, timeout: float, stable_for: float = 4.0,
                poll: float = 1.0) -> int:
    """Poll a length-returning JS expression until it stops changing."""
    last = -1
    stable_since = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            n = int(cdp.evaluate(js_len, timeout=15) or 0)
        except Exception:
            n = last
        now = time.time()
        if n == last and n > 0:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= stable_for:
                return n
        else:
            stable_since = None
        last = n
        time.sleep(poll)
    return last


# -------------------------------------------------------------------- config
def load_sites() -> dict:
    if not SITES_PATH.exists():
        raise BridgeError("missing %s" % SITES_PATH)
    return json.loads(SITES_PATH.read_text(encoding="utf-8"))


def pick_site(cfg: dict, name: str | None) -> tuple[str, dict]:
    name = name or cfg.get("default_site")
    sites = cfg.get("sites", {})
    if name not in sites:
        raise BridgeError("site %r not configured; have: %s" % (name, list(sites)))
    return name, sites[name]


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Text-only CDP bridge to a web chat UI")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--site", default=None, help="site key in sites.json")
    ap.add_argument("--list", action="store_true", help="list open page targets")
    ap.add_argument("--probe", action="store_true", help="dump input/button candidates")
    ap.add_argument("--ask", action="store_true", help="send text, wait, save reply")
    ap.add_argument("--inject", action="store_true",
                    help="put text in the input box but do NOT submit")
    ap.add_argument("--text", help="inline prompt text")
    ap.add_argument("--text-file", help="file containing the prompt")
    ap.add_argument("--out", help="where to write the reply (for --ask)")
    ap.add_argument("--wait-timeout", type=float, default=180.0)
    ap.add_argument("--dump-candidates", action="store_true",
                    help="after --ask, dump reply container candidates")
    ap.add_argument("--new-chat", action="store_true",
                    help="navigate to the site root first to start a fresh thread")
    ap.add_argument("--prefer", choices=["pre", "text"], default="pre",
                    help="which rendering to save: <pre> blocks (indentation-safe, "
                         "UI-noise-free) or full mixed text")
    ap.add_argument("--all-blocks", action="store_true",
                    help="with --prefer pre, keep every code block instead of "
                         "only the last one")
    args = ap.parse_args()

    if args.list:
        for t in http_json("http://127.0.0.1:%d/json/list" % args.port):
            if t.get("type") != "page":
                continue
            print("%-14s %s" % (t.get("id", "")[:14], (t.get("url") or "")[:110]))
        return 0

    cfg = load_sites()
    name, site = pick_site(cfg, args.site)

    wants_io = args.probe or args.inject or args.ask
    if not (args.list or wants_io):
        ap.error("nothing to do: pass --list, --probe, --inject, or --ask")

    if args.inject or args.ask:
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        elif args.text is not None:
            text = args.text
        else:
            ap.error("--inject/--ask needs --text or --text-file")

    tab = find_tab(args.port, site.get("url_contains"))
    print("connecting: %s" % (tab.get("url") or "")[:110])
    cdp = CDP(tab["webSocketDebuggerUrl"])
    try:
        cdp.call("Runtime.enable")
        focus_page(cdp)

        if args.new_chat:
            in_sels = (list(site.get("input", {}).get("selectors") or [])
                       + list(site.get("input", {}).get("fallback_selectors") or []))
            ok = new_chat(cdp, "https://" + site["url_contains"] + "/", in_sels)
            print("new chat: %s" % ("ok" if ok else "TIMEOUT waiting for input box"))
            if not ok:
                return 1

        if args.probe:
            probe(cdp, name)
            print("\nNext: copy the discovered selectors into sites.json[%r]." % name)
            return 0

        # Resolve the input box: configured selectors first, then fallbacks.
        cands = list(site.get("input", {}).get("selectors") or [])
        cands += list(site.get("input", {}).get("fallback_selectors") or [])
        selector = None
        for sel in cands:
            if cdp.evaluate("!!document.querySelector(%s)" % json.dumps(sel)):
                selector = sel
                break
        if selector is None:
            raise BridgeError("no input element matched; run --probe first")
        print("input selector: %s" % selector)

        res = inject(cdp, selector, text)
        print("inject: %s" % json.dumps(res, ensure_ascii=False))
        if not res.get("ok"):
            return 1

        if args.inject:
            print("injected only (not submitted).")
            return 0

        # ---- ask: submit, wait, extract
        before = cdp.evaluate("document.body.innerText.length") or 0
        reply_sels = site.get("reply", {}).get("selectors") or []
        if not reply_sels or not args.out:
            raise BridgeError(
                "reply extraction needs sites.json reply.selectors and --out"
            )
        reply_sel = reply_sels[0]

        sub = site.get("submit", {})
        method = sub.get("method", "click")
        button = sub.get("button")
        # 提交成功的**硬信号** = 助手消息容器数量增加。
        # 不能用「body 长度变了」判断：首页按 Enter 只会往 composer 里塞一个换行，
        # body 长度也会变，于是误判成已发送、跳过发送键回退，最后空等一场。
        before_n = count_nodes(cdp, reply_sel)
        sent = False

        if method == "enter":
            send_enter(cdp, selector)
            sent = wait_reply_start(cdp, reply_sel, before_n, timeout=12.0)
            print("tried trusted Enter -> %s"
                  % ("submitted" if sent else "no reply bubble"))
            if not sent and sub.get("enter_fallback", True) and button:
                # Home page of some SPAs does not bind Enter; the dedicated
                # send button is the real submit path there.
                click_selector(cdp, button)
                sent = wait_reply_start(cdp, reply_sel, before_n, timeout=15.0)
                print("fallback: clicked %s -> %s"
                      % (button, "submitted" if sent else "still nothing"))
        else:
            sent = click_send(cdp)
            print("submitted via send-button click (%s)" % ("ok" if sent else "FAILED"))
            sent = sent and wait_reply_start(cdp, reply_sel, before_n, timeout=15.0)

        if not sent:
            raise BridgeError("submit failed: no reply bubble appeared after both "
                              "Enter and the send button (site=%s, method=%s)"
                              % (name, method))
        print("waiting for reply text (timeout %.0fs)" % args.wait_timeout)

        n = wait_reply(cdp, reply_sel, timeout=args.wait_timeout)
        print("last assistant message: %d chars (body %d -> %d)"
              % (n, before, cdp.evaluate("document.body.innerText.length") or 0))

        if args.dump_candidates:
            cands_out = dump_reply_candidates(cdp)
            print("\n--- REPLY CONTAINER CANDIDATES ---")
            for i, c in enumerate(cands_out):
                print("[%d] %s" % (i, json.dumps(c, ensure_ascii=False)))
            print("\nPick one, put its selector in sites.json[%r].reply.selectors" % name)
            return 0

        if n == 0:
            raise BridgeError("no reply appeared (last assistant message empty)")

        got = extract_last(cdp, reply_sels[0])
        if not got.get("ok"):
            raise BridgeError("extract failed: %s" % got)

        # Which rendering to persist.
        #   pre   -> the <pre> blocks: indentation verbatim, and the code-block
        #            toolbar (language label / copy / download) lives OUTSIDE
        #            <pre>, so it never leaks in. Preferred.
        #            By default only the LAST block is kept: models often echo
        #            the input in a fence before giving the real answer, and
        #            concatenating both yields invalid source.
        #   text  -> full mixed walk: keeps prose outside code blocks, but picks
        #            up UI noise and (if the model never fenced its code)
        #            collapses leading whitespace.
        blocks = [b for b in (got.get("preTexts") or []) if (b or "").strip()]
        use_pre = (args.prefer == "pre") and bool(blocks)
        if use_pre:
            body = "\n\n".join(blocks) if args.all_blocks else blocks[-1]
            if len(blocks) > 1 and not args.all_blocks:
                print("note: %d code blocks in reply, kept the last one" % len(blocks))
        else:
            body = got["text"]
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
        print("wrote %d chars (%s) -> %s" % (len(body), "pre" if use_pre else "text",
                                             out_path))
        if got.get("warn"):
            print("WARN: %s" % got["warn"])
        print("--- reply head ---")
        print(body[:300])
        return 0
    finally:
        cdp.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BridgeError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
