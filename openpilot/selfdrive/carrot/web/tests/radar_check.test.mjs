import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const script = readFileSync(new URL("../js/pages/radar_check.js", import.meta.url), "utf8");

function fixture(fetch) {
  const elements = new Map();
  for (const id of ["btnRadarCheck", "radarCheckResult", "radarCheckBus", "radarCheckMode"]) {
    assert.ok(html.includes(`id="${id}"`));
    elements.set(id, { disabled: false, value: id === "radarCheckBus" ? "auto" : "scc", textContent: "", addEventListener(_name, listener) { this.click = listener; } });
  }
  const window = { addEventListener() {} };
  vm.runInNewContext(script, { document: { getElementById: id => elements.get(id) }, window, fetch, AbortController,
    setTimeout(fn, ms) { return ms === 300 ? setTimeout(fn, 0) : setTimeout(fn, ms); }, clearTimeout, Date, Error, JSON, Number, Promise });
  return elements;
}

const job = (status, result = null) => ({ id: "test", status, progress: status === "done" ? 100 : 20, bus: 2, mode: "scc", result });
const response = body => ({ ok: true, status: 200, async json() { return { ok: true, ...body }; } });

test("actual Tools markup exposes button, controls and script", () => {
  const tools = html.slice(html.indexOf('id="pageTools"'), html.indexOf('data-tools-group="git"'));
  assert.match(tools, /정차 레이더 수신 검사/);
  assert.match(tools, /value="raw"/);
  assert.match(html, /src="\/js\/pages\/radar_check.js"/);
});

test("render + real click handler polls result, prevents rapid click and escapes strings", async () => {
  const calls = [];
  const elements = fixture(async (path, options) => {
    calls.push({ path, options });
    return calls.length === 1 ? response({ job: job("running") }) : response({ job: job("done", {
      verdict: "stable_rx", reason: "<img src=x onerror=alert(1)>", rows: [{ bus: 2, id: "0x420", hz: 50, count: 500, age_ms: 0, max_gap_ms: 20, counter_errors: 0, checksum_errors: null }],
    }) });
  });
  const button = elements.get("btnRadarCheck");
  const pending = button.click();
  await button.click();
  assert.equal(button.disabled, true);
  await pending;
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, "/api/radar_check/start");
  assert.equal(calls[0].options.headers["X-Carrot-Radar-Check"], "1");
  assert.equal(calls[1].path, "/api/radar_check/status");
  assert.match(elements.get("radarCheckResult").textContent, /수신 안정/);
  assert.match(elements.get("radarCheckResult").textContent, /<img src=x/); // textContent only
  assert.match(elements.get("radarCheckResult").textContent, /배선 전체\/가감속 보증 아님/);
  assert.equal(button.disabled, false);
});

test("explicit bus/raw and HTTP errors recover controls without asserting pass", async () => {
  let payload;
  const elements = fixture(async (_path, options) => {
    payload = JSON.parse(options.body);
    return { ok: false, async json() { return { ok: false, error: "P 기어 확인 필요" }; } };
  });
  elements.get("radarCheckBus").value = "1";
  elements.get("radarCheckMode").value = "raw";
  await elements.get("btnRadarCheck").click();
  assert.deepEqual(payload, { bus: 1, mode: "raw" });
  assert.match(elements.get("radarCheckResult").textContent, /판정 불가: P 기어/);
  assert.equal(elements.get("radarCheckMode").disabled, false);
});

test("poll failure and changed job are inconclusive", async () => {
  for (const fail of [true, false]) {
    let calls = 0;
    const elements = fixture(async () => {
      if (++calls === 1) return response({ job: job("running") });
      if (fail) throw new Error("network disconnected");
      return response({ job: { ...job("done"), id: "other" } });
    });
    await elements.get("btnRadarCheck").click();
    assert.match(elements.get("radarCheckResult").textContent, /판정 불가/);
    assert.equal(elements.get("btnRadarCheck").disabled, false);
  }
});
