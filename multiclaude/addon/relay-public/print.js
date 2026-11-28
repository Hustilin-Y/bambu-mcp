/*
 * The Print tab of the chat Mini App (bambu add-in), the owner's only. Served publicly by the relay, so
 * nothing private is in here: the printer's state comes through the chat's signed feed (api.onState), and
 * every action is an api.request the router checks like any message. Starting, resuming and stopping a
 * print are presses on the buttons the service sent to Telegram (action "confirm"), never requests of
 * their own. Design: bambu-mcp docs/superpowers/specs/2026-11-28-miniapp-print-tab-design.md, 6.6.
 */

const STATES = {
  IDLE: "Свободен", RUNNING: "Печатает", PAUSE: "На паузе", PREPARE: "Готовится", FINISH: "Готово",
  FAILED: "Ошибка", UNKNOWN: "Нет данных",
};
const OUTCOMES = { finished: "готово", failed: "ошибка", cancelled: "остановлено" };

/* What the tab shows for a state, without the DOM: the tests check this. */
export function view(state) {
  const snap = state?.snapshot;
  if (!snap) return { loading: true, down: state?.down || null, buttons: [], slots: [], jobs: [], history: [], notes: [], confirm: null };
  const status = snap.status || {};
  const printer = snap.printer || {};
  const gcode = status.gcode_state || "UNKNOWN";
  const pending = snap.pending || [];
  const control = pending.find((p) => p.kind === "control") || null;
  const down = state.down || null;
  const buttons = [];
  if (!down) {
    buttons.push("snapshot");
    if (gcode === "RUNNING") buttons.push("pause");
    if (gcode === "PAUSE" && control) buttons.push("resume");
    if ((printer.stoppable_states || []).includes(gcode)) buttons.push("stop");
  }
  const sheet = pending.find((p) => p.kind === "start" || p.kind === "stop") || null;
  return {
    loading: false,
    down,
    status: {
      state: gcode,
      label: STATES[gcode] || gcode,
      job: status.job_name || null,
      percent: status.percent ?? null,
      layer: status.layer ?? null,
      layers: status.total_layers ?? null,
      remaining: status.remaining_min ?? null,
      hms: (status.hms || []).map((h) => `${h.severity}: ${h.code}`),
      model: printer.model || "",
    },
    buttons,
    resume: control && gcode === "PAUSE" ? { message_id: control.message_id, index: 0 } : null,
    confirm: sheet ? { message_id: sheet.message_id, kind: sheet.kind, text: sheet.text, labels: sheet.labels } : null,
    slots: (snap.slots || []).map((s) => ({
      label: s.slot === "external" ? "Внешняя катушка" : `Слот ${s.slot}`,
      color: colorOf(s),
      material: s.spool?.material || s.tray_type || "пусто",
      grams: s.spool ? `${s.spool.remaining_g} г` : "",
    })),
    jobs: (snap.jobs || []).map((j) => ({ id: j.id, name: j.name, status: j.status, plates: j.plates || [] })),
    history: (snap.history || []).map((h) => ({
      name: h.name, outcome: OUTCOMES[h.outcome] || h.outcome || "идёт", grams: h.grams, cost: h.cost,
    })),
    notes: (snap.notes || []).map((n) => n.text),
    symbol: snap.currency?.symbol || "",
    truncated: Boolean(snap.truncated),
  };
}

function colorOf(slot) {
  const hex = slot.spool?.color_hex || (slot.color_rgba ? `#${slot.color_rgba.slice(0, 6)}` : null);
  return hex && /^#[0-9a-fA-F]{6}$/.test(hex) ? hex : null;
}

function minutes(m) {
  if (m == null) return "";
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
}

