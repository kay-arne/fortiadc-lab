const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(location.origin + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { location.href = "/login"; throw new Error("niet ingelogd"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json().catch(() => ({})) : await r.text();
  if (!r.ok) throw new Error((body && body.detail) || r.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

// --- tabs ------------------------------------------------------------------
function goTo(tab) {
  $$("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + tab));
  if (tab === "setup") loadSettings();
  if (tab === "demo") loadDemo();
  if (tab === "access") loadAccess();
  if (tab === "updates") loadVersion();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => goTo(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) { e.preventDefault(); goTo(g.dataset.goto); }
});

// --- job log ---------------------------------------------------------------
let logTimer = null;
let activeJobId = null;
function followJob(job, onDone) {
  activeJobId = job.id;
  document.body.classList.add("logopen");
  $("#job-pill").hidden = true;
  $("#log-stop").hidden = false;
  $("#log-title").textContent = job.title;
  $("#log-body").textContent = "";
  clearInterval(logTimer);
  let offset = 0;
  logTimer = setInterval(async () => {
    let j;
    try { j = await api(`/api/jobs/${job.id}?offset=${offset}`); } catch (e) { return; }
    offset = j.offset;
    if (j.lines.length) {
      $("#log-body").textContent += j.lines.join("\n") + "\n";
      $("#log-body").scrollTop = $("#log-body").scrollHeight;
    }
    if (j.status !== "running") {
      clearInterval(logTimer);
      activeJobId = null;
      $("#log-stop").hidden = true;
      $("#log-title").textContent = `${job.title} — ${j.status}`;
      refresh();
      onDone && onDone();
    }
  }, 1000);
}
$("#log-close").onclick = () => { document.body.classList.remove("logopen"); clearInterval(logTimer); refresh(); };
$("#log-stop").onclick = async () => {
  if (!activeJobId || !confirm("De lopende taak stoppen?")) return;
  try { await post(`/api/jobs/${activeJobId}/stop`); } catch (e) { alert(e.message); }
};
$("#job-pill").onclick = () => { if ($("#job-pill")._job) followJob($("#job-pill")._job); };

async function action(path, data, onDone) {
  try { followJob(await post(path, data), onDone); } catch (e) { alert(e.message); }
}

// --- viewer (config, demo-scripts) -------------------------------------------
function openViewer(title, html, copyText) {
  $("#viewer-title").textContent = title;
  $("#viewer-body").innerHTML = html;
  $("#viewer-copy").hidden = !copyText;
  $("#viewer-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(copyText); $("#viewer-copy").textContent = "Gekopieerd"; }
    catch { $("#viewer-copy").textContent = "Niet gelukt"; }
    setTimeout(() => ($("#viewer-copy").textContent = "Kopieer"), 1500);
  };
  $("#viewer").hidden = false;
}
$("#viewer-close").onclick = () => ($("#viewer").hidden = true);
$("#viewer").addEventListener("click", (e) => { if (e.target.id === "viewer") $("#viewer").hidden = true; });

// --- componentselectie -----------------------------------------------------
const selected = new Set();
const CLABEL = { network: "Netwerk (bridges)", template: "LXC-template",
                 desktop: "Ubuntu desktop", config: "FortiADC-configuratie (beide)" };
const label = (i) => CLABEL[i] || i.replace("node:", "").replace("fortiadc:", "FortiADC ");

function updateSelBar() {
  const n = selected.size;
  $("#sel-count").textContent = n ? `${n} geselecteerd: ${[...selected].map(label).join(", ")}` : "Niets geselecteerd";
  ["#btn-sel-deploy", "#btn-sel-recreate", "#btn-sel-destroy", "#btn-sel-clear"].forEach((b) => ($(b).disabled = !n));
  $$("[data-comp]").forEach((el) => {
    const on = selected.has(el.dataset.comp);
    el.classList.toggle("selected", on);
    const cb = el.querySelector("input[type=checkbox]");
    if (cb) cb.checked = on;
  });
  $$("[data-seg-select]").forEach((cb) => {
    const ids = [...$$(`[data-seg="${cb.dataset.segSelect}"]`)].map((el) => el.dataset.comp);
    cb.checked = ids.length > 0 && ids.every((i) => selected.has(i));
  });
}
const check = () => `<input type="checkbox" tabindex="-1" aria-label="selecteer">`;

function renderLab(lab) {
  if (!lab) return;
  const bySeg = {};
  lab.nodes.forEach((n) => (bySeg[n.segment] ||= []).push(n));
  const t = lab.template, d = lab.desktop;
  const bridges = lab.segments.filter((s) => s.present).length;
  const prov = lab.adcs.filter((a) => a.provisioned);
  const probs = lab.adcs.reduce((n, a) => n + (a.problems || 0), 0);
  const cfgState = prov.length === lab.adcs.length ? (probs ? `toegepast, ${probs} waarschuwing(en)` : "toegepast op beide")
    : prov.length ? `toegepast op ${prov.map((a) => a.site).join(", ")}` : "nog niet toegepast";
  let html = `<div class="purdue"><div class="infra">`;
  lab.adcs.forEach((f) => {
    const info = f.info || {};
    html += `<div class="comp${f.reachable ? " comp-ok" : ""}" data-comp="fortiadc:${esc(f.site)}">${check()}<span class="dot ${f.status}"></span>
      <div><b>${esc(f.name)} · ${f.vmid}</b><div class="ip">FortiADC ${esc(f.dc)} · port1 ${esc(f.mgmt)} · ${esc(f.status)}${f.reachable ? " · GUI bereikbaar" : ""}
        <br>${(f.ports || []).map((p) => `${esc(p.port)} ${esc(p.ip)}`).join(" · ")}
        ${info.version ? `<br>${esc(info.version)} · licentie: ${esc(info.license)}` : ""}</div></div></div>`;
  });
  html += `
      <div class="comp${prov.length === lab.adcs.length && !probs ? " comp-ok" : ""}" data-comp="config">${check()}<span class="dot ${prov.length ? (probs ? "stopped" : "running") : "absent"}"></span>
        <div><b>FortiADC-configuratie</b><div class="ip">${cfgState} · SLB/WAF per datacenter, GSLB op beide</div></div></div>
      <div class="comp" data-comp="network">${check()}<span class="dot ${bridges === lab.segments.length ? "running" : bridges ? "stopped" : "absent"}"></span>
        <div><b>Netwerk</b><div class="ip">${bridges}/${lab.segments.length} lab-bridges aanwezig</div></div></div>
      <div class="comp" data-comp="template">${check()}<span class="dot ${t.status === "stopped" ? "running" : t.status}"></span>
        <div><b>LXC-template · ${t.vmid}</b><div class="ip">${t.status === "absent" ? "nog niet gebouwd" : "basis voor alle containers"}</div></div></div>
      <div class="comp" data-comp="desktop">${check()}<span class="dot ${d.status}"></span>
        <div><b>${esc(d.name)} · ${d.vmid}</b><div class="ip">Ubuntu desktop · ${esc(d.ip)} · ${esc(d.status)}
          ${d.status === "running" ? ' · <a href="#" id="desk-progress">installatiestatus</a>' : ""}</div></div></div>
    </div>`;
  lab.segments.forEach((s) => {
    const ports = Object.entries(s.adc || {}).map(([k, v]) => `FAD-${esc(k)} ${esc(v.port)} ${esc(v.ip)}`).join(", ");
    html += `<div class="zone"><div class="meta"><label class="segsel"><input type="checkbox" data-seg-select="${esc(s.id)}"> <b>${esc(s.name)}</b></label>
      <div class="lvl">${esc(s.bridge)} ${s.present ? "" : "(ontbreekt)"} · ${esc(s.subnet)}<br>${ports}</div></div><div class="nodes">`;
    (bySeg[s.id] || []).forEach((n) => {
      html += `<div class="node" data-comp="node:${esc(n.name)}" data-seg="${esc(s.id)}">
        <div class="n">${check()}<span class="dot ${n.status}"></span>${esc(n.hostname || n.name)}</div>
        <div class="ip">${esc(n.ip)} · ${esc(n.role)}${n.app ? " · " + esc(n.app) : ""} · ${n.vmid}</div></div>`;
    });
    html += `</div></div>`;
  });
  $("#topology").innerHTML = html + "</div>";
  $$("#topology [data-comp]").forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.id === "desk-progress") return;
    const id = el.dataset.comp;
    selected.has(id) ? selected.delete(id) : selected.add(id);
    updateSelBar();
  }));
  $$("#topology [data-seg-select]").forEach((cb) => cb.addEventListener("change", () => {
    $$(`[data-seg="${cb.dataset.segSelect}"]`).forEach((el) => (cb.checked ? selected.add(el.dataset.comp) : selected.delete(el.dataset.comp)));
    updateSelBar();
  }));
  const dp = $("#desk-progress");
  if (dp) dp.onclick = async (e) => {
    e.preventDefault();
    dp.textContent = "ophalen…";
    try { dp.textContent = "cloud-init: " + (await api("/api/desktop/progress")).status; } catch (err) { dp.textContent = err.message; }
  };
  updateSelBar();
}

