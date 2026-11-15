// Tarn web UI. No build step: plain ES modules, DOM built with h().

// ---------------------------------------------------------------- helpers

const $ = (sel, root = document) => root.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  kids.forEach((kid) => el.append(kid));
  return el;
}

// replaceChildren() turns null into the text "null"; this skips empty slots.
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}

const GiB = 1024 ** 3;
const fmt = {
  bytes(n, digits = 1) {
    if (n === null || n === undefined) return "–";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : n < 10 ? digits : 0)} ${units[i]}`;
  },
  gib(n) {
    const g = n / GiB;
    return `${g >= 10 || Number.isInteger(g) ? Math.round(g) : g.toFixed(1)} GiB`;
  },
  rate(n) { return n === null || n === undefined ? "–" : `${fmt.bytes(n)}/s`; },
  pct(n) { return n === null || n === undefined ? "–" : `${n < 10 ? n.toFixed(1) : Math.round(n)}%`; },
  duration(sec) {
    sec = Math.max(0, Math.floor(sec));
    const d = Math.floor(sec / 86400), hr = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d) return `${d}d ${hr}h`;
    if (hr) return `${hr}h ${m}m`;
    return `${m}m`;
  },
  date(ts) { return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); },
};

const OS_LABEL = { linux: "Linux", windows: "Windows", other: "Other OS" };
const STATE_LABEL = {
  running: "Running", stopped: "Stopped", paused: "Paused", stopping: "Shutting down",
  crashed: "Crashed", suspended: "Suspended", unknown: "Unknown",
};

async function api(method, path, body) {
  const opts = { method, headers: { "X-Tarn": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (r.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Signed out");
  }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}
const GET = (p) => api("GET", p);

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 9000 : 4000);
}

// Run an async action from a button: disable it, show a spinner, report errors.
async function busy(btn, fn) {
  const original = [...btn.childNodes];
  btn.disabled = true;
  btn.prepend(h("span", { class: "busy", "aria-hidden": "true" }));
  try {
    return await fn();
  } catch (e) {
    if (e.message !== "Signed out") toast(e.message, "error");
    return undefined;
  } finally {
    btn.disabled = false;
    put(btn, ...original);
  }
}

function dialog({ title, body = [], confirm = "Confirm", danger = false, cancel = "Cancel" }) {
  const dlg = $("#dialog");
  return new Promise((resolve) => {
    const form = h("form", { method: "dialog" },
      h("h3", {}, title),
      ...[].concat(body),
      h("div", { class: "foot" },
        h("button", { class: "btn", value: "cancel", type: "submit" }, cancel),
        h("button", { class: `btn ${danger ? "btn-solid-danger" : "btn-primary"}`, value: "ok", type: "submit" }, confirm)));
    put(dlg, form);
    dlg.addEventListener("close", () => resolve({ ok: dlg.returnValue === "ok", form }), { once: true });
    dlg.returnValue = "";
    dlg.showModal();
    form.querySelector("button[value=ok]").focus();
  });
}

function every(ms, fn) {
  let stopped = false, timer = null;
  const tick = async () => {
    if (stopped) return;
    if (!document.hidden) {
      try { await fn(); } catch (e) { if (e.message !== "Signed out") console.warn(e); }
    }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

// ---------------------------------------------------------------- ridgelines

const POINTS = 150;

function ridgeSvg(cls = "ridge", grid = false) {
  const svg = s("svg", { class: cls, viewBox: `0 0 ${POINTS - 1} 100`, preserveAspectRatio: "none", "aria-hidden": "true" });
  if (grid) [25, 50, 75].forEach((y) => svg.append(s("line", { class: "grid", x1: 0, x2: POINTS - 1, y1: y, y2: y })));
  svg.append(s("path", { class: "fill" }), s("path", { class: "edge" }), s("path", { class: "second" }));
  return svg;
}

function ridgePaths(values, max) {
  const vals = values.slice(-POINTS);
  const offset = POINTS - vals.length;
  if (vals.length < 2) return { fill: "", edge: "" };
  const y = (v) => (100 - Math.min(1, Math.max(0, (v || 0) / max)) * 94).toFixed(2);
  let edge = "";
  vals.forEach((v, i) => { edge += `${i ? "L" : "M"}${offset + i},${y(v)}`; });
  const fill = `M${offset},100${edge.replace(/^M/, "L")}L${POINTS - 1},100Z`;
  return { fill, edge };
}

function setRidge(svg, values, max = 100, second = null) {
  const { fill, edge } = ridgePaths(values, max);
  svg.querySelector(".fill").setAttribute("d", fill);
  svg.querySelector(".edge").setAttribute("d", edge);
  svg.querySelector(".second").setAttribute("d", second ? ridgePaths(second, max).edge : "");
}

// ---------------------------------------------------------------- login

function showLogin() {
  stopView();
  $("#topbar").hidden = true;
  const range = ridgeSvg("range");
  // A still mountain line for the sign-in page.
  const peaks = Array.from({ length: POINTS }, (_, i) =>
    38 + 22 * Math.sin(i / 11) + 14 * Math.sin(i / 4.3 + 1) + 9 * Math.sin(i / 1.9));
  setRidge(range, peaks, 100);
  const err = h("div", { class: "error-text", role: "alert" });
  const pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", required: true });
  const btn = h("button", { class: "btn btn-primary", type: "submit", style: "width:100%;margin-top:6px" }, "Sign in");
  const form = h("form", {
    async onsubmit(ev) {
      ev.preventDefault();
      err.textContent = "";
      await busy(btn, async () => {
        try {
          await api("POST", "/api/login", { password: pw.value });
          pw.value = "";
          $("#topbar").hidden = false;
          route();
        } catch (e) { err.textContent = e.message; throw e; }
      });
    },
  },
    h("h1", {}, "Tarn"),
    h("p", {}, "Sign in to manage the virtual machines on this host."),
    h("div", { class: "field" }, h("label", { for: "pw" }, "Admin password"), pw),
    btn, err);
  put($("#main"), h("div", { class: "login" }, form, range));
  pw.focus();
}

// ---------------------------------------------------------------- router

let stopCurrent = null;
let routeSeq = 0;  // bumps on every navigation so slow views can't paint over newer ones
function stopView() {
  routeSeq++;
  if (stopCurrent) { try { stopCurrent(); } catch { /* ignore */ } }
  stopCurrent = null;
}

const views = {};

async function route() {
  stopView();
  const seq = routeSeq;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const name = parts[0] || "overview";
  const view = views[name] || views.overview;
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const active = a.dataset.nav === (name === "vm" || name === "new" ? "overview" : name);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const main = $("#main");
  put(main, h("div", { class: "wrap loading" }, "Loading…"));
  const container = h("div", { class: "view" });
  try {
    const stop = (await view(container, ...parts.slice(1))) || null;
    if (seq !== routeSeq) { if (stop) stop(); return; }
    stopCurrent = stop;
    put(main, container);
  } catch (e) {
    if (seq !== routeSeq || e.message === "Signed out") return;
    put(main, h("div", { class: "wrap section" },
      h("h2", {}, "This page couldn't load"),
      h("p", { class: "muted" }, e.message),
      h("a", { class: "btn", href: "#/" }, "Back to overview")));
  }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- overview

function vmActions(vm, onDone) {
  const act = (action, label, cls = "btn btn-sm") => h("button", {
    class: cls, type: "button",
    async onclick(ev) {
      ev.stopPropagation();
      await busy(ev.currentTarget, async () => {
        await api("POST", `/api/vms/${vm.uuid}/action`, { action });
        toast(`${vm.name}: ${label.toLowerCase()} sent`);
        onDone && onDone();
      });
    },
  }, label);
  if (vm.state === "running") {
    return [
      h("a", { class: "btn btn-sm", href: `#/vm/${vm.uuid}/console` }, "Console"),
      act("shutdown", "Shut down"),
    ];
  }
  if (vm.state === "paused") return [act("resume", "Resume")];
  if (vm.state === "stopped" || vm.state === "crashed") return [act("start", "Start", "btn btn-sm btn-primary")];
  return [];
}

