"use strict";
/* script.js - all frontend behaviour (vanilla JavaScript, talks to Flask with fetch()). */

// If index.html is opened as a file, talk to the local Flask server; otherwise use the same origin.
const API = location.protocol === "file:" ? "http://127.0.0.1:5000" : "";
const state = { dash: null, editingId: null, whatIf: null, formType: null };
const DISCLAIMER = "Approximate bill based on the entered consumption, tariff and usage assumptions. " +
  "Actual electricity bills may vary due to fixed charges, taxes, duties, fuel surcharge, meter charges and local tariff rules.";
const COLORS = ["#0f6b8f", "#f2a516", "#1e8e5a", "#8e5ea2", "#c0392b", "#7f8c8d"];

/* ---------- small helpers ---------- */
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(n || 0);
const num = (n, d = 2) => Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: d });
const cycleText = (m) => (m === 2 ? "2 Months" : "1 Month");
const typeText = (t) => (t === "factory" ? "Factory / Industrial" : "House / Flat");
const tariffText = (u) => (u.tariff_mode === "custom" ? `Custom Rate ${money(u.custom_rate)}/unit` : "Default Slab");
const fmtDate = (s) => new Date(String(s).replace(" ", "T")).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
const radio = (name) => (document.querySelector(`input[name="${name}"]:checked`) || {}).value;
const setRadio = (name, value) => document.querySelectorAll(`input[name="${name}"]`).forEach((r) => (r.checked = r.value === String(value)));

function toast(message, type = "info") {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), type === "error" ? 7000 : 4500);
}

let pending = 0;
function loading(on) {
  pending = Math.max(0, pending + (on ? 1 : -1));
  $("loading").hidden = pending === 0;
}

/* ---------- fetch wrapper: JSON in, JSON out, friendly errors ---------- */
async function api(method, url, body, isForm = false) {
  loading(true);
  try {
    const options = { method, credentials: "include", headers: {} };
    if (body !== undefined && !isForm) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    if (isForm) options.body = body;
    let res;
    try { res = await fetch(API + url, options); }
    catch (e) { throw new Error("Cannot reach the server. Is the backend running (python app.py)?"); }
    let json;
    try { json = await res.json(); } catch (e) { json = { success: false, message: "Unexpected server response." }; }
    if (!json.success) {
      const err = new Error(json.message || "Request failed.");
      err.status = res.status; err.data = json;
      throw err;
    }
    return json;
  } finally { loading(false); }
}

/* run an API action, show toasts, return the response (or null on error) */
async function act(promiseFn, { silentSuccess = false } = {}) {
  try {
    const res = await promiseFn();
    if (!silentSuccess) toast(res.message + (res.data && res.data.email_status ? " " + res.data.email_status : ""), "success");
    return res;
  } catch (e) {
    if (e.status === 401) { showView("auth"); }
    toast(e.message, "error");
    return null;
  }
}

function showView(name) {
  ["auth", "setup", "app"].forEach((v) => ($(v + "-view").hidden = v !== name));
  window.scrollTo(0, 0);
}

/* ---------- start-up ---------- */
async function init() {
  if (location.protocol === "file:") $("file-warning").hidden = false;
  try {
    const me = await api("GET", "/api/me");
    await routeUser(me.data);
  } catch (e) { showView("auth"); }
}

async function routeUser(user) {
  if (!user.profile_complete) { showView("setup"); return; }
  const res = await api("GET", "/api/dashboard");
  applyDash(res.data);
  showView("app");
}

function applyDash(d) { state.dash = d; renderAll(); }

/* ---------- login / register / setup ---------- */
$("show-register").onclick = () => { $("login-form").hidden = true; $("register-form").hidden = false; };
$("show-login").onclick = () => { $("register-form").hidden = true; $("login-form").hidden = false; };

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await act(() => api("POST", "/api/login", { email: $("login-email").value, password: $("login-password").value }));
  if (!res) return;
  $("login-password").value = "";
  await act(() => routeUser(res.data).then(() => ({ message: "" })), { silentSuccess: true });
});