function selAction(path, recreate, verb) {
  const items = [...selected];
  if (!confirm(`${verb}:\n- ${items.map(label).join("\n- ")}`)) return;
  action(path, { items, recreate }, () => { selected.clear(); updateSelBar(); });
}
$("#btn-sel-deploy").onclick = () => selAction("/api/deploy/selected", false, "Uitrollen (bestaande worden overgeslagen)");
$("#btn-sel-recreate").onclick = () => selAction("/api/deploy/selected", true, "Verwijderen en opnieuw uitrollen");
$("#btn-sel-destroy").onclick = () => selAction("/api/destroy/selected", false, "Verwijderen");
$("#btn-sel-clear").onclick = () => { selected.clear(); updateSelBar(); };

$("#btn-deploy").onclick = () => confirm("Volledig lab uitrollen op de Proxmox host?") && action("/api/deploy");
$("#btn-provision").onclick = () => action("/api/provision");
$("#btn-update").onclick = () => action("/api/update-apps");
$("#btn-update-banner").onclick = () => action("/api/update-apps", {}, loadVersion);
$("#btn-config").onclick = async () => {
  const txt = await api("/api/config");
  openViewer("Gegenereerde FortiADC-configuratie", `<pre class="cfg">${esc(txt)}</pre>`, txt);
};
$("#btn-destroy").onclick = () => {
  if (!confirm("Alle lab-VM's en containers verwijderen (FortiADC, desktop, realservers)?")) return;
  const template = confirm("Ook het LXC-template verwijderen?");
  const network = confirm("Ook de lab-bridges op de host verwijderen?");
  action("/api/destroy", { template, network });
};