export function mount(root, api) {
  const { el } = api;
  let state = null;
  let busy = false;
  const body = el("div", {});
  const sheet = el("div", { className: "pt-sheet", hidden: true });
  root.append(style(el), body, sheet);
  api.onState((next) => {
    state = next;
    draw();
  });
  draw();

  async function ask(action, values = {}) {
    if (busy) return;
    busy = true;
    draw();
    const answer = await api.request(action, values);
    busy = false;
    if (answer?.error) api.toast(answer.error);
    else if (answer?.text) api.toast(answer.text);
    draw();
    return answer;
  }

  function draw() {
    const v = view(state);
    const parts = [];
    if (v.down) parts.push(el("p", { className: "pt-banner" }, v.down));
    if (v.loading) {
      body.replaceChildren(...parts, el("p", { className: "sub" }, "Загружаю…"));
      drawSheet(null);
      return;
    }
    parts.push(statusCard(v), ...v.notes.map((n) => el("p", { className: "pt-note" }, n)));
    parts.push(section("Слоты", v.slots.length ? v.slots.map(slotNode) : [el("p", { className: "sub" }, "Принтер не прислал слоты.")]));
    parts.push(section("Задания", v.jobs.length ? v.jobs.map((j) => jobNode(j, v)) : [el("p", { className: "sub" }, "Заданий нет.")]));
    parts.push(section("История", v.history.length ? v.history.map((h) => historyNode(h, v)) : [el("p", { className: "sub" }, "Печатей ещё не было.")]));
    if (v.truncated) parts.push(el("p", { className: "sub" }, "Старые задания не поместились."));
    parts.push(el("button", { className: "pt-button", onclick: capabilities }, "Что умеет система ▸"));
    body.replaceChildren(...parts);
    drawSheet(v.confirm);
  }

  function statusCard(v) {
    const s = v.status;
    const line = [s.model, s.label, s.percent != null && s.state !== "IDLE" ? `${s.percent}%` : "",
      s.layer != null && s.layers ? `слой ${s.layer}/${s.layers}` : "", s.remaining ? `ещё ${minutes(s.remaining)}` : ""]
      .filter(Boolean).join(" · ");
    const actions = {
      snapshot: ["📷 Снимок", () => ask("snapshot")],
      pause: ["⏸ Пауза", () => ask("pause")],
      resume: ["▶️ Продолжить", () => ask("confirm", { message_id: v.resume.message_id, index: String(v.resume.index) })],
      stop: ["⏹ Стоп", () => ask("stop")],
    };
    return el("div", { className: "pt-card" },
      el("div", { className: "pt-title" }, line),
      s.job ? el("div", { className: "sub" }, s.job) : null,
      ...s.hms.map((h) => el("div", { className: "pt-warn" }, `⚠ HMS ${h}`)),
      el("div", { className: "pt-row" }, ...v.buttons.map((b) =>
        el("button", { className: "pt-button", disabled: busy, onclick: actions[b][1] }, actions[b][0]))));
  }

  function slotNode(s) {
    return el("div", { className: "pt-slot" },
      el("span", { className: "pt-swatch", style: s.color ? `background:${s.color}` : "" }),
      el("span", {}, `${s.label}: ${s.material}`), s.grams ? el("span", { className: "sub" }, ` ${s.grams}`) : null);
  }

  function jobNode(j, v) {
    const plates = j.plates.map((p) => el("div", { className: "pt-row" },
      el("span", { className: "sub" }, [`стол ${p.index}`, minutes(p.minutes), p.grams != null ? `${p.grams} г` : "",
        p.cost != null ? `${p.cost} ${v.symbol}` : ""].filter(Boolean).join(" · ")),
      ["sliced", "chosen"].includes(j.status) && v.buttons.length && !v.down
        ? el("button", { className: "pt-button", disabled: busy || v.status.state !== "IDLE" && v.status.state !== "FINISH",
          onclick: () => ask("prepare", { job_id: String(j.id), plate: String(p.index) }) }, "🖨 Печать")
        : null));
    return el("div", { className: "pt-card" }, el("div", { className: "pt-title" }, `${j.name} (j${j.id})`),
      el("div", { className: "sub" }, j.status), ...plates);
  }

  function historyNode(h, v) {
    return el("div", { className: "pt-hist" }, el("span", {}, h.name),
      el("span", { className: "sub" }, [h.outcome, h.grams != null ? `${Math.round(h.grams)} г` : "",
        h.cost != null ? `${h.cost} ${v.symbol}` : ""].filter(Boolean).join(" · ")));
  }

  function drawSheet(confirm) {
    sheet.hidden = !confirm;
    if (!confirm) return sheet.replaceChildren();
    sheet.replaceChildren(
      el("div", { className: "pt-confirm" }, confirm.text),
      el("div", { className: "pt-row" }, ...confirm.labels.map((label, index) =>
        el("button", { className: `pt-button${index === 0 ? " primary" : ""}`, disabled: busy,
          onclick: () => ask("confirm", { message_id: confirm.message_id, index: String(index) }) }, label))));
  }

  async function capabilities() {
    const answer = await ask("capabilities");
    if (!answer?.text) return;
    let caps;
    try {
      caps = JSON.parse(answer.text);
    } catch {
      return;
    }
    const groups = { read: "Смотреть", change: "Менять", destructive: "Опасное (нужно подтверждение)" };
    const p = caps.printer || {};
    sheet.hidden = false;
    sheet.replaceChildren(
      el("div", { className: "pt-title" }, "Что умеет система"),
      el("p", { className: "sub" }, [p.model, p.ams_units ? `AMS: ${p.ams_units} × ${p.ams_slots}` : "без AMS, внешняя катушка",
        `камера ${p.camera}`, `носитель ${p.storage}`].filter(Boolean).join(" · ")),
      ...(p.unverified?.length ? [el("p", { className: "sub" }, `Не проверено на живом принтере: ${p.unverified.join(", ")}`)] : []),
      ...Object.entries(groups).map(([key, title]) => section(title,
        (caps.tools || []).filter((t) => t.class === key).map((t) => el("div", { className: "pt-hist" },
          el("code", {}, t.name), el("span", { className: "sub" }, t.summary))))),
      section("Кнопки", (caps.buttons || []).map((b) => el("div", { className: "pt-hist" },
        el("span", {}, b.label), el("span", { className: "sub" }, b.what)))),
      el("button", { className: "pt-button", onclick: draw }, "Закрыть"));
  }

  function section(title, children) {
    return el("div", { className: "pt-section" }, el("div", { className: "pt-head" }, title), ...children);
  }
}

