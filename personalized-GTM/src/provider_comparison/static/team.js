"use strict";
/* Team login, search log and member management. Loaded before app.js; app.js calls Team.ready()
   at startup and Team.onUnauthorized() whenever the API answers 401. */
(function () {
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const when = v => { const d = new Date(v); return isNaN(d) ? "" : d.toLocaleString(undefined, {day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit"}); };
  const initials = name => String(name || "?").split(/\s+/).filter(Boolean).map(p => p[0]).slice(0, 2).join("").toUpperCase();
  const KIND = {identify: "Identity check", research: "Research"};
  let me = null, loginShown = false, entries = [], logTimer = null;

  async function call(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = response.status === 204 ? null : await response.json(); } catch { /* empty body */ }
    if (!response.ok) {
      const error = new Error((body && body.error) || "Request failed");
      error.status = response.status;
      throw error;
    }
    return body;
  }
  const post = (path, data) => call(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});

  /* ---------- sign in ---------- */
  function showLogin(message) {
    const existing = $(".auth-screen .auth-error");
    if (loginShown) { if (existing && message) existing.textContent = message; return; }
    loginShown = true;
    document.querySelectorAll("dialog[open]").forEach(d => d.close());
    const screen = document.createElement("div");
    screen.className = "auth-screen";
    screen.innerHTML = `<form class="auth-card" aria-labelledby="auth-title">
      <div class="brand-icon">PR</div>
      <h1 id="auth-title">Sign in</h1>
      <p class="muted">Personalized Reachout · team workspace</p>
      <div class="auth-error" role="alert">${esc(message || "")}</div>
      <label class="field">Email<input name="username" autocomplete="username" required maxlength="254" autocapitalize="off" spellcheck="false"></label>
      <label class="field">Password<input name="password" type="password" autocomplete="current-password" required maxlength="200"></label>
      <button class="primary full" type="submit">Sign in</button>
      <p class="form-foot">Need an account? Ask your admin.</p>
    </form>`;
    document.body.appendChild(screen);
    const form = screen.querySelector("form");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        await post("/api/login", {username: form.elements.username.value, password: form.elements.password.value});
        location.reload();
      } catch (error) {
        screen.querySelector(".auth-error").textContent = error.message;
        form.elements.password.value = "";
        form.elements.password.focus();
        button.disabled = false;
      }
    });
    form.elements.username.focus();
  }

  /* ---------- first-run: create the admin account ---------- */
  function showSetup() {
    loginShown = true;
    const screen = document.createElement("div");
    screen.className = "auth-screen";
    screen.innerHTML = `<form class="auth-card" aria-labelledby="auth-title">
      <div class="brand-icon">PR</div>
      <h1 id="auth-title">Create admin account</h1>
      <p class="muted">First-time setup. This account can add team members and edit the search log. This screen closes once it is created.</p>
      <div class="auth-error" role="alert"></div>
      <label class="field">Your name<input name="name" autocomplete="name" required maxlength="80"></label>
      <label class="field">Email (your login ID)<input name="username" type="email" autocomplete="username" required maxlength="254" autocapitalize="off" spellcheck="false" placeholder="you@yourcompany.com"></label>
      <label class="field">Password<input name="password" type="password" autocomplete="new-password" required minlength="10" maxlength="200"></label>
      <label class="field">Confirm password<input name="confirm" type="password" autocomplete="new-password" required minlength="10" maxlength="200"></label>
      <button class="primary full" type="submit">Create admin account</button>
      <p class="form-foot">At least 10 characters. You can add members afterwards under Manage team.</p>
    </form>`;
    document.body.appendChild(screen);
    const form = screen.querySelector("form"), err = screen.querySelector(".auth-error");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const f = form.elements, button = form.querySelector("button");
      if (f.password.value !== f.confirm.value) { err.textContent = "The two passwords do not match."; return; }
      button.disabled = true;
      try {
        await post("/api/setup", {name: f.name.value, username: f.username.value, password: f.password.value});
        location.reload();
      } catch (error) {
        err.textContent = error.message;
        button.disabled = false;
        if (error.status === 409) setTimeout(() => location.reload(), 1500);  // someone else finished setup
      }
    });
    form.elements.name.focus();
  }

  /* ---------- dialogs ---------- */
  function drawer(id, wide) {
    let dialog = document.getElementById(id);
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = id;
      dialog.className = "team-dialog" + (wide ? " wide" : "");
      document.body.appendChild(dialog);
      dialog.addEventListener("click", event => { if (event.target.closest("[data-close]")) dialog.close(); });
    }
    return dialog;
  }

  function formDialog({title, body, submit, onSubmit, cancel = "Cancel"}) {
    const dialog = document.createElement("dialog");
    dialog.className = "team-dialog form";
    dialog.innerHTML = `<div class="drawer-head"><span class="eyebrow">${esc(title)}</span><button type="button" class="icon-button" data-close aria-label="Close">×</button></div>
      <form>${body}<div class="inline-error" role="alert"></div>
      <div class="form-actions"><button class="primary" type="submit">${esc(submit)}</button>${cancel ? `<button class="secondary" type="button" data-close>${esc(cancel)}</button>` : ""}</div></form>`;
    document.body.appendChild(dialog);
    dialog.addEventListener("click", event => { if (event.target.closest("[data-close]")) dialog.close(); });
    dialog.addEventListener("close", () => dialog.remove());
    dialog.querySelector("form").addEventListener("submit", async event => {
      event.preventDefault();
      const error = dialog.querySelector(".inline-error");
      error.textContent = "";
      try { await onSubmit(event.target); dialog.close(); } catch (e) { error.textContent = e.message; }
    });
    dialog.showModal();
    return dialog;
  }

  /* ---------- search log ---------- */
  async function openLog() {
    const dialog = drawer("team-log", true);
    const admin = me.role === "admin";
    dialog.innerHTML = `<div class="drawer-head"><span class="eyebrow">TEAM SEARCH LOG</span><button class="icon-button" data-close aria-label="Close">×</button></div>
      <p class="team-note">${admin ? "Every search by every member. As an admin you can edit or delete entries." : "Every search by every member. Read-only: only an admin can edit this list."}</p>
      <div class="team-filters">
        <input type="search" id="log-q" placeholder="Search employee, prospect, company, note" aria-label="Search the log">
        <select id="log-user" aria-label="Filter by employee"></select>
        <select id="log-kind" aria-label="Filter by type"><option value="">All types</option><option value="identify">Identity check</option><option value="research">Research</option></select>
        <button class="secondary" id="log-refresh" type="button">Refresh</button>
      </div>
      <div id="log-count" class="team-count"></div><div id="log-body" class="team-table-wrap"></div>`;
    dialog.querySelector("#log-q").addEventListener("input", renderLog);
    dialog.querySelector("#log-user").addEventListener("change", renderLog);
    dialog.querySelector("#log-kind").addEventListener("change", renderLog);
    dialog.querySelector("#log-refresh").addEventListener("click", () => loadLog().catch(showError));
    dialog.querySelector("#log-body").addEventListener("click", logClick);
    dialog.addEventListener("close", () => clearInterval(logTimer), {once: true});
    dialog.showModal();
    await loadLog().catch(showError);
    clearInterval(logTimer);
    logTimer = setInterval(() => { if (dialog.open && !document.querySelector("dialog.form[open]")) loadLog().catch(() => {}); }, 15000);
  }

  async function loadLog() {
    entries = (await call("/api/team/activity?limit=1000")).entries;
    const select = $("#log-user");
    const chosen = select.value;
    const people = [...new Map(entries.map(e => [e.username, e.user_name])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
    select.innerHTML = `<option value="">All employees</option>` + people.map(([u, n]) => `<option value="${esc(u)}">${esc(n)} (${esc(u)})</option>`).join("");
    select.value = people.some(([u]) => u === chosen) ? chosen : "";
    renderLog();
  }

  function renderLog() {
    const q = $("#log-q").value.trim().toLowerCase(), user = $("#log-user").value, kind = $("#log-kind").value;
    const rows = entries.filter(e => (!user || e.username === user) && (!kind || e.kind === kind) &&
      (!q || [e.user_name, e.username, e.prospect.name, e.prospect.company, e.prospect.role, e.note].join(" ").toLowerCase().includes(q)));
    $("#log-count").textContent = `${rows.length} of ${entries.length} searches`;
    const admin = me.role === "admin";
    $("#log-body").innerHTML = rows.length ? `<table class="team-table"><thead><tr><th>When</th><th>Employee</th><th>Search</th><th>Prospect</th><th>Note</th><th></th></tr></thead><tbody>${rows.map(e => {
      const canOpen = admin || e.user_id === me.id;
      const edited = e.edited_by ? `<small class="edited" title="${esc(e.edit_log.map(l => `${l.by}, ${when(l.at)}`).join("\n"))}">Edited by ${esc(e.edited_by)}</small>` : "";
      return `<tr><td class="nowrap">${esc(when(e.at))}</td>
        <td><strong>${esc(e.user_name)}</strong><small>${esc(e.username)}</small></td>
        <td><span class="kind kind-${esc(e.kind)}">${esc(KIND[e.kind] || e.kind)}</span></td>
        <td><strong>${esc(e.prospect.name)}</strong><small>${esc(e.prospect.company)} · ${esc(e.prospect.role)}</small></td>
        <td class="note">${esc(e.note)}${edited}</td>
        <td class="row-actions">${canOpen ? `<button class="link-button" data-open="${esc(e.run_id)}">Open</button>` : ""}${admin ? `<button class="link-button" data-edit="${esc(e.id)}">Edit</button><button class="link-button danger" data-del="${esc(e.id)}">Delete</button>` : ""}</td></tr>`;
    }).join("")}</tbody></table>` : `<p class="team-empty">No searches match.</p>`;
  }

  function logClick(event) {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.open) { $("#team-log").close(); location.hash = button.dataset.open; return; }
    const entry = entries.find(e => e.id === (button.dataset.edit || button.dataset.del));
    if (!entry) return;
    if (button.dataset.del) {
      if (!confirm(`Delete this search by ${entry.user_name} (${entry.prospect.name}) from the log? This cannot be undone.`)) return;
      call(`/api/team/activity/${entry.id}`, {method: "DELETE"}).then(loadLog).catch(showError);
      return;
    }
    formDialog({
      title: "EDIT LOG ENTRY",
      submit: "Save changes",
      body: `<p class="team-note">Searched by ${esc(entry.user_name)} on ${esc(when(entry.at))}. Edits are recorded with your name.</p>
        <label class="field">Prospect name<input name="name" required maxlength="160" value="${esc(entry.prospect.name)}"></label>
        <label class="field">Company<input name="company" required maxlength="160" value="${esc(entry.prospect.company)}"></label>
        <label class="field">Role<input name="role" required maxlength="200" value="${esc(entry.prospect.role)}"></label>
        <label class="field">Note<textarea name="note" rows="4" maxlength="2000">${esc(entry.note)}</textarea></label>`,
      onSubmit: async form => {
        const f = form.elements;
        await post(`/api/team/activity/${entry.id}`, {name: f.name.value, company: f.company.value, role: f.role.value, note: f.note.value});
        await loadLog();
      },
    });
  }

  /* ---------- member management (admin) ---------- */
  async function openMembers() {
    const dialog = drawer("team-members-dialog", true);
    dialog.innerHTML = `<div class="drawer-head"><span class="eyebrow">MANAGE TEAM</span><button class="icon-button" data-close aria-label="Close">×</button></div>
      <p class="team-note">Each member signs in with their own email address and the password you set here; they can change it any time from the Password link. Deactivating an account signs the member out and keeps their search history.</p>
      <div class="team-filters"><button class="primary" id="add-member" type="button">Add member</button></div>
      <div id="members-body" class="team-table-wrap"></div>`;
    dialog.querySelector("#add-member").addEventListener("click", () => memberForm(null));
    dialog.querySelector("#members-body").addEventListener("click", event => {
      const button = event.target.closest("[data-member]");
      if (button) memberForm(JSON.parse(button.dataset.member));
    });
    dialog.showModal();
    await loadMembers().catch(showError);
  }

  async function loadMembers() {
    const {users} = await call("/api/team/users");
    $("#members-body").innerHTML = `<table class="team-table"><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead><tbody>${users.map(u =>
      `<tr><td><strong>${esc(u.name)}</strong></td><td>${esc(u.username)}</td><td>${u.role === "admin" ? "Admin" : "Member"}</td>
       <td><span class="kind ${u.active ? "kind-research" : "kind-off"}">${u.active ? "Active" : "Disabled"}</span></td>
       <td class="nowrap">${u.last_login ? esc(when(u.last_login)) : "Never"}</td>
       <td class="row-actions"><button class="link-button" data-member="${esc(JSON.stringify(u))}">Edit</button></td></tr>`).join("")}</tbody></table>`;
  }

  function memberForm(user) {
    const isNew = !user;
    formDialog({
      title: isNew ? "ADD MEMBER" : "EDIT MEMBER",
      submit: isNew ? "Create account" : "Save changes",
      body: `${isNew ? `<label class="field">Email (login ID)<input name="username" type="email" required maxlength="254" autocapitalize="off" spellcheck="false" autocomplete="off" placeholder="taylor@yourcompany.com"></label>` : `<p class="team-note">Login ID: <strong>${esc(user.username)}</strong></p>`}
        <label class="field">Full name<input name="name" required maxlength="80" value="${esc(user ? user.name : "")}"></label>
        <label class="field">Role<select name="role"><option value="member"${user && user.role === "member" ? " selected" : ""}>Member</option><option value="admin"${user && user.role === "admin" ? " selected" : ""}>Admin</option></select></label>
        ${isNew ? "" : `<label class="check"><input type="checkbox" name="active"${user.active ? " checked" : ""}> Account active</label>`}
        <label class="field">${isNew ? "Password" : "New password"} <span class="optional">${isNew ? "At least 10 characters" : "Leave blank to keep the current one"}</span><input name="password" type="password" ${isNew ? "required" : ""} minlength="10" maxlength="200" autocomplete="new-password"></label>`,
      onSubmit: async form => {
        const f = form.elements;
        if (isNew) await post("/api/team/users", {username: f.username.value, name: f.name.value, role: f.role.value, password: f.password.value});
        else await post(`/api/team/users/${user.id}`, {name: f.name.value, role: f.role.value, active: f.active.checked, password: f.password.value});
        await loadMembers();
      },
    });
  }

  /* ---------- own password ---------- */
  function passwordForm() {
    formDialog({
      title: "CHANGE PASSWORD",
      submit: "Change password",
      body: `<label class="field">Current password<input name="current" type="password" required autocomplete="current-password"></label>
        <label class="field">New password <span class="optional">At least 10 characters</span><input name="next" type="password" required minlength="10" maxlength="200" autocomplete="new-password"></label>
        <label class="field">Repeat new password<input name="again" type="password" required autocomplete="new-password"></label>`,
      onSubmit: async form => {
        const f = form.elements;
        if (f.next.value !== f.again.value) throw new Error("The new passwords do not match.");
        await post("/api/me/password", {current: f.current.value, new: f.next.value});
        notice("Password changed.");
      },
    });
  }

  function notice(message) {
    const el = $("#notice");
    if (!el) return;
    el.textContent = message;
    el.classList.add("visible");
    setTimeout(() => el.classList.remove("visible"), 4000);
  }
  function showError(error) { if (error.status !== 401) notice(error.message); }

  /* ---------- sidebar ---------- */
  function mount() {
    $(".sidebar .brand").insertAdjacentHTML("afterend", `<div class="user-chip">
      <span class="user-avatar" aria-hidden="true">${esc(initials(me.name))}</span>
      <span class="user-meta"><strong>${esc(me.name)}</strong><small>${me.role === "admin" ? "Admin" : "Member"} · ${esc(me.username)}</small></span>
      <span class="chip-actions"><button class="chip-button" id="my-password" type="button">Password</button><button class="chip-button" id="sign-out" type="button">Sign out</button></span></div>`);
    $("#outreach-principles").insertAdjacentHTML("afterend",
      `<button class="brief-button team-link" id="team-log-open" type="button">Team search log <span>↗</span></button>` +
      (me.role === "admin" ? `<button class="brief-button team-link" id="team-members-open" type="button">Manage team <span>↗</span></button>` : ""));
    $("#team-log-open").addEventListener("click", () => openLog().catch(showError));
    if ($("#team-members-open")) $("#team-members-open").addEventListener("click", () => openMembers().catch(showError));
    $("#my-password").addEventListener("click", passwordForm);
    $("#sign-out").addEventListener("click", async () => { try { await post("/api/logout", {}); } finally { location.reload(); } });
  }

  async function ready() {
    try {
      me = (await call("/api/me")).user;
    } catch (error) {
      if (error.status === 401) {
        let setup = false;
        try { setup = (await call("/api/setup")).needs_setup === true; } catch { /* show sign-in */ }
        if (setup) showSetup(); else showLogin();
        await new Promise(() => {});  // page reloads after sign-in
      }
      throw error;
    }
    mount();
    document.dispatchEvent(new CustomEvent("team-ready", {detail: {user: me}}));
  }

  window.Team = {
    ready,
    onUnauthorized: () => showLogin("Your session ended. Please sign in again."),
    ui: {call, post, esc, when, drawer, formDialog, notice, showError},  // shared with outreach.js
  };
})();