async function loadProblems() {
  const { problems } = await api("/api/problems");
  const el = $("#problems");
  el.hidden = !problems.length;
  if (problems.length) {
    el.innerHTML = `<div><b>${problems.length} configuratieregel(s) niet geaccepteerd door de FortiADC</b>
      <div class="hint">Waarschijnlijk een optie die in deze FortiADC-build anders heet. De rest van de config staat.</div></div>
      <button id="btn-problems" class="ghost">Toon</button>`;
    $("#btn-problems").onclick = () => openViewer("Geweigerde configuratieregels", `<pre class="cfg">${esc(problems.join("\n"))}</pre>`, problems.join("\n"));
  }
}

// --- setup -----------------------------------------------------------------
const fmtSize = (b) => (b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB");

function renderChecks(checks) {
  $("#checklist").innerHTML = checks.map((c) =>
    `<li class="${c.ok ? "ok" : c.optional ? "opt" : "todo"}">${c.ok ? "✓" : c.optional ? "○" : "•"} ${esc(c.label)}${c.optional ? " <small>(optioneel)</small>" : ""}</li>`).join("");
  checks.forEach((c) => {
    const b = $(`.badge[data-check=${c.id}]`);
    if (!b) return;
    b.className = "badge " + (c.ok ? "ok" : c.optional ? "opt" : "todo");
    b.textContent = c.ok ? "ingesteld" : c.optional ? "optioneel" : "nog invullen";
  });
}

function fillSettings(s) {
  renderChecks(s.checks);
  ["boot", "data"].forEach((k) => {
    const i = (s.adc_images || {})[k] || {};
    $(`#cur-image-${k}`).textContent = i.size ? `${i.name} (${fmtSize(i.size)})` : "geen";
  });
  $("#i-wanmode").value = s.wan.mode || "static";
  $("#i-wangw").value = s.wan.gateway || "";
  $("#adc-ips").innerHTML = s.adcs.map((a) => `
    <div class="field" data-wan="static"><label for="i-ip-${a.site}">${esc(a.name)} (${esc(a.dc)}): IP met prefix</label>
      <input id="i-ip-${a.site}" data-adc-ip="${a.site}" placeholder="192.168.1.9${a.site === "AMS" ? 8 : 9}/24" value="${esc(a.mgmt)}"></div>
    <div class="field" data-wan="dhcp"><label for="i-dh-${a.site}">${esc(a.name)} (${esc(a.dc)}): gekregen IP</label>
      <input id="i-dh-${a.site}" data-adc-dhcp="${a.site}" placeholder="na de eerste boot invullen" value="${esc(a.mgmt_dhcp)}"></div>`).join("");
  $("#lic-per-adc").innerHTML = s.adcs.map((a) => `
    <div class="field acc"><label>${esc(a.name)} (${esc(a.dc)})</label>
      <div data-lic="file"><div class="row"><input type="file" id="f-lic-${a.site}" accept=".lic">
        <button type="button" data-upload-lic="${a.site}">Uploaden</button></div>
        <div class="current">Huidig: <b>${esc(a.license_file || "geen")}</b></div></div>
      <div data-lic="fortiflex"><div class="secret"><input id="i-tok-${a.site}" data-token="${a.site}" type="password"
        autocomplete="off" placeholder="FortiFlex-token" value="${esc(a.fortiflex_token)}">
        <button class="eye" data-eye="i-tok-${a.site}" type="button"></button></div></div></div>`).join("");
  bindEyes($("#lic-per-adc"));
  $$("[data-upload-lic]").forEach((b) => (b.onclick = () =>
    upload(`#f-lic-${b.dataset.uploadLic}`, "/api/upload/license", null, `&site=${b.dataset.uploadLic}`)));
  $("#i-dns1").value = s.wan.dns1 || "";
  $("#i-dns2").value = s.wan.dns2 || "";
  const sugg = s.dns_suggestion || [];
  $("#dns-suggest").innerHTML = sugg.length && !s.wan.dns1
    ? `Voorstel (DNS van de controller): <a href="#" id="dns-use">${sugg.map(esc).join(", ")} gebruiken</a>` : "";
  const use = $("#dns-use");
  if (use) use.onclick = (e) => { e.preventDefault(); $("#i-dns1").value = sugg[0] || ""; $("#i-dns2").value = sugg[1] || ""; };
  $("#i-adminpw").value = s.adc_admin_password || "";
  const lic = $(`input[name=lic][value=${s.license_mode || "none"}]`);
  if (lic) lic.checked = true;
  const du = s.desktop_uplink || { mode: "dhcp" };
  $("#i-dmode").value = du.mode;
  $("#i-dip").value = du.ip || "";
  $("#i-dgw").value = du.gateway || "";
  $("#i-ddns").value = du.dns || "";
  toggles();
}
async function loadSettings() { fillSettings(await api("/api/settings")); }

function toggles() {
  const mode = $("#i-wanmode").value;
  $$("[data-wan]").forEach((el) => (el.hidden = el.dataset.wan !== mode));
  const dm = $("#i-dmode").value;
  $$("[data-dmode]").forEach((el) => (el.hidden = el.dataset.dmode !== dm));
  const v = ($("input[name=lic]:checked") || {}).value;
  $$("[data-lic]").forEach((el) => (el.hidden = el.dataset.lic !== v));
  $("#lic-per-adc").hidden = v === "none";
}
["#i-wanmode", "#i-dmode"].forEach((s) => $(s).addEventListener("change", toggles));
$$("input[name=lic]").forEach((r) => r.addEventListener("change", toggles));

function bindEyes(root = document) {
  root.querySelectorAll("[data-eye]").forEach((b) => b.addEventListener("click", () => {
    const input = $("#" + b.dataset.eye);
    input.type = input.type === "password" ? "text" : "password";
    b.classList.toggle("open", input.type === "text");
  }));
}
bindEyes();
$("#btn-genpw").onclick = () => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!#%+-";
  const rnd = crypto.getRandomValues(new Uint32Array(16));
  $("#i-adminpw").value = Array.from(rnd, (n) => chars[n % chars.length]).join("");
  $("#i-adminpw").type = "text";
  $("[data-eye=i-adminpw]").classList.add("open");
};