$("register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { name: $("reg-name").value, email: $("reg-email").value, password: $("reg-password").value, confirm_password: $("reg-confirm").value };
  const res = await act(() => api("POST", "/api/register", body));
  if (!res) return;
  $("register-form").reset();
  $("show-login").click();
  $("login-email").value = body.email;
  $("login-password").focus();
});

document.querySelectorAll('input[name="setup-tariff"]').forEach((r) => r.addEventListener("change", () => ($("setup-rate-row").hidden = radio("setup-tariff") !== "custom")));

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { consumer_type: radio("setup-type"), billing_cycle: Number(radio("setup-cycle")), tariff_mode: radio("setup-tariff") };
  if (body.tariff_mode === "custom") body.custom_rate = $("setup-rate").value;
  const res = await act(() => api("PUT", "/api/profile", body));
  if (res) { applyDash(res.data); showView("app"); }
});

/* ---------- rendering ---------- */
function renderAll() {
  const d = state.dash, a = d.analysis, u = d.user;
  renderHeader(d); renderCards(a, u); renderAnomaly(a);
  renderBars(a); renderHighest(a); renderBill(a);
  renderPie(a); renderLine(d); renderRecs(a); renderTree(a);
  renderDeviceForm(d); renderDeviceTable(d); renderWhatIfSelect(d);
  renderSettings(d); renderReports(d); renderHistory(d);
  $("email-state").textContent = d.email_configured
    ? "Email: configured. A report is emailed only when you click Email Report."
    : "Email: not configured (SMTP environment variables are missing). Reports are still generated and can be downloaded.";
}

function renderHeader(d) {
  const u = d.user;
  $("welcome").textContent = "WELCOME, " + u.name.toUpperCase();
  $("user-email").textContent = u.email;
  $("profile-chips").innerHTML = [typeText(u.consumer_type), "Billing Cycle: " + cycleText(u.billing_cycle), "Tariff: " + tariffText(u)]
    .map((t) => `<span class="chip">${esc(t)}</span>`).join("");
}

function renderCards(a, u) {
  const two = a.months === 2, h = a.highest;
  const cards = [
    ["Total Consumption", `${num(a.cycle_kwh)} kWh`, two ? `Monthly average: ${num(a.monthly_average_kwh)} kWh` : "Estimated for 1 month", false],
    ["Approximate Bill", money(a.bill.total), two ? `Average monthly bill: ${money(a.bill.monthly_average)}` : tariffText(u), false],
    ["Average Daily Usage", `${num(a.avg_daily_kwh)} kWh`, "Per day", false],
    ["Connected Load", `${num(a.connected_load_kw)} kW`, "All devices running together", false],
    ["Highest Consumer", h ? esc(h.name) : "-", h ? `${num(h.monthly_kwh)} kWh/month` : "Add a device to see this", true, true],
    ["Number of Devices", a.device_count, `${a.unit_count} units in total`, false],
  ];
  $("cards").innerHTML = cards.map(([label, value, sub, accent, isText]) =>
    `<div class="card stat ${accent ? "accent" : ""}"><div class="label">${label}</div><div class="value ${isText ? "text" : ""}">${value}</div><div class="sub">${sub}</div></div>`).join("");
}

function renderAnomaly(a) {
  const s = a.statistics;
  const days = s.anomaly_days.map((x) => `Simulated daily pattern: day ${x.day} reaches ${num(x.kwh)} kWh (z-score ${x.z}).`);
  const devices = s.device_outliers.map((o) => `${esc(o.name)} stands out from your other devices (z-score ${o.z}).`);
  $("anomaly").innerHTML =
    (days.length ? `<div class="banner alert"><b>UNUSUAL CONSUMPTION DETECTED</b><br>${days.join("<br>")}</div>` : "") +
    (devices.length ? `<div class="banner warn"><b>Statistical outlier</b><br>${devices.join("<br>")}</div>` : "");
}

