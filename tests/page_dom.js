// Runs the coach page's own script under a very small stand-in for the browser's document and
// prints, as JSON, the text of the Overview and the Cost tab it builds for each period. No browser and no window:
// tests/test_site.py uses it (when node is installed) to catch a script error that reading the
// page's source cannot.
// Usage: node page_dom.js <page.html> <now as an ISO time>
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const data = /<script type="application\/json" id="coach-data">([\s\S]*?)<\/script>/.exec(html)[1];
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const templates = {};
for (const m of html.matchAll(/<template id="([^"]+)">([\s\S]*?)<\/template>/g)) templates[m[1]] = m[2];

class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.className = ""; this._text = null; this.hidden = false; this.listeners = {}; this.style = {}; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return (this._text || "") + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); }
  get classList() {
    const e = this;
    const list = () => e.className.split(/\s+/).filter(Boolean);
    return { add(c) { if (!list().includes(c)) e.className = list().concat(c).join(" "); },
      remove(c) { e.className = list().filter((x) => x !== c).join(" "); },
      toggle(c, on) { (on === undefined ? !list().includes(c) : on) ? this.add(c) : this.remove(c); },
      contains(c) { return list().includes(c); } };
  }
  matches(sel) {
    if (sel.startsWith(".")) return this.className.split(/\s+/).includes(sel.slice(1));
    const m = /^(\w+)?(?:\.(\w[\w-]*))?(?:\[(\w+)\])?$/.exec(sel);
    if (!m) return false;
    return (!m[1] || this.tagName === m[1]) && (!m[2] || this.className.split(/\s+/).includes(m[2])) && (!m[3] || m[3] in this.attrs);
  }
  all() { return this.children.flatMap((c) => (c instanceof El ? [c, ...c.all()] : [])); }
  querySelector(sel) { return this.all().find((e) => e.matches(sel)) || null; }
  querySelectorAll(sel) { return this.all().filter((e) => e.matches(sel)); }
  closest() { return null; }
  cloneNode() { return this; }
  focus() {}
  dump(depth = 0) {
    const own = this._text ? " " + JSON.stringify(this._text) : "";
    const cls = this.className ? "." + this.className.trim().replace(/\s+/g, ".") : "";
    const extra = ["style", "title", "aria-pressed"].filter((k) => k in this.attrs).map((k) => ` ${k}=${JSON.stringify(this.attrs[k]).slice(0, 60)}`).join("");
    let out = "  ".repeat(depth) + this.tagName + cls + extra + own + "\n";
    for (const c of this.children) out += c instanceof El ? c.dump(depth + 1) : "  ".repeat(depth + 1) + JSON.stringify(c._text) + "\n";
    return out;
  }
}
class Text { constructor(t) { this._text = t; } get textContent() { return this._text; } }

const ids = new Set([...html.replace(/<script[\s\S]*?<\/script>/g, "").replace(/<template[\s\S]*?<\/template>/g, "").matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
ids.add("coach-data");
const byId = {};
const document = {
  documentElement: new El("html"),
  createElement: (t) => new El(t),
  createTextNode: (t) => new Text(t),
  getElementById(id) {
    if (id in templates) {
      const marker = new El("fragment");
      marker._text = "[graph " + id + "]";
      if (templates[id].includes("lg-avg")) { const a = new El("polyline"); a.className = "lg-avg"; marker.children.push(a); }
      return { content: { cloneNode: () => marker } };
    }
    // as in a browser: an id the page's HTML does not have gives null, and the script's use of it throws
    if (!ids.has(id)) return null;
    if (!byId[id]) { byId[id] = new El("div"); byId[id].id = id; if (id === "coach-data") byId[id]._text = data; if (id === "ov-conn") { byId[id].appendChild(new El("i")); byId[id].appendChild(new El("span")); } }
    return byId[id];
  },
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {},
};
const store = {};
const fixedNow = process.argv[3] ? Date.parse(process.argv[3]) : Date.now();
const RealDate = Date;
class FakeDate extends RealDate { constructor(...a) { if (a.length) super(...a); else super(fixedNow); } static now() { return fixedNow; } }
const sandbox = { document, window: { addEventListener() {} }, location: { hash: "" }, history: { replaceState() {} },
  localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  navigator: { clipboard: null }, console, Date: FakeDate, setTimeout, setInterval: () => 0, Math, JSON, isFinite, isNaN, Array, Object, String, Number, RegExp };
const vm = require("vm");
vm.createContext(sandbox);
for (const s of scripts) vm.runInContext(s, sandbox, { filename: "page-script.js" });

const out = { live: byId["ov-conn"] ? byId["ov-conn"].className + " | " + byId["ov-conn"].textContent : "", periods: {} };
for (const p of ["today", "week", "month", "all"]) {
  byId["ov-p-" + p].listeners.click[0]();
  out.periods[p] = { cls: byId["overview-root"].className, pressed: byId["ov-p-" + p].getAttribute("aria-pressed"), tree: byId["overview-root"].dump(),
    cost: byId["cost-root"] ? byId["cost-root"].dump() : "" };
}
out.today = byId["today-root"] ? byId["today-root"].dump() : "";
out.weekly = byId["week-root"] ? byId["week-root"].dump() : "";
console.log(JSON.stringify(out));