function showResult(key, ok, msg) {
  const el = $(`[data-result=${key}]`);
  el.className = "result " + (ok ? "ok" : "bad");
  el.textContent = msg;
  if (ok) setTimeout(() => (el.textContent = ""), 4000);
}
const savePayload = {
  wan: () => ({ wan_mode: $("#i-wanmode").value, wan_gateway: $("#i-wangw").value.trim(),
                adc_ips: Object.fromEntries([...$$("[data-adc-ip]")].map((i) => [i.dataset.adcIp, i.value.trim()])),
                adc_mgmt_ip: Object.fromEntries([...$$("[data-adc-dhcp]")].map((i) => [i.dataset.adcDhcp, i.value.trim()])),
                wan_dns1: $("#i-dns1").value.trim(), wan_dns2: $("#i-dns2").value.trim() }),
  admin: () => ({ admin_password: $("#i-adminpw").value }),
  license: () => ({ license_mode: ($("input[name=lic]:checked") || {}).value || "none",
                    fortiflex_tokens: Object.fromEntries([...$$("[data-token]")].map((i) => [i.dataset.token, i.value.trim()])) }),
  desktop: () => ({ desktop_mode: $("#i-dmode").value, desktop_ip: $("#i-dip").value.trim(),
                    desktop_gateway: $("#i-dgw").value.trim(), desktop_dns: $("#i-ddns").value.trim() }),
};
$$("[data-save]").forEach((b) => b.addEventListener("click", async () => {
  const key = b.dataset.save;
  try { fillSettings(await post("/api/settings", savePayload[key]())); showResult(key, true, "Opgeslagen"); }
  catch (e) { showResult(key, false, e.message); }
}));