function renderBars(a) {
  const list = a.devices.slice(0, 12);
  if (!list.length) { $("bar-chart").innerHTML = `<p class="muted">No devices yet. Add one below or use Generate Sample Data.</p>`; return; }
  const max = list[0].monthly_kwh || 1;
  $("bar-chart").innerHTML = list.map((d, i) =>
    `<div class="bar-row"><div class="bar-label" title="${esc(d.label)}">${esc(d.label)}</div>
      <div class="bar-track"><div class="bar ${i === 0 ? "top" : ""}" style="width:${Math.max(0.5, (d.monthly_kwh / max) * 76)}%"></div>
      <span class="bar-value">${num(d.monthly_kwh)} kWh</span></div></div>`).join("") +
    `<p class="axis-note">Bars show kWh per month${a.devices.length > 12 ? " (top 12 devices)" : ""}. The longest bar is your highest consumer.</p>`;
}

function renderHighest(a) {
  const h = a.highest, el = $("highest");
  if (!h || !a.monthly_kwh) { el.innerHTML = `<p class="muted">Nothing to show yet.</p>`; return; }
  const level = h.percentage > 50 ? "alert" : "warn";
  el.innerHTML = `<div class="highest-name">${esc(h.name)}</div>
    <p class="num">${num(h.monthly_kwh)} kWh/month &middot; ${h.percentage}% of total consumption</p>
    ${a.highest_message ? `<div class="banner ${level}">${esc(a.highest_message)}</div>` : ""}
    ${a.lowest ? `<p class="muted" style="margin-top:.7rem">Lowest consumer: ${esc(a.lowest.name)} (${num(a.lowest.monthly_kwh)} kWh/month)</p>` : ""}`;
}

function renderBill(a) {
  const b = a.bill;
  const rows = b.breakdown.map((r) => `<tr><td>${esc(r.label)}</td><td class="r num">${num(r.units)}</td><td class="r num">${money(r.rate)}</td><td class="r num">${money(r.amount)}</td></tr>`).join("");
  $("bill-breakdown").innerHTML = `<p class="muted">Billing cycle: ${cycleText(a.months)} &middot; ${esc(b.rate_info.replace("Rs.", "\u20B9"))}</p>
    <div class="table-scroll"><table><tr><th>Slab (units)</th><th class="r">Units</th><th class="r">Rate</th><th class="r">Amount</th></tr>${rows ||
      `<tr><td colspan="4" class="muted">No consumption yet.</td></tr>`}
      <tr><td colspan="3"><b>Approximate bill</b></td><td class="r num"><b>${money(b.total)}</b></td></tr></table></div>
    <p class="disclaimer">${DISCLAIMER}</p>`;
}

function renderPie(a) {
  if (!a.devices.length || !a.monthly_kwh) { $("pie").innerHTML = `<p class="muted">Nothing to show yet.</p>`; return; }
  const top = a.devices.slice(0, 5), rest = a.devices.slice(5).reduce((s, d) => s + d.percentage, 0);
  const slices = top.map((d) => [d.name, d.percentage]);
  if (rest > 0.05) slices.push(["Others", rest]);
  let start = 0;
  const stops = slices.map(([, p], i) => { const s = `${COLORS[i % COLORS.length]} ${start}% ${start + p}%`; start += p; return s; });
  $("pie").innerHTML = `<div class="row" style="gap:1.2rem"><div class="pie" role="img" aria-label="Share of consumption by device" style="background:conic-gradient(${stops.join(",")})"></div>
    <div class="legend">${slices.map(([n, p], i) => `<div><i style="background:${COLORS[i % COLORS.length]}"></i>${esc(n)} - ${p.toFixed(1)}%</div>`).join("")}</div></div>`;
}

function renderLine(d) {
  const rows = [...d.reports].reverse();
  if (rows.length < 2) { $("line").innerHTML = `<p class="muted">The line chart appears once you have reports from at least two periods.</p>`; return; }
  const W = 420, H = 170, pad = 34, max = Math.max(...rows.map((r) => r.total_kwh)) || 1;
  const x = (i) => pad + (i * (W - pad * 2)) / (rows.length - 1), y = (v) => H - pad - (v / max) * (H - pad * 2);
  const pts = rows.map((r, i) => `${x(i)},${y(r.total_kwh)}`).join(" ");
  $("line").innerHTML = `<svg class="line-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Consumption by report period">
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="#d3dee5"/>
    <polyline fill="none" stroke="#0f6b8f" stroke-width="2.5" points="${pts}"/>
    ${rows.map((r, i) => `<circle cx="${x(i)}" cy="${y(r.total_kwh)}" r="4" fill="#f2a516"/><text x="${x(i)}" y="${H - 12}" font-size="10" text-anchor="middle" fill="#5b7080">${esc(r.period.split(" ")[0].slice(0, 3))}</text>
      <text x="${x(i)}" y="${y(r.total_kwh) - 8}" font-size="10" text-anchor="middle" fill="#0e2a38">${num(r.total_kwh, 0)}</text>`).join("")}</svg>
    <p class="muted">kWh per report. Trend: ${esc(d.trend.direction)}.</p>`;
}

