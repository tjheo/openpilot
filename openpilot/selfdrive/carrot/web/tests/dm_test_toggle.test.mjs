import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const api = await readFile(new URL("../js/shared/api.js", import.meta.url), "utf8");
const settingPage = await readFile(new URL("../js/pages/setting.js", import.meta.url), "utf8");

function sliceFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} is defined`);
  const next = source.indexOf("\nfunction ", start + 1);
  const nextLet = source.indexOf("\nlet ", start + 1);
  const end = [next, nextLet].filter((index) => index > start).reduce((a, b) => Math.min(a, b));
  return source.slice(start, end);
}

class FakeClassList {
  constructor() { this.names = new Set(); }
  toggle(name, force) { if (force) this.names.add(name); else this.names.delete(name); }
  contains(name) { return this.names.has(name); }
}

class FakeElement {
  constructor(className = "") {
    this.className = className;
    this.children = [];
    this.parent = null;
    this.textContent = "";
    this.dataset = {};
    this.classList = new FakeClassList();
  }
  matches(selector) { return selector.startsWith(".") && this.className.split(/\s+/).includes(selector.slice(1)); }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  insertBefore(child, ref) {
    child.parent = this;
    const index = ref ? this.children.indexOf(ref) : -1;
    if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
    return child;
  }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
  }
}

function makeRow(name) {
  const row = new FakeElement("setting");
  row.dataset.settingName = name;
  row.ownerDocument = { createElement: () => new FakeElement() };
  const titleRow = row.appendChild(new FakeElement("setting-title-row"));
  titleRow.appendChild(new FakeElement("setting-marquee"));
  titleRow.appendChild(new FakeElement("setting-risk-badge"));
  return row;
}

function badgeContext() {
  const context = vm.createContext({ getUIText: (_key, fallback) => fallback });
  vm.runInContext(sliceFunction(settingPage, "syncDriverMonitoringTestBadge"), context);
  return context;
}

test("turning driver monitoring OFF asks once; cancel writes nothing, ON never asks", async () => {
  const writes = [];
  let accepted = false;
  let prompts = 0;
  const context = vm.createContext({
    window: { CarrotParamCommit: { create: () => ({ commit: async (...args) => writes.push(args) }) } },
    appConfirm: async () => { prompts++; return accepted; },
    getUIText: (_key, fallback) => fallback,
  });
  vm.runInContext(api, context);

  await assert.rejects(context.setParam("DriverMonitoringEnabled", 0), /stays ON/);
  assert.equal(writes.length, 0);

  accepted = true;
  await context.setParam("DriverMonitoringEnabled", "0");
  assert.deepEqual(writes.map(([name, value]) => [name, value]), [["DriverMonitoringEnabled", "0"]]);

  await context.setParam("DriverMonitoringEnabled", 1);
  assert.equal(prompts, 2);
  assert.equal(writes.length, 2);
});

test("OFF shows a TEST marker on the DM row and ON removes it", () => {
  const context = badgeContext();
  const row = makeRow("DriverMonitoringEnabled");

  context.syncDriverMonitoringTestBadge(row, 1);
  assert.equal(row.querySelector(".setting-dm-test-badge"), null);
  assert.equal(row.classList.contains("setting--dm-test"), false);

  context.syncDriverMonitoringTestBadge(row, "0");
  const badge = row.querySelector(".setting-dm-test-badge");
  assert.ok(badge);
  assert.match(badge.textContent, /TEST/);
  assert.match(badge.className, /chip--danger/);
  assert.equal(row.classList.contains("setting--dm-test"), true);
  // Placed right after the title, before the risk badge.
  assert.equal(row.querySelector(".setting-title-row").children[1], badge);

  context.syncDriverMonitoringTestBadge(row, 0);
  assert.equal(row.querySelector(".setting-title-row").children.filter((c) => c.matches(".setting-dm-test-badge")).length, 1);

  context.syncDriverMonitoringTestBadge(row, true);
  assert.equal(row.querySelector(".setting-dm-test-badge"), null);
  assert.equal(row.classList.contains("setting--dm-test"), false);
});

test("other settings never receive the TEST marker", () => {
  const context = badgeContext();
  const row = makeRow("DriverMonitoringMode");
  context.syncDriverMonitoringTestBadge(row, 0);
  assert.equal(row.querySelector(".setting-dm-test-badge"), null);
  assert.equal(row.classList.contains("setting--dm-test"), false);
});

test("every control sync applies the marker and a failed write restores the saved value", () => {
  assert.match(sliceFunction(settingPage, "syncSettingControlState"), /syncDriverMonitoringTestBadge\(row, value\);/);
  assert.match(settingPage,
    /catch \(e\) \{[\s\S]{0,200}syncSettingControlState\(el, val\.dataset\.committedValue\)[\s\S]{0,120}set_failed/);
});

test("all UI languages define the DM test strings", async () => {
  for (const lang of ["en", "ko", "zh"]) {
    const source = await readFile(new URL(`../js/translations/${lang}.js`, import.meta.url), "utf8");
    for (const key of ["dm_disable_test_confirm", "dm_disable_test_cancelled", "dm_test_badge"]) {
      assert.match(source, new RegExp(`\\b${key}: "`), `${lang} ${key}`);
    }
  }
});
