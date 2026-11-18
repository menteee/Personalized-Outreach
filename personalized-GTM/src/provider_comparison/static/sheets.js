"use strict";
/* Team spreadsheets: import any Excel/CSV, create blank sheets, edit cells in the browser,
   add/delete rows and columns, download as .xlsx. Uses helpers exposed by team.js. */
(function () {
  const $ = s => document.querySelector(s);
  const PAGE = 100;
  let ui, me, cur = null, page = 0, sheets = [];
  let pending = new Map(), flushTimer = null, saving = null, pollTimer = null, failed = false;

  document.addEventListener("team-ready", event => {
    ui = window.Team.ui;
    me = event.detail.user;
    const anchor = $("#outreach-open") || $("#outreach-principles");
    anchor.insertAdjacentHTML("afterend", `<button class="brief-button team-link" id="sheets-open" type="button">Sheets <span>↗</span></button>`);
    $("#sheets-open").addEventListener("click", () => openSheets().catch(fail));
  });

  /* ---------- small helpers ---------- */
  function dlg() { return document.getElementById("sheets-dialog"); }
  function msg(text, bad) {
    const el = $("#sh-msg");
    if (el) { el.textContent = text || ""; el.className = "sheet-msg" + (bad ? " bad" : ""); }
  }
  function fail(error) {
    if (error && error.status === 401) window.Team.onUnauthorized();
    else msg((error && error.message) || "Something went wrong.", true);
  }
  function status(text, bad) {
    const el = $("#sh-status");
    if (el) { el.textContent = text; el.className = "sheet-status" + (bad ? " bad" : ""); }
  }
  const stamp = v => ui.when(v);

  /* ---------- the dialog and the sheet list ---------- */
  async function openSheets() {
    const d = ui.drawer("sheets-dialog", true);
    if (!d.dataset.bound) {
      d.dataset.bound = "1";
      d.addEventListener("close", () => { stopPoll(); flush().catch(() => {}); });
      d.addEventListener("input", onInput);
      d.addEventListener("keydown", onKey);
      d.addEventListener("paste", onPaste);
      d.addEventListener("change", onChange);
      d.addEventListener("click", onClick);
    }
    if (!d.open) d.showModal();
    await showList();
  }

  async function showList() {
    stopPoll();
    cur = null;
    $("#sheets-dialog").innerHTML = `<div class="drawer-head"><span class="eyebrow">SHEETS</span><button class="icon-button" data-close aria-label="Close">×</button></div>
      <p class="team-note">Shared with your whole team. Import any Excel or CSV file, or start a blank sheet and type straight into it. Download as .xlsx any time. Cells hold text and numbers (no formulas).</p>
      <div id="sh-msg" class="sheet-msg" role="status"></div>
      <div class="team-filters">
        <button class="primary" id="sh-new" type="button">New sheet</button>
        <label class="secondary file-button">Import Excel / CSV<input type="file" id="sh-file" accept=".xlsx,.csv,.txt" hidden></label>
      </div>
      <div id="sh-body" class="team-table-wrap"></div>`;
    $("#sh-file").addEventListener("change", async event => {
      const file = event.target.files[0];
      event.target.value = "";
      try { await importFile(file); } catch (error) { fail(error); }
    });
    await reloadList();
  }

  async function reloadList() {
    sheets = (await ui.call("/api/sheets")).sheets;
    const body = $("#sh-body");
    if (!body) return;
    body.innerHTML = sheets.length ? `<table class="team-table"><thead><tr><th>Name</th><th>Size</th><th>Last edited</th><th>Created by</th><th></th></tr></thead><tbody>${sheets.map(s =>
      `<tr><td><strong>${ui.esc(s.name)}</strong>${s.source ? `<small>from ${ui.esc(s.source)}</small>` : ""}</td>
       <td class="nowrap">${s.nrows} rows × ${s.ncols} cols</td>
       <td class="nowrap">${ui.esc(stamp(s.updated_at))}<small>${ui.esc(s.updated_by_name)}</small></td>
       <td>${ui.esc(s.created_by_name)}</td>
       <td class="row-actions"><button class="link-button" data-sheet-act="open" data-id="${s.id}" type="button">Open</button>
         <button class="link-button" data-sheet-act="download" data-id="${s.id}" type="button">Download</button>
         ${s.can_delete ? `<button class="link-button danger" data-sheet-act="delete" data-id="${s.id}" type="button">Delete</button>` : ""}</td></tr>`).join("")}</tbody></table>`
      : `<div class="team-empty">No sheets yet. Create a blank one or import an Excel file.</div>`;
  }

  async function importFile(file) {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) throw new Error("Choose a file under 5 MB.");
    msg("Importing…");
    const response = await fetch("/api/sheets/import?filename=" + encodeURIComponent(file.name), {
      method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file});
    const body = await response.json().catch(() => ({}));
    if (response.status === 401) window.Team.onUnauthorized();
    if (!response.ok) throw new Error(body.error || "Import failed");
    openEditor(body.sheet);
  }

  function newSheet() {
    ui.formDialog({
      title: "NEW SHEET", submit: "Create",
      body: `<label class="field">Name<input name="name" required maxlength="80" value="Untitled sheet" autocomplete="off"></label>`,
      onSubmit: async form => { openEditor((await ui.post("/api/sheets", {name: form.elements.name.value})).sheet); },
    });
  }

  function download(id) {
    const link = document.createElement("a");
    link.href = `/api/sheets/${id}/export.xlsx`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  /* ---------- editor ---------- */
  function openEditor(sheet) {
    cur = sheet;
    page = 0;
    pending = new Map();
    render();
    startPoll();
  }

  function render() {
    const s = cur, start = page * PAGE, end = Math.min(start + PAGE, s.rows.length), pages = Math.max(1, Math.ceil(s.rows.length / PAGE));
    const esc = ui.esc;
    const head = s.columns.map((name, c) => `<th class="col-head"><span class="col-letter">${esc(letter(c))}</span>
      <input data-r="-1" data-c="${c}" value="${esc(name)}" maxlength="120" aria-label="Column ${esc(letter(c))} name">
      ${s.columns.length > 1 ? `<button class="col-del" data-delcol="${c}" type="button" title="Delete column" aria-label="Delete column ${esc(name)}">×</button>` : ""}</th>`).join("");
    let rows = "";
    for (let r = start; r < end; r++) {
      rows += `<tr><th class="row-head"><span>${r + 1}</span><button class="row-del" data-delrow="${r}" type="button" title="Delete row" aria-label="Delete row ${r + 1}">×</button></th>` +
        s.rows[r].map((v, c) => `<td><input data-r="${r}" data-c="${c}" value="${esc(v)}" maxlength="2000" aria-label="Row ${r + 1}, ${esc(s.columns[c])}"></td>`).join("") + "</tr>";
    }
    $("#sheets-dialog").innerHTML = `<div class="drawer-head"><span class="eyebrow">SHEET</span><button class="icon-button" data-close aria-label="Close">×</button></div>
      <div id="sh-msg" class="sheet-msg" role="status"></div>
      <div class="sheet-bar">
        <button class="secondary" id="sh-back" type="button">← All sheets</button>
        <input id="sh-name" class="sheet-name" value="${esc(s.name)}" maxlength="80" aria-label="Sheet name">
        <span id="sh-status" class="sheet-status" role="status"></span>
      </div>
      <div class="sheet-bar">
        <button class="secondary" id="sh-addrows" type="button">+ 10 rows</button>
        <button class="secondary" id="sh-addcol" type="button">+ Column</button>
        <button class="secondary" id="sh-tracker" type="button">Add emails to Outreach tracker</button>
        <button class="primary" id="sh-download" type="button">Download .xlsx</button>
        <span class="sheet-size">${s.rows.length} rows · ${s.columns.length} columns · last edited ${esc(stamp(s.updated_at))} by ${esc(s.updated_by_name)}</span>
      </div>
      <div class="sheet-wrap"><table class="sheet-grid"><thead><tr><th class="corner"></th>${head}</tr></thead><tbody>${rows}</tbody></table></div>
      <div class="sheet-pager"><span>Rows ${start + 1}–${end} of ${s.rows.length}</span>
        <button class="secondary" data-page="-1" type="button" ${page === 0 ? "disabled" : ""}>Previous</button>
        <button class="secondary" data-page="1" type="button" ${page >= pages - 1 ? "disabled" : ""}>Next</button></div>`;
    if (pending.size) status("Unsaved changes…");
  }

  function letter(i) {
    let out = "", n = i + 1;
    while (n) { const rem = (n - 1) % 26; out = String.fromCharCode(65 + rem) + out; n = Math.floor((n - 1) / 26); }
    return out;
  }

  /* ---------- saving ---------- */
  function queue(r, c, v) {
    if (r === -1) cur.columns[c] = v; else cur.rows[r][c] = v;
    pending.set(`${r}:${c}`, {r, c, v});
    status("Unsaved changes…");
    clearTimeout(flushTimer);
    flushTimer = setTimeout(() => flush().catch(() => {}), 700);
  }

  async function flush() {
    clearTimeout(flushTimer);
    flushTimer = null;
    if (saving) await saving;
    if (!cur || !pending.size) return;
    const batch = [...pending.values()].slice(0, 500);
    const sent = new Map(batch.map(b => [`${b.r}:${b.c}`, b.v]));
    const sheet = cur;
    failed = false;
    saving = (async () => {
      status("Saving…");
      try {
        const res = await ui.post(`/api/sheets/${sheet.id}/cells`, {sr: sheet.sr, changes: batch});
        for (const [k, v] of sent) { const p = pending.get(k); if (p && p.v === v) pending.delete(k); }
        if (cur === sheet) { if (res.rev === sheet.rev + 1) sheet.rev = res.rev; else sheet.stale = true; }
        status(pending.size ? "Unsaved changes…" : "Saved");
      } catch (error) {
        failed = true;
        if (error.status === 409) { pending = new Map(); await reload("Someone added or removed rows or columns, so the sheet was reloaded. Please repeat your last edit."); }
        else if (error.status === 401) window.Team.onUnauthorized();
        else { status("Not saved: " + error.message, true); flushTimer = setTimeout(() => flush().catch(() => {}), 4000); }
      } finally { saving = null; }
    })();
    await saving;
    if (!failed && pending.size) await flush();
  }

  async function reload(note) {
    try {
      cur = (await ui.call(`/api/sheets/${cur.id}`)).sheet;
      page = Math.min(page, Math.max(0, Math.ceil(cur.rows.length / PAGE) - 1));
      render();
      if (note) msg(note, true);
    } catch (error) {
      if (error.status === 404) { await showList(); msg("This sheet was deleted.", true); } else throw error;
    }
  }

  async function structure(payload) {
    await flush();
    if (pending.size) { msg("Wait for your changes to save, then try again.", true); return; }
    try {
      cur = (await ui.post(`/api/sheets/${cur.id}/ops`, {...payload, sr: cur.sr})).sheet;
      page = Math.min(page, Math.max(0, Math.ceil(cur.rows.length / PAGE) - 1));
      render();
      status("Saved");
    } catch (error) {
      if (error.status === 409) await reload("The sheet had changed, so it was reloaded. Please try again.");
      else fail(error);
    }
  }

  /* ---------- polling for teammates' edits ---------- */
  function startPoll() { stopPoll(); pollTimer = setInterval(poll, 8000); }
  function stopPoll() { clearInterval(pollTimer); pollTimer = null; }
  async function poll() {
    const d = dlg();
    if (!cur || !d || !d.open || pending.size || saving) return;
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest(".sheet-grid")) return;
    try {
      const data = (await ui.call(`/api/sheets/${cur.id}`)).sheet;
      if (cur && data.id === cur.id && (cur.stale || data.rev !== cur.rev) && !pending.size && !saving) {
        cur = data;
        page = Math.min(page, Math.max(0, Math.ceil(cur.rows.length / PAGE) - 1));
        render();
        status(`Updated by ${data.updated_by_name}`);
      }
    } catch (error) {
      if (error.status === 404) { await showList(); msg("This sheet was deleted.", true); }
    }
  }

  /* ---------- events (delegated on the dialog) ---------- */
  function cellOf(target) {
    return target && target.matches && target.matches(".sheet-grid input") ? {r: +target.dataset.r, c: +target.dataset.c} : null;
  }
  function onInput(event) {
    const cell = cellOf(event.target);
    if (cell && cur) queue(cell.r, cell.c, event.target.value);
  }
  function onKey(event) {
    const cell = cellOf(event.target);
    if (!cell || cell.r < 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const step = event.key === "Enter" || event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    const next = dlg().querySelector(`.sheet-grid input[data-r="${cell.r + step}"][data-c="${cell.c}"]`);
    if (next) { event.preventDefault(); next.focus(); next.select(); }
  }
  async function onPaste(event) {
    const cell = cellOf(event.target);
    if (!cell || cell.r < 0 || !cur) return;
    const text = (event.clipboardData || window.clipboardData).getData("text").replace(/\r\n?/g, "\n").replace(/\n$/, "");
    if (!/[\t\n]/.test(text)) return;  // a single value pastes normally
    event.preventDefault();
    const grid = text.split("\n").map(line => line.split("\t"));
    const need = cell.r + grid.length - cur.rows.length;
    if (need > 0) await structure({op: "add_rows", count: Math.min(need, 100)});
    let skipped = 0;
    grid.forEach((line, i) => line.forEach((value, j) => {
      const r = cell.r + i, c = cell.c + j;
      if (r >= cur.rows.length || c >= cur.columns.length) { skipped++; return; }
      queue(r, c, value.slice(0, 2000));
    }));
    render();
    if (skipped) msg(`${skipped} pasted cells did not fit and were skipped (add columns or rows first).`, true);
  }
  async function onChange(event) {
    if (event.target.id !== "sh-name" || !cur) return;
    try {
      cur.name = (await ui.post(`/api/sheets/${cur.id}/rename`, {name: event.target.value})).sheet.name;
      event.target.value = cur.name;
      status("Saved");
    } catch (error) { event.target.value = cur.name; fail(error); }
  }
  async function onClick(event) {
    const t = event.target.closest("button");
    if (!t) return;
    try {
      if (t.id === "sh-new") return newSheet();
      if (t.id === "sh-back") { await flush(); return await showList(); }
      if (t.id === "sh-addrows") return await structure({op: "add_rows", count: 10});
      if (t.id === "sh-addcol") return await structure({op: "add_col"});
      if (t.id === "sh-download") { await flush(); return download(cur.id); }
      if (t.id === "sh-tracker") return await toTracker();
      if (t.dataset.page) { page += +t.dataset.page; render(); return; }
      if (t.dataset.delrow !== undefined) {
        const r = +t.dataset.delrow;
        if (confirm(`Delete row ${r + 1}?`)) await structure({op: "delete_rows", rows: [r]});
        return;
      }
      if (t.dataset.delcol !== undefined) {
        const c = +t.dataset.delcol;
        if (confirm(`Delete column "${cur.columns[c]}" and everything in it?`)) await structure({op: "delete_col", col: c});
        return;
      }
      const act = t.dataset.sheetAct;
      if (act === "open") openEditor((await ui.call(`/api/sheets/${t.dataset.id}`)).sheet);
      else if (act === "download") download(t.dataset.id);
      else if (act === "delete") {
        const s = sheets.find(x => x.id === t.dataset.id);
        if (s && confirm(`Delete the sheet "${s.name}" for everyone? This cannot be undone.`)) {
          await ui.call(`/api/sheets/${s.id}`, {method: "DELETE"});
          await reloadList();
          msg("Sheet deleted.");
        }
      }
    } catch (error) { fail(error); }
  }

  async function toTracker() {
    await flush();
    if (pending.size) { msg("Wait for your changes to save, then try again.", true); return; }
    const r = await ui.post(`/api/sheets/${cur.id}/to-tracker`, {});
    msg(`Added ${r.added} of ${r.rows} rows to the Outreach tracker.` +
      (r.already_tracked_count ? ` ${r.already_tracked_count} were already there.` : "") +
      (r.invalid_count ? ` ${r.invalid_count} had no valid email.` : ""), r.added === 0);
  }

  /* leaving the page with unsaved edits: send them with keepalive */
  window.addEventListener("pagehide", () => {
    if (!cur || !pending.size) return;
    try {
      fetch(`/api/sheets/${cur.id}/cells`, {method: "POST", keepalive: true, headers: {"Content-Type": "application/json"},
        body: JSON.stringify({sr: cur.sr, changes: [...pending.values()].slice(0, 200)})});
    } catch { /* nothing more to do */ }
  });
})();