function renderRecs(a) {
  $("recs").innerHTML = a.recommendations.map((r) => `<div class="rec ${esc(r.level)}">${esc(r.text)}</div>`).join("");
}

function renderTree(a) {
  const node = (n) => `<li>${esc(n.name)} <span class="kwh">${num(n.kwh)} kWh</span>${n.children.length ? `<ul>${n.children.map(node).join("")}</ul>` : ""}</li>`;
  $("tree").innerHTML = a.hierarchy ? `<ul class="tree">${node(a.hierarchy)}</ul>` : `<p class="muted">Add devices to see this map.</p>`;
}

/* ----- devices ----- */
function renderDeviceForm(d) {
  const factory = d.user.consumer_type === "factory";
  if (state.formType !== d.user.consumer_type) {
    state.formType = d.user.consumer_type;
    $("dev-category").innerHTML = (factory ? d.meta.machine_types : d.meta.categories).map((c) => `<option>${esc(c)}</option>`).join("");
    $("name-suggestions").innerHTML = "";
  }
  $("lbl-category").textContent = factory ? "Machine type" : "Category";
  $("lbl-name").textContent = factory ? "Machine name" : "Appliance";
  $("lbl-days").textContent = factory ? "Working days / month" : "Days / month";
  $("device-table-title").textContent = factory ? "Your machines" : "Your appliances";
  if (state.editingId === null) {
    $("device-heading").textContent = factory ? "Add Machine" : "Add Appliance";
    $("dev-submit").textContent = factory ? "+ Add Machine" : "+ Add Appliance";
  }
  $("undo-btn").hidden = !d.can_undo_import;
}

function renderDeviceTable(d) {
  const factory = d.user.consumer_type === "factory", list = d.analysis.devices;
  const head = factory ? ["Machine", "Power", "Quantity", "Hours", "Days", "Monthly kWh", "Actions"]
                       : ["Appliance", "Wattage", "Quantity", "Hours/day", "Days/month", "Monthly kWh", "Actions"];
  const rows = list.map((x) => `<tr>
      <td>${esc(x.name)}<div class="muted">${esc(x.category)}</div></td>
      <td class="num">${num(x.wattage, 2)} W</td><td class="num">${x.quantity}</td>
      <td class="num">${num(x.hours_per_day)}</td><td class="num">${x.days_per_month}</td>
      <td class="num"><b>${num(x.monthly_kwh)}</b></td>
      <td><button class="btn ghost small" data-action="edit-device" data-id="${x.id}">Edit</button>
          <button class="btn danger small" data-action="delete-device" data-id="${x.id}">Delete</button></td></tr>`).join("");
  $("device-table").innerHTML = `<tr>${head.map((h, i) => `<th>${h}</th>`).join("")}</tr>` +
    (rows || `<tr><td colspan="7" class="muted">Nothing added yet. Use the form above, import a CSV, or generate sample data.</td></tr>`);
}

function resetDeviceForm() {
  state.editingId = null;
  $("device-form").reset();
  $("dev-cancel").hidden = true;
  renderDeviceForm(state.dash);
}

$("device-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { name: $("dev-name").value, category: $("dev-category").value, wattage: $("dev-wattage").value,
                 quantity: $("dev-quantity").value, hours_per_day: $("dev-hours").value, days_per_month: $("dev-days").value };
  const editing = state.editingId;
  const res = await act(() => editing ? api("PUT", `/api/devices/${editing}`, body) : api("POST", "/api/devices", body));
  if (res) { resetDeviceForm(); applyDash(res.data); }
});
$("dev-cancel").onclick = resetDeviceForm;