views.overview = async (main) => {
  const range = ridgeSvg("range", true);
  const title = h("h1", {}, "…");
  const sub = h("div", { class: "sub" });
  const readouts = h("div", { class: "readouts" });
  const list = h("div", { class: "vmlist", role: "list" });
  const count = h("span", { class: "muted" });

  put(main, 
    h("section", { class: "horizon", "aria-label": "Host" },
      h("div", { class: "wrap" }, title, sub, readouts), range),
    h("section", { class: "wrap section" },
      h("div", { class: "section-head" }, h("h2", {}, "Virtual machines"), count),
      list));

  GET("/api/host").then((info) => {
    title.textContent = info.hostname;
    sub.textContent = `${info.os}, kernel ${info.kernel}. ${info.cpu_model}, ${info.threads} threads, ${fmt.gib(info.memory_total)} RAM.`;
    if (info.virt_type !== "kvm") sub.append(h("span", { style: "color:var(--warn)" }, " No KVM acceleration: see the Host page."));
  }).catch(() => {});

  const rows = new Map();

  const readout = (k, v, unit, warn) =>
    h("div", { class: `readout${warn ? " warn" : ""}` }, h("div", { class: "v" }, v, unit ? h("small", {}, /^[%°]/.test(unit) ? unit : ` ${unit}`) : null), h("div", { class: "k" }, k));

  function renderRow(vm) {
    let row = rows.get(vm.uuid);
    if (!row) {
      const ridge = ridgeSvg();
      row = {
        el: h("div", { class: "vmrow", role: "listitem" }),
        ridge, sig: "",
      };
      rows.set(vm.uuid, row);
    }
    const sig = `${vm.name}|${vm.state}|${vm.vcpus}|${vm.memory}|${vm.os_type}`;
    if (sig !== row.sig) {
      row.sig = sig;
      row.cpu = h("div", { class: "cpu" });
      row.mem = h("div", { class: "mem" });
      put(row.el, 
        h("div", { class: "vmname" },
          h("span", { class: `dot ${vm.state}`, title: STATE_LABEL[vm.state] || vm.state }),
          h("div", { style: "min-width:0" },
            h("a", { href: `#/vm/${vm.uuid}` }, vm.name),
            h("div", { class: "os" }, `${OS_LABEL[vm.os_type] || vm.os_type}, `,
              h("span", { class: `state ${vm.state}` }, (STATE_LABEL[vm.state] || vm.state).toLowerCase())))),
        h("div", { class: "res" }, `${vm.vcpus} vCPU, ${fmt.gib(vm.memory)}`),
        h("div", { class: "ridgecell" }, row.ridge),
        row.cpu, row.mem,
        h("div", { class: "acts" }, vmActions(vm, refresh)));
    }
    const st = vm.stats;
    row.cpu.textContent = st ? fmt.pct(st.cpu) : "";
    row.cpu.title = st ? `CPU, share of ${vm.vcpus} vCPUs` : "";
    row.mem.textContent = st && st.mem_max ? `${fmt.bytes(st.mem_used)} used` : "";
    setRidge(row.ridge, vm.cpu_history || [], 100);
    return row.el;
  }

  async function refresh() {
    const [data, hs] = await Promise.all([GET("/api/vms"), GET("/api/host/stats")]);
    const vms = data.vms;
    const running = vms.filter((v) => v.state === "running").length;
    count.textContent = vms.length ? `${running} of ${vms.length} running` : "";

    const cur = hs.current || {};
    const items = [
      readout("Host CPU", cur.cpu === null || cur.cpu === undefined ? "–" : Math.round(cur.cpu), "%"),
      readout("Memory in use", cur.mem_total ? (cur.mem_used / GiB).toFixed(1) : "–", cur.mem_total ? `of ${fmt.gib(cur.mem_total)}` : ""),
      readout("VMs running", running, `of ${vms.length}`),
    ];
    const p = hs.power || {};
    if (p.batteries && p.batteries.length) {
      const b = p.batteries[0];
      items.push(readout(p.ac_online ? "Battery, on charger" : "Battery, unplugged", b.capacity ?? "–", "%", !p.ac_online));
    }
    const tctl = (p.temps || []).find((t) => t.sensor === "k10temp" || t.sensor === "coretemp" || t.sensor === "zenpower");
    if (tctl) items.push(readout("CPU temperature", Math.round(tctl.celsius), "°C", tctl.celsius >= 90));
    if (p.platform_profile) items.push(readout("Power profile", p.platform_profile.replace(/-/g, " ")));
    put(readouts, ...items);
    setRidge(range, (hs.history || []).map((x) => x.cpu), 100);

    if (!vms.length) {
      rows.clear();
      put(list, h("div", { class: "empty" },
        h("h3", {}, "No virtual machines yet"),
        h("p", { class: "muted" }, "Create one from an installer ISO, or import a disk image you already have, such as a Windows disk converted from Disk2vhd."),
        h("a", { class: "btn btn-primary", href: "#/new" }, "Create VM")));
      return;
    }
    const seen = new Set(vms.map((v) => v.uuid));
    for (const id of [...rows.keys()]) if (!seen.has(id)) rows.delete(id);
    const els = vms.map(renderRow);
    // Only touch the DOM order when it changed, so hover and focus survive polling.
    if (els.length !== list.children.length || els.some((el, i) => list.children[i] !== el)) put(list, ...els);
  }

  return every(2000, refresh);
};

// ---------------------------------------------------------------- VM page

const TABS = [
  ["overview", "Overview"], ["console", "Console"], ["hardware", "Hardware"],
  ["snapshots", "Snapshots"], ["xml", "XML"],
];

views.vm = async (main, uuid, tab = "overview") => {
  if (!TABS.some(([k]) => k === tab)) tab = "overview";
  let vm = await GET(`/api/vms/${uuid}`);
  const head = h("div", { class: "vmhead" });
  const body = h("div", { class: "wrap section" });
  put(main, head, body);
  let lastState = null;
  let tabStop = null;

  const reload = async () => {
    vm = await GET(`/api/vms/${uuid}`);
    renderHead();
    return vm;
  };

  function action(actionName, label, opts = {}) {
    return h("button", {
      class: opts.cls || "btn", type: "button",
      async onclick(ev) {
        const btn = ev.currentTarget;
        if (opts.confirm) {
          const { ok } = await dialog({ title: opts.confirm[0], body: h("p", {}, opts.confirm[1]), confirm: label, danger: true });
          if (!ok) return;
        }
        await busy(btn, async () => {
          await api("POST", `/api/vms/${uuid}/action`, { action: actionName });
          toast(`${vm.name}: ${label.toLowerCase()} sent`);
          setTimeout(reload, 700);
        });
      },
    }, label);
  }

  async function deleteVm() {
    const disks = vm.disks.filter((d) => d.device === "disk" && d.path);
    const cb = h("input", { type: "checkbox", id: "deldisks", name: "disks", checked: true });
    const { ok } = await dialog({
      title: `Delete ${vm.name}?`,
      body: [
        h("p", {}, vm.state === "running" ? "The VM is running and will be forced off first. " : "", "This removes the VM's definition, firmware variables and TPM state."),
        disks.length ? h("label", { class: "check", for: "deldisks" }, cb,
          h("span", {}, "Also delete its disk images",
            h("span", { class: "hint" }, disks.map((d) => d.path).join(", ")))) : null,
      ],
      confirm: "Delete VM", danger: true,
    });
    if (!ok) return;
    try {
      const res = await api("DELETE", `/api/vms/${uuid}?disks=${cb.checked ? 1 : 0}`);
      toast(`Deleted ${vm.name}${res.deleted_disks.length ? " and its disks" : ""}`);
      location.hash = "#/";
    } catch (e) { toast(e.message, "error"); }
  }

  function renderHead() {
    const ips = (vm.ips || []).map((ip) => ip.addr);
    const st = vm.state;
    const actions = [];
    if (st === "stopped" || st === "crashed") actions.push(action("start", "Start", { cls: "btn btn-primary" }));
    if (st === "running") {
      actions.push(h("a", { class: "btn btn-primary", href: `#/vm/${uuid}/console` }, "Open console"));
      actions.push(action("shutdown", "Shut down"));
      actions.push(action("reboot", "Reboot"));
      actions.push(action("pause", "Pause"));
      actions.push(action("poweroff", "Force off", { cls: "btn btn-danger", confirm: ["Force off this VM?", "This is like pulling the power cord. Anything unsaved inside the VM is lost."] }));
      actions.push(action("reset", "Reset", { cls: "btn btn-danger", confirm: ["Reset this VM?", "This is like pressing the reset button. Anything unsaved inside the VM is lost."] }));
    }
    if (st === "paused") {
      actions.push(action("resume", "Resume", { cls: "btn btn-primary" }));
      actions.push(action("poweroff", "Force off", { cls: "btn btn-danger", confirm: ["Force off this VM?", "Anything unsaved inside the VM is lost."] }));
    }
    if (st === "stopping") actions.push(action("poweroff", "Force off", { cls: "btn btn-danger", confirm: ["Force off this VM?", "It is already shutting down. Forcing it off may lose unsaved data."] }));
    actions.push(h("span", { style: "flex:1" }));
    actions.push(h("button", { class: "btn btn-danger", type: "button", onclick: deleteVm }, "Delete"));

    put(head, h("div", { class: "wrap" },
      h("div", { class: "crumbs" }, h("a", { href: "#/" }, "Overview")),
      h("div", { class: "title" },
        h("span", { class: `dot ${st}`, style: "width:14px;height:14px" }),
        h("h1", {}, vm.name),
        h("span", { class: `state ${st}` }, STATE_LABEL[st] || st)),
      h("div", { class: "facts" },
        h("span", {}, OS_LABEL[vm.os_type] || vm.os_type),
        h("span", {}, `${vm.vcpus} vCPU`),
        h("span", {}, fmt.gib(vm.memory)),
        h("span", {}, vm.firmware === "efi" ? `UEFI${vm.secure_boot ? " with Secure Boot" : ""}` : "BIOS"),
        vm.tpm ? h("span", {}, `TPM ${vm.tpm.version || ""}`) : null,
        ips.length ? h("span", { class: "mono" }, ips.join(", ")) : null),
      h("div", { class: "actionbar" }, actions),
      h("nav", { class: "tabs", role: "tablist", "aria-label": "VM sections" },
        TABS.map(([k, label]) => h("a", {
          href: `#/vm/${uuid}/${k}`, role: "tab", "aria-selected": String(k === tab),
        }, label)))));
  }

  function renderTab() {
    if (tabStop) { tabStop(); tabStop = null; }
    tabStop = tabs[tab](body, vm, uuid, reload) || null;
  }

  renderHead();
  lastState = vm.state;
  renderTab();

  const stopPoll = every(3000, async () => {
    const prev = lastState;
    await reload();
    lastState = vm.state;
    // Most tabs depend on the run state; refresh them when it changes.
    if (prev !== vm.state && tab !== "xml") renderTab();
  });
  return () => { stopPoll(); if (tabStop) tabStop(); };
};

