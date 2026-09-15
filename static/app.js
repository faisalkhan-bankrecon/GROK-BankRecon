/* Concord — multi-user bank recon UI with Google sign-in */

const state = {
  transactions: [],
  matches: [],
  period: {},
  report: {},
  selected: new Set(),
  search: "",
  statusFilter: "all",
  sideView: "bank",
  session: null,
  accessToken: null,
};

let supabaseClient = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function money(cents) {
  if (cents == null || Number.isNaN(cents)) return "—";
  const neg = cents < 0;
  const abs = Math.abs(cents) / 100;
  const s = abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return (neg ? "−" : "") + "$" + s;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, ms = 2800) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.hidden = true;
  }, ms);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.accessToken) {
    headers["Authorization"] = `Bearer ${state.accessToken}`;
  }
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    await showLogin("Session expired — please sign in again");
    throw new Error("Sign in required");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || JSON.stringify(j);
    } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  // binary exports
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("text/csv") || ct.includes("spreadsheet") || ct.includes("octet-stream")) {
    return res.blob();
  }
  return res.json();
}

function applySnapshot(data) {
  state.transactions = data.transactions || [];
  state.matches = data.matches || [];
  state.period = data.period || {};
  state.report = data.report || {};
  render();
}

async function loadState() {
  const data = await api("/api/state");
  applySnapshot(data);
}

function filtered(side) {
  const q = state.search.trim().toLowerCase();
  return state.transactions.filter((t) => {
    if (t.side !== side) return false;
    if (state.statusFilter !== "all" && t.status !== state.statusFilter) return false;
    if (!q) return true;
    return (
      (t.description || "").toLowerCase().includes(q) ||
      (t.reference || "").toLowerCase().includes(q) ||
      (t.date || "").includes(q)
    );
  });
}

function renderKpis() {
  const r = state.report || {};
  $("#k-bank").textContent = r.bank_count ?? 0;
  $("#k-book").textContent = r.book_count ?? 0;
  $("#k-matched").textContent = r.matched_count ?? 0;
  $("#k-proposed").textContent = r.proposed_count ?? 0;
  $("#k-umb").textContent = r.unmatched_bank_count ?? 0;
  $("#k-umk").textContent = r.unmatched_book_count ?? 0;
  $("#k-diff").textContent = money(r.activity_difference_cents ?? 0);
}

function rowHtml(t) {
  const sel = state.selected.has(t.id) ? "selected" : "";
  const amtClass = t.amount_cents >= 0 ? "amt-pos" : "amt-neg";
  return `<tr class="tx ${sel} ${t.status}" data-id="${t.id}">
    <td><input type="checkbox" ${state.selected.has(t.id) ? "checked" : ""} data-id="${t.id}" /></td>
    <td class="mono">${escapeHtml(t.date)}</td>
    <td class="desc">${escapeHtml(t.description)}</td>
    <td class="num ${amtClass}">${money(t.amount_cents)}</td>
    <td><span class="badge ${t.status}">${t.status}</span></td>
  </tr>`;
}

function renderLedgers() {
  const bank = filtered("bank");
  const book = filtered("book");
  $("#bank-count").textContent = `${bank.length}`;
  $("#book-count").textContent = `${book.length}`;
  $("#bank-body").innerHTML = bank.length
    ? bank.map(rowHtml).join("")
    : `<tr><td colspan="5" class="empty">No bank lines — import a statement</td></tr>`;
  $("#book-body").innerHTML = book.length
    ? book.map(rowHtml).join("")
    : `<tr><td colspan="5" class="empty">No book lines — import your cash book</td></tr>`;
}

function selectedTxs(side) {
  return state.transactions.filter(
    (t) => state.selected.has(t.id) && (!side || t.side === side)
  );
}