function startEdit(id) {
  const x = state.dash.analysis.devices.find((v) => v.id === id);
  if (!x) return;
  state.editingId = id;
  $("dev-name").value = x.name; $("dev-category").value = x.category;
  $("dev-wattage").value = x.wattage; $("dev-quantity").value = x.quantity;
  $("dev-hours").value = x.hours_per_day; $("dev-days").value = x.days_per_month;
  $("device-heading").textContent = "Edit " + x.name;
  $("dev-submit").textContent = "Save changes";
  $("dev-cancel").hidden = false;
  $("sec-devices").scrollIntoView({ behavior: "smooth" });
  $("dev-wattage").focus();
}

/* Trie autocomplete: ask the server for names that start with what the user typed */
let suggestTimer;
$("dev-name").addEventListener("input", () => {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    const q = $("dev-name").value.trim();
    if (q.length < 2) return;
    try {
      const res = await fetch(`${API}/api/suggest?q=${encodeURIComponent(q)}&type=${state.formType || "house"}`, { credentials: "include" });
      const json = await res.json();
      $("name-suggestions").innerHTML = (json.data || []).map((n) => `<option value="${esc(n)}">`).join("");
    } catch (e) { /* suggestions are optional */ }
  }, 200);
});

/* CSV import */
$("csv-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  $("csv-errors").innerHTML = "";
  try {
    const res = await api("POST", "/api/import-csv", form, true);
    toast(res.message + (res.data.email_status ? " " + res.data.email_status : ""), "success");
    applyDash(res.data);
  } catch (err) {
    toast(err.message, "error");
    const list = (err.data && err.data.errors) || [];
    if (list.length) $("csv-errors").innerHTML = `<div class="banner alert"><b>${esc(err.message)}</b><ul>${list.map((m) => `<li>${esc(m)}</li>`).join("")}</ul></div>`;
  }
  e.target.value = "";
});

/* ----- what-if ----- */
function renderWhatIfSelect(d) {
  const list = d.analysis.devices, select = $("wi-device"), previous = select.value;
  $("whatif-empty").hidden = list.length > 0;
  $("whatif-body").hidden = list.length === 0;
  select.innerHTML = list.map((x) => `<option value="${x.id}">${esc(x.label)}</option>`).join("");
  if (list.some((x) => String(x.id) === previous)) select.value = previous;
  updateWhatIfCurrent();
}
function updateWhatIfCurrent() {
  const x = state.dash.analysis.devices.find((v) => String(v.id) === $("wi-device").value);
  $("wi-current").textContent = x ? `${num(x.hours_per_day)} hours/day` : "-";
  if (x && !$("wi-hours").value) $("wi-hours").value = x.hours_per_day;
  $("wi-result").innerHTML = ""; state.whatIf = null;
}
$("wi-device").addEventListener("change", () => { $("wi-hours").value = ""; updateWhatIfCurrent(); });

$("wi-calc").onclick = async () => {
  const id = Number($("wi-device").value), hours = $("wi-hours").value;
  const res = await act(() => api("POST", "/api/what-if", { device_id: id, new_hours: hours }), { silentSuccess: true });
  if (!res) return;
  const r = res.data, saving = r.saving_kwh_monthly >= 0, billSaving = r.bill_saving >= 0;
  state.whatIf = { id, hours: Number(hours) };
  $("wi-result").innerHTML = `<div class="grid cols-3">
      <div class="card stat"><div class="label">Current consumption (${esc(r.device_name)})</div><div class="value">${num(r.current_device_kwh)} kWh</div><div class="sub">per month</div></div>
      <div class="card stat"><div class="label">New consumption</div><div class="value">${num(r.new_device_kwh)} kWh</div><div class="sub">per month at ${num(r.new_hours)} h/day</div></div>
      <div class="card stat accent"><div class="label">${saving ? "Energy saving" : "Extra energy"}</div><div class="value">${num(Math.abs(r.saving_kwh_monthly))} kWh</div><div class="sub">per month</div></div>
      <div class="card stat accent"><div class="label">Approximate bill ${billSaving ? "saving" : "increase"}</div><div class="value">${money(Math.abs(r.bill_saving))}</div><div class="sub">per ${cycleText(r.months).toLowerCase()} cycle: ${money(r.current_bill)} \u2192 ${money(r.new_bill)}</div></div></div>
    <p class="muted" style="margin-top:.8rem">Nothing has been saved. Click Apply Changes to update your profile and report.</p>
    <button class="btn" data-action="apply-whatif">Apply Changes</button>`;
};