const tabs = {};

tabs.overview = (body, vm, uuid) => {
  const chart = (label) => {
    const svg = ridgeSvg("ridge", true);
    const value = h("b", {}, "–");
    const note = h("span", {});
    return { svg, value, note, el: h("div", { class: "chart" }, h("div", { class: "top" }, h("div", {}, h("div", { class: "muted", style: "font-size:14px" }, label), value), note), svg) };
  };
  const cpu = chart("CPU");
  const mem = chart("Memory");
  const disk = chart("Disk read / write");
  const net = chart("Network in / out");

  const kv = h("dl", { class: "kv" });
  const fill = () => {
    const ips = (vm.ips || []).map((i) => `${i.addr}/${i.prefix} (${i.iface})`);
    const g = vm.guest;  // from the guest agent, while the VM runs
    const guest = g ? [
      g.os ? ["Guest OS", g.os + (g.hostname ? `, named ${g.hostname}` : "")] : null,
      ["Signed in", g.users.join(", ") || "nobody"],
      ...g.filesystems.map((f) => [`Drive ${f.mount.replace(/\\$/, "")}`,
        `${fmt.bytes(f.total - (f.used || 0))} free of ${fmt.bytes(f.total)}`]),
    ].filter(Boolean) : [];
    put(kv, ...[
      ["State", STATE_LABEL[vm.state] || vm.state],
      ...guest,
      ["IP addresses", ips.length ? h("span", { class: "mono" }, ips.join(", ")) : vm.state === "running" ? "Waiting for a DHCP lease or the guest agent" : "–"],
      ["Firmware", vm.firmware === "efi" ? `UEFI${vm.secure_boot ? ", Secure Boot on" : ""}` : "Legacy BIOS"],
      ["Machine", `${vm.machine || "–"}, ${vm.virt_type === "kvm" ? "KVM accelerated" : "software emulation (TCG)"}`],
      ["CPU model", vm.cpu_mode || "default"],
      ["Starts with host", vm.autostart ? "Yes" : "No"],
      ["Snapshots", String(vm.snapshot_count)],
      ["UUID", h("span", { class: "mono" }, vm.uuid)],
    ].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  };
  fill();

  put(body, 
    vm.state !== "running" ? h("p", { class: "note info" }, "Live charts appear while the VM is running.") : null,
    h("div", { class: "charts" }, cpu.el, mem.el, disk.el, net.el),
    h("div", { class: "panel", style: "padding-top:28px" }, h("h3", {}, "Summary"), kv));

  return every(2000, async () => {
    const { current: c, history } = await GET(`/api/vms/${uuid}/stats`);
    fill();
    if (!c) return;
    const cpuVals = history.map((p) => p.cpu);
    setRidge(cpu.svg, cpuVals, 100);
    cpu.value.textContent = fmt.pct(c.cpu);
    cpu.note.textContent = `of ${vm.vcpus} vCPU`;

    const memMax = c.mem_max || vm.memory;
    setRidge(mem.svg, history.map((p) => p.mem_used), memMax);
    mem.value.textContent = fmt.bytes(c.mem_used);
    mem.note.textContent = `of ${fmt.gib(memMax)}`;

    const peak = (keys) => Math.max(64 * 1024, ...history.flatMap((p) => keys.map((k) => p[k] || 0))) * 1.15;
    setRidge(disk.svg, history.map((p) => p.rd), peak(["rd", "wr"]), history.map((p) => p.wr));
    disk.value.textContent = fmt.rate(c.rd);
    disk.note.textContent = `write ${fmt.rate(c.wr)}`;
    setRidge(net.svg, history.map((p) => p.rx), peak(["rx", "tx"]), history.map((p) => p.tx));
    net.value.textContent = fmt.rate(c.rx);
    net.note.textContent = `out ${fmt.rate(c.tx)}`;
  });
};

tabs.console = (body, vm, uuid) => {
  if (vm.state !== "running" && vm.state !== "paused") {
    put(body, h("div", { class: "empty" },
      h("h3", {}, "The VM is not running"),
      h("p", { class: "muted" }, "Start it to see its screen here.")));
    return null;
  }
  const src = `/console?uuid=${encodeURIComponent(uuid)}&embed=1`;
  put(body, 
    h("iframe", { class: "console-frame", src, title: `Console of ${vm.name}`, allow: "fullscreen; clipboard-read; clipboard-write" }),
    h("p", { class: "muted", style: "margin-top:10px;font-size:14px" },
      "Click inside the screen to type. ",
      h("a", { href: src.replace("&embed=1", ""), target: "_blank", rel: "noopener" }, "Open in a new window")));
  return null;
};

tabs.hardware = (body, vm, uuid, reload) => {
  const running = vm.state !== "stopped" && vm.state !== "crashed";
  const rerender = async () => { const fresh = await reload(); tabs.hardware(body, fresh, uuid, reload); };

  // Resources
  const vcpus = h("input", { type: "number", min: 1, max: 256, value: vm.vcpus, id: "hw-vcpus" });
  const memGiB = h("input", { type: "number", min: 0.25, step: 0.25, value: +(vm.memory / GiB).toFixed(2), id: "hw-mem" });
  const saveRes = h("button", { class: "btn btn-primary", type: "submit" }, "Save");
  const resources = h("form", {
    class: "inline-form",
    async onsubmit(ev) {
      ev.preventDefault();
      await busy(saveRes, async () => {
        const res = await api("PATCH", `/api/vms/${uuid}`, { vcpus: +vcpus.value, memory_mib: Math.round(+memGiB.value * 1024) });
        toast(res.applies_after_restart ? "Saved. The change applies the next time the VM starts." : "Saved");
        await rerender();
      });
    },
  },
    h("div", { class: "field", style: "width:110px" }, h("label", { for: "hw-vcpus" }, "vCPUs"), vcpus),
    h("div", { class: "field", style: "width:140px" }, h("label", { for: "hw-mem" }, "Memory (GiB)"), memGiB),
    saveRes);

  // Autostart + rename
  const auto = h("input", {
    type: "checkbox", id: "hw-auto", checked: vm.autostart,
    async onchange(ev) {
      try {
        await api("PATCH", `/api/vms/${uuid}`, { autostart: ev.target.checked });
        toast(ev.target.checked ? "Starts with the host" : "Won't start with the host");
      } catch (e) { toast(e.message, "error"); ev.target.checked = !ev.target.checked; }
    },
  });
  const newName = h("input", { type: "text", value: vm.name, id: "hw-name", disabled: running });
  const renameBtn = h("button", { class: "btn", type: "submit", disabled: running }, "Rename");
  const rename = h("form", {
    class: "inline-form",
    async onsubmit(ev) {
      ev.preventDefault();
      await busy(renameBtn, async () => { await api("PATCH", `/api/vms/${uuid}`, { name: newName.value }); toast("Renamed"); await rerender(); });
    },
  }, h("div", { class: "field", style: "flex:1;min-width:200px" }, h("label", { for: "hw-name" }, "Name"), newName), renameBtn);

  // Disks
  const diskTable = h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Device"), h("th", {}, "Bus"), h("th", {}, "Image"), h("th", { class: "num" }, "Size"), h("th", {}, ""))),
    h("tbody", {}));
  const isoSelects = [];
  for (const d of vm.disks) {
    let image, acts = null;
    if (d.device === "cdrom") {
      const sel = h("select", { "aria-label": `ISO for ${d.target}` }, h("option", { value: "" }, d.path ? "Loading ISOs…" : "Empty"));
      isoSelects.push([sel, d.path]);
      image = sel;
      acts = h("span", {},
        h("button", {
          class: "btn btn-sm", type: "button",
          async onclick(ev) {
            await busy(ev.currentTarget, async () => {
              await api("POST", `/api/vms/${uuid}/media`, { target: d.target, path: sel.value || null });
              toast(sel.value ? "ISO inserted" : "Drive emptied");
              await rerender();
            });
          },
        }, "Apply"));
    } else {
      image = h("span", { class: "mono" }, d.path || "–");
    }
    diskTable.tBodies[0].append(h("tr", {},
      h("td", {}, d.device === "cdrom" ? `CD/DVD (${d.target})` : `Disk (${d.target})`, d.boot_order ? h("div", { class: "muted", style: "font-size:13px" }, `boot ${d.boot_order}`) : null),
      h("td", {}, d.bus || "–"),
      h("td", { style: "min-width:220px" }, image),
      h("td", { class: "num" }, d.capacity ? fmt.bytes(d.capacity) : "–"),
      h("td", { class: "acts" }, acts)));
  }
  if (isoSelects.length) {
    GET("/api/meta").then((meta) => {
      for (const [sel, current] of isoSelects) {
        put(sel, h("option", { value: "" }, "Empty drive"),
          ...meta.isos.map((iso) => h("option", { value: iso.path, selected: iso.path === current }, iso.name)));
        if (current && !meta.isos.some((i) => i.path === current)) sel.append(h("option", { value: current, selected: true }, current));
      }
    }).catch(() => {});
  }
  const bootDisk = h("button", {
    class: "btn", type: "button",
    async onclick(ev) {
      await busy(ev.currentTarget, async () => {
        await api("PATCH", `/api/vms/${uuid}`, { boot_from_disk: true });
        toast("The VM now boots from its hard disk first");
        await rerender();
      });
    },
  }, "Boot from hard disk");

  // NICs
  const nicTable = h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", {}, "Connected to"), h("th", {}, "MAC address"))),
    h("tbody", {}, vm.nics.map((n) => h("tr", {},
      h("td", {}, n.model || "–"),
      h("td", {}, `${n.type === "bridge" ? "Bridge" : "Network"} ${n.source || ""}`),
      h("td", { class: "mono" }, n.mac || "–")))));

  // CPU pinning: one vCPU per physical core of one CPU cluster (CCD)
  const pin = h("input", {
    type: "checkbox", id: "hw-pin", checked: !!vm.pinning,
    async onchange(ev) {
      const box = ev.currentTarget;
      try {
        const res = await api("PATCH", `/api/vms/${uuid}`, { cpu_pinning: box.checked });
        toast([box.checked ? "CPU pinning on." : "CPU pinning off.", res && res.note,
          res && res.applies_after_restart ? "It applies the next time the VM starts." : null].filter(Boolean).join(" "));
        await rerender();
      } catch (e) { toast(e.message, "error"); box.checked = !box.checked; }
    },
  });
  const pinHint = vm.pinning
    ? `On: vCPUs on cores ${[...new Set(Object.values(vm.pinning.vcpus))].join(", ")}; QEMU's housekeeping on CPUs ${vm.pinning.emulator || "any"}.`
    : "Gives the VM whole cores of one CPU cluster, sharing one L3 cache. Best for games and audio.";

  // Looking Glass: the kvmfr shared memory the GPU's picture comes through
  const lg = h("input", {
    type: "checkbox", id: "hw-lg", checked: vm.looking_glass, disabled: running,
    async onchange(ev) {
      const box = ev.currentTarget;
      try {
        const res = await api("PATCH", `/api/vms/${uuid}`, { looking_glass: box.checked });
        toast([box.checked ? "Looking Glass shared memory added." : "Looking Glass shared memory removed.",
          res && res.note].filter(Boolean).join(" "));
        await rerender();
      } catch (e) { toast(e.message, "error"); box.checked = !box.checked; }
    },
  });

  // Whole graphics cards: every function (GPU + HDMI audio) in one guest slot
  const cardList = h("div", {}, h("p", { class: "muted" }, "Loading cards…"));
  GET("/api/pci/cards").then(({ cards }) => {
    const mine = new Set(vm.hostdevs);
    put(cardList, cards.length ? h("table", { class: "list" }, h("tbody", {}, cards.map((c) => {
      const attached = c.functions.some((f) => mine.has(f.address));
      const others = c.assigned_to.filter((n) => n !== vm.name);
      const status = [
        attached ? "Attached to this VM"
          : c.ready ? (c.isolated ? "Ready" : "Ready, but its IOMMU group holds other devices")
          : "Used by the host (not on vfio-pci)",
        others.length ? `${attached ? "also assigned" : "assigned"} to ${others.join(", ")}`
          + (c.running_on && c.running_on !== vm.name ? ` (${c.running_on} is running)` : "") : null,
      ].filter(Boolean).join(", ");
      const act = attached || c.ready ? h("button", {
        class: `btn btn-sm${attached ? " btn-danger" : ""}`, type: "button", disabled: running,
        async onclick(ev) {
          await busy(ev.currentTarget, async () => {
            const res = attached ? await api("DELETE", `/api/vms/${uuid}/cards/${c.slot}`)
              : await api("POST", `/api/vms/${uuid}/cards`, { slot: c.slot });
            toast(attached ? "Card removed" : ["Card attached. Start the VM to use it.", res && res.note].filter(Boolean).join(" "));
            await rerender();
          });
        },
      }, attached ? "Remove" : "Attach") : null;
      return h("tr", {},
        h("td", {}, h("div", {}, c.name), h("div", { class: "muted", style: "font-size:13px" },
          `${status}. ${c.functions.map((f) => `${f.address} (${f.driver || "no driver"})`).join(", ")}`)),
        h("td", { class: "acts" }, act));
    }))) : h("p", { class: "muted" }, "No graphics cards found."));
  }).catch((e) => put(cardList, h("p", { class: "muted" }, e.message)));

  // PCI passthrough
  const pciList = h("div", {});
  const pciSel = h("select", { "aria-label": "PCI device", disabled: running }, h("option", { value: "" }, "Loading devices…"));
  const pciAdd = h("button", {
    class: "btn", type: "button", disabled: running,
    async onclick(ev) {
      const btn = ev.currentTarget;
      if (!pciSel.value) return;
      const { ok } = await dialog({
        title: "Pass this device to the VM?",
        body: h("p", {}, "While the VM runs, the host loses the device. Passing the GPU that drives your screen will blank the display. Make sure the whole IOMMU group can go with it."),
        confirm: "Add device",
      });
      if (!ok) return;
      await busy(btn, async () => {
        await api("POST", `/api/vms/${uuid}/pci`, { address: pciSel.value });
        toast("PCI device added");
        await rerender();
      });
    },
  }, "Add device");
  pciList.append(vm.hostdevs.length
    ? h("table", { class: "list" }, h("tbody", {}, vm.hostdevs.map((addr) => h("tr", {},
      h("td", { class: "mono" }, addr),
      h("td", { class: "acts" }, h("button", {
        class: "btn btn-sm btn-danger", type: "button", disabled: running,
        async onclick(ev) {
          await busy(ev.currentTarget, async () => {
            await api("DELETE", `/api/vms/${uuid}/pci/${addr}`);
            toast("PCI device removed");
            await rerender();
          });
        },
      }, "Remove"))))))
    : h("p", { class: "muted" }, "No host devices passed through."));
  GET("/api/host/iommu").then(({ groups }) => {
    const devs = groups.flatMap((g) => g.devices.map((d) => ({ ...d, group: g.id })))
      .filter((d) => !d.class_code.startsWith("06") && !vm.hostdevs.includes(d.address));
    put(pciSel, h("option", { value: "" }, devs.length ? "Choose a device" : "No IOMMU groups (IOMMU off?)"),
      ...devs.map((d) => h("option", { value: d.address }, `${d.address}  ${d.name}  (group ${d.group}${d.driver ? `, ${d.driver}` : ""})`)));
  }).catch(() => { put(pciSel, h("option", { value: "" }, "Couldn't list devices")); });

  put(body, h("div", { class: "cols" },
    h("div", {},
      h("div", { class: "panel" }, h("h3", {}, "CPU and memory"), resources,
        h("label", { class: "check", for: "hw-pin", style: "margin-top:14px" }, pin,
          h("span", {}, "Pin to one CPU cluster", h("span", { class: "hint" }, pinHint))),
        running ? h("p", { class: "note info" }, "Changes apply the next time the VM starts.") : null),
      h("div", { class: "panel" }, h("h3", {}, "General"), rename,
        h("label", { class: "check", for: "hw-auto", style: "margin-top:16px" }, auto,
          h("span", {}, "Start this VM when the host boots"))),
      h("div", { class: "panel" }, h("h3", {}, "Firmware"),
        h("dl", { class: "kv" },
          h("dt", {}, "Boot firmware"), h("dd", {}, vm.firmware === "efi" ? "UEFI" : "Legacy BIOS"),
          h("dt", {}, "Secure Boot"), h("dd", {}, vm.secure_boot ? "On" : "Off"),
          h("dt", {}, "TPM"), h("dd", {}, vm.tpm ? `${vm.tpm.version} (${vm.tpm.model})` : "None"),
          h("dt", {}, "Video"), h("dd", {}, vm.video || "–"),
          h("dt", {}, "Machine"), h("dd", {}, vm.machine || "–")),
        h("p", { class: "muted", style: "margin-top:12px;font-size:14px" }, "Change these in the XML tab."))),
    h("div", {},
      h("div", { class: "panel" }, h("h3", {}, "Disks and drives"), h("div", { class: "table-scroll" }, diskTable),
        h("div", { style: "margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap" }, bootDisk,
          h("span", { class: "muted", style: "font-size:14px" }, "Use after installing, so the VM stops booting the installer."))),
      h("div", { class: "panel" }, h("h3", {}, "Network"), h("div", { class: "table-scroll" }, nicTable)),
      h("div", { class: "panel" }, h("h3", {}, "Graphics card passthrough"), cardList,
        h("p", { class: "muted", style: "font-size:14px;margin-top:10px" }, "Passes every function of the card (GPU and its HDMI audio) together, the way GPU drivers expect."),
        h("label", { class: "check", for: "hw-lg", style: "margin-top:14px" }, lg,
          h("span", {}, "Looking Glass shared memory", h("span", { class: "hint" },
            "Shows the passed-through GPU's picture on this host's screen, via /dev/kvmfr0 and the Looking Glass host app in the guest."))),
        running ? h("p", { class: "note info" }, "Shut the VM down to change this.") : null),
      h("div", { class: "panel" }, h("h3", {}, "Other PCI devices"), pciList,
        h("div", { class: "inline-form", style: "margin-top:12px" }, h("div", { class: "field", style: "flex:1;min-width:220px" }, pciSel), pciAdd),
        running ? h("p", { class: "note info" }, "Shut the VM down to add or remove devices.") : null))));
  return null;
};

