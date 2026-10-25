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

function badgeContext(sessionValues = { DriverMonitoringSessionDisabled: false }) {
  const reads = [];
  const context = vm.createContext({
    getUIText: (_key, fallback) => fallback,
    bulkGet: async (names) => {
      reads.push(names);
      if (sessionValues instanceof Error) throw sessionValues;
      return sessionValues;
    },
  });
  const start = settingPage.indexOf("function paramFlagIsOn(");
  const end = settingPage.indexOf("let settingGroupTransitionLock", start);
  assert.ok(start > 0 && end > start, "DM badge block is present");
  vm.runInContext(settingPage.slice(start, end), context);
  context.reads = reads;
  return context;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const badgeOf = (row) => row.querySelector(".setting-dm-test-badge");

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

test("saved OFF shows a TEST marker on the DM row and ON removes it", async () => {
  const context = badgeContext();
  const row = makeRow("DriverMonitoringEnabled");

  context.syncDriverMonitoringTestBadge(row, 1);
  await settle();
  assert.equal(badgeOf(row), null);
  assert.equal(row.classList.contains("setting--dm-test"), false);

  // /api/params_bulk returns real booleans for BOOL params.
  for (const off of [false, 0, "0", "false"]) {
    context.syncDriverMonitoringTestBadge(row, off);
    await settle();
    const badge = badgeOf(row);
    assert.ok(badge, `OFF marker for ${JSON.stringify(off)}`);
    assert.equal(badge.textContent, "TEST · DM OFF");
    assert.match(badge.className, /chip--danger/);
    assert.equal(row.classList.contains("setting--dm-test"), true);
    // Placed right after the title, before the risk badge; never duplicated.
    const titleChildren = row.querySelector(".setting-title-row").children;
    assert.equal(titleChildren[1], badge);
    assert.equal(titleChildren.filter((c) => c.matches(".setting-dm-test-badge")).length, 1);
  }

  for (const on of [true, 1, "1", "true"]) {
    context.syncDriverMonitoringTestBadge(row, false);
    await settle();
    context.syncDriverMonitoringTestBadge(row, on);
    await settle();
    assert.equal(badgeOf(row), null, `no marker for ${JSON.stringify(on)}`);
    assert.equal(row.classList.contains("setting--dm-test"), false);
  }
});

test("saved ON with the CANCEL session marker set is not shown as plain ON", async () => {
  const context = badgeContext({ DriverMonitoringSessionDisabled: true });
  const row = makeRow("DriverMonitoringEnabled");
  context.syncDriverMonitoringTestBadge(row, true);
  await settle();
  assert.deepEqual(context.reads.map((names) => [...names]), [["DriverMonitoringSessionDisabled"]]);
  assert.equal(badgeOf(row).textContent, "TEST · DM OFF this drive (CANCEL ×3)");
  assert.equal(row.classList.contains("setting--dm-test"), true);

  // Saved OFF takes precedence in the label.
  context.syncDriverMonitoringTestBadge(row, false);
  await settle();
  assert.equal(badgeOf(row).textContent, "TEST · DM OFF");
});

test("a failed session-marker read shows saved ON as unknown, never plain ON", async () => {
  const context = badgeContext(new Error("offline"));
  const row = makeRow("DriverMonitoringEnabled");
  context.syncDriverMonitoringTestBadge(row, false);
  await settle();
  assert.equal(badgeOf(row).textContent, "TEST · DM OFF");

  context.syncDriverMonitoringTestBadge(row, true);
  await settle();
  assert.equal(badgeOf(row).textContent, "Saved ON · this-drive status unknown");
  assert.match(badgeOf(row).className, /chip--warning/);
  assert.equal(row.classList.contains("setting--dm-test"), false);
});

test("a failed re-read drops a previously known session state back to unknown", async () => {
  let fail = false;
  const context = badgeContext({ DriverMonitoringSessionDisabled: false });
  context.bulkGet = async () => {
    if (fail) throw new Error("offline");
    return { DriverMonitoringSessionDisabled: false };
  };
  const row = makeRow("DriverMonitoringEnabled");
  await context.syncDriverMonitoringTestBadge(row, true);
  assert.equal(badgeOf(row), null);
  fail = true;
  await context.syncDriverMonitoringTestBadge(row, true);
  assert.equal(badgeOf(row).textContent, "Saved ON · this-drive status unknown");
});

test("a response without the marker is unknown, not ON", async () => {
  const context = badgeContext({});
  const row = makeRow("DriverMonitoringEnabled");
  await context.syncDriverMonitoringTestBadge(row, 1);
  assert.equal(badgeOf(row).textContent, "Saved ON · this-drive status unknown");
});

test("rows sharing one in-flight read all update; a row detached by navigation does not", async () => {
  let release;
  const reads = [];
  const context = badgeContext();
  context.bulkGet = (names) => {
    reads.push([...names]);
    return new Promise((resolve) => { release = resolve; });
  };

  const oldRow = makeRow("DriverMonitoringEnabled");
  oldRow.isConnected = true;
  const first = context.syncDriverMonitoringTestBadge(oldRow, true);
  await settle();
  // Pending read: saved ON is shown as unknown, not ON.
  assert.equal(badgeOf(oldRow).textContent, "Saved ON · this-drive status unknown");

  // Navigation re-renders the group: old row detached, new row asks while the read is in flight.
  oldRow.isConnected = false;
  const newRow = makeRow("DriverMonitoringEnabled");
  newRow.isConnected = true;
  const second = context.syncDriverMonitoringTestBadge(newRow, true);
  const otherRow = makeRow("DriverMonitoringEnabled");
  const third = context.syncDriverMonitoringTestBadge(otherRow, true);
  assert.equal(second, first);
  assert.equal(third, first);
  assert.equal(reads.length, 1, "one shared read");

  release({ DriverMonitoringSessionDisabled: true });
  await first;
  for (const row of [newRow, otherRow]) {
    assert.equal(badgeOf(row).textContent, "TEST · DM OFF this drive (CANCEL ×3)");
    assert.equal(row.classList.contains("setting--dm-test"), true);
  }
  // Detached row keeps its earlier render and is not touched.
  assert.equal(oldRow.dataset.dmSessionDisabled, undefined);
  assert.equal(badgeOf(oldRow).textContent, "Saved ON · this-drive status unknown");

  // After the read settles, the next sync issues a fresh read.
  const fourth = context.syncDriverMonitoringTestBadge(newRow, true);
  assert.notEqual(fourth, first);
  await settle();
  assert.equal(reads.length, 2);
  release({ DriverMonitoringSessionDisabled: false });
  await fourth;
  assert.equal(badgeOf(newRow), null);
});

test("other settings never receive the TEST marker or read the session marker", async () => {
  const context = badgeContext({ DriverMonitoringSessionDisabled: true });
  const row = makeRow("DriverMonitoringMode");
  context.syncDriverMonitoringTestBadge(row, 0);
  await settle();
  assert.equal(badgeOf(row), null);
  assert.equal(row.classList.contains("setting--dm-test"), false);
  assert.equal(context.reads.length, 0);
});

test("every control sync applies the marker and a failed write restores the saved value", () => {
  assert.match(sliceFunction(settingPage, "syncSettingControlState"), /syncDriverMonitoringTestBadge\(row, value\);/);
  assert.match(settingPage,
    /catch \(e\) \{[\s\S]{0,200}syncSettingControlState\(el, val\.dataset\.committedValue\)[\s\S]{0,120}set_failed/);
});

test("all UI languages define the DM test strings", async () => {
  for (const lang of ["en", "ko", "zh"]) {
    const source = await readFile(new URL(`../js/translations/${lang}.js`, import.meta.url), "utf8");
    for (const key of ["dm_disable_test_confirm", "dm_disable_test_cancelled", "dm_test_badge", "dm_test_badge_session", "dm_test_badge_unknown"]) {
      assert.match(source, new RegExp(`\\b${key}: "`), `${lang} ${key}`);
    }
  }
});