/* ----- tariff / billing settings ----- */
function renderSettings(d) {
  const slabs = d.tariff.default_slabs, u = d.user;
  $("slab-table").innerHTML = slabs.map((s, i) => {
    const label = s.max === null ? `Above ${slabs[i - 1].max}` : `${s.min}-${s.max}`;
    return `<tr><td>${label} units</td><td class="r num">${money(s.rate)}/unit</td></tr>`;
  }).join("");
  $("tariff-note").textContent = `${d.tariff.note} Slabs are applied to the units of one whole billing cycle. ${DISCLAIMER}`;
  setRadio("set-cycle", u.billing_cycle); setRadio("set-tariff", u.tariff_mode);
  $("set-rate").value = u.custom_rate || "";
  $("set-rate-row").hidden = u.tariff_mode !== "custom";
  $("set-type").value = u.consumer_type;
  $("set-type").disabled = d.analysis.device_count > 0;
  $("set-type-note").textContent = d.analysis.device_count > 0 ? "Delete all devices to change the consumer type." : "";
}
document.querySelectorAll('input[name="set-tariff"]').forEach((r) => r.addEventListener("change", () => ($("set-rate-row").hidden = radio("set-tariff") !== "custom")));

/* ----- reports & history ----- */
function renderReports(d) {
  if (!d.reports.length) { $("reports").innerHTML = `<p class="muted">No reports available yet.</p>`; return; }
  $("reports").innerHTML = `<div class="table-scroll"><table><tr><th>Period</th><th class="r">Consumption</th><th class="r">Approx Bill</th><th>Created</th><th>Actions</th></tr>` +
    d.reports.map((r) => `<tr><td>${esc(r.period)}</td><td class="r num">${num(r.total_kwh)} kWh</td><td class="r num">${money(r.approx_bill)}</td><td>${fmtDate(r.created_at)}</td>
      <td><button class="btn ghost small" data-action="view-report" data-id="${r.id}">View</button>
          <a class="btn ghost small" style="text-decoration:none;display:inline-block" href="${API}/api/reports/${r.id}/pdf">Download PDF</a>
          <button class="btn ghost small" data-action="email-report" data-id="${r.id}">Email Report</button></td></tr>`).join("") + `</table></div>`;
}

function renderHistory(d) {
  $("history").innerHTML = d.history.length
    ? d.history.map((h) => `<div style="margin:.55rem 0"><b>${fmtDate(h.created_at)}</b><br><span class="muted" style="font-size:.92rem">${esc(h.description)}</span></div>`).join("")
    : `<p class="muted">No updates yet.</p>`;
}

async function viewReport(id) {
  const res = await act(() => api("GET", `/api/reports/${id}`), { silentSuccess: true });
  if (!res) return;
  const s = res.data, a = s.analysis;
  const rows = a.devices.map((x) => `<tr><td>${esc(x.name)}</td><td class="num">${num(x.wattage)} W</td><td class="num">${x.quantity}</td><td class="num">${num(x.hours_per_day)} h x ${x.days_per_month} d</td><td class="r num">${num(x.monthly_kwh)}</td><td class="r num">${x.percentage}%</td></tr>`).join("");
  $("modal-card").innerHTML = `<div class="row spread"><h2>Electricity Consumption Report - ${esc(s.period)}</h2><button class="btn ghost small" data-action="close-modal">Close</button></div>
    <p class="muted">${esc(s.user.name)} &middot; ${esc(s.user.email)} &middot; ${typeText(s.profile.consumer_type)} &middot; ${cycleText(a.months)} &middot; Generated ${esc(s.generated_at)}</p>
    <div class="grid cols-3" style="margin:.8rem 0">
      <div class="card stat"><div class="label">Total consumption</div><div class="value">${num(a.cycle_kwh)} kWh</div></div>
      <div class="card stat"><div class="label">Approximate bill</div><div class="value">${money(a.bill.total)}</div></div>
      <div class="card stat"><div class="label">Connected load</div><div class="value">${num(a.connected_load_kw)} kW</div></div></div>
    <div class="table-scroll"><table><tr><th>Device</th><th>Wattage</th><th>Qty</th><th>Usage</th><th class="r">Monthly kWh</th><th class="r">Share</th></tr>${rows}</table></div>
    <h3 style="margin-top:1rem">Recommendations</h3>${a.recommendations.map((r) => `<div class="rec ${esc(r.level)}">${esc(r.text)}</div>`).join("")}
    <p class="disclaimer">${DISCLAIMER}</p>
    <a class="btn" style="text-decoration:none;display:inline-block" href="${API}/api/reports/${id}/pdf">Download PDF</a>`;
  $("modal").hidden = false;
}
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) $("modal").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("modal").hidden = true; });

