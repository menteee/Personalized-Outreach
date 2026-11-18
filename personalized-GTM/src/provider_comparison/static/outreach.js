"use strict";
/* Outreach tracker: import prospects from Excel/CSV, start searches, send approved emails,
   and download the "reached out / not reached out" spreadsheet. Uses helpers from team.js. */
(function () {
  const $ = s => document.querySelector(s);
  let ui, me, smtp = {configured: false}, contacts = [], selected = new Set(), timer = null;
  const KIND_CLASS = {new: "kind-new", skipped: "kind-new", researching: "kind-warn", confirm_identity: "kind-warn",
    attention: "kind-off", draft_ready: "kind-research", approved: "kind-research", sent: "kind-sent"};
  const IN_PROGRESS = ["researching", "confirm_identity", "attention", "draft_ready", "approved"];
  const FILTERS = [["", "All contacts"], ["sent", "Reached out"], ["not_sent", "Not reached out"], ["new", "Not started"],
    ["progress", "In progress"], ["skipped", "Skipped"]];

  document.addEventListener("team-ready", event => {
    ui = window.Team.ui;
    me = event.detail.user;
    $("#outreach-principles").insertAdjacentHTML("afterend",
      `<button class="brief-button team-link" id="outreach-open" type="button">Outreach tracker <span>↗</span></button>`);
    $("#outreach-open").addEventListener("click", () => openTracker().catch(ui.showError));
  });

  /* ---------- tracker dialog ---------- */
  async function openTracker() {
    const dialog = ui.drawer("outreach-dialog", true);
    dialog.innerHTML = `<div class="drawer-head"><span class="eyebrow">OUTREACH TRACKER</span><button class="icon-button" data-close aria-label="Close">×</button></div>
      <div id="oc-banner"></div>
      <div class="team-filters">
        <label class="secondary file-button">Import Excel / CSV<input type="file" id="oc-file" accept=".xlsx,.csv,.txt" hidden></label>
        <button class="secondary" id="oc-download" type="button">Download spreadsheet</button>
        <button class="primary" id="oc-start-selected" type="button" disabled>Start selected</button>
        <select id="oc-filter" aria-label="Filter contacts">${FILTERS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
        <input type="search" id="oc-q" placeholder="Search name, email, company" aria-label="Search contacts">
      </div>
      <p class="team-note">Columns: <strong>Email</strong> (required), Name, Company, Role, LinkedIn URL. Missing names and companies are guessed from the email address and marked, and you confirm them before anything is searched. Nothing is emailed until you approve each draft and click Send.</p>
      <div id="oc-import"></div><div id="oc-stats" class="team-count"></div><div id="oc-body" class="team-table-wrap"></div>`;
    $("#oc-file").addEventListener("change", async event => {
      const file = event.target.files[0];
      event.target.value = "";
      try { await importFile(file); } catch (error) { ui.showError(error); }
    });
    $("#oc-download").addEventListener("click", download);
    $("#oc-filter").addEventListener("change", render);
    $("#oc-q").addEventListener("input", render);
    $("#oc-start-selected").addEventListener("click", () => startSelected().catch(ui.showError));
    $("#oc-body").addEventListener("click", rowClick);
    $("#oc-body").addEventListener("change", selectionChange);
    dialog.addEventListener("close", () => clearInterval(timer), {once: true});
    dialog.showModal();
    await load().catch(ui.showError);
    clearInterval(timer);
    timer = setInterval(() => { if (dialog.open && !document.querySelector("dialog.form[open]")) load().catch(() => {}); }, 5000);
  }

  async function load() {
    const data = await ui.call("/api/outreach/contacts");
    contacts = data.contacts;
    smtp = data.smtp;
    selected = new Set([...selected].filter(id => contacts.some(c => c.id === id && c.can_start && c.complete)));
    render();
  }

  function visibleContacts() {
    const filter = $("#oc-filter").value, q = $("#oc-q").value.trim().toLowerCase();
    return contacts.filter(c => {
      const matches = !filter || (filter === "sent" && c.state === "sent") || (filter === "not_sent" && c.state !== "sent") ||
        (filter === "new" && c.state === "new") || (filter === "progress" && IN_PROGRESS.includes(c.state)) ||
        (filter === "skipped" && c.state === "skipped");
      return matches && (!q || [c.name, c.email, c.company, c.role, c.note].join(" ").toLowerCase().includes(q));
    });
  }

  function render() {
    const esc = ui.esc, admin = me.role === "admin";
    $("#oc-banner").innerHTML = smtp.configured
      ? `<div class="team-banner">Emails are sent from <strong>${esc(smtp.from_name)} &lt;${esc(smtp.from)}&gt;</strong>, one at a time, only after you confirm each draft.</div>`
      : `<div class="team-banner warn">Email sending is not set up on this server, so you can research and draft but not send yet. Add the SMTP_* settings to <code>.env</code> (see <code>env.example</code>) and restart.</div>`;
    const sent = contacts.filter(c => c.state === "sent").length;
    const rows = visibleContacts();
    $("#oc-stats").textContent = `${contacts.length} contacts · ${sent} reached out · ${contacts.length - sent} not reached out · showing ${rows.length}`;
    $("#oc-start-selected").disabled = selected.size === 0;
    $("#oc-start-selected").textContent = selected.size ? `Start selected (${selected.size})` : "Start selected";
    if (!contacts.length) { $("#oc-body").innerHTML = `<p class="team-empty">No contacts yet. Import an Excel or CSV file with an Email column to begin.</p>`; return; }
    if (!rows.length) { $("#oc-body").innerHTML = `<p class="team-empty">No contacts match.</p>`; return; }
    const startable = rows.filter(c => c.can_start && c.complete);
    $("#oc-body").innerHTML = `<table class="team-table"><thead><tr>
      <th><input type="checkbox" id="oc-all" aria-label="Select all startable" ${startable.length && startable.every(c => selected.has(c.id)) ? "checked" : ""} ${startable.length ? "" : "disabled"}></th>
      <th>Contact</th><th>Company · Role</th><th>Status</th><th>Sent</th>${admin ? "<th>Owner</th>" : ""}<th></th></tr></thead><tbody>${rows.map(rowHtml).join("")}</tbody></table>`;
  }

  function rowHtml(c) {
    const esc = ui.esc, admin = me.role === "admin";
    const guess = field => (c.guessed || []).includes(field) ? ` <span class="guess" title="Guessed from the email address. Check it before searching.">guessed</span>` : "";
    const missing = text => text ? esc(text) : `<span class="missing">missing</span>`;
    const canChange = c.state !== "sent" || admin;
    const actions = [];
    if (c.can_start) actions.push(`<button class="link-button" data-act="start" data-id="${c.id}">Start</button>`);
    if (c.run_id && c.state !== "sent") actions.push(`<button class="link-button" data-act="open" data-id="${c.id}">Open</button>`);
    if (c.can_send) actions.push(`<button class="link-button" data-act="send" data-id="${c.id}">Send</button>`);
    if (c.message) actions.push(`<button class="link-button" data-act="view" data-id="${c.id}">${c.state === "sent" ? "Message" : "Draft"}</button>`);
    if (c.state === "new") actions.push(`<button class="link-button" data-act="skip" data-id="${c.id}">Skip</button>`);
    if (c.state === "skipped") actions.push(`<button class="link-button" data-act="restore" data-id="${c.id}">Restore</button>`);
    if (canChange) actions.push(`<button class="link-button" data-act="edit" data-id="${c.id}">Edit</button><button class="link-button danger" data-act="delete" data-id="${c.id}">Delete</button>`);
    return `<tr>
      <td>${c.can_start && c.complete ? `<input type="checkbox" data-sel="${c.id}" aria-label="Select ${esc(c.email)}" ${selected.has(c.id) ? "checked" : ""}>` : ""}</td>
      <td><strong>${c.name ? esc(c.name) : `<span class="missing">no name</span>`}</strong>${guess("name")}<small>${esc(c.email)}</small>${c.note ? `<small class="edited">${esc(c.note)}</small>` : ""}</td>
      <td>${missing(c.company)}${guess("company")}<small>${missing(c.role)}</small></td>
      <td><span class="kind ${KIND_CLASS[c.state]}">${esc(c.label)}</span>${c.send_error ? `<small class="send-error">${esc(c.send_error)}</small>` : ""}</td>
      <td class="nowrap">${c.sent_at ? `${esc(ui.when(c.sent_at))}<small>by ${esc(c.sent_by_name || "")}</small>` : "—"}</td>
      ${admin ? `<td>${esc(c.owner_name || "")}</td>` : ""}
      <td class="row-actions">${actions.join("")}</td></tr>`;
  }

  function selectionChange(event) {
    if (event.target.id === "oc-all") {
      const ids = visibleContacts().filter(c => c.can_start && c.complete).map(c => c.id);
      ids.forEach(id => event.target.checked ? selected.add(id) : selected.delete(id));
      render();
    } else if (event.target.dataset.sel) {
      event.target.checked ? selected.add(event.target.dataset.sel) : selected.delete(event.target.dataset.sel);
      render();
    }
  }

  /* ---------- actions ---------- */
  function rowClick(event) {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const c = contacts.find(x => x.id === button.dataset.id);
    if (!c) return;
    const act = {start: startForm, open: openRun, send: sendPreview, view: viewMessage, edit: editForm,
      skip: () => patch(c, {skip: true}), restore: () => patch(c, {skip: false}), delete: remove}[button.dataset.act];
    Promise.resolve(act(c)).catch(ui.showError);
  }

  async function patch(c, changes) {
    await ui.post(`/api/outreach/contacts/${c.id}`, changes);
    await load();
  }

  async function remove(c) {
    const extra = c.state === "sent" ? " This removes the record of the sent email." : "";
    if (!confirm(`Delete ${c.name || c.email} from the tracker?${extra}`)) return;
    await ui.call(`/api/outreach/contacts/${c.id}`, {method: "DELETE"});
    await load();
  }

  function openRun(c) {
    $("#outreach-dialog").close();
    location.hash = c.run_id;
  }

  function fields(c, withEmail) {
    const esc = ui.esc;
    return `${withEmail ? `<label class="field">Email<input name="email" type="email" required maxlength="254" value="${esc(c.email)}"></label>` : `<p class="team-note">Email: <strong>${esc(c.email)}</strong></p>`}
      <label class="field">Full name<input name="name" required maxlength="160" value="${esc(c.name)}"></label>
      <label class="field">Company<input name="company" required maxlength="160" value="${esc(c.company)}"></label>
      <label class="field">Role<input name="role" required maxlength="200" value="${esc(c.role)}" placeholder="Head of Talent Acquisition"></label>
      <label class="field">LinkedIn URL <span class="optional">Optional. Helps tell people with the same name apart</span><input name="profile_url" type="url" maxlength="2000" value="${esc(c.profile_url || "")}"></label>`;
  }

  function startForm(c) {
    ui.formDialog({
      title: "START SEARCH",
      submit: "Start search",
      body: `<p class="team-note">We check who this is first, then you confirm before any research runs.${(c.guessed || []).length ? " Some details were guessed from the email address, so please check them." : ""}</p>${fields(c, false)}`,
      onSubmit: async form => {
        const f = form.elements;
        const view = await ui.post(`/api/outreach/contacts/${c.id}/start`, {name: f.name.value, company: f.company.value, role: f.role.value, profile_url: f.profile_url.value});
        $("#outreach-dialog").close();
        location.hash = view.run_id;
      },
    });
  }

  function editForm(c) {
    const locked = c.state === "sent";
    ui.formDialog({
      title: "EDIT CONTACT",
      submit: "Save changes",
      body: `${locked ? `<p class="team-note">This email was already sent, so only the note can be changed.</p>` : fields(c, true)}
        <label class="field">Note<textarea name="note" rows="3" maxlength="2000">${ui.esc(c.note || "")}</textarea></label>`,
      onSubmit: async form => {
        const f = form.elements;
        const changes = {note: f.note.value};
        if (!locked) Object.assign(changes, {email: f.email.value, name: f.name.value, company: f.company.value, role: f.role.value, profile_url: f.profile_url.value});
        await ui.post(`/api/outreach/contacts/${c.id}`, changes);
        await load();
      },
    });
  }

  function viewMessage(c) {
    const m = c.message, esc = ui.esc;
    ui.formDialog({
      title: c.state === "sent" ? "SENT MESSAGE" : "DRAFT (NOT SENT)",
      submit: "Close",
      cancel: null,
      body: `<p class="team-note">To: <strong>${esc(c.email)}</strong>${c.sent_at ? `<br>Sent ${esc(ui.when(c.sent_at))} by ${esc(c.sent_by_name || "")}` : ""}</p>
        <div class="message-box"><strong>${esc(m.subject)}</strong>\n\n${esc(m.body)}</div>`,
      onSubmit: async () => {},
    });
  }

  function sendPreview(c) {
    const m = c.message, esc = ui.esc;
    ui.formDialog({
      title: "SEND EMAIL",
      submit: "Send email now",
      body: `<p class="team-note">To: <strong>${esc(c.email)}</strong><br>From: <strong>${esc(smtp.from_name)} &lt;${esc(smtp.from)}&gt;</strong></p>
        <div class="message-box"><strong>${esc(m.subject)}</strong>\n\n${esc(m.body)}</div>
        <p class="team-note warn-text">This sends a real email and cannot be undone. Edit the draft on the run page first if anything needs changing.</p>`,
      onSubmit: async () => {
        await ui.post(`/api/outreach/contacts/${c.id}/send`, {});
        ui.notice(`Email sent to ${c.email}.`);
        await load();
      },
    }).addEventListener("close", () => load().catch(() => {}));
  }

  async function startSelected() {
    const ids = [...selected].slice(0, 20);
    const result = await ui.post("/api/outreach/start-batch", {ids});
    selected = new Set();
    ui.notice(`Started ${result.started} search${result.started === 1 ? "" : "es"}${result.skipped.length ? `, skipped ${result.skipped.length}` : ""}. Open each one to confirm the identity.`);
    await load();
  }

  /* ---------- import / export ---------- */
  async function importFile(file) {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) throw new Error("Choose a file under 5 MB.");
    const response = await fetch("/api/outreach/import?filename=" + encodeURIComponent(file.name), {
      method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file});
    const body = await response.json().catch(() => ({}));
    if (response.status === 401) window.Team.onUnauthorized();
    if (!response.ok) throw new Error(body.error || "Import failed");
    showImport(body, file.name);
    await load();
  }

  function showImport(r, name) {
    const esc = ui.esc;
    const list = (title, items) => items.length ? `<details><summary>${title}</summary><ul>${items.map(i => `<li>${i}</li>`).join("")}</ul></details>` : "";
    $("#oc-import").innerHTML = `<div class="import-box"><strong>Added ${r.added} of ${r.rows} rows from ${esc(name)}.</strong>
      ${r.needs_details ? ` ${r.needs_details} ${r.needs_details === 1 ? "needs" : "need"} a name, company or role. Use Start or Edit to complete ${r.needs_details === 1 ? "it" : "them"}.` : ""}
      ${r.already_tracked_count ? ` ${r.already_tracked_count} already in the tracker.` : ""}${r.repeated_in_file ? ` ${r.repeated_in_file} repeated in the file.` : ""}${r.invalid_count ? ` ${r.invalid_count} ${r.invalid_count === 1 ? "row" : "rows"} had no valid email.` : ""}
      ${list("Already in the tracker", r.already_tracked.map(d => esc(d.email) + (d.yours ? " (yours)" : d.owner ? ` (${esc(d.owner)})` : " (another member)")))}
      ${list("Rows skipped", r.invalid.map(i => `Row ${i.row}: ${esc(i.reason)}${i.value ? ` “${esc(i.value)}”` : ""}`))}</div>`;
  }

  function download() {
    const link = document.createElement("a");
    link.href = `/api/outreach/export.xlsx?tz=${-new Date().getTimezoneOffset()}`;
    link.download = "outreach-tracker.xlsx";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }
})();