function renderMatchBar() {
  const bar = $("#match-bar");
  const banks = selectedTxs("bank");
  const books = selectedTxs("book");
  if (!banks.length && !books.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const bt = banks.reduce((s, t) => s + t.amount_cents, 0);
  const kt = books.reduce((s, t) => s + t.amount_cents, 0);
  let text = `${banks.length} bank (${money(bt)}) · ${books.length} book (${money(kt)})`;
  if (banks.length && books.length) {
    text += bt === kt ? " · totals match" : " · totals differ";
  }
  $("#match-bar-text").textContent = text;
  $("#btn-manual-match").disabled = !(banks.length && books.length && bt === kt);
}

function renderExceptions() {
  const r = state.report || {};
  const fmt = (list) =>
    (list || []).length
      ? list
          .map(
            (t) => `<li class="exc-item">
          <span class="mono">${escapeHtml(t.date)}</span>
          <span>${escapeHtml(t.description)}</span>
          <span class="mono ${t.amount_cents >= 0 ? "amt-pos" : "amt-neg"}">${money(t.amount_cents)}</span>
        </li>`
          )
          .join("")
      : `<li class="empty">None</li>`;
  $("#exc-bank").innerHTML = fmt(r.unmatched_bank);
  $("#exc-book").innerHTML = fmt(r.unmatched_book);
}

function renderReport() {
  const r = state.report || {};
  const rows = [
    ["Bank activity total", r.bank_activity_cents],
    ["Book activity total", r.book_activity_cents],
    ["Activity difference (book − bank)", r.activity_difference_cents],
    ["Unmatched bank sum", r.unmatched_bank_cents],
    ["Unmatched book sum", r.unmatched_book_cents],
  ];
  $("#report-body").innerHTML =
    `<table class="report-table"><tbody>` +
    rows
      .map(
        ([label, cents]) =>
          `<tr><td>${label}</td><td class="mono">${money(cents)}</td></tr>`
      )
      .join("") +
    `</tbody></table>`;
}

function render() {
  renderKpis();
  renderLedgers();
  renderMatchBar();
  renderExceptions();
  renderReport();
}

/* ---------- Auth ---------- */

async function initSupabase() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  if (!cfg.supabase_url || !cfg.supabase_anon_key) {
    throw new Error(
      "Supabase is not configured on the server (SUPABASE_URL / SUPABASE_ANON_KEY)"
    );
  }
  supabaseClient = window.supabase.createClient(
    cfg.supabase_url,
    cfg.supabase_anon_key
  );
}

async function showLogin(errMsg) {
  $("#app").hidden = true;
  $("#login-screen").hidden = false;
  const err = $("#login-error");
  if (errMsg) {
    err.hidden = false;
    err.textContent = errMsg;
  } else {
    err.hidden = true;
  }
}

async function showApp(session) {
  state.session = session;
  state.accessToken = session.access_token;
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  const email = session.user?.email || "Signed in";
  $("#user-email").textContent = email;
  await loadState();
}

async function handleSession(session) {
  if (session) {
    await showApp(session);
  } else {
    state.session = null;
    state.accessToken = null;
    await showLogin();
  }
}

async function signInWithGoogle() {
  const err = $("#login-error");
  err.hidden = true;
  const redirectTo = window.location.origin + "/";
  const { error } = await supabaseClient.auth.signInWithOAuth({
    provider: "google",
    options: { redirectTo },
  });
  if (error) {
    err.hidden = false;
    err.textContent = error.message;
  }
}

async function signOut() {
  await supabaseClient.auth.signOut();
  state.session = null;
  state.accessToken = null;
  state.transactions = [];
  state.matches = [];
  await showLogin();
}

/* ---------- UI bindings ---------- */

function bindLedgerClicks() {
  document.addEventListener("click", (e) => {
    const cb = e.target.closest('input[type="checkbox"][data-id]');
    if (cb) {
      const id = cb.dataset.id;
      if (cb.checked) state.selected.add(id);
      else state.selected.delete(id);
      renderMatchBar();
      const tr = cb.closest("tr");
      if (tr) tr.classList.toggle("selected", cb.checked);
      return;
    }
    const tr = e.target.closest("tr.tx");
    if (tr && !e.target.closest("input")) {
      const id = tr.dataset.id;
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
      renderLedgers();
      renderMatchBar();
    }
  });
}