/* ---------- button actions (one click listener for every data-action) ---------- */
function downloadText(filename, text) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  link.download = filename; link.click(); URL.revokeObjectURL(link.href);
}

document.addEventListener("click", async (e) => {
  const button = e.target.closest("[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id), d = state.dash;
  let res;
  switch (button.dataset.action) {
    case "logout":
      await act(() => api("POST", "/api/logout"), { silentSuccess: true });
      state.dash = null; state.formType = null; state.editingId = null;
      showView("auth"); break;
    case "generate": res = await act(() => api("POST", "/api/recalculate", {})); if (res) applyDash(res.data); break;
    case "download-current": window.location.href = API + "/api/reports/current/pdf"; break;
    case "email-current": await act(() => api("POST", "/api/email-report", {})); break;
    case "email-report": await act(() => api("POST", "/api/email-report", { report_id: id })); break;
    case "view-report": await viewReport(id); break;
    case "close-modal": $("modal").hidden = true; break;
    case "sample": res = await act(() => api("POST", "/api/sample-data", {})); if (res) applyDash(res.data); break;
    case "undo-import": res = await act(() => api("POST", "/api/undo-import", {})); if (res) applyDash(res.data); break;
    case "export-csv": window.location.href = API + "/api/export-csv"; break;
    case "template-csv": {
      const f = d.user.consumer_type === "factory";
      downloadText("devices_template.csv", (f ? "name,machine_type" : "name,category") + ",wattage,quantity,hours_per_day,days_per_month\n" +
        (f ? "CNC Machine,CNC Machine,5000,3,10,26\n" : "LED Bulb,Lighting,9,6,6,30\n"));
      break;
    }
    case "edit-device": startEdit(id); break;
    case "delete-device": {
      const x = d.analysis.devices.find((v) => v.id === id);
      if (x && confirm(`Delete ${x.name} (${x.wattage} W)? This will update your report.`)) {
        res = await act(() => api("DELETE", `/api/devices/${id}`));
        if (res) { if (state.editingId === id) resetDeviceForm(); applyDash(res.data); }
      }
      break;
    }
    case "save-cycle": {
      const body = { billing_cycle: Number(radio("set-cycle")) };
      if (!$("set-type").disabled) body.consumer_type = $("set-type").value;
      res = await act(() => api("PUT", "/api/profile", body)); if (res) applyDash(res.data); break;
    }
    case "save-tariff": {
      const body = { tariff_mode: radio("set-tariff") };
      if (body.tariff_mode === "custom") body.custom_rate = $("set-rate").value;
      res = await act(() => api("PUT", "/api/tariff", body)); if (res) applyDash(res.data); break;
    }
    case "apply-whatif": {
      // Only now is the database changed: send the full device with the new hours.
      const w = state.whatIf, x = d.analysis.devices.find((v) => v.id === (w && w.id));
      if (!x) break;
      const body = { name: x.name, category: x.category, wattage: x.wattage, quantity: x.quantity, hours_per_day: w.hours, days_per_month: x.days_per_month };
      res = await act(() => api("PUT", `/api/devices/${x.id}`, body));
      if (res) { $("wi-hours").value = ""; applyDash(res.data); }
      break;
    }
  }
});

init();