function upload(inputSel, url, progressSel, extra = "") {
  const file = $(inputSel).files[0];
  if (!file) return alert("Kies eerst een bestand");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `${location.origin}${url}?filename=${encodeURIComponent(file.name)}${extra}`);
  if (progressSel) {
    $(progressSel).hidden = false;
    xhr.upload.onprogress = (e) => ($(progressSel).value = (e.loaded / e.total) * 100);
  }
  xhr.onload = () => {
    if (xhr.status === 401) return (location.href = "/login");
    if (xhr.status >= 400) alert(xhr.responseText);
    else fillSettings(JSON.parse(xhr.responseText));
    if (progressSel) $(progressSel).hidden = true;
  };
  xhr.send(file);
}
$("#btn-upload-boot").onclick = () => upload("#f-image-boot", "/api/upload/image", "#p-image-boot", "&kind=boot");
$("#btn-upload-data").onclick = () => upload("#f-image-data", "/api/upload/image", "#p-image-data", "&kind=data");

function secretRow(id, value) {
  return `<div class="secret"><input id="${id}" type="password" readonly value="${esc(value)}">
    <button class="eye" data-eye="${id}" type="button" title="Tonen/verbergen"></button></div>`;
}

async function loadHost() {
  const h = await api("/api/host");
  if (!h.configured) {
    $("#host-info").innerHTML = `<p class="warn">Geen <code>host.json</code> gevonden: draai de installer op de Proxmox host.</p>`;
    return;
  }
  const rows = [
    ["Proxmox host", `${esc(h.node)} <span class="mono">${esc(h.host)}</span>`, "SSH (TCP 22) vanaf de controller."],
    ["SSH-gebruiker", `<span class="mono">${esc(h.ssh_user)}</span> ${h.sudo ? '<span class="tag">sudo: qm, pct, pvesh</span>' : '<span class="tag warn">root</span>'}`,
      "Intrekken met <code>adclab-revoke</code> op de host."],
    ["Werkmap op host", `<span class="mono">${esc(h.workdir)}</span>`, "Images en de config-ISO's."],
    ["Lab-storage", `<span class="mono">${esc(h.storage)}</span>`, ""],
    ["Uplink", `<span class="mono">${esc(h.uplink_bridge)}</span>${h.uplink_vlan ? ` VLAN <span class="mono">${esc(h.uplink_vlan)}</span>` : ""}`, "FortiADC port1 en de desktop-uplink."],
    ["Template-build", `<span class="mono">${esc(h.template_net)}</span>`, "Tijdelijk netwerk om packages en Docker-images te downloaden."],
    ["VMID's", `lab <span class="mono">${esc(h.vmid_range)}</span> · ${esc(h.adc_vmids)} · template ${h.template_vmid} · desktop ${h.desktop_vmid} · controller ${esc(h.controller_ctid)}`, ""],
    ["Web UI-login", `<span class="mono">admin</span> / ${secretRow("o-uipw", h.ui_password)}`, "In de controller: <code>/opt/adclab/data/ui_password</code>."],
  ];
  $("#host-info").innerHTML = `<table class="kv">${rows.map(([k, v, hint]) =>
    `<tr><th>${k}</th><td>${v}${hint ? `<div class="hint">${hint}</div>` : ""}</td></tr>`).join("")}</table>`;
  $("#wan-bridge").textContent = h.uplink_bridge + (h.uplink_vlan ? ` (VLAN ${h.uplink_vlan})` : "");
  bindEyes($("#host-info"));
}
$("#btn-host-test").onclick = async () => {
  const el = $("#host-test-result");
  el.className = "result"; el.textContent = "testen…";
  try { const r = await post("/api/host/test"); el.className = "result " + (r.ok ? "ok" : "bad"); el.textContent = r.detail; }
  catch (e) { el.className = "result bad"; el.textContent = e.message; }
};

