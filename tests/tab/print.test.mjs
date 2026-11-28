// The Print tab's view logic (design 2026-11-28, 6.6). Run: node --test tests/tab/
import { test } from "node:test";
import assert from "node:assert/strict";
import { view } from "../../multiclaude/addon/relay-public/print.js";

const printer = { model: "P2S", stoppable_states: ["PAUSE", "PREPARE", "RUNNING"], ams_units: 4 };

function state(gcode, extra = {}) {
  return {
    snapshot: {
      status: { gcode_state: gcode, percent: 42, job_name: "bracket", layer: 3, total_layers: 9, remaining_min: 72, hms: [] },
      printer, slots: [], jobs: [], history: [], pending: [], notes: [], currency: { code: "UAH", symbol: "грн" },
      ...extra,
    },
    updated: 1, down: null,
  };
}

const control = (labels) => ({ message_id: "9", kind: "control", text: "t", labels, expires_at: null });

test("pause only while running", () => {
  assert.ok(view(state("RUNNING")).buttons.includes("pause"));
  assert.ok(!view(state("PAUSE")).buttons.includes("pause"));
  assert.ok(!view(state("IDLE")).buttons.includes("pause"));
});

test("resume needs a control pending", () => {
  assert.ok(!view(state("PAUSE")).buttons.includes("resume"));
  const v = view(state("PAUSE", { pending: [control(["▶️ Продолжить", "Остановить…"])] }));
  assert.ok(v.buttons.includes("resume"));
  assert.deepEqual(v.resume, { message_id: "9", index: 0 });
});

test("stop follows the profile's stoppable states", () => {
  assert.ok(view(state("PREPARE")).buttons.includes("stop"));
  assert.ok(!view(state("FINISH")).buttons.includes("stop"));
  const a1 = state("PREPARE");
  a1.snapshot.printer = { ...printer, stoppable_states: ["RUNNING"] };
  assert.ok(!view(a1).buttons.includes("stop"));
});

test("snapshot always offered while the service answers", () => {
  assert.ok(view(state("IDLE")).buttons.includes("snapshot"));
});

test("a1 state shows one external slot", () => {
  const s = state("IDLE", { slots: [{ slot: "external", tray_type: "PLA", color_rgba: "FFFFFFFF", spool: null }] });
  const v = view(s);
  assert.equal(v.slots.length, 1);
  assert.equal(v.slots[0].label, "Внешняя катушка");
});

test("start pending opens the sheet with its labels", () => {
  const pending = [{ message_id: "901", kind: "start", text: "🖨 Печатать?", labels: ["Стол чистый, печатать", "Отмена"], expires_at: 5 }];
  const v = view(state("IDLE", { pending }));
  assert.deepEqual(v.confirm, { message_id: "901", kind: "start", text: "🖨 Печатать?", labels: ["Стол чистый, печатать", "Отмена"] });
});

test("the newest start or stop pending wins, control never opens the sheet", () => {
  const pending = [
    { message_id: "902", kind: "stop", text: "stop?", labels: ["Да, остановить", "Отмена"], expires_at: 5 },
    { message_id: "901", kind: "start", text: "go?", labels: ["a", "b"], expires_at: 5 },
    control(["⏸ Пауза", "Остановить…"]),
  ];
  assert.equal(view(state("RUNNING", { pending })).confirm.message_id, "902");
  assert.equal(view(state("RUNNING", { pending: [control(["⏸ Пауза", "Остановить…"])] })).confirm, null);
});

test("down state shows the banner and keeps the data", () => {
  const s = state("RUNNING");
  s.down = "Сервис принтера не отвечает, данные на 12:00";
  const v = view(s);
  assert.equal(v.down, s.down);
  assert.equal(v.status.state, "RUNNING");
  assert.deepEqual(v.buttons, []);
});

test("no state yet", () => {
  const v = view(null);
  assert.equal(v.loading, true);
  assert.deepEqual(v.buttons, []);
});

test("jobs list their plates and notes show", () => {
  const s = state("IDLE", {
    jobs: [{ id: 7, name: "bracket", kind: "kit", status: "sliced", plates: [{ index: 1, minutes: 60, grams: 12.5, cost: 9.1 }, { index: 2, minutes: 5, grams: 1, cost: null }] }],
    notes: [{ at: 1, text: "Печать не подготовлена: принтер занят" }],
  });
  const v = view(s);
  assert.deepEqual(v.jobs[0].plates.map((p) => p.index), [1, 2]);
  assert.equal(v.notes[0], "Печать не подготовлена: принтер занят");
});

// A DOM stand-in: enough for mount() to draw, so runtime errors in drawing show here.
function fakeApi() {
  const node = (tag, props = {}, ...children) => {
    const n = { tag, props, children: [], hidden: props.hidden, append(...c) { this.children.push(...c.flat().filter(Boolean)); },
      replaceChildren(...c) { this.children = c.flat().filter(Boolean); } };
    n.append(...children);
    return n;
  };
  const api = { el: node, listeners: [], requests: [], toasts: [],
    onState(fn) { this.listeners.push(fn); },
    async request(action, values) { this.requests.push([action, values]); return { text: "ok" }; },
    toast(t) { this.toasts.push(t); } };
  const root = node("div");
  return { api, root };
}

function find(n, pred, out = []) {
  if (n && typeof n === "object") {
    if (pred(n)) out.push(n);
    for (const c of n.children || []) find(c, pred, out);
  }
  return out;
}

test("mount draws every state without throwing, and buttons send strings", async () => {
  const { api, root } = fakeApi();
  const { mount } = await import("../../multiclaude/addon/relay-public/print.js");
  mount(root, api);
  const pending = [{ message_id: "901", kind: "start", text: "go?", labels: ["Стол чистый, печатать", "Отмена"], expires_at: 5 },
    control(["▶️ Продолжить", "Остановить…"])];
  const s = state("PAUSE", { pending, jobs: [{ id: 7, name: "b", status: "sliced", plates: [{ index: 1, minutes: 5, grams: 1, cost: 2 }] }],
    slots: [{ slot: 1, tray_type: "PLA", color_rgba: "FF0000FF", spool: { material: "PLA", color_hex: "#FF0000", remaining_g: 640 } }] });
  for (const next of [null, state("IDLE"), s, { ...s, down: "x" }]) api.listeners[0](next);
  api.listeners[0](s);
  const buttons = find(root, (n) => n.tag === "button");
  const go = buttons.find((b) => b.children.includes("Стол чистый, печатать"));
  await go.props.onclick();
  const resume = buttons.find((b) => b.children.includes("▶️ Продолжить"));
  await resume.props.onclick();
  assert.deepEqual(api.requests, [["confirm", { message_id: "901", index: "0" }], ["confirm", { message_id: "9", index: "0" }]]);
  for (const [, values] of api.requests) for (const v of Object.values(values)) assert.equal(typeof v, "string");
});