tabs.snapshots = (body, vm, uuid) => {
  const name = h("input", { type: "text", id: "snap-name", placeholder: "before-updates", required: true, pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]*" });
  const desc = h("input", { type: "text", id: "snap-desc", placeholder: "Optional note" });
  const take = h("button", { class: "btn btn-primary", type: "submit" }, "Take snapshot");
  const table = h("div", {});

  async function load() {
    const { snapshots } = await GET(`/api/vms/${uuid}/snapshots`);
    if (!snapshots.length) {
      put(table, h("p", { class: "muted" }, "No snapshots yet. Take one before risky changes, like driver installs or upgrades."));
      return;
    }
    put(table, h("div", { class: "table-scroll" }, h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Taken"), h("th", {}, "VM was"), h("th", {}, "Note"), h("th", {}, ""))),
      h("tbody", {}, snapshots.slice().reverse().map((sn) => h("tr", {},
        h("td", {}, h("b", {}, sn.name), sn.current ? h("div", { class: "muted", style: "font-size:13px" }, "current") : null),
        h("td", {}, fmt.date(sn.created)),
        h("td", {}, sn.state || "–"),
        h("td", {}, sn.description || ""),
        h("td", { class: "acts" },
          h("button", {
            class: "btn btn-sm", type: "button",
            async onclick(ev) {
              const btn = ev.currentTarget;
              const { ok } = await dialog({ title: `Revert to ${sn.name}?`, body: h("p", {}, "The VM's disks and state go back to this snapshot. Changes made since then are lost."), confirm: "Revert", danger: true });
              if (!ok) return;
              await busy(btn, async () => { await api("POST", `/api/vms/${uuid}/snapshots/${encodeURIComponent(sn.name)}/revert`); toast(`Reverted to ${sn.name}`); await load(); });
            },
          }, "Revert"), " ",
          h("button", {
            class: "btn btn-sm btn-danger", type: "button",
            async onclick(ev) {
              const btn = ev.currentTarget;
              const { ok } = await dialog({ title: `Delete snapshot ${sn.name}?`, body: h("p", {}, "The VM keeps running as it is now; only the snapshot is removed."), confirm: "Delete snapshot", danger: true });
              if (!ok) return;
              await busy(btn, async () => { await api("DELETE", `/api/vms/${uuid}/snapshots/${encodeURIComponent(sn.name)}`); toast("Snapshot deleted"); await load(); });
            },
          }, "Delete"))))))));
  }

  put(body, 
    h("form", {
      class: "inline-form",
      async onsubmit(ev) {
        ev.preventDefault();
        await busy(take, async () => {
          await api("POST", `/api/vms/${uuid}/snapshots`, { name: name.value.trim(), description: desc.value });
          toast(`Snapshot ${name.value.trim()} taken`);
          name.value = desc.value = "";
          await load();
        });
      },
    },
      h("div", { class: "field", style: "width:220px" }, h("label", { for: "snap-name" }, "Snapshot name"), name),
      h("div", { class: "field", style: "flex:1;min-width:200px" }, h("label", { for: "snap-desc" }, "Note"), desc),
      take),
    vm.firmware === "efi" && vm.state === "running"
      ? h("p", { class: "note" }, "Some libvirt versions can't snapshot a running UEFI VM. If it fails, shut the VM down and try again.")
      : null,
    h("div", { style: "margin-top:22px" }, table));
  load().catch((e) => put(table, h("p", { class: "muted" }, e.message)));
  return null;
};