// --- demo-cockpit ------------------------------------------------------------
let catalog = null;
function runDemo(act) { action("/api/demo/run", act); }

async function loadDemo() {
  if (!catalog) catalog = await api("/api/demo/catalog");
  $("#demo-groups").innerHTML = catalog.catalog.map((g, gi) => `
    <div class="card demo-group"><div class="card-head"><h2>${esc(g.group)}</h2>
      <a href="#" data-doc="${esc(g.doc)}">script</a></div>
      <div class="demo-actions">${g.actions.map((a, ai) =>
        `<button class="${a.danger ? "danger" : ""}" data-act="${gi}:${ai}">${esc(a.label)}</button>`).join("")}</div></div>`).join("");
  $$("[data-act]").forEach((b) => (b.onclick = () => {
    const [gi, ai] = b.dataset.act.split(":").map(Number);
    const a = catalog.catalog[gi].actions[ai];
    runDemo({ action: a.id, arg: a.arg, label: a.label });
  }));
  $$("[data-doc]").forEach((l) => (l.onclick = (e) => { e.preventDefault(); openDoc(l.dataset.doc); }));
  renderServers(catalog.servers.map((s) => ({ ...s, state: "-", service: "-" })));
}

function renderServers(list) {
  const btn = (name, what, txt, cls = "") => `<button class="${cls}" data-srv="${esc(name)}" data-what="${what}">${txt}</button>`;
  $("#servers").innerHTML = `<table><tr><th>Server</th><th>App</th><th>DC / IP</th><th>Toestand</th><th></th></tr>${list.map((s) => `
    <tr><td><b>${esc(s.name)}</b></td><td>${esc(s.app)}</td><td>${esc(s.dc || s.segment)} · ${esc(s.ip)}</td>
      <td><span class="mono">${esc(s.state)}</span>${s.service && s.service !== "-" ? ` · ${esc(s.service)}` : ""}</td>
      <td class="srvbtns">${btn(s.name, "fail", "Fail", "danger")}${btn(s.name, "ok", "OK")}${btn(s.name, "slow", "Traag")}${btn(s.name, "fast", "Snel")}${btn(s.name, "stop", "Stop", "danger")}${btn(s.name, "start", "Start")}</td></tr>`).join("")}</table>`;
  $$("[data-srv]").forEach((b) => (b.onclick = () =>
    runDemo({ action: "server", arg: [b.dataset.srv, b.dataset.what], label: `${b.dataset.srv}: ${b.textContent}` })));
}
$("#btn-servers").onclick = async () => {
  $("#btn-servers").textContent = "ophalen…";
  try { renderServers((await api("/api/demo/servers")).servers); } catch (e) { alert(e.message); }
  $("#btn-servers").textContent = "Toestand ophalen";
};
$("#btn-reset").onclick = () => runDemo({ action: "reset", label: "Alles herstellen" });

// --- toegang & scripts -----------------------------------------------------------
async function openDoc(name) {
  try { openViewer(name, `<div class="doc">${await api(`/api/docs/${encodeURIComponent(name)}`)}</div>`); }
  catch (e) { alert(e.message); }
}