function bindTabs() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      $$(".panel").forEach((p) => p.classList.remove("active"));
      $(`#panel-${tab.dataset.tab}`).classList.add("active");
    });
  });
}

async function uploadFile(file, side) {
  const replace = $(`#replace-${side}`).checked;
  const fd = new FormData();
  fd.append("side", side);
  fd.append("file", file);
  fd.append("replace", replace ? "true" : "false");
  try {
    const data = await api("/api/upload", { method: "POST", body: fd });
    applySnapshot(data);
    state.selected.clear();
    const u = data.upload || {};
    $("#upload-status").textContent = `Loaded ${u.count} ${side} line(s) from ${u.filename || file.name}`;
    toast(`Imported ${u.count} ${side} transactions`);
  } catch (err) {
    toast(String(err.message || err));
    $("#upload-status").textContent = String(err.message || err);
  }
}

function bindUpload() {
  $("#btn-upload-bank").addEventListener("click", () => {
    const f = $("#file-bank").files?.[0];
    if (f) uploadFile(f, "bank");
    else toast("Choose a bank file first");
  });
  $("#btn-upload-book").addEventListener("click", () => {
    const f = $("#file-book").files?.[0];
    if (f) uploadFile(f, "book");
    else toast("Choose a books file first");
  });
}

function bindActions() {
  $("#btn-google").addEventListener("click", signInWithGoogle);
  $("#btn-signout").addEventListener("click", signOut);

  $("#btn-run").addEventListener("click", async () => {
    try {
      const data = await api("/api/match/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      applySnapshot(data);
      const n = (data.matches || []).filter((m) => m.status === "proposed").length;
      toast(n ? `${n} proposal(s) ready` : "No new proposals");
    } catch (e) {
      toast(e.message);
    }
  });

  $("#btn-accept-high").addEventListener("click", async () => {
    try {
      const data = await api("/api/match/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ min_confidence: 80 }),
      });
      applySnapshot(data);
      toast("Accepted proposals ≥ 80%");
    } catch (e) {
      toast(e.message);
    }
  });

  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("Clear all your transactions and matches?")) return;
    const data = await api("/api/reset", { method: "POST" });
    state.selected.clear();
    applySnapshot(data);
    toast("Workspace cleared");
  });

  $("#btn-clear-sel").addEventListener("click", () => {
    state.selected.clear();
    renderLedgers();
    renderMatchBar();
  });

  $("#btn-manual-match").addEventListener("click", async () => {
    const bank_ids = selectedTxs("bank").map((t) => t.id);
    const book_ids = selectedTxs("book").map((t) => t.id);
    try {
      const data = await api("/api/match/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bank_ids, book_ids }),
      });
      state.selected.clear();
      applySnapshot(data);
      toast("Manual match saved");
    } catch (e) {
      toast(e.message);
    }
  });

  // Authenticated downloads
  async function downloadAuth(path, filename) {
    try {
      const blob = await api(path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(e.message);
    }
  }
  $("#btn-export-csv").addEventListener("click", (e) => {
    e.preventDefault();
    downloadAuth("/api/export/csv", "concord-recon-report.csv");
  });
  $("#btn-export-xlsx").addEventListener("click", (e) => {
    e.preventDefault();
    downloadAuth("/api/export/xlsx", "concord-recon-report.xlsx");
  });

  $("#search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderLedgers();
  });
  $("#status-filter").addEventListener("change", (e) => {
    state.statusFilter = e.target.value;
    renderLedgers();
  });
  $$("[data-side-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.sideView = btn.dataset.sideView;
      $$("[data-side-view]").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      renderLedgers();
    });
  });
}

/* ---------- Boot ---------- */

document.addEventListener("DOMContentLoaded", async () => {
  bindTabs();
  bindLedgerClicks();
  bindUpload();
  bindActions();

  try {
    await initSupabase();
    const { data } = await supabaseClient.auth.getSession();
    await handleSession(data.session);

    supabaseClient.auth.onAuthStateChange(async (_event, session) => {
      await handleSession(session);
    });
  } catch (e) {
    await showLogin(String(e.message || e));
  }
});