tabs.xml = (body, vm, uuid) => {
  const area = h("textarea", { rows: 28, spellcheck: "false", "aria-label": "Domain XML" }, "Loading…");
  const save = h("button", { class: "btn btn-primary", type: "button" }, "Save XML");
  const load = async () => { area.value = (await GET(`/api/vms/${uuid}/xml`)).xml; };
  save.addEventListener("click", async () => {
    const { ok } = await dialog({ title: "Save this XML?", body: h("p", {}, "libvirt validates it before saving. Changes to a running VM apply the next time it starts."), confirm: "Save XML" });
    if (!ok) return;
    await busy(save, async () => { await api("PUT", `/api/vms/${uuid}/xml`, { xml: area.value }); toast("XML saved"); await load(); });
  });
  put(body, 
    h("p", { class: "muted" }, "The full libvirt definition. Edit anything Tarn has no form for, like CPU pinning, hugepages or extra devices."),
    area,
    h("div", { class: "formfoot", style: "padding-bottom:0" }, save, h("button", { class: "btn", type: "button", onclick: (ev) => busy(ev.currentTarget, load) }, "Discard changes")));
  load().catch((e) => { area.value = e.message; });
  return null;
};

// ---------------------------------------------------------------- create VM

views.new = async (main) => {
  const meta = await GET("/api/meta");
  const caps = meta.caps;
  const f = {};
  const opt = (value, label, selected) => h("option", { value, selected: !!selected }, label);

  // Operating system tiles
  const osTile = (value, title, note) => h("div", { class: "ostile" },
    h("input", { type: "radio", name: "os", id: `os-${value}`, value, checked: value === "linux" }),
    h("label", { for: `os-${value}` }, title, h("span", {}, note)));
  const osTiles = h("div", { class: "ostiles", role: "radiogroup", "aria-label": "Operating system" },
    osTile("linux", "Linux", "VirtIO devices, UEFI"),
    osTile("windows", "Windows", "TPM, Secure Boot, Hyper-V tuning"),
    osTile("other", "Other", "BSD or older systems, BIOS"));

  f.name = h("input", { type: "text", id: "c-name", required: true, autocomplete: "off", pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]{0,62}", placeholder: "alpine-dev" });

  const isoOptions = (blank) => [opt("", blank), ...meta.isos.map((i) => opt(i.path, i.name))];
  f.iso = h("select", { id: "c-iso" }, isoOptions("No installer, boot from disk"));
  f.driverIso = h("select", { id: "c-driver" }, isoOptions("None"));
  const virtioWin = meta.isos.find((i) => /virtio-win/i.test(i.name));
  if (virtioWin) f.driverIso.value = virtioWin.path;
  f.virtio = h("input", { type: "checkbox", id: "c-virtio" });
  const windowsBits = h("div", {},
    h("div", { class: "field" }, h("label", { for: "c-driver" }, "VirtIO driver ISO"), f.driverIso,
      h("div", { class: "hint" }, "Attached as a second drive so Windows setup can load storage and network drivers. Get virtio-win.iso from the Fedora project and upload it on the Storage page.")),
    h("label", { class: "check", for: "c-virtio" }, f.virtio,
      h("span", {}, "Use VirtIO disk and network",
        h("span", { class: "hint" }, "Faster, but Windows setup won't see the disk until you load the driver from the VirtIO ISO. For an imported Windows disk, tick this only if the VirtIO drivers were installed before imaging."))));

  // Disk
  const diskMode = (value, label, checked) => h("label", {}, h("input", { type: "radio", name: "diskmode", value, checked }), label);
  const diskSeg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Disk" },
    diskMode("new", "New disk", true), diskMode("existing", "Use an existing image"), diskMode("none", "No disk"));
  f.size = h("input", { type: "number", id: "c-size", min: 1, value: 32 });
  f.pool = h("select", { id: "c-pool" }, meta.pools.filter((p) => p.active).map((p) =>
    opt(p.name, `${p.name}${p.available ? `, ${fmt.gib(p.available)} free` : ""}`, p.name === meta.default_pool)));
  const newDisk = h("div", { class: "row2" },
    h("div", { class: "field" }, h("label", { for: "c-size" }, "Size (GiB)"), f.size,
      h("div", { class: "hint" }, "Thin provisioned: it only uses space as the guest writes.")),
    h("div", { class: "field" }, h("label", { for: "c-pool" }, "Storage pool"), f.pool));
  f.image = h("select", { id: "c-image" },
    meta.images.length ? meta.images.map((i) => opt(i.path, `${i.name} (${i.format || "?"}, ${fmt.bytes(i.capacity)})`))
      : [opt("", "No unused images found")]);
  const existingDisk = h("div", { hidden: true },
    h("div", { class: "field" }, h("label", { for: "c-image" }, "Disk image"), f.image,
      h("div", { class: "hint" }, "Images in any storage pool that no VM uses yet. Copy a converted image into /var/lib/libvirt/images and it shows up here.")));

  // CPU & memory
  f.vcpus = h("input", { type: "number", id: "c-vcpus", min: 1, max: meta.host_threads || 256, value: 2 });
  f.mem = h("input", { type: "number", id: "c-mem", min: 0.25, step: 0.25, value: 2 });
  const cpuMem = h("div", { class: "row2" },
    h("div", { class: "field" }, h("label", { for: "c-vcpus" }, "vCPUs"), f.vcpus, h("div", { class: "hint" }, `The host has ${meta.host_threads} threads.`)),
    h("div", { class: "field" }, h("label", { for: "c-mem" }, "Memory (GiB)"), f.mem, h("div", { class: "hint" }, `The host has ${fmt.gib(meta.host_memory)}.`)));

  // Firmware
  f.efi = h("select", { id: "c-fw" }, opt("efi", "UEFI", true), opt("bios", "Legacy BIOS"));
  if (!caps.efi) { f.efi.value = "bios"; f.efi.disabled = true; }
  f.sb = h("input", { type: "checkbox", id: "c-sb", disabled: !caps.secure_boot });
  f.tpm = h("input", { type: "checkbox", id: "c-tpm", disabled: !caps.tpm });
  const firmware = h("div", {},
    h("div", { class: "field", style: "max-width:260px" }, h("label", { for: "c-fw" }, "Boot firmware"), f.efi),
    h("label", { class: "check", for: "c-sb" }, f.sb, h("span", {}, "Secure Boot",
      h("span", { class: "hint" }, caps.secure_boot ? "Windows 11 wants this." : "This host has no secure-boot UEFI build."))),
    h("label", { class: "check", for: "c-tpm" }, f.tpm, h("span", {}, "TPM 2.0",
      h("span", { class: "hint" }, caps.tpm ? "Emulated with swtpm. Windows 11 requires it." : "Install swtpm on the host to enable this."))));

  // Network
  f.net = h("select", { id: "c-net" },
    meta.networks.map((n) => opt(`network:${n.name}`, `${n.name} (${n.mode === "nat" ? "NAT" : n.mode}${n.subnet ? `, ${n.subnet}` : ""})`, n.name === meta.default_network)),
    meta.bridges.map((b) => opt(`bridge:${b}`, `Bridge ${b} (VM appears on your LAN)`)),
    opt("none:", "No network"));
  f.start = h("input", { type: "checkbox", id: "c-start", checked: true });

  // Gaming: the same tweaks as the VM's Hardware tab, applied before the first start
  f.pin = h("input", { type: "checkbox", id: "c-pin" });
  f.useCard = h("input", { type: "checkbox", id: "c-usecard", onchange: () => { f.card.disabled = !f.useCard.checked; } });
  f.card = h("select", { id: "c-card", disabled: true }, (meta.cards || []).map((c) =>
    opt(c.slot, c.name + (c.assigned_to.length ? ` (also on ${c.assigned_to.join(", ")})` : ""))));
  f.lg = h("input", { type: "checkbox", id: "c-lg" });
  const gaming = [
    h("label", { class: "check", for: "c-pin" }, f.pin, h("span", {}, "Pin to one CPU cluster",
      h("span", { class: "hint" }, "Whole cores of one CPU cluster, sharing one L3 cache. Smoother games and audio."))),
    (meta.cards || []).length ? h("label", { class: "check", for: "c-usecard" }, f.useCard, h("span", {}, "Pass through a graphics card",
      h("span", { class: "hint" }, "Simplest after Windows is installed and updated: the VM's Hardware tab can add it later. NVIDIA laptop cards also get a fake battery against Code 43."))) : null,
    (meta.cards || []).length ? h("div", { class: "field", style: "max-width:520px;margin-left:28px" }, f.card) : null,
    meta.looking_glass ? h("label", { class: "check", for: "c-lg" }, f.lg, h("span", {}, "Looking Glass",
      h("span", { class: "hint" }, "Shows the card's picture on the host's screen through /dev/kvmfr0."))) : null,
  ];

  const warn = h("p", { class: "note", hidden: caps.virt_type === "kvm" },
    "KVM isn't available, so this VM will run in slow software emulation. Check the Host page.");

  function applyOsDefaults(os) {
    const win = os === "windows";
    f.vcpus.value = win ? Math.min(4, meta.host_threads || 4) : 2;
    f.mem.value = win ? 8 : os === "other" ? 1 : 2;
    f.size.value = win ? 80 : os === "other" ? 16 : 32;
    if (caps.efi) f.efi.value = os === "other" ? "bios" : "efi";
    f.sb.checked = win && caps.secure_boot;
    f.tpm.checked = win && caps.tpm;
    f.virtio.checked = false;
    f.pin.checked = f.useCard.checked = f.lg.checked = false;
    f.card.disabled = true;
    windowsBits.hidden = !win;
    f.sb.disabled = !caps.secure_boot || f.efi.value !== "efi";
  }
  osTiles.addEventListener("change", (e) => applyOsDefaults(e.target.value));
  f.efi.addEventListener("change", () => {
    f.sb.disabled = !caps.secure_boot || f.efi.value !== "efi";
    if (f.sb.disabled) f.sb.checked = false;
  });
  diskSeg.addEventListener("change", (e) => {
    newDisk.hidden = e.target.value !== "new";
    existingDisk.hidden = e.target.value !== "existing";
  });

  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Create VM");
  const section = (title, note, ...content) => [
    h("div", { class: "label-col" }, h("h3", {}, title), note ? h("p", {}, note) : null),
    h("div", { class: "input-col" }, ...content)];

  const form = h("form", {
    async onsubmit(ev) {
      ev.preventDefault();
      const os = form.querySelector("input[name=os]:checked").value;
      const mode = form.querySelector("input[name=diskmode]:checked").value;
      const [ntype, nname] = f.net.value.split(":");
      const spec = {
        name: f.name.value.trim(),
        os_type: os,
        vcpus: +f.vcpus.value,
        memory_mib: Math.round(+f.mem.value * 1024),
        firmware: f.efi.value,
        secure_boot: f.sb.checked,
        tpm: f.tpm.checked,
        virtio: os === "windows" ? f.virtio.checked : os === "linux",
        iso: f.iso.value || null,
        driver_iso: os === "windows" ? f.driverIso.value || null : null,
        disk: mode === "new" ? { mode, size_gib: +f.size.value, pool: f.pool.value }
          : mode === "existing" ? { mode, path: f.image.value } : { mode },
        network: { type: ntype, name: nname || null },
        start: f.start.checked,
        cpu_pinning: f.pin.checked,
        card: f.useCard.checked ? f.card.value : null,
        looking_glass: f.lg.checked,
      };
      await busy(submit, async () => {
        const res = await api("POST", "/api/vms", spec);
        if (res.warning) toast(`Created, but it didn't start: ${res.warning}`, "error");
        else toast([`${spec.name} created${res.started ? " and started" : ""}.`, res.note].filter(Boolean).join(" "));
        location.hash = `#/vm/${res.uuid}/${res.started ? "console" : "overview"}`;
      });
    },
  },
    h("div", { class: "form-grid" },
      section("Operating system", "Sets sensible hardware defaults. You can change everything below.", osTiles),
      section("Name", null, h("div", { class: "field", style: "max-width:360px" }, h("label", { for: "c-name" }, "VM name"), f.name,
        h("div", { class: "hint" }, "Letters, digits, dots, dashes and underscores."))),
      section("Install media", meta.isos.length ? null : "No ISOs uploaded yet.",
        h("div", { class: "field" }, h("label", { for: "c-iso" }, "Installer ISO"), f.iso,
          h("div", { class: "hint" }, "Upload ISOs on the ", h("a", { href: "#/storage" }, "Storage page"), ".")),
        windowsBits),
      section("Disk", null, diskSeg, newDisk, existingDisk),
      section("CPU and memory", null, cpuMem),
      section("Firmware", null, firmware),
      section("Gaming", "Optional. Each one can be changed later on the VM's Hardware tab.", ...gaming),
      section("Network", "NAT works over Wi-Fi. A bridge needs wired Ethernet.",
        h("div", { class: "field", style: "max-width:420px" }, h("label", { for: "c-net" }, "Connect to"), f.net),
        h("label", { class: "check", for: "c-start" }, f.start, h("span", {}, "Start the VM after creating it")))),
    h("div", { class: "formfoot" }, submit, h("a", { class: "btn", href: "#/" }, "Cancel")));

  applyOsDefaults("linux");
  put(main, h("div", { class: "wrap section" },
    h("div", { class: "section-head" }, h("h2", { style: "font-size:30px;letter-spacing:-0.03em" }, "Create a virtual machine")),
    warn, form));
  setTimeout(() => f.name.focus());
  return null;
};