async function loadAccess() {
  const a = await api("/api/access");
  $("#access").innerHTML = `
    <div class="acc"><h3>FortiADC's</h3>
      <table class="kv">${a.adcs.map((f) => `<tr><th>${esc(f.dc)}</th><td>${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.url)}</a>` : "-"}
          <div class="hint">${esc(f.name)} · GSLB-DNS op ${esc(f.dns_ip)}</div></td></tr>`).join("")}
        <tr><th>Login</th><td><span class="mono">${esc(a.adc_user)}</span> / ${secretRow("acc-adcpw", a.adc_password)}</td></tr></table></div>
    <div class="acc"><h3>Ubuntu desktop <span class="tag">VM ${a.desktop_vmid}</span></h3>
      <p>Neem hem over met een RDP-client (Windows: <i>Extern bureaublad</i>, macOS: <i>Windows App</i>) op het
        uplink-adres, of via de Proxmox-console. ${a.desktop_autologin
          ? "De console logt automatisch in; log daar uit voordat je via RDP verbindt."
          : "Log in met de gegevens hieronder; dezelfde gebruiker kan niet tegelijk op de console én via RDP ingelogd zijn."}</p>
      <table class="kv"><tr><th>RDP</th><td><span class="mono" id="rdp-addr">-</span>
          <button class="ghost" id="btn-rdp" type="button">Adres ophalen</button></td></tr>
        <tr><th>Gebruiker</th><td><span class="mono">${esc(a.desktop_user)}</span></td></tr>
        <tr><th>Wachtwoord</th><td>${secretRow("acc-dpw", a.desktop_password)}</td></tr>
        <tr><th>Lab-IP</th><td><span class="mono">${esc(a.desktop_lab_ip)}</span></td></tr></table>
      <div class="row"><button id="btn-desktop-tune" type="button"
        title="Wizard uit, RDP afstellen, autologin volgens de topologie; daarna herstart de desktop">Desktop bijwerken en herstarten</button></div></div>
    <div class="acc"><h3>Lab-CA</h3>
      <p>De desktop vertrouwt deze CA al. Op je eigen laptop? Download en vertrouw hem: hij is via
        <i>Name Constraints</i> beperkt tot <code>${esc(a.domain)}</code>.</p>
      <table class="kv"><tr><th>SHA-256</th><td class="mono small">${esc(a.ca_fingerprint || "nog niet aangemaakt")}</td></tr></table>
      <div class="row"><a class="button" href="/ca.crt">fortiadc-lab-root-ca.crt</a></div></div>`;
  bindEyes($("#access"));
  const loadRdp = async () => {
    $("#rdp-addr").textContent = "ophalen…";
    try {
      const r = await api("/api/desktop/address");
      $("#rdp-addr").textContent = r.error ? r.error : (r.uplink ? `${r.uplink}:3389` : "geen uplink-adres");
    } catch (e) { $("#rdp-addr").textContent = e.message; }
  };
  $("#btn-rdp").onclick = loadRdp;
  loadRdp();
  $("#btn-desktop-tune").onclick = () => confirm("De desktop afstellen en herstarten? Een open sessie wordt afgesloten.")
    && action("/api/desktop/tune");
  $("#names").innerHTML = `<table><tr><th>Naam</th><th>Virtual server</th><th>VIP's</th><th>DNS</th></tr>${a.names.map((n) => `
    <tr><td><a href="https://${esc(n.host)}/" target="_blank" rel="noopener">${esc(n.host)}</a></td><td>${esc(n.title)}</td>
      <td class="mono">${Object.entries(n.sites).map(([k, v]) => `${esc(k)} ${esc(v)}`).join("<br>")}</td>
      <td>${n.gslb ? `CNAME → <span class="mono">${esc(n.host.split(".")[0])}.${esc(a.gslb_domain)}</span> (GSLB op beide FortiADC's)` : "A-record"}</td></tr>`).join("")}</table>`;
  const { docs } = await api("/api/docs");
  $("#docs").innerHTML = docs.map((d) => `<a href="#" data-doc="${esc(d.name)}">${esc(d.title)}</a>`).join("");
  $$("#docs [data-doc]").forEach((l) => (l.onclick = (e) => { e.preventDefault(); openDoc(l.dataset.doc); }));
}

// --- updates -----------------------------------------------------------------
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("nl-NL", { dateStyle: "medium", timeStyle: "short" }) : "");
let currentCommit = null;