function style(el) {
  return el("style", {}, `
.pt-banner { margin: 8px 0; padding: 8px 12px; border-radius: 10px; background: var(--warn); color: #000; }
.pt-note { margin: 8px 0; padding: 8px 12px; border-radius: 10px; background: var(--card); }
.pt-card { margin: 8px 0; padding: 10px 12px; border-radius: 12px; background: var(--card); }
.pt-title { font-weight: 600; overflow-wrap: anywhere; }
.pt-warn { color: var(--warn); }
.pt-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 6px; }
.pt-button { min-height: 40px; padding: 0 14px; border: 1px solid var(--separator); border-radius: 10px;
  background: transparent; color: var(--text); }
.pt-button.primary { background: var(--accent); color: var(--accent-text); border: 0; }
.pt-button:disabled { opacity: 0.45; }
.pt-section { margin: 12px 0; }
.pt-head { font-weight: 600; margin-bottom: 4px; }
.pt-slot, .pt-hist { display: flex; gap: 8px; align-items: baseline; padding: 4px 0; flex-wrap: wrap; }
.pt-swatch { width: 14px; height: 14px; border-radius: 4px; border: 1px solid var(--separator); display: inline-block; }
.pt-sheet { position: sticky; bottom: 0; margin-top: 12px; padding: 12px; border-radius: 14px; background: var(--card);
  box-shadow: 0 -2px 12px rgba(0, 0, 0, 0.25); }
.pt-confirm { white-space: pre-wrap; margin-bottom: 8px; }
`);
}