// ---------------------------------------------------------------- storage

function cleanFileName(name) {
  let n = name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");
  // Volume names max out at 63 characters; shorten the stem so ".iso" survives.
  const ext = (n.match(/\.[A-Za-z0-9]{1,4}$/) || [""])[0];
  if (n.length > 63) n = n.slice(0, 63 - ext.length) + ext;
  return n || "upload.iso";
}

function uploadFile(pool, file, name, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", `/api/storage/${encodeURIComponent(pool)}/upload?name=${encodeURIComponent(name)}`);
    x.setRequestHeader("X-Tarn", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    x.onload = () => {
      let data = {};
      try { data = JSON.parse(x.responseText); } catch { /* ignore */ }
      if (x.status >= 200 && x.status < 300) resolve(data); else reject(new Error(data.error || `Upload failed (${x.status})`));
    };
    x.onerror = () => reject(new Error("Upload failed: connection lost"));
    x.send(file);
  });
}

views.storage = async (main) => {
  const wrap = h("div", { class: "wrap section" });
  put(main, wrap);

  async function render() {
    const { pools, roles } = await GET("/api/storage");
    const out = [h("div", { class: "section-head" }, h("h2", { style: "font-size:30px;letter-spacing:-0.03em" }, "Storage"))];
    for (const pool of pools) {
      const used = pool.capacity ? pool.allocation / pool.capacity : 0;
      const isIso = pool.name === roles.iso;
      const isImages = pool.name === roles.images;
      const block = h("div", { class: "panel" },
        h("div", { class: "section-head", style: "margin-bottom:6px" },
          h("h3", { style: "font-size:19px" }, pool.name),
          h("span", { class: "muted mono" }, pool.path || ""),
          isIso ? h("span", { class: "muted" }, "installer ISOs") : isImages ? h("span", { class: "muted" }, "VM disks") : null),
        pool.active
          ? h("div", { style: "margin-bottom:16px" },
            h("div", { class: "muted", style: "font-size:14px" }, `${fmt.bytes(pool.allocation)} used of ${fmt.bytes(pool.capacity)}, ${fmt.bytes(pool.available)} free`),
            h("div", { class: `bar${used > 0.85 ? " high" : ""}` }, h("span", { style: `width:${(used * 100).toFixed(1)}%` })))
          : h("p", { class: "note" }, "This pool is not active."));

      if (isIso) block.append(uploadBox(pool.name));
      if (isImages) block.append(newDiskForm(pool.name));

      if (pool.volumes.length) {
        block.append(h("div", { class: "table-scroll", style: "margin-top:14px" }, h("table", { class: "list" },
          h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Format"), h("th", { class: "num" }, "Size"), h("th", { class: "num" }, "On disk"), h("th", {}, "Used by"), h("th", {}, ""))),
          h("tbody", {}, pool.volumes.map((v) => h("tr", {},
            h("td", { class: "mono" }, v.name),
            h("td", {}, v.format || "–"),
            h("td", { class: "num" }, fmt.bytes(v.capacity)),
            h("td", { class: "num" }, fmt.bytes(v.allocation)),
            h("td", {}, v.used_by || h("span", { class: "muted" }, "nothing")),
            h("td", { class: "acts" }, h("button", {
              class: "btn btn-sm btn-danger", type: "button", disabled: !!v.used_by,
              title: v.used_by ? `Used by ${v.used_by}` : null,
              async onclick(ev) {
                const btn = ev.currentTarget;
                const { ok } = await dialog({ title: `Delete ${v.name}?`, body: h("p", {}, "The file is removed from disk. This can't be undone."), confirm: "Delete file", danger: true });
                if (!ok) return;
                await busy(btn, async () => {
                  await api("DELETE", `/api/storage/${encodeURIComponent(pool.name)}/volumes/${encodeURIComponent(v.name)}`);
                  toast(`Deleted ${v.name}`);
                  await render();
                });
              },
            }, "Delete"))))))));
      } else if (pool.active) {
        block.append(h("p", { class: "muted", style: "margin-top:14px" }, "Empty."));
      }
      out.push(block);
    }
    out.push(h("div", { class: "panel" }, h("h3", {}, "Importing a disk from another machine"),
      h("p", {}, "Convert it to qcow2 on the host, then it appears in the list above and in the Create VM form:"),
      h("p", {}, h("code", {}, "qemu-img convert -p -O qcow2 windows.vhdx /var/lib/libvirt/images/windows.qcow2"))));
    put(wrap, ...out);
  }

  function uploadBox(pool) {
    const input = h("input", { type: "file", accept: ".iso,.img", id: "upl", style: "max-width:300px" });
    const status = h("div", { class: "progress muted" }, "Drop an ISO here or choose a file.");
    const bar = h("span", { style: "width:0%" });
    const barEl = h("div", { class: "bar", hidden: true }, bar);
    const box = h("div", { class: "drop" }, input, h("div", { class: "progress" }, status, barEl));
    let uploading = false;
    async function start(file) {
      if (!file || uploading) return;
      uploading = true;
      const name = cleanFileName(file.name);
      barEl.hidden = false;
      const t0 = performance.now();
      try {
        await uploadFile(pool, file, name, (frac) => {
          bar.style.width = `${(frac * 100).toFixed(1)}%`;
          const secs = (performance.now() - t0) / 1000;
          status.textContent = `Uploading ${name}: ${Math.round(frac * 100)}%, ${fmt.rate((frac * file.size) / Math.max(secs, 0.1))}`;
        });
        toast(`Uploaded ${name}`);
        await render();
      } catch (e) {
        toast(e.message, "error");
        status.textContent = e.message;
      } finally { uploading = false; }
    }
    input.addEventListener("change", () => start(input.files[0]));
    box.addEventListener("dragover", (e) => { e.preventDefault(); box.classList.add("over"); });
    box.addEventListener("dragleave", () => box.classList.remove("over"));
    box.addEventListener("drop", (e) => { e.preventDefault(); box.classList.remove("over"); start(e.dataTransfer.files[0]); });
    return box;
  }

  function newDiskForm(pool) {
    const name = h("input", { type: "text", id: `nd-name-${pool}`, placeholder: "data-disk.qcow2", required: true });
    const size = h("input", { type: "number", id: `nd-size-${pool}`, min: 1, value: 20, required: true });
    const btn = h("button", { class: "btn", type: "submit" }, "Create disk");
    return h("form", {
      class: "inline-form",
      async onsubmit(ev) {
        ev.preventDefault();
        await busy(btn, async () => {
          await api("POST", `/api/storage/${encodeURIComponent(pool)}/volumes`, { name: name.value.trim(), size_gib: +size.value, format: "qcow2" });
          toast(`Created ${name.value.trim()}`);
          await render();
        });
      },
    },
      h("div", { class: "field", style: "flex:1;min-width:200px;max-width:320px" }, h("label", { for: `nd-name-${pool}` }, "New empty disk"), name),
      h("div", { class: "field", style: "width:120px" }, h("label", { for: `nd-size-${pool}` }, "Size (GiB)"), size),
      btn);
  }

  await render();
  return null;
};

