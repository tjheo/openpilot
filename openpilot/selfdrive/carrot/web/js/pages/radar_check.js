/* Receive-only stationary radar diagnostic; no settings writes or raw CAN export. */
(function () {
  "use strict";
  const button = document.getElementById("btnRadarCheck");
  const output = document.getElementById("radarCheckResult");
  const bus = document.getElementById("radarCheckBus");
  const mode = document.getElementById("radarCheckMode");
  let running = false;
  let controller;
  const labels = { stable_rx: "수신 안정", no_signal: "신호 없음", inconclusive: "판정 불가" };
  async function request(path, options = {}) {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal, cache: "no-store" });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } finally { clearTimeout(timeout); }
  }
  function render(job) {
    if (!job) throw new Error("검사 상태 없음");
    if (job.status !== "done") {
      output.textContent = `${job.status === "checking" ? "P·시동·정차 확인 중" : "CAN RX 검사 중"} ${job.progress}% (10초)\n버스 ${job.bus} / ${job.mode}`;
      return;
    }
    const result = job.result || {};
    const lines = [`${labels[result.verdict] || "판정 불가"}: ${result.reason || "결과 없음"}`, `선택 버스 ${job.bus} / ${job.mode}`];
    for (const row of result.rows || []) {
      lines.push(`bus ${row.bus} ${row.id}: ${row.count}개 / ${row.hz} Hz / 최근 ${row.age_ms ?? "?"} ms / 최대 간격 ${row.max_gap_ms} ms / 카운터 오류 ${row.counter_errors ?? "미검사"} / 체크섬 오류 ${row.checksum_errors ?? "미검사"}`);
    }
    if (result.checksum_note) lines.push(result.checksum_note);
    if (job.context) lines.push(`참고 설정: SCC 예상 bus ${job.context.expected_bus ?? "미확인"}, raw 예상 bus ${job.context.expected_raw_bus ?? 1}, CameraSCC ${job.context.HyundaiCameraSCC}, RadarTracks ${job.context.EnableRadarTracks} (수신 증명 아님)`);
    lines.push("통신 수신 확인이며 배선 전체/가감속 보증 아님. 전달 RX는 원 송신 ECU 증명이 아님.");
    output.textContent = lines.join("\n"); // never interpret server strings as markup
  }
  if (!button) return;
  button.addEventListener("click", async () => {
    if (running) return;
    running = true;
    button.disabled = bus.disabled = mode.disabled = true;
    const deadline = Date.now() + 18000;
    try {
      let data = await request("/api/radar_check/start", { method: "POST", headers: { "Content-Type": "application/json", "X-Carrot-Radar-Check": "1" },
        body: JSON.stringify({ bus: bus.value === "auto" ? "auto" : Number(bus.value), mode: mode.value }) });
      const jobId = data.job.id;
      render(data.job);
      while (data.job.status !== "done") {
        if (Date.now() >= deadline) throw new Error("검사 응답 시간 초과 (서버 검사는 자동 종료됩니다)");
        await new Promise(resolve => setTimeout(resolve, 300));
        data = await request("/api/radar_check/status");
        if (data.job?.id !== jobId) throw new Error("다른 검사로 변경됨. 다시 확인하세요");
        render(data.job);
      }
    } catch (error) { output.textContent = `판정 불가: ${error.message}`; }
    finally { running = false; button.disabled = bus.disabled = mode.disabled = false; }
  });
  window.addEventListener("pagehide", () => controller?.abort());
})();