async function loadVersion() {
  const v = await api("/api/version");
  if (!v.git) {
    $("#version-pill").textContent = "geen git";
    $("#upd-current").textContent = "Geen git-checkout gevonden; bijwerken via de UI is niet mogelijk.";
    return v;
  }
  currentCommit = v.commit;
  $("#version-pill").textContent = v.commit;
  $("#upd-current").innerHTML = `<span class="mono">${esc(v.commit)}</span> ${esc(v.subject)} <span class="hint">(${esc(fmtDate(v.date))})</span>`;
  $("#upd-branch").textContent = v.branch;
  $("#app-banner").hidden = !v.labapp_outdated;
  if (v.updating && $("#upd-log").hidden) followUpdate();
  return v;
}

function markUpdateAvailable(n) {
  $("#update-dot").hidden = !n;
  $("#version-pill").classList.toggle("new", !!n);
  const b = $("#upd-badge");
  b.className = "badge " + (n ? "todo" : "ok");
  b.textContent = n ? `${n} update(s) beschikbaar` : "up-to-date";
}

async function checkUpdates(silent) {
  const el = $("#upd-result");
  if (!silent) { el.className = "result"; el.textContent = "controleren…"; }
  try {
    const r = await post("/api/update/check");
    markUpdateAvailable(r.behind);
    if (silent) return;
    el.textContent = "";
    let html = "";
    if (r.behind) {
      html += `<ul class="commits">${r.commits.map((c) =>
        `<li><span class="mono">${esc(c.commit)}</span>${esc(c.subject)} <span class="hint">${esc(fmtDate(c.date))}</span></li>`).join("")}</ul>`;
      if (r.labapp_changed) html += `<p class="help">Deze update wijzigt ook de labapp; klik daarna onder <b>2. Lab</b> op <b>Labapp bijwerken</b>.</p>`;
    } else { el.className = "result ok"; el.textContent = "De controller is up-to-date."; }
    if (r.dirty.length) html += `<p class="warn">Lokale wijzigingen in <code>/opt/adclab</code> blokkeren de update:</p><pre class="logbox">${esc(r.dirty.join("\n"))}</pre>`;
    $("#upd-commits").innerHTML = html;
    $("#btn-upd-run").hidden = !r.behind || r.dirty.length > 0;
  } catch (e) { if (!silent) { el.className = "result bad"; el.textContent = e.message; } }
}
$("#btn-upd-check").onclick = () => checkUpdates(false);
$("#btn-upd-run").onclick = async () => {
  if (!confirm("Controller bijwerken en herstarten? Het lab zelf blijft draaien.")) return;
  try { await post("/api/update/run"); followUpdate(); }
  catch (e) { $("#upd-result").className = "result bad"; $("#upd-result").textContent = e.message; }
};

function followUpdate() {
  const box = $("#upd-log");
  box.hidden = false; box.textContent = "";
  $("#btn-upd-run").hidden = true;
  const before = currentCommit;
  let offset = 0, restarting = false;
  const timer = setInterval(async () => {
    try {
      if (!restarting) {
        const r = await api(`/api/update/log?offset=${offset}`);
        offset = r.offset;
        if (r.lines.length) { box.textContent += r.lines.join("\n") + "\n"; box.scrollTop = box.scrollHeight; }
        if (!r.running) {
          if (/FOUT|Al up-to-date/.test(box.textContent) && !/herstarten/.test(box.textContent)) { clearInterval(timer); return; }
          restarting = true;
          box.textContent += "Wachten tot de controller weer bereikbaar is…\n";
        }
      } else {
        const v = await api("/api/version");
        if (v.commit && v.commit !== before) { clearInterval(timer); location.reload(); }
      }
    } catch (e) { restarting = true; }
  }, 1500);
}

// --- polling ---------------------------------------------------------------
async function refresh() {
  try {
    const st = await api("/api/status");
    renderChecks(st.settings.checks);
    renderLab(st.lab);
    const hs = $("#hoststatus");
    hs.textContent = st.host_error ? "Proxmox: geen verbinding" : "Proxmox: verbonden";
    hs.className = "pill " + (st.host_error ? "bad" : "ok");
    hs.title = st.host_error || "";
    const run = (st.jobs || []).find((j) => j.status === "running");
    const pill = $("#job-pill");
    if (run && !document.body.classList.contains("logopen")) {
      pill._job = { id: run.id, title: run.title };
      pill.textContent = `⟳ ${run.title} loopt — toon log`;
      pill.hidden = false;
    } else pill.hidden = true;
    loadProblems();
  } catch (e) { console.error(e); }
}
loadSettings();
loadHost();
loadVersion().then((v) => v.git && checkUpdates(true));
refresh();
setInterval(refresh, 10000);