// ---------------------------------------------------------------- host

views.host = async (main) => {
  const info = await GET("/api/host");
  const wrap = h("div", { class: "wrap section" });
  put(main, wrap);
  const marks = { ok: "✓", warn: "!", error: "✕" };

  const checks = h("ul", { class: "checks" }, info.readiness.map((c) => h("li", { class: c.level },
    h("span", { class: "mark", "aria-label": c.level }, marks[c.level]),
    h("div", {},
      h("div", { class: "what" }, c.label),
      h("div", { class: "detail" }, c.detail),
      c.fix ? h("div", { class: "fix" }, h("code", {}, c.fix)) : null))));

  const p = info.power;
  const powerRows = [];
  for (const b of p.batteries) powerRows.push(["Battery", `${b.capacity ?? "?"}%, ${(b.status || "").toLowerCase()}`]);
  if (p.ac_online !== null) powerRows.push(["Charger", p.ac_online ? "Connected" : "Unplugged"]);
  if (p.battery_conservation !== null) powerRows.push(["Battery conservation", p.battery_conservation ? "On (holds charge around 60–80%)" : "Off"]);
  if (p.platform_profile) powerRows.push(["Platform profile", `${p.platform_profile}${p.platform_profile_choices ? ` (choices: ${p.platform_profile_choices.join(", ")})` : ""}`]);
  for (const t of p.temps) powerRows.push([`${t.label} (${t.sensor})`, `${t.celsius.toFixed(0)} °C`]);

  const gpuTable = info.gpus.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Address"), h("th", {}, "Device"), h("th", {}, "Driver"), h("th", {}, "Power"))),
    h("tbody", {}, info.gpus.map((g) => h("tr", {},
      h("td", { class: "mono" }, g.address), h("td", {}, g.name),
      h("td", {}, g.driver || h("span", { class: "muted" }, "none")), h("td", {}, g.power || "–")))))
    : h("p", { class: "muted" }, "No GPUs found.");

  const iommu = h("div", {}, h("p", { class: "muted" }, "Loading…"));
  GET("/api/host/iommu").then(({ groups }) => {
    if (!groups.length) {
      put(iommu, h("p", { class: "muted" }, "No IOMMU groups. For PCI passthrough, turn on IOMMU (AMD-Vi or VT-d) in the BIOS and add iommu=pt to the kernel command line (Intel also needs intel_iommu=on)."));
      return;
    }
    put(iommu, h("div", { class: "table-scroll" }, h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", {}, "Group"), h("th", {}, "Address"), h("th", {}, "Device"), h("th", {}, "Driver"), h("th", {}, "Passthrough"))),
      h("tbody", {}, groups.flatMap((g) => g.devices.map((d, i) => h("tr", {},
        h("td", {}, i === 0 ? String(g.id) : ""),
        h("td", { class: "mono" }, d.address),
        h("td", {}, d.name, h("div", { class: "muted", style: "font-size:13px" }, d.class)),
        h("td", {}, d.driver || h("span", { class: "muted" }, "none")),
        h("td", {}, d.class_code.startsWith("06") ? h("span", { class: "muted" }, "bridge")
          : d.isolated ? "Clean group" : h("span", { style: "color:var(--warn)" }, "Shares its group")))))))));
  }).catch((e) => put(iommu, h("p", { class: "muted" }, e.message)));

  put(wrap, 
    h("div", { class: "section-head" }, h("h2", { style: "font-size:30px;letter-spacing:-0.03em" }, info.hostname)),
    h("div", { class: "cols" },
      h("div", {},
        h("div", { class: "panel" }, h("h3", {}, "Machine"), h("dl", { class: "kv" },
          ...[
            ["System", info.os], ["Kernel", info.kernel], ["CPU", info.cpu_model],
            ["Threads", String(info.threads)], ["Memory", fmt.gib(info.memory_total)],
            ["Uptime", fmt.duration(info.uptime)], ["libvirt", info.libvirt_version],
            ["QEMU", info.qemu_version || "–"], ["Accelerator", info.virt_type === "kvm" ? "KVM" : "TCG (software)"],
            ["Connection", h("span", { class: "mono" }, info.uri)],
          ].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))),
        powerRows.length ? h("div", { class: "panel" }, h("h3", {}, "Power and temperature"),
          h("dl", { class: "kv" }, powerRows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))) : null,
        h("div", { class: "panel" }, h("h3", {}, "Graphics"), h("div", { class: "table-scroll" }, gpuTable))),
      h("div", {},
        h("div", { class: "panel" }, h("h3", {}, "Ready for VMs?"), checks))),
    h("div", { class: "panel" }, h("h3", {}, "IOMMU groups"),
      h("p", { class: "muted", style: "font-size:14px" }, "A device can be passed to a VM cleanly when nothing else shares its group, apart from bridges and other functions of the same card."),
      iommu));
  return null;
};

// ---------------------------------------------------------------- boot

$("#logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout"); } catch { /* ignore */ }
  showLogin();
});

(async () => {
  try {
    await api("GET", "/api/me");
    $("#topbar").hidden = false;
    route();
  } catch (e) {
    if (e.message !== "Signed out") {
      put($("#main"), h("div", { class: "wrap section" }, h("h2", {}, "Tarn isn't responding"), h("p", { class: "muted" }, e.message)));
    }
  }
})();
