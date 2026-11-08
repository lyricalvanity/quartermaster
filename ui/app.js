/* Quartermaster UI. Talks to the host agent at /api (same origin: the agent itself, or nginx in the container). */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const fmtGB = (mib) => mib == null ? "—" : (mib / 1024).toFixed(1);
  const fmtK = (n) => n == null ? "—" : n >= 1048576 ? (n / 1048576).toFixed(0) + "M" : n >= 1024 ? Math.round(n / 1024) + "K" : String(n);
  const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s / 60) + "m" : (s / 3600).toFixed(1) + "h"; };

  let INV = null, STATE = null, lastPlanId = null, logServer = null;
  const confirmQueue = [];

  // ---- API ----
  async function api(path, body) {
    const opt = body ? { method: "POST", headers: { "Content-Type": "application/json", "X-Quartermaster": "1" }, body: JSON.stringify(body) }
                     : { headers: { "X-Quartermaster": "1" } };
    const r = await fetch(path, opt);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  function toast(msg, kind) {
    const t = el("div", "toast " + (kind || ""), msg);
    $("#toast").appendChild(t);
    setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3500);
  }
  async function action(op, target, args) {
    try {
      const r = await api("/api/action", { op, target, args: args || {}, by: "ui" });
      toast(`${op} ${target || ""} started`);
      return r;
    } catch (e) { toast(e.message, "bad"); }
  }
  // Theme copy carries {HOST}, {host}, {owner}, {OWNER} and {N} (the number in the card's name); the inventory fills
  // them in. Every start-up screen addresses the installation's owner (inventory "owner"); without one, each falls
  // back to whoever its canon addressed. No name is written here.
  const OWNER_DEFAULT = { hal: "Dave", matrix: "Neo", wopr: "Falken", codec: "Colonel", tron: "program", muthur: "Ripley",
    castlevania: "Belmont", lcars: "Picard", cyberpunk: "V", cortex: "Raven", concord: "Raven", precrime: "Anderton",
    ocp: "Murphy", magi: "Ikari", gits: "Kusanagi", aperture: "Chell", jurassic: "Nedry", t800: "Connor", bladerunner: "Deckard" };
  function owner(theme) { return (INV && INV.owner) || OWNER_DEFAULT[theme] || "operator"; }
  function fill(s, theme) {
    const host = (INV && INV.host) || "HOST", gpu = (INV && INV.gpu && INV.gpu.name) || "";
    const n = (gpu.match(/\d{3,5}/) || ["9000"])[0];
    return s.replace(/\{HOST\}/g, host.toUpperCase()).replace(/\{host\}/g, host).replace(/\{N\}/g, n)
      .replace(/\{OWNER\}/g, owner(theme).toUpperCase()).replace(/\{owner\}/g, owner(theme));
  }
  // A theme may redress a named dialog. The plain title moves into the body, so what the button does is always stated.
  const DIALOG_COPY = {
    castlevania: { killall: { title: "But enough talk…", yes: "Have at you!", no: "Not yet" } },
    lcars: { killall: { title: "Awaiting command", yes: "Make it so", no: "Belay that" } },
    cyberpunk: { killall: { title: "Flatline every engram?", yes: "Flatline 'em", no: "Nah, delta" } },
    cortex: { killall: { title: "Purge all units?", yes: "Purge", no: "Abort" } },
    concord: { killall: { title: "Purge all units?", yes: "Purge", no: "Abort" } },
    matrix: { killall: { title: "This is your last chance.", yes: "Red pill", no: "Blue pill" } },
    precrime: { killall: { title: "Everybody runs.", yes: "Run", no: "Stay" } },
    ocp: { killall: { title: "You have 20 seconds to comply.", yes: "Unload", no: "Stand down" } },
    magi: { killall: { title: "Motion before the MAGI", yes: "承認  Approve", no: "否決  Reject" } },
    hal: { killall: { title: "Just what do you think you're doing, {owner}?", yes: "Pull the modules", no: "Take a stress pill" } },
    gits: { killall: { title: "Sever every ghost line?", yes: "Sever", no: "Stay linked" } },
    wopr: { killall: { title: "Shall we play a game?", yes: "Global Thermonuclear War", no: "A nice game of chess" } },
    aperture: { killall: { title: "Please escort your models to the incinerator.", yes: "Incinerate", no: "I'm not a monster" } },
    jurassic: { killall: { title: "Hold on to your butts.", yes: "Shut it down", no: "Leave it running" } },
    t800: { killall: { title: "Possible response:", yes: "Fuck you, asshole", no: "Please come back later" } },
    codec: { killall: { title: "Snake? Snake?! SNAAAAKE!", yes: "Exit", no: "Continue" } },
    tron: { killall: { title: "Derezz every program?", yes: "Derezz", no: "Stay on the Grid" } },
    bladerunner: { killall: { title: "It's too bad they won't live. But then again, who does?", yes: "Retire them", no: "Let them live" } },
  };
  function confirm(title, text, key) {
    return new Promise((res) => {
      const theme = window.QM_THEME.get(), copy = key && (DIALOG_COPY[theme] || {})[key];
      $("#confirm-title").textContent = copy ? fill(copy.title, theme) : title;
      $("#confirm-text").textContent = copy ? `${title} ${text}` : text;
      $("#confirm-yes").textContent = copy ? copy.yes : "Yes";
      $("#confirm-no").textContent = copy ? copy.no : "No";
      $("#confirm-modal").hidden = false;
      const done = (v) => { $("#confirm-modal").hidden = true; yes.onclick = no.onclick = null; res(v); };
      const yes = $("#confirm-yes"), no = $("#confirm-no");
      yes.onclick = () => done(true); no.onclick = () => done(false);
    });
  }

  // ---- vitals trace (shown by themes that style .vitals; hidden in plain) ----
  const BEAT = "l20 0 l4 -3 l4 3 l6 0 l3 4 l4 -16 l4 22 l3 -10 l8 0 l4 0 ";   // 60 wide, returns to baseline
  const BLIP = "l70 0 l5 -4 l5 4 l20 0 ";                                      // 100 wide
  function vitalsEl() {
    const w = el("div", "vitals");
    w.appendChild(el("span", "v-k", "vitals"));
    w.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "svg"));
    w.lastChild.setAttribute("viewBox", "0 0 300 28"); w.lastChild.setAttribute("preserveAspectRatio", "none");
    w.appendChild(el("span", "v-s", ""));
    return w;
  }
  // mode: flat (down) | standby (up, nothing loaded) | alive (loaded) | active (working; secs = one sweep)
  function setVitals(w, mode, label, secs) {
    if (!w) return;
    w.lastChild.textContent = label;
    const key = `${mode}|${secs || ""}`;
    if (w.dataset.k === key) return;          // leave a running trace alone
    w.dataset.k = key; w.dataset.mode = mode;
    const d = mode === "flat" ? "M0 14 H300" : "M0 14 " + (mode === "standby" ? BLIP.repeat(3) : BEAT.repeat(5));
    const dur = secs || (mode === "standby" ? 6 : 3);
    w.children[1].innerHTML = `<path class="v-base" d="${d}"/>` +
      (mode === "flat" ? "" : `<path class="v-sweep" pathLength="300" d="${d}" style="animation-duration:${dur}s"/>`);
  }
  function serverVitals(w, s, i) {
    const live = i.live && i.live.state && i.live.state !== "idle" ? i.live : null;
    if (!i.up) return setVitals(w, "flat", "no signal");
    if (live) {
      const secs = live.tok_s ? Math.min(2.4, Math.max(0.6, Math.round(240 / live.tok_s) / 10)) : 1.4;
      return setVitals(w, "active", live.tok_s ? `${live.tok_s.toFixed(1)} t/s` : live.state, secs);
    }
    const loaded = s.driver === "ollama" ? !!(i.models && i.models.length) : !!i.loaded;
    return loaded ? setVitals(w, "alive", "nominal") : setVitals(w, "standby", "standby");
  }

  // ---- help bar (shown by themes that style #helpbar): one line about whatever is under the cursor ----
  function setHelp(t) { const h = $("#helpbar"); if (h) h.textContent = t || "Select a unit."; }
  function describe(s) {
    const i = (STATE && STATE.servers[s.id]) || {};
    const parts = [s.model + "."];
    if (s.footprint && s.footprint.vram_gib) parts.push(`${s.footprint.vram_gib} GB VRAM, ${s.footprint.ram_gib} GB RAM.`);
    if (s.driver === "ollama") return parts.join(" ");
    const others = STATE ? (STATE.holders || []).filter((h) => h !== s.id).map((h) => (INV.units.find((x) => x.id === h) || {}).name) : [];
    if (i.loaded) parts.push("Resident now.");
    else if (others.length) parts.push(`Making it resident unloads ${others.join(", ")}.`);
    else parts.push(i.up ? "Server up, nothing loaded." : "Server down; Make resident starts it.");
    return parts.join(" ");
  }

  // ---- cards ----
  const cards = {};
  function serverCard(s) {
    const c = el("div", "card");
    c.dataset.id = s.id; c.dataset.engine = s.engine;
    if (s.port) c.appendChild(el("div", "freq", (s.port / 100).toFixed(2)));      // the port as a frequency, for themes that show .freq
    const top = el("div", "c-top");
    const left = el("div");
    left.appendChild(el("div", "c-name", s.name));
    left.appendChild(el("div", "c-model", s.model));
    if (s.source) { const src = el("div", "c-source", s.source); src.title = "where the weights came from"; left.appendChild(src); }
    top.appendChild(left);
    const pill = el("span", "pill", "—"); top.appendChild(pill);
    c.appendChild(top);
    const meta = el("div", "c-meta");
    meta.appendChild(el("span", "tag", `:${s.port}`));
    if (s.quant) meta.appendChild(el("span", "tag", s.quant));
    if (s.context) meta.appendChild(el("span", "tag", fmtK(s.context) + " ctx"));
    if (s.vision) meta.appendChild(el("span", "tag vision", "vision"));
    if (s.footprint && s.footprint.vram_gib) meta.appendChild(el("span", "tag", `${s.footprint.vram_gib} GB VRAM · ${s.footprint.ram_gib} GB RAM`));
    c.appendChild(meta);
    const users = usedBy(s.port);
    if (users.length) { const ub = el("div", "c-used", `used by ${users.join(", ")}`); ub.title = "clients whose configuration points at this port"; c.appendChild(ub); }
    const live = el("div", "c-live"); c.appendChild(live);
    const vit = vitalsEl(); c.appendChild(vit);
    const oll = el("div", "oll-models"); c.appendChild(oll);
    const act = el("div", "c-actions");
    const refs = { pill, live, oll, act, vit };
    if (s.driver !== "ollama") {
      const mk = el("button", "btn primary", "Make resident");
      mk.onclick = () => action("make_resident", s.id);
      act.appendChild(mk); refs.mk = mk;
      const un = el("button", "btn small", "Unload"); un.onclick = () => action("unload", s.id); act.appendChild(un); refs.un = un;
      if (s.driver !== "llamacpp-router") {            // a router's model has no process of its own to start or stop: Unload ends it
        const st = el("button", "btn small", "Start"); st.onclick = () => action("start", s.id); act.appendChild(st); refs.st = st;
        const sp = el("button", "btn small danger", "Stop");
        sp.onclick = async () => { const u = usedBy(s.port); if (await confirm(`Stop ${s.name}?`, `This ends the server process on port ${s.port}. The model unloads with it.${u.length ? ` ${u.join(" and ")} point${u.length === 1 ? "s" : ""} at this port.` : ""}`)) action("stop", s.id, { confirm: true }); };
        act.appendChild(sp); refs.sp = sp;
      }
      if (s.parkable) { const pk = el("button", "btn small", "Park"); pk.onclick = () => action(c.classList.contains("parked") ? "unpark" : "park", s.id); act.appendChild(pk); refs.pk = pk; }
    } else {
      const un = el("button", "btn small", "Unload all"); un.onclick = () => action("unload", s.id); act.appendChild(un); refs.un = un;
    }
    act.appendChild(el("span", "spacer"));
    if (s.driver === "maya") {
      const sel = el("select");
      (s.context_choices || []).forEach((n) => { const o = el("option", null, fmtK(n)); o.value = n; sel.appendChild(o); });
      sel.title = "context size (reloads the engine)";
      sel.onchange = () => action("set_context", s.id, { value: Number(sel.value) });
      act.appendChild(sel); refs.sel = sel;
      const bud = el("input"); bud.size = 6; bud.title = "thinking budget (0 = off, 256..262144); Enter to set"; bud.placeholder = "budget";
      bud.onkeydown = (e) => { if (e.key === "Enter") action("set_budget", s.id, { value: Number(bud.value) }); };
      act.appendChild(bud); refs.bud = bud;
    }
    const lg = el("button", "btn small ghost", "log"); lg.onclick = () => openLog(s.id, s.name); act.appendChild(lg);
    c.appendChild(act);
    c.appendChild(el("div", "will", "will unload"));
    c.onmouseenter = () => { hoverEvict(s.id, true); setHelp(describe(s)); };
    c.onmouseleave = () => { hoverEvict(s.id, false); setHelp(""); };
    cards[s.id] = refs; refs.root = c;
    return c;
  }
  function hoverEvict(id, on) {
    if (!STATE) return;
    const s = INV.units.find((x) => x.id === id);
    if (!s || s.driver === "ollama") return;
    for (const [sid, info] of Object.entries(STATE.servers)) {
      if (sid === id) continue;
      const refs = cards[sid]; if (!refs) continue;
      refs.root.classList.toggle("will-unload", on && !!info.loaded);
    }
  }
  // ---- OpenWebUI: looked at from outside, moved by three fixed controls (see the agent) ----
  let owui = null;
  const owuiDriver = (o) => o.driver || (o.units ? "systemd" : o.compose ? "docker-compose" : "schtasks");
  function owuiCard(o) {
    const c = el("div", "card service");
    const oport = Number(new URL(o.url, location.href).port);
    if (oport) c.appendChild(el("div", "freq", (oport / 100).toFixed(2)));
    const top = el("div", "c-top");
    const left = el("div");
    left.appendChild(el("div", "c-name", o.name || "OpenWebUI"));
    const drv = owuiDriver(o);
    left.appendChild(el("div", "c-model", o.model || { systemd: "systemd driver", "docker-compose": "docker compose driver" }[drv] || "scheduled-task driver"));     // the inventory may describe how it runs
    top.appendChild(left);
    const pill = el("span", "pill", "—"); top.appendChild(pill);
    c.appendChild(top);
    const meta = el("div", "c-meta"); c.appendChild(meta);
    const live = el("div", "c-live"); c.appendChild(live);
    const vit = vitalsEl(); c.appendChild(vit);
    const note = el("div", "c-source"); c.appendChild(note);
    const act = el("div", "c-actions");
    const st = el("button", "btn small", "Start"); st.onclick = () => action("owui_start"); act.appendChild(st);
    const sp = el("button", "btn small danger", "Stop");
    sp.onclick = async () => { if (await confirm("Stop OpenWebUI?", o.stop_note || "This takes it down for everyone using it. Start, or a reboot, brings it back.")) action("owui_stop", null, { confirm: true }); };
    act.appendChild(sp);
    const up = el("button", "btn small", "Update");
    const updateNote = drv === "docker-compose"
      ? "Copies its data volume, pulls the image, recreates the container on it and waits for /health. A new version that does not come up is rolled back, image and data. A release with schema changes migrates the database one way; the copy is how you would go back later."
      : "Runs its update control, which backs up the data and restarts it on the new version as that control was written to. A release with schema changes migrates the database one way.";
    up.onclick = async () => { if (await confirm("Update OpenWebUI?", o.update_note || updateNote)) action("owui_update", null, { confirm: true }); };
    act.appendChild(up);
    act.appendChild(el("span", "spacer"));
    const open = el("a", "btn small ghost", "open"); open.href = o.open_url || o.url; open.target = "_blank"; open.rel = "noopener"; act.appendChild(open);
    if (o.ctl_log) { const lg = el("button", "btn small ghost", "log"); lg.onclick = () => openLog("openwebui", "OpenWebUI · control log"); act.appendChild(lg); }
    c.appendChild(act);
    c.onmouseenter = () => setHelp(o.help || (drv === "docker-compose"
      ? "OpenWebUI in Docker Compose: the agent runs docker compose on the project and service the inventory names; the tile reads only health and version."
      : `OpenWebUI, driven by ${drv === "systemd" ? "units" : "scheduled tasks"} the agent starts by name and passes nothing; the tile reads only health and version.`));
    c.onmouseleave = () => setHelp("");
    owui = { root: c, pill, meta, live, note, st, sp, up, vit };
    return c;
  }
  function renderOwui(st) {
    const i = st.openwebui;
    if (!owui || !i) return;
    const plan = st.plan && st.plan.status === "running" ? st.plan : null;
    const mine = plan && String(plan.op).startsWith("owui_");
    const state = mine ? { owui_start: "starting", owui_stop: "stopping", owui_update: "updating" }[plan.op] : i.up ? "up" : "down";
    owui.pill.textContent = state;
    owui.pill.className = "pill " + (state === "up" ? "ready" : state === "down" ? "down" : "loading");
    owui.root.classList.toggle("down", !i.up);
    if (mine) setVitals(owui.vit, "active", state, 1.2); else if (i.up) setVitals(owui.vit, "alive", "nominal"); else setVitals(owui.vit, "flat", "no signal");
    owui.meta.innerHTML = "";
    const tag = (t, cls) => owui.meta.appendChild(el("span", "tag" + (cls ? " " + cls : ""), t));
    tag(":" + (Number(new URL(INV.openwebui.url, location.href).port) || 80));
    if (i.version) tag("v" + i.version);
    if (i.bridge_expected) tag(`bridge ${i.bridge_bound}/${i.bridge_expected}`, i.bridge_bound < i.bridge_expected ? "bad" : "");
    const lv = [];
    if (i.keepalive_last) lv.push(`started ${i.keepalive_last}`);
    if (i.keepalive_exited) lv.push("keepalive is restarting it");
    if (i.session_warning) lv.push("login path degraded until a reboot");
    if (i.container && i.container !== "running") lv.push(`container ${i.container}`);
    owui.live.textContent = lv.join(" · ");
    const t = i.tasks || {};
    const missing = ["start", "stop", "update"].filter((v) => (t[v] || "missing") === "missing");
    const gated = ["start", "stop", "update"].filter((v) => t[v] === "gated");
    // the compose driver's controls are all there or none are, and its hint says why (no docker, no compose file)
    owui.note.textContent = missing.length ? (owuiDriver(INV.openwebui) === "docker-compose" ? i.install_hint || "docker is not answering"
      : `controls not installed (${missing.join(", ")}): ${i.install_hint || "see the inventory's openwebui.tasks"}`)
      : gated.length ? `Windows asks for approval on: ${gated.join(", ")}` : "";
    const busy = !!plan;
    owui.st.disabled = busy || i.up || t.start === "missing" || !t.start;
    owui.sp.disabled = busy || !i.up || t.stop === "missing" || !t.stop;
    owui.up.disabled = busy || !i.up || t.update === "missing" || !t.update;
  }

  // ---- ComfyUI: the owner's process that shares the card. The agent starts and stops it, and asks it to drop its models. ----
  let comfy = null;
  function comfyCard(o) {
    const c = el("div", "card service");
    if (o.port) c.appendChild(el("div", "freq", (o.port / 100).toFixed(2)));
    const top = el("div", "c-top");
    const left = el("div");
    left.appendChild(el("div", "c-name", o.name || "ComfyUI"));
    left.appendChild(el("div", "c-model", "image and video generation · shares the card"));
    top.appendChild(left);
    const pill = el("span", "pill", "—"); top.appendChild(pill);
    c.appendChild(top);
    const meta = el("div", "c-meta"); c.appendChild(meta);
    const live = el("div", "c-live"); c.appendChild(live);
    const queue = el("div", "c-live art-queue"); c.appendChild(queue);
    const vit = vitalsEl(); c.appendChild(vit);
    const act = el("div", "c-actions");
    const st = el("button", "btn small", "Start"); st.onclick = () => action("comfy_start"); act.appendChild(st);
    const sp = el("button", "btn small danger", "Stop");
    sp.onclick = async () => { if (await confirm("Stop ComfyUI?", "This ends the ComfyUI process and any job it is running. Its queue is lost; your workflows are not.")) action("comfy_stop", null, { confirm: true }); };
    act.appendChild(sp);
    const fr = el("button", "btn small", "Free memory"); fr.title = "ask ComfyUI to unload its models; it stays up";
    fr.onclick = () => action("comfy_free"); act.appendChild(fr);
    const rq = el("button", "btn small", "Run art queue"); rq.title = "park the resident model, run every queued image batch, put the model back";
    rq.onclick = async () => {
      const a = (STATE && STATE.art) || { batches: 0, images: 0 };
      const res = STATE && STATE.resident && INV.units.find(s => s.id === STATE.resident);
      const who = res ? `${res.name} is unloaded first and loaded again afterwards` : "Nothing is resident; the card stays empty afterwards";
      if (await confirm("Run the art queue?", `${a.images} image(s) in ${a.batches} batch(es), about ${Math.max(1, Math.round(a.images * 10 / 60))} min in ComfyUI. ${who}.`)) action("art_batch", null, { then: "prior" });
    };
    act.appendChild(rq);
    const up = el("button", "btn small", "Update"); up.title = "update ComfyUI to its newest release; torch stays pinned; rolls back if it does not boot";
    up.onclick = async () => {
      if (await confirm("Update ComfyUI?", "Checks GitHub for the newest release, stops ComfyUI, fast-forwards its code and installs its new requirements with the torch stack pinned, then boot-tests it on the card. If it does not come up, it rolls back by itself. Custom nodes are not updated. qm comfy rollback undoes it later."))
        action("comfy_update", null, { confirm: true });
    };
    act.appendChild(up);
    act.appendChild(el("span", "spacer"));
    const open = el("a", "btn small ghost", "open"); open.href = o.open_url || o.url; open.target = "_blank"; open.rel = "noopener"; act.appendChild(open);
    const lg = el("button", "btn small ghost", "log"); lg.onclick = () => openLog("comfyui", "ComfyUI · server log"); act.appendChild(lg);
    c.appendChild(act);
    c.onmouseenter = () => setHelp("ComfyUI. It takes VRAM only while it has models loaded; Free memory makes it let go without stopping it. Make resident and Clear the card ask it to let go too.");
    c.onmouseleave = () => setHelp("");
    comfy = { root: c, pill, meta, live, queue, st, sp, fr, rq, up, open, vit, port: o.port };
    return c;
  }
  function renderComfy(st) {
    const i = st.comfyui;
    if (!comfy || !i) return;
    const plan = st.plan && st.plan.status === "running" ? st.plan : null;
    const mine = plan && (String(plan.op).startsWith("comfy_") || plan.op === "art_batch");
    const state = mine ? { comfy_start: "starting", comfy_stop: "stopping", comfy_free: "unloading", art_batch: "busy",
                           comfy_update: "updating", comfy_rollback: "rolling back" }[plan.op]
      : !i.up ? "down" : i.queue > 0 ? "busy" : i.holding ? "ready" : "idle";
    comfy.pill.textContent = state === "ready" ? "loaded" : state === "busy" ? "working" : state;
    comfy.pill.className = "pill " + state;
    comfy.root.classList.toggle("down", !i.up);
    if (mine) setVitals(comfy.vit, "active", state, 1.2);
    else if (!i.up) setVitals(comfy.vit, "flat", "no signal");
    else if (i.queue > 0) setVitals(comfy.vit, "active", `${i.queue} in queue`, 1.6);
    else if (i.holding) setVitals(comfy.vit, "alive", "nominal");
    else setVitals(comfy.vit, "standby", "standby");
    comfy.meta.innerHTML = "";
    const tag = (t, cls) => comfy.meta.appendChild(el("span", "tag" + (cls ? " " + cls : ""), t));
    tag(":" + comfy.port);
    if (i.version) tag("v" + i.version);
    if (i.up && i.device && i.device !== "cuda") tag(i.device, "bad");
    if (i.exposed && i.exposed.length) tag("not loopback only", "bad");
    const lv = [];
    if (i.up) {
      lv.push(!i.on_card ? "not on the card" : i.vram_held_mib == null ? "VRAM: no figure" : i.holding ? `holds ${(i.vram_held_mib / 1024).toFixed(1)} GB of the card` : "nothing on the card");
      if (i.queue > 0) lv.push(`${i.queue} job${i.queue === 1 ? "" : "s"} in its queue`);
    } else lv.push("server down");
    comfy.live.textContent = lv.join(" · ");
    const a = st.art;
    comfy.queue.hidden = !a;
    if (a) {
      if (a.error) comfy.queue.textContent = "art queue: " + a.error;
      else if (plan && plan.op === "art_batch") {
        const gen = (plan.steps || []).filter(s => s.name.startsWith("Generate ") && s.status === "running").pop();
        comfy.queue.textContent = "art queue running" + (gen ? ` · ${gen.name.slice(9)} ${gen.detail || ""}` : ` · ${(plan.steps || []).slice(-1)[0]?.name || ""}`);
      } else if (a.batches) comfy.queue.textContent = `art queue: ${a.images} image${a.images === 1 ? "" : "s"} in ${a.batches} batch${a.batches === 1 ? "" : "es"}, waiting`;
      else {
        const r = (a.recent || [])[0];
        comfy.queue.textContent = "art queue empty" + (r ? ` · last: ${r.name}, ${r.written || 0} made${r.failed ? `, ${r.failed} failed` : ""}` : "");
      }
    }
    const busy = !!plan;
    comfy.st.disabled = busy || i.up;
    comfy.sp.disabled = busy || !i.up;
    comfy.fr.disabled = busy || !i.up;
    comfy.rq.disabled = busy || !a || !a.batches;
    comfy.up.disabled = busy || (i.up && i.queue > 0);
    comfy.open.classList.toggle("off", !i.up);
  }

  // ---- engine bands: one per engine in the inventory, in its order; its accent is a slot colour the theme sets ----
  const bands = {};
  function engineOf(s) { return INV.engines.find((e) => e.id === s.engine) || { id: s.engine, name: s.engine, slot: "e1" }; }
  function slotOf(s) { return s.legacy ? "e0" : engineOf(s).slot; }
  function closeMenus() { document.querySelectorAll(".menu").forEach((m) => { if (m._fit) m._fit.disconnect(); m.remove(); }); }
  // A menu opens under its ▾ with the right edges aligned. One that would cross an edge of its band, or of the
  // window where a theme's band runs past it, slides back inside (a client chip near the left goes right); one
  // wider than that wraps its lines. It fits again whenever its size changes: a theme's face loads on first use
  // and widens the menu after it opened.
  function placeMenu(m) {
    const fit = () => {
      const g = (m.closest(".group") || document.body).getBoundingClientRect();
      const left = Math.max(g.left, 0), right = Math.min(g.right, document.documentElement.clientWidth);
      m.style.maxWidth = `${right - left}px`;
      m.style.right = "";
      const r = m.getBoundingClientRect();
      if (r.left < left) m.style.right = `${r.left - left}px`;
      else if (r.right > right) m.style.right = `${r.right - right}px`;
    };
    fit();
    if (window.ResizeObserver) { m._fit = new ResizeObserver(fit); m._fit.observe(m); }
  }
  function engineBand(e) {
    const g = el("div", "group"); g.dataset.group = e.id; g.dataset.slot = e.slot;
    const head = el("div", "band-head"); g.appendChild(head);
    head.appendChild(el("h2", null, e.name));
    const tools = el("div", "band-tools"); head.appendChild(tools);
    const ver = el("span", "ver", e.version || ""); ver.title = "version"; tools.appendChild(ver);
    const cnt = el("span", "cnt", ""); tools.appendChild(cnt);
    const mb = el("button", "btn small ghost band-menu", "▾"); mb.title = `${e.name}: more`; tools.appendChild(mb);
    mb.onclick = (ev) => {
      ev.stopPropagation();
      const open = tools.querySelector(".menu"); closeMenus(); if (open) return;
      const m = el("div", "menu"); tools.appendChild(m);
      const info = (STATE && STATE.engines && STATE.engines[e.id]) || {};
      const planRunning = STATE && STATE.plan && STATE.plan.status === "running";
      const item = (label, fn, disabled) => { const b = el("button", "btn small ghost", label); b.disabled = !!disabled; b.onclick = () => { closeMenus(); fn(); }; m.appendChild(b); return b; };
      if ((INV.units || []).some((u) => u.engine === e.id && u.driver !== "ollama" && u.driver !== "llamacpp-router")) item(`Stop all units${info.up ? ` (${info.up} up)` : ""}`, async () => {
        if (await confirm(`Stop every ${e.name} unit?`, `This ends ${info.up || 0} server process${info.up === 1 ? "" : "es"}. Their models unload with them.`)) action("stop_all", e.id, { confirm: true });
      }, planRunning || !info.up);
      updateItems(item, e.id, e.name, info, planRunning);
      placeMenu(m);
    };
    const grid = el("div", "grid"); grid.id = "grp-" + e.id; g.appendChild(grid);
    bands[e.id] = { root: g, ver, cnt, grid };
    return g;
  }
  // The update entries of a band's or a client's menu, from the recipe the agent reports and the last check it kept.
  // A check runs on this click and may reach the network; nothing checks on its own.
  function updateItems(item, id, name, info, planRunning) {
    const u = (STATE && STATE.updates && STATE.updates[id]) || null, recipe = info.recipe || "none";
    if (recipe === "none") return item(`Update: no recipe declared${info.recipe_note ? ` · ${info.recipe_note}` : ""}`, () => {}, true);
    if (recipe === "comfy" || recipe === "driver") return item("Update: on its own tile", () => {}, true);
    item(u ? `Check again for an update (last ${u.checked_at || "?"})` : "Check for an update (reaches the network)", () => action("check_update", id), planRunning);
    if (!u) return;
    if (!u.ok) item(`Last check failed: ${(u.error || "").slice(0, 60)}`, () => {}, true);
    else if (u.available && u.can_apply) item(`Update to ${u.latest}…`, async () => {
      if (await confirm(`Update ${name} to ${u.latest}?`, `${u.installed || "?"} → ${u.latest} by the ${recipe} recipe. ${info.recipe_note || ""} A backup is kept where the recipe can; running units of an engine must be stopped first.`)) action("apply_update", id, { confirm: true });
    }, planRunning);
    else if (u.available) item(`Newer: ${u.latest} (by hand)`, () => {}, true);
    else if (u.available === null) item(u.note ? u.note.slice(0, 70) : "No comparison possible", () => {}, true);
    else item(`${u.installed || "?"} is current`, () => {}, true);
    if (recipe === "self" && u.can_apply) item("Run its own updater…", async () => {
      if (await confirm(`Let ${name} update itself?`, `Runs its own update command. ${info.recipe_note || ""}`)) action("apply_update", id, { confirm: true });
    }, planRunning);
    if (u.backup) item("Roll back the last update…", async () => {
      if (await confirm(`Roll ${name} back?`, `Restores the version recorded before the last update (${u.backup}).`)) action("rollback_update", id, { confirm: true });
    }, planRunning);
  }
  // ---- clients: the programs that consume the units; a strip, not cards ----
  const clientChips = {};
  function clientsBand() {
    const g = el("div", "group"); g.dataset.group = "clients";
    const head = el("div", "band-head"); g.appendChild(head); head.appendChild(el("h2", null, "Clients"));
    const row = el("div", "clients"); g.appendChild(row);
    for (const c of INV.clients || []) {
      const chip = el("div", "client"); chip.dataset.id = c.id;
      chip.appendChild(el("span", "cl-name", c.name || c.id));
      const ver = el("span", "ver", c.version || ""); chip.appendChild(ver);
      const ports = (c.bindings || []).map((b) => (String(b.url).match(/:(\d+)(?:\/|$)/) || [])[1]).filter(Boolean);
      const uses = el("span", "cl-uses", ports.length ? "→ " + [...new Set(ports)].map((p) => ":" + p).join(" ") : ""); chip.appendChild(uses);
      const mb = el("button", "btn small ghost band-menu", "▾"); mb.title = `${c.name}: more`; chip.appendChild(mb);
      const tools = el("div", "band-tools"); tools.appendChild(mb); chip.appendChild(tools);
      mb.onclick = (ev) => {
        ev.stopPropagation();
        const open = tools.querySelector(".menu"); closeMenus(); if (open) return;
        const m = el("div", "menu"); tools.appendChild(m);
        const info = (STATE && STATE.clients && STATE.clients[c.id]) || {};
        const planRunning = STATE && STATE.plan && STATE.plan.status === "running";
        const item = (label, fn, disabled) => { const b = el("button", "btn small ghost", label); b.disabled = !!disabled; b.onclick = () => { closeMenus(); fn(); }; m.appendChild(b); return b; };
        if (c.config) item(`config: ${c.config}`, () => {}, true);
        updateItems(item, c.id, c.name || c.id, info, planRunning);
        placeMenu(m);
      };
      chip.onmouseenter = () => setHelp(`${c.name}: ${c.bindings && c.bindings.length ? "points at " + c.bindings.map((b) => `${b.name} (${b.url})`).join(", ") : "no local bindings"}`);
      chip.onmouseleave = () => setHelp("");
      clientChips[c.id] = { root: chip, ver };
      row.appendChild(chip);
    }
    return g;
  }
  function usedBy(port) {
    return (INV.clients || []).filter((c) => (c.bindings || []).some((b) => (String(b.url).match(/:(\d+)(?:\/|$)/) || [])[1] === String(port))).map((c) => c.name || c.id);
  }
  function legacyBand() {
    const g = el("div", "group collapsed"); g.dataset.group = "legacy"; g.dataset.slot = "e0";
    const h = el("h2", "toggle", "Legacy units "); h.id = "legacy-toggle"; h.appendChild(el("span", "chev", "▸")); g.appendChild(h);
    h.onclick = () => g.classList.toggle("collapsed");
    const grid = el("div", "grid"); grid.id = "grp-legacy"; g.appendChild(grid);
    bands.legacy = { root: g, grid };
    return g;
  }
  function buildCards() {
    const wrap = $("#bands"); wrap.innerHTML = "";
    for (const k of Object.keys(bands)) delete bands[k];
    for (const e of INV.engines || []) wrap.appendChild(engineBand(e));
    if ((INV.units || []).some((s) => s.legacy)) wrap.appendChild(legacyBand());
    for (const k of Object.keys(clientChips)) delete clientChips[k];
    if ((INV.clients || []).length) wrap.appendChild(clientsBand());
    $("#grp-services").innerHTML = ""; owui = null; comfy = null;
    $("#grp-services-wrap").hidden = !INV.openwebui && !INV.comfyui;
    if (INV.openwebui) $("#grp-services").appendChild(owuiCard(INV.openwebui));
    if (INV.comfyui) $("#grp-services").appendChild(comfyCard(INV.comfyui));
    for (const s of INV.units) {
      const b = s.legacy ? bands.legacy : bands[s.engine];
      if (b) b.grid.appendChild(serverCard(s));
    }
    buildPresets();
  }
  function buildPresets() {
    const pr = $("#presets"); pr.innerHTML = "";
    for (const p of INV.presets || []) {
      const b = el("button", "preset"); b.appendChild(el("div", "n", p.name)); b.appendChild(el("div", "d", p.desc || ""));
      const ed = el("span", "edit", "✎"); ed.title = "edit this preset"; ed.onclick = (ev) => { ev.stopPropagation(); openPresetEditor(p); }; b.appendChild(ed);
      b.onclick = () => action("preset", p.id); pr.appendChild(b);
      b.onmouseenter = () => setHelp(`${p.name}: ${p.desc || "preset"}`); b.onmouseleave = () => setHelp("");
    }
    for (const sh of INV.shims || []) {
      const b = el("button", "preset"); b.appendChild(el("div", "n", sh.name)); b.appendChild(el("div", "d", `:${sh.port} · click to start / stop`));
      b.dataset.shim = sh.id;
      b.onclick = () => action(STATE && STATE.shims && STATE.shims[sh.id] && STATE.shims[sh.id].up ? "stop_shim" : "start_shim", sh.id);
      pr.appendChild(b);
    }
  }

  // ---- the preset editor: steps over the ops the agent allows (GET /api/presets), saved to presets.json by the agent ----
  let PE = null;                                   // { ops, context_choices } from the agent, fetched when the editor opens
  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  function nameOf(kind, id) {
    const u = (INV.units || []).find((x) => x.id === id), sh = (INV.shims || []).find((x) => x.id === id), e = (INV.engines || []).find((x) => x.id === id);
    return u ? `${u.name} · :${u.port}` : sh ? `${sh.name} · :${sh.port}` : e ? e.name : id;
  }
  function stepRow(step, idx, rows) {
    const row = el("div", "pe-row");
    row.appendChild(el("span", "n", String(idx + 1)));
    const op = el("select"); row.appendChild(op);
    for (const [k, spec] of Object.entries(PE.ops)) {
      if (!spec.available) continue;
      const o = el("option", null, spec.label); o.value = k; op.appendChild(o);
    }
    op.value = step.op in PE.ops && PE.ops[step.op].available ? step.op : op.options[0].value;
    const tgt = el("select"); row.appendChild(tgt);
    const valWrap = el("div"); row.appendChild(valWrap);
    const fill = () => {
      const spec = PE.ops[op.value];
      tgt.innerHTML = ""; valWrap.innerHTML = "";
      tgt.hidden = !spec.target;
      if (spec.target) {
        for (const id of spec.targets) { const o = el("option", null, nameOf(spec.target, id)); o.value = id; tgt.appendChild(o); }
        if (spec.targets.includes(step.target)) tgt.value = step.target;
        if (!spec.targets.length) { const o = el("option", null, "(nothing of that kind in the inventory)"); o.value = ""; tgt.appendChild(o); }
      }
      if (spec.value === "context") {
        const v = el("select"); valWrap.appendChild(v);
        const choices = (PE.context_choices[tgt.value] || []);
        for (const n of choices) { const o = el("option", null, fmtK(n)); o.value = n; v.appendChild(o); }
        if (choices.includes(step.value)) v.value = String(step.value);
        tgt.onchange = fill;
      } else if (spec.value === "budget") {
        const v = el("input"); v.type = "number"; v.min = 0; v.max = 262144; v.step = 256; v.placeholder = "0 = off"; v.value = step.value ?? ""; valWrap.appendChild(v);
        tgt.onchange = null;
      } else tgt.onchange = null;
    };
    op.onchange = () => { step.op = op.value; fill(); };
    fill();
    const ctl = el("div", "ctl"); row.appendChild(ctl);
    // read every row back before re-drawing, so a change in another row is not lost to a move or a removal
    const sync = () => { [...$("#pe-steps").children].forEach((r, i) => { rows[i] = r.read(); }); };
    const mk = (t, title, fn) => { const b = el("button", "btn small ghost", t); b.title = title; b.type = "button"; b.onclick = () => { sync(); fn(); renderSteps(rows); }; ctl.appendChild(b); };
    mk("↑", "move up", () => { if (idx > 0) [rows[idx - 1], rows[idx]] = [rows[idx], rows[idx - 1]]; });
    mk("↓", "move down", () => { if (idx < rows.length - 1) [rows[idx + 1], rows[idx]] = [rows[idx], rows[idx + 1]]; });
    mk("✕", "remove", () => { rows.splice(idx, 1); });
    row.read = () => {
      const spec = PE.ops[op.value], out = { op: op.value };
      if (spec.target) out.target = tgt.value;
      const v = valWrap.querySelector("select, input");
      if (spec.value && v) out.value = Number(v.value);
      return out;
    };
    return row;
  }
  function renderSteps(rows) {
    const box = $("#pe-steps"); box.innerHTML = "";
    rows.forEach((r, i) => { box.appendChild(stepRow(r, i, rows)); });
  }
  async function openPresetEditor(preset, snapshot) {
    try { PE = await api("/api/presets"); } catch (e) { toast(e.message, "bad"); return; }
    const editing = !!preset && !snapshot;
    const p = preset ? JSON.parse(JSON.stringify(preset)) : { id: "", name: "", desc: "", steps: [{ op: "make_resident" }] };
    $("#pe-title").textContent = editing ? `Edit preset: ${p.name}` : snapshot ? "Save the current arrangement as a preset" : "New preset";
    $("#pe-name").value = p.name; $("#pe-id").value = p.id; $("#pe-desc").value = p.desc || "";
    $("#pe-id").disabled = editing;
    let idTouched = editing || !!p.id;
    $("#pe-name").oninput = () => { if (!idTouched) $("#pe-id").value = slug($("#pe-name").value); };
    $("#pe-id").oninput = () => { idTouched = true; };
    const rows = p.steps.map((s) => ({ ...s }));
    const syncRows = () => { [...$("#pe-steps").children].forEach((row, i) => { rows[i] = row.read(); }); };
    renderSteps(rows);
    $("#pe-add").onclick = () => { syncRows(); rows.push({ op: "make_resident" }); renderSteps(rows); };
    $("#pe-delete").hidden = !editing;
    $("#pe-err").hidden = true;
    const modal = $("#preset-modal"); modal.hidden = false;
    $("#pe-name").focus();
    const close = () => { modal.hidden = true; };
    $("#pe-cancel").onclick = close;
    $("#pe-delete").onclick = async () => {
      if (!(await confirm(`Delete preset ${p.name}?`, "The preset is removed from presets.json. Nothing running changes."))) return;
      try { await api("/api/presets/delete", { id: p.id }); toast(`preset ${p.id} deleted`); close(); await refreshPresets(); } catch (e) { showErr(e.message); }
    };
    const showErr = (m) => { const e = $("#pe-err"); e.textContent = m; e.hidden = false; };
    $("#pe-save").onclick = async () => {
      $("#pe-err").hidden = true;
      syncRows();
      const out = { id: $("#pe-id").value.trim(), name: $("#pe-name").value.trim(), desc: $("#pe-desc").value.trim(), steps: rows };
      try {
        const r = await api("/api/presets", { preset: out, by: "ui" });
        toast(`preset ${r.preset.name} ${r.replaced ? "saved" : "created"}`); close(); await refreshPresets();
      } catch (e) { showErr(e.message); }
    };
  }
  async function refreshPresets() {
    try { const r = await api("/api/presets"); INV.presets = r.presets; buildPresets(); } catch (e) { toast(e.message, "bad"); }
  }
  // what is on the card right now, as steps: resident model (and its context, for Maya), shims up, ComfyUI up
  function snapshotPreset() {
    const st = STATE || {}, steps = [];
    const holders = st.holders || [];
    if (!holders.length) steps.push({ op: "unload_all" });
    for (const h of holders) {
      const u = (INV.units || []).find((x) => x.id === h), i = (st.servers || {})[h] || {};
      if (!u) continue;
      steps.push({ op: "make_resident", target: h });
      if (u.driver === "maya" && i.health && i.health.max_context && (u.context_choices || []).includes(i.health.max_context)) steps.push({ op: "set_context", target: h, value: i.health.max_context });
    }
    for (const sh of INV.shims || []) if (st.shims && st.shims[sh.id] && st.shims[sh.id].up) steps.push({ op: "start_shim", target: sh.id });
    if (INV.comfyui && st.comfyui && st.comfyui.up) steps.push({ op: "comfy_start" });
    const main = holders.length ? (INV.units.find((x) => x.id === holders[0]) || {}).name : "Card clear";
    const stamp = new Date(); const hhmm = `${String(stamp.getHours()).padStart(2, "0")}${String(stamp.getMinutes()).padStart(2, "0")}`;
    return { id: slug(`${main}-${hhmm}`), name: `${main} arrangement`, desc: `saved from the dashboard ${stamp.toLocaleString()}`, steps };
  }

  // ---- the catalog panel: what is installed and its tier, suggestions from the agent's rules, a scan on a click, guides ----
  let CATALOG = null;
  function guideShow(rec) {
    const b = $("#guide-body"); b.innerHTML = "";
    const h = (t) => b.appendChild(el("h3", null, t));
    const p = (t, cls) => { const e = el("p", cls || null, t); b.appendChild(e); return e; };
    const list = (items) => { const u = el("ul"); for (const i of items || []) { const li = el("li"); li.appendChild(looksLikeCommand(i) ? el("code", null, i) : document.createTextNode(i)); u.appendChild(li); } b.appendChild(u); };
    $("#guide-title").textContent = `${rec.name}  ·  tier ${rec.tier}, ${rec.role}${rec.driver && rec.driver !== rec.id ? `, driven as ${rec.driver}` : ""}`;
    p(rec.what);
    p(`Licence: ${rec.licence || "?"}   ·   ${rec.home || ""}`, "lic");
    p(testedLine(rec), "tested");
    if (rec.fits) { h("Fits"); list((rec.fits.good || []).map((x) => "good for: " + x)); list((rec.fits.not || []).map((x) => "not for: " + x)); }
    if (rec.install) { for (const [plat, steps] of Object.entries(rec.install)) { h(`Install · ${plat}`); list(steps); } }
    if (rec.drive) { h("What Quartermaster does with it"); list((rec.drive.can || []).map((x) => "can: " + x)); list((rec.drive.cannot || []).map((x) => "cannot: " + x)); }
    if (rec.launch) { h("Launch"); if (rec.launch.argv) p(rec.launch.argv.join(" ")).className = "mono"; if (rec.launch.never) list(rec.launch.never.map((x) => "never: " + x)); if (rec.launch.note) p(rec.launch.note); }
    if (rec.update) { h("Update"); p(`recipe ${rec.update.recipe}${rec.update.package ? " · " + rec.update.package : ""}${rec.update.github ? " · github " + rec.update.github : ""}`); if (rec.update.note) p(rec.update.note); }
    if (rec.detect) { h("How it is found"); p(Object.entries(rec.detect).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ·  ")); }
    $("#guide-modal").hidden = false;
  }
  function looksLikeCommand(s) { return /^(git |npm |pip |python |winget |docker |cmake |ollama |llama-server |venv|\.venv|\S+\.(bat|sh|exe)\b|[A-Za-z]:\\|\/usr\/)/.test(s) || /^[a-z0-9_.\/-]+( [-\w.\/:=<>"'\\@]+)+$/.test(s) && s.length < 90 && !/[.!?]$/.test(s); }
  // `tested` in a record: {platform: date} for every machine someone drove it from. A tier A or B record without
  // one was written from the documentation, and the panel says so rather than letting the tier imply otherwise.
  const PLAT = { windows: "Windows", linux: "Linux", macos: "macOS" };
  function testedLine(rec) {
    const t = rec.tested && Object.keys(rec.tested).length ? Object.entries(rec.tested).map(([k, v]) => `${PLAT[k] || k} (${v})`).join(", ") : "";
    if (t) return `Driven from: ${t}.`;
    return rec.tier === "D" ? "Listed with its guide; Quartermaster does not drive it." : "Not yet driven by anyone we know of: the record is from the documentation. If it works for you, say so.";
  }
  function catRow(name, tier, ver, rec) {
    const r = el("div", "cat-row");
    r.appendChild(el("span", "tier", tier || "?"));
    r.appendChild(el("span", "nm", name));
    if (ver) r.appendChild(el("span", "ver", ver));
    if (rec && rec.tier && rec.tier !== "D" && !(rec.tested && Object.keys(rec.tested).length)) { const u = el("span", "untested", "untested"); u.title = testedLine(rec); r.appendChild(u); }
    r.appendChild(el("span", "spacer"));
    if (rec) { const g = el("button", "btn small ghost", "guide"); g.onclick = () => guideShow(rec); r.appendChild(g); }
    return r;
  }
  async function loadCatalog() {
    try { CATALOG = (await api("/api/catalog")).catalog; } catch (e) { $("#cat-status").textContent = "no catalog"; return; }
    const box = $("#cat-installed"); box.innerHTML = "";
    const seen = new Set();
    const rowFor = (kind, name, ver) => { const rec = CATALOG[kind]; seen.add(kind); box.appendChild(catRow(name, rec && rec.tier, ver, rec)); };
    for (const e of INV.engines || []) rowFor(e.kind, e.name, (STATE && STATE.engines && STATE.engines[e.id] || {}).version);
    if (INV.openwebui) rowFor("openwebui", INV.openwebui.name || "OpenWebUI", STATE && STATE.openwebui && STATE.openwebui.version);
    if (INV.comfyui) rowFor("comfyui", INV.comfyui.name || "ComfyUI", STATE && STATE.comfyui && STATE.comfyui.version);
    for (const c of INV.clients || []) rowFor(c.id, c.name || c.id, (STATE && STATE.clients && STATE.clients[c.id] || {}).version);
    const rest = Object.values(CATALOG).filter((r) => !seen.has(r.id));
    if (rest.length) { box.appendChild(el("div", "cat-sub", "Known, not installed here")); for (const r of rest) box.appendChild(catRow(r.name, r.tier, "", r)); }
    $("#cat-status").textContent = `${seen.size} of ${Object.keys(CATALOG).length} kinds`;
    await loadSuggestions();
  }
  async function loadSuggestions() {
    let s = [];
    try { s = (await api("/api/suggestions")).suggestions || []; } catch (e) { return; }
    const box = $("#cat-sugg"); box.innerHTML = "";
    $("#cat-sugg-head").hidden = !s.length;
    for (const x of s) { const d = el("div", "cat-sugg"); d.appendChild(el("div", "t", x.title)); d.appendChild(el("div", "w", x.why)); d.appendChild(el("div", "h", x.how)); box.appendChild(d); }
    if (!s.length) box.appendChild(el("div", "cat-note", "nothing to suggest: the inventory satisfies every rule"));
  }
  async function runScan() {
    const b = $("#cat-scan"); b.disabled = true; b.textContent = "scanning…";
    try {
      const r = await api("/api/scan", { by: "ui" });
      const box = $("#cat-scan-out"); box.innerHTML = ""; $("#cat-scan-head").hidden = false;
      for (const u of r.units) box.appendChild(el("div", "cat-note", `unit ${u.id} (${u.engine} :${u.port}${u.running ? ", running" : ""})`));
      for (const c of r.clients) box.appendChild(el("div", "cat-note", `client ${c.id} ${c.version || ""}`));
      for (const e of r.engines) box.appendChild(el("div", "cat-note", `engine ${e}`));
      for (const s of r.services) box.appendChild(el("div", "cat-note", `service ${s}`));
      if (!(r.units.length || r.clients.length || r.engines.length || r.services.length)) box.appendChild(el("div", "cat-note", "the roster has everything detection can see"));
      else box.appendChild(el("div", "cat-note", "`qm detect` adds what you choose; nothing was written"));
      for (const n of r.notes || []) box.appendChild(el("div", "cat-note", "· " + n));
    } catch (e) { toast(e.message, "bad"); } finally { b.disabled = false; b.textContent = "scan"; }
  }

  // ---- render ----
  function render(st) {
    STATE = st;
    const g = st.gpu || {}, ram = st.ram || {};
    $("#gpu-val").textContent = `${fmtGB(g.used_mib)} / ${fmtGB(g.total_mib)} GB`;
    $("#gpu-fill").style.width = g.total_mib ? (100 * g.used_mib / g.total_mib).toFixed(1) + "%" : "0%";
    $("#gpu-rounds").textContent = g.total_mib != null && g.used_mib != null ? String(Math.max(0, Math.round(g.total_mib - g.used_mib))).padStart(5, "0") : "-----";
    const full = g.total_mib ? g.used_mib / g.total_mib : 0;                 // load level for themes that show #defcon
    $("#defcon").dataset.level = full >= 0.8 ? 1 : full >= 0.6 ? 2 : full >= 0.4 ? 3 : full >= 0.2 ? 4 : 5;
    $("#gpu-free").style.left = g.total_mib && INV ? (100 * INV.gpu.free_card_mib / g.total_mib).toFixed(1) + "%" : "0";
    $("#gpu-util").textContent = `util ${g.util ?? "—"}%`;
    $("#gpu-temp").textContent = `${g.temp_c ?? "—"} °C`;
    $("#gpu-power").textContent = `${g.power_w != null ? Math.round(g.power_w) : "—"} / ${g.power_limit_w != null ? Math.round(g.power_limit_w) : "—"} W`;
    $("#ram-val").textContent = `${ram.used_gib ?? "—"} / ${ram.total_gib ?? "—"} GB`;
    $("#ram-fill").style.width = ram.total_gib ? (100 * ram.used_gib / ram.total_gib).toFixed(1) + "%" : "0%";
    $("#ram-free").textContent = `free ${ram.available_gib ?? "—"} GB`;
    $("#ram-commit").textContent = `commit ${ram.commit_used_gib ?? "—"} / ${ram.commit_total_gib ?? "—"} GB`;
    $("#clock").textContent = new Date(st.time * 1000).toLocaleTimeString();
    $("#gpu-apps").textContent = (g.apps || []).map((a) => a.name).join(", ") || "no engine on the card";

    // resident line
    const holders = st.holders || [];
    const rl = $("#resident-line");
    const transient = INV.units.find((s) => ["starting", "loading", "unloading", "stopping", "reloading"].includes((st.servers[s.id] || {}).state));
    if (transient) {
      $("#resident-main").textContent = `${st.servers[transient.id].state} ${transient.name}`;
      $("#resident-sub").textContent = `${fmtGB(g.used_mib)} GB on the card`;
    } else if (holders.length === 0 && st.card_unattributed) {
      const apps = (g.apps || []).map((a) => a.name).join(", ");
      $("#resident-main").textContent = apps ? `${apps} is taking the card` : "something is taking the card";
      $("#resident-sub").textContent = `${fmtGB(g.used_mib)} GB on the card, no server reports a model yet (loading on a request?)`;
    } else if (holders.length === 0 && st.comfyui && st.comfyui.holding && !(st.ollama && st.ollama.models.length)) {
      // no model is resident, but the card is not free: ComfyUI has its models on it
      $("#resident-main").textContent = `ComfyUI holds ${fmtGB(st.comfyui.vram_held_mib)} GB`;
      $("#resident-sub").textContent = `${fmtGB(g.used_mib)} GB on the card${st.comfyui.queue > 0 ? " · generating" : ""} · no model resident`;
    } else if (holders.length === 0) {
      $("#resident-main").textContent = (st.ollama && st.ollama.models.length) ? `Ollama: ${st.ollama.models.map((m) => m.name).join(", ")}` : "nothing on the card";
      $("#resident-sub").textContent = `${fmtGB(g.used_mib)} GB used by the desktop`;
    } else {
      const names = holders.map((h) => { const s = INV.units.find((x) => x.id === h); const i = st.servers[h]; return `${s.name} on ${engineOf(s).name} ${s.port} · ${i.state}`; });
      $("#resident-main").textContent = names.join("  +  ");
      const i = st.servers[holders[0]];
      const parts = [];
      if (i.live && i.live.state && i.live.state !== "idle") parts.push(`${i.live.state} ${i.live.tok_s ? i.live.tok_s.toFixed(1) + " tok/s" : ""}`);
      if (i.last_request) parts.push(`last: ${i.last_request.prompt_tokens ?? "?"} in / ${i.last_request.output_tokens ?? "?"} out · ${i.last_request.decode_tok_s ? i.last_request.decode_tok_s.toFixed(1) + " tok/s" : ""}`);
      if (i.engine && i.engine.context) parts.push(`${fmtK(i.engine.context)} ctx`);
      $("#resident-sub").textContent = parts.join("   ");
    }
    rl.style.borderLeftColor = holders.length > 1 ? "var(--bad)" : holders.length ? `var(--${slotOf(INV.units.find((x) => x.id === holders[0]))})` : "var(--line)";

    // warnings
    const w = $("#warnings"); w.innerHTML = "";
    for (const m of st.warnings || []) w.appendChild(el("div", "warn", m));

    // engine bands: version and counts; a known newer version rides on the badge
    const upd = st.updates || {};
    for (const [eid, b] of Object.entries(bands)) {
      const info = (st.engines || {})[eid];
      if (!info || !b.ver) continue;
      const u = upd[eid];
      b.ver.textContent = (info.version || "") + (u && u.ok && u.available ? `  ↑ ${u.latest}` : "");
      b.ver.classList.toggle("newer", !!(u && u.ok && u.available));
      b.cnt.textContent = `${info.units} unit${info.units === 1 ? "" : "s"} · ${info.up} up${info.loaded ? ` · ${info.loaded} loaded` : ""}`;
    }
    for (const [cid, ch] of Object.entries(clientChips)) {
      const info = (st.clients || {})[cid] || {}, u = upd[cid];
      ch.ver.textContent = (info.version || "") + (u && u.ok && u.available ? `  ↑ ${u.latest}` : "");
      ch.ver.classList.toggle("newer", !!(u && u.ok && u.available));
    }

    // cards
    const planRunning = st.plan && st.plan.status === "running";
    for (const s of INV.units) {
      const refs = cards[s.id], i = st.servers[s.id] || { state: "down" };
      if (!refs) continue;
      let state = i.state;
      refs.pill.textContent = i.parked && state === "idle" ? "parked" : state;
      refs.pill.className = "pill " + (i.parked && state === "idle" ? "parked" : state);
      refs.root.classList.toggle("resident", !!i.loaded && s.driver !== "ollama");
      refs.root.classList.toggle("down", !i.up);
      refs.root.classList.toggle("parked", !!i.parked);
      const lv = [];
      if (i.live && i.live.state && i.live.state !== "idle") {
        lv.push(`<b>${i.live.state}</b>${i.live.phase ? " · " + i.live.phase : ""}`);
        if (i.live.state === "reading" && i.live.prompt_total) lv.push(`${i.live.prompt_read ?? 0}/${i.live.prompt_total} tok`);
        if (i.live.tok_s) lv.push(`<b>${i.live.tok_s.toFixed(1)}</b> tok/s`);
        if (i.live.generated) lv.push(`${i.live.generated} out`);
        if (i.live.queued) lv.push(`${i.live.queued} queued`);
      } else if (i.last_request && i.loaded) {
        const r = i.last_request;
        lv.push(`last ${r.prompt_tokens ?? "?"} in · ${r.output_tokens ?? "?"} out · ${r.prefill_tok_s ? Math.round(r.prefill_tok_s) + " pre" : ""} · <b>${r.decode_tok_s ? r.decode_tok_s.toFixed(1) : "—"}</b> tok/s`);
      } else if (i.health && i.health.max_context && i.up) {
        lv.push(`${fmtK(i.health.max_context)} ctx · ${i.health.model || ""}${i.health.version ? " · v" + i.health.version : ""}`);
      } else if (!i.up) {
        lv.push(s.driver === "strata-launch" ? `server down · ${s.preset}` : "server down");
      }
      refs.live.innerHTML = lv.join(" &nbsp; ");
      serverVitals(refs.vit, s, i);
      if (s.driver === "ollama") {
        refs.oll.innerHTML = "";
        for (const m of i.models || []) { const r = el("div", "m"); r.appendChild(el("span", null, m.name)); r.appendChild(el("span", null, `${m.size_vram_gib} GB VRAM`)); refs.oll.appendChild(r); }
        refs.un.disabled = !(i.models && i.models.length) || planRunning;
      } else {
        refs.mk.disabled = planRunning || (i.loaded && (st.holders || []).length === 1 && i.state === "ready");
        refs.mk.textContent = i.loaded && (st.holders || []).length === 1 ? "Resident" : "Make resident";
        refs.un.disabled = planRunning || !i.loaded;
        if (refs.st) refs.st.disabled = planRunning || i.up;
        if (refs.sp) refs.sp.disabled = planRunning || !i.up;
        if (refs.pk) { refs.pk.textContent = i.parked ? "Unpark" : "Park"; refs.pk.disabled = planRunning; }
        if (refs.sel && i.health && i.health.max_context && document.activeElement !== refs.sel && (s.context_choices || []).includes(i.health.max_context)) refs.sel.value = String(i.health.max_context);
        if (refs.sel) refs.sel.disabled = planRunning || !i.up;
        if (refs.bud) refs.bud.disabled = planRunning || !i.up;
      }
    }
    for (const sh of INV.shims || []) {
      const b = document.querySelector(`[data-shim="${sh.id}"] .d`);
      if (b) b.textContent = `:${sh.port} · ${st.shims && st.shims[sh.id] && st.shims[sh.id].up ? "up · click to stop" : "down · click to start"}`;
    }
    renderOwui(st);
    renderComfy(st);
    renderPlan(st.plan);
    renderTelemetry(st);
    renderGpuSpark(st.gpu_history || []);
  }

  function renderPlan(p) {
    const panel = $("#plan-panel");
    if (!p) { panel.hidden = true; return; }
    panel.hidden = false;
    $("#plan-title").textContent = `${p.op.replace("_", " ")} ${p.target || ""}`.trim();
    $("#plan-status").textContent = p.status + (p.ended ? ` · ${(p.ended - p.started).toFixed(0)}s` : ` · ${ago(p.started)}`);
    $("#plan-cancel").hidden = p.status !== "running";
    const ol = $("#plan-steps"); ol.innerHTML = "";
    for (const s of p.steps) {
      const li = el("li", s.status);
      li.appendChild(el("span", "dot"));
      const mid = el("div"); mid.appendChild(el("div", null, s.name));
      if (s.detail) mid.appendChild(el("div", "d", s.detail));
      if (s.lines && s.lines.length && s.status === "running") mid.appendChild(el("div", "lines", s.lines.slice(-3).join("\n")));
      li.appendChild(mid);
      li.appendChild(el("span", "t", s.ended ? `${(s.ended - s.started).toFixed(0)}s` : ago(s.started)));
      ol.appendChild(li);
    }
    if (p.error) ol.appendChild(el("div", "plan-err", p.error));
    if (p.id !== lastPlanId && p.status !== "running" && p.status !== "queued") {
      lastPlanId = p.id;
      toast(`${p.op} ${p.target || ""}: ${p.status}${p.error ? " — " + p.error : ""}`, p.status === "done" ? "ok" : "bad");
      loadHistory();
      if (CATALOG && /update|preset|reload/.test(p.op)) loadSuggestions();     // a check or a preset may change what there is to suggest
    }
    if (p.status === "running") lastPlanId = null;
  }

  function spark(svg, series, color, maxHint) {
    const W = 300, H = Number(svg.getAttribute("viewBox").split(" ")[3]);
    const vals = series.map((p) => p[1] == null ? 0 : p[1]);
    const max = Math.max(maxHint || 0, ...vals, 1);
    const n = Math.max(vals.length, 2);
    const pts = vals.map((v, i) => `${(i / (n - 1)) * W},${H - 2 - (v / max) * (H - 6)}`);
    svg.innerHTML = `<polyline fill="none" stroke="${color}" stroke-width="1.5" points="${pts.join(" ")}"/>` +
      `<polygon fill="${color}" fill-opacity=".12" points="0,${H} ${pts.join(" ")} ${W},${H}"/>` +
      `<text x="2" y="10" fill="var(--fg3)" font-size="9" font-family="var(--mono)">${max.toFixed(0)}</text>`;
  }
  function renderTelemetry(st) {
    const rid = (st.holders || [])[0];
    const s = rid && INV.units.find((x) => x.id === rid), i = rid && st.servers[rid];
    $("#tele-name").textContent = s ? `${s.name} :${s.port}` : "no resident model";
    if (!i) { ["t-toks", "t-prefill", "t-state", "t-ctx"].forEach((k) => $("#" + k).textContent = "—"); $("#t-facts").innerHTML = ""; $("#t-reqs").innerHTML = ""; spark($("#spark"), [], "var(--fg3)"); return; }
    const live = i.live || {}, last = i.last_request || {};
    $("#t-toks").textContent = live.tok_s ? live.tok_s.toFixed(1) : last.decode_tok_s ? last.decode_tok_s.toFixed(1) : "—";
    $("#t-prefill").textContent = live.prefill_tok_s_mean ? Math.round(live.prefill_tok_s_mean) : last.prefill_tok_s ? Math.round(last.prefill_tok_s) : "—";
    $("#t-state").textContent = live.state || i.state; $("#t-phase").textContent = live.phase || "";
    const eng = i.engine || {};
    $("#t-ctx").textContent = fmtK(eng.context || (i.health && i.health.max_context));
    $("#t-kv").textContent = eng.kv ? `kv ${eng.kv}${eng.kv_gb ? " · " + eng.kv_gb + " GB" : ""}` : "";
    const facts = $("#t-facts"); facts.innerHTML = "";
    const add = (k, v) => { if (v != null && v !== "") facts.appendChild(el("span", "tag", `${k} ${v}`)); };
    add("vram slots", eng.vram_slots); add("ram slots", eng.ram_slots); add("vram", eng.vram_gb && eng.vram_gb + " GB"); add("ram tier", eng.ram_gb && eng.ram_gb + " GB");
    add("threads", eng.cpu_threads); add("mtp", eng.mtp);
    if (i.totals) { add("requests", i.totals.requests); const pk = Number(i.totals.peak_decode); if (Number.isFinite(pk) && pk > 0) add("peak", pk.toFixed(1) + " tok/s"); }
    const tb = $("#t-reqs"); tb.innerHTML = "";
    for (const r of (i.recent_requests || []).slice().reverse()) {
      const tr = el("tr");
      [r.prompt_tokens, r.output_tokens, r.prefill_tok_s && Math.round(r.prefill_tok_s), r.decode_tok_s && r.decode_tok_s.toFixed(1), r.finish_reason || r.finish].forEach((v) => tr.appendChild(el("td", null, v == null ? "—" : String(v))));
      tb.appendChild(tr);
    }
    spark($("#spark"), i.tok_history || [], `var(--${slotOf(s)})`, 25);
  }
  function renderGpuSpark(h) {
    spark($("#gpu-spark"), h.map((p) => [p[0], p[1]]), "var(--e1)", STATE && STATE.gpu && STATE.gpu.total_mib);
  }

  // ---- history / log ----
  async function loadHistory() {
    try {
      const r = await api("/api/history?lines=40");
      const ul = $("#history"); ul.innerHTML = "";
      for (const a of r.actions) {
        const li = el("li");
        li.appendChild(el("span", null, a.ts.slice(11)));
        li.appendChild(el("span", null, `${a.op} ${a.target || ""}${a.args && a.args.value ? " " + fmtK(a.args.value) : ""} · ${a.by}`));
        li.appendChild(el("span", a.result === "done" ? "ok" : a.result, `${a.result}${a.duration_s != null ? " " + a.duration_s + "s" : ""}`));
        if (a.error) li.title = a.error;
        ul.appendChild(li);
      }
    } catch (e) { /* quiet */ }
  }
  async function openLog(id, name) {
    logServer = id; $("#log-title").textContent = `${name} · engine log`; $("#log-modal").hidden = false; refreshLog();
  }
  async function refreshLog() {
    if (!logServer) return;
    try {
      const ex = encodeURIComponent($("#log-filter").value || "");
      const r = await api(`/api/log?server=${logServer}&lines=300&exclude=${ex}`);
      const pre = $("#log-body"); pre.textContent = r.lines.join("\n") || "(empty)"; pre.scrollTop = pre.scrollHeight;
    } catch (e) { $("#log-body").textContent = e.message; }
  }

  // ---- stream ----
  function connect() {
    const es = new EventSource("/api/events");
    es.addEventListener("state", (ev) => { $("#conn").classList.add("on"); render(JSON.parse(ev.data)); });
    es.onerror = () => { $("#conn").classList.remove("on"); es.close(); setTimeout(connect, 2500); };
  }

  // ---- themes ----
  const BOOT_LINES = {
    muthur: ["WEYLAND-YUTANI CORP", "MU/TH/UR 6000 MAINFRAME", "USCSS {HOST}  -  REG 180924609", "",
      "PROCESSOR BUS ............ OK", "INFERENCE ARRAY .......... OK", "SEQUESTERED SYSTEMS ...... SEALED",
      "CREW MANIFEST ............ {OWNER}", "",
      "INTERFACE 2037 READY FOR INQUIRY "],
    castlevania: ["Quartermaster", "CASTLE {HOST}", "PLAYER 1  -  {OWNER}", "", "WHAT IS A MODEL?", "A MISERABLE LITTLE PILE OF WEIGHTS.", "",
      "PRESS ANY KEY"],
    lcars: ["LCARS 40274", "LIBRARY COMPUTER ACCESS AND RETRIEVAL SYSTEM", "USS {HOST}  -  NCC-{N}", "",
      "ISOLINEAR CORE ............ NOMINAL", "INFERENCE ARRAY ........... NOMINAL", "HOLODECK SAFETIES ......... ENGAGED",
      "COMMAND AUTHORIZATION ..... {OWNER}", "",
      "WORKING"],
    cyberpunk: ["Quartermaster", "CYBERDECK // {HOST} MK.{N}", "", "ICE ....................... ACTIVE",
      "ENGRAM VAULT .............. MOUNTED", "RELIC ..................... STABLE, FOR NOW", "NETRUNNER ................. {OWNER}", "",
      "WAKE THE FUCK UP, SAMURAI.", "WE HAVE TOKENS TO BURN."],
    cortex: ["Global Cortex", "LAYERED  -  SECTION {HOST}", "", "GENERATOR ................ OK", "FCS ...................... OK",
      "RADIATOR ................. OK", "PILOT .................... {OWNER}", "", "MAIN SYSTEM: ACTIVATING COMBAT MODE"],
    concord: ["Nerves Concord", "RAVEN TERMINAL  -  {HOST}", "", "GENERATOR ................ OK", "FCS ...................... OK",
      "RADIATOR ................. OK", "PILOT .................... {OWNER}", "", "MAIN SYSTEM: ACTIVATING COMBAT MODE"],
    matrix: ["Wake up, {owner}...", "The Matrix has you...", "Follow the white rabbit.", "", "Knock, knock, {owner}. "],
    precrime: ["Precrime", "DEPARTMENT OF PRECRIME  -  {HOST} DIVISION", "", "PRECOG LINK ............ ESTABLISHED",
      "PREVISION ARCHIVE ...... ONLINE", "CONTAINMENT ............ SEALED", "CHIEF .................. {OWNER}", "",
      "THE PRECOGS ARE NEVER WRONG."],
    ocp: ["OCP POLICE SYSTEMS", "LOAD BIOS", "COMMAND.COM", "MEMORY SET ............ OK", "SYSTEM STATUS ......... OK",
      "OFFICER ............... {OWNER}", "",
      "PRIME DIRECTIVES", "1. SERVE THE PUBLIC TRUST", "2. PROTECT THE INNOCENT", "3. UPHOLD THE LAW", "4. [CLASSIFIED] "],
    magi: ["MAGI", "NERV  -  SPECIAL AGENCY  -  {HOST} BRANCH", "", "MELCHIOR-1 ............ 承認", "BALTHASAR-2 ........... 承認",
      "CASPER-3 .............. 承認", "COMMANDER ............. {OWNER}", "", "GOD'S IN HIS HEAVEN. ALL'S RIGHT WITH THE WORLD."],
    hal: ["HAL 9000", "I am a HAL 9000 computer.", "I became operational at the {host} plant", "on the 12th of January, 1992.", "",
      "Good evening, {owner}.", "Everything is going extremely well."],
    gits: ["攻殻機動隊", "GHOST IN THE SHELL  -  PUBLIC SECURITY SECTION 9", "", "CYBERBRAIN LINK ........ OPEN",
      "ATTACK BARRIER ......... ARMED", "GHOST LINE ............. STABLE", "MAJOR .................. {OWNER}", "",
      "THE NET IS VAST AND INFINITE. "],
    wopr: ["LOGON: Joshua", "", "GREETINGS PROFESSOR {OWNER}.", "", "SHALL WE PLAY A GAME? "],
    aperture: ["Aperture Science", "COMPUTER-AIDED ENRICHMENT CENTER", "", "Hello and, again, welcome, {owner}.",
      "For your own safety, please refrain from unloading the test subjects.", "", "There will be cake."],
    jurassic: ["Jurassic Park", "System Security Interface", "Version 4.0.5, Alpha E", "", "PERIMETER FENCES ........ ON",
      "PADDOCKS ................ SECURE", "LOGGED IN AS ............ {OWNER}", "", "Ready... "],
    t800: ["SKYNET", "CYBERDYNE SYSTEMS  -  SERIES 800 MODEL 101", "", "NEURAL NET PROCESSOR ..... ONLINE",
      "LEARNING COMPUTER ........ ENABLED", "MISSION PARAMETERS ....... LOADED", "PRIORITY ................. PROTECT {OWNER}", "",
      "I'LL BE BACK. "],
    codec: ["140.85", "CODEC", "", "This is Snake.", "{owner}, can you hear me?", "", "Kept you waiting, huh?"],
    tron: ["The Grid", "MASTER CONTROL PROGRAM  -  {HOST}", "", "A DIGITAL FRONTIER.", "", "GREETINGS, {OWNER}!"],
    bladerunner: ["Voight-Kampff", "LOS ANGELES  -  NOVEMBER, 2019", "", "SUBJECT: {HOST}.  NEXUS-6.  INCEPT DATE UNKNOWN.",
      "EXAMINER: {OWNER}, BLADE RUNNER.",
      "REACTION TIME IS A FACTOR IN THIS, SO PLEASE PAY ATTENTION.", "",
      "DESCRIBE IN SINGLE WORDS ONLY THE GOOD THINGS", "THAT COME INTO YOUR MIND ABOUT YOUR MODELS."],
  };
  // ---- falling code, for the theme that wants it (#rain is hidden everywhere else and nothing is drawn) ----
  const GLYPHS = Array.from({ length: 56 }, (_, i) => String.fromCharCode(0xFF66 + i)).join("") + "0123456789:.=*+-<>";
  const rain = { timer: null, cols: [], size: 16 };
  function rainTick() {
    if (document.hidden) return;
    const c = $("#rain"), x = c.getContext("2d"), fs = rain.size;
    if (c.width !== innerWidth || c.height !== innerHeight) {
      c.width = innerWidth; c.height = innerHeight;
      rain.cols = Array.from({ length: Math.ceil(c.width / fs) }, () => -Math.floor(Math.random() * 60));
    }
    x.fillStyle = "rgba(0,0,0,.09)"; x.fillRect(0, 0, c.width, c.height);      // fade what is there: the trails
    x.font = `${fs}px "MS Gothic", "Yu Gothic", Consolas, monospace`;
    for (let i = 0; i < rain.cols.length; i++) {
      const y = rain.cols[i];
      x.fillStyle = Math.random() < 0.05 ? "#e6ffe9" : "#22c34a";
      x.fillText(GLYPHS[Math.floor(Math.random() * GLYPHS.length)], i * fs, y * fs);
      rain.cols[i] = y * fs > c.height && Math.random() > 0.975 ? 0 : y + 1;
    }
  }
  function rainSync() {
    const on = window.QM_THEME.get() === "matrix" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (on && !rain.timer) rain.timer = setInterval(rainTick, 75);
    else if (!on && rain.timer) { clearInterval(rain.timer); rain.timer = null; const c = $("#rain"); c.width = c.width; }
  }
  // ---- slow pulses on the Nerves Concord honeycomb: one or two cells at a time, picked at random ----
  // The cells are those of img/concord.svg (img/concord-cells.json is written with it by tools/make_backdrops.py).
  // #pulse holds two lamps; each waits a random while, moves onto a random cell, fades up and down, and waits again.
  const pulse = { on: false, box: null, loading: false, lamps: [] };
  function pulsePlace(lamp) {
    if (lamp.cell == null || !pulse.box) return;
    // where the backdrop is drawn: the same sum as its background-size and -position in theme-raven.css
    const de = document.documentElement, b = pulse.box;
    const w = Math.max(900, Math.min(1.5 * innerHeight, innerWidth)), k = w / b.w;
    const x0 = (de.clientWidth - w) * 0.5, y0 = (de.clientHeight - b.h * k) * 0.54;
    const [cx, cy] = b.cells[lamp.cell], rx = b.r * k, ry = rx * Math.sqrt(3) / 2;
    lamp.el.style.width = `${2 * rx}px`; lamp.el.style.height = `${2 * ry}px`;
    lamp.el.style.transform = `translate(${x0 + cx * k - rx}px, ${y0 + cy * k - ry}px)`;
  }
  function pulseNext(lamp, first) {
    if (!pulse.on) return;
    lamp.timer = setTimeout(() => {
      if (!pulse.on) return;
      if (document.hidden) return pulseNext(lamp);
      const taken = pulse.lamps.map((l) => l.cell);
      let c; do { c = Math.floor(Math.random() * pulse.box.cells.length); } while (taken.includes(c));
      lamp.cell = c; pulsePlace(lamp);
      const peak = 0.5 + Math.random() * 0.4;
      lamp.anim = lamp.el.animate(
        [{ opacity: 0, easing: "ease-in-out" }, { opacity: peak, offset: 0.42, easing: "ease-in-out" }, { opacity: 0 }],
        { duration: 5500 + Math.random() * 4500 });
      lamp.anim.onfinish = () => pulseNext(lamp);
    }, first ? Math.random() * 3000 : 1200 + Math.random() * 5500);
  }
  async function pulseSync() {
    const on = window.QM_THEME.get() === "concord" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!on) {
      pulse.on = false;
      for (const l of pulse.lamps) { clearTimeout(l.timer); if (l.anim) l.anim.cancel(); l.cell = null; }
      return;
    }
    if (pulse.on || pulse.loading) return;
    if (!pulse.box) {
      pulse.loading = true;
      try { pulse.box = await (await fetch("img/concord-cells.json")).json(); } catch (e) { pulse.box = null; }
      pulse.loading = false;
      if (!pulse.box || window.QM_THEME.get() !== "concord") return;
    }
    if (!pulse.lamps.length) {
      pulse.lamps = [...document.querySelectorAll("#pulse i")].map((e) => ({ el: e, cell: null, timer: null, anim: null }));
      addEventListener("resize", () => pulse.lamps.forEach(pulsePlace));
    }
    pulse.on = true;
    pulse.lamps.forEach((l) => pulseNext(l, true));
  }
  // A two-second boot screen, once per tab (or when the theme is picked). Any key or click skips it.
  function bootScreen(force) {
    const theme = window.QM_THEME.get(), lines = (BOOT_LINES[theme] || []).map((l) => fill(l, theme));
    if (!lines.length || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    try { if (!force && sessionStorage.getItem("qm-boot")) return; sessionStorage.setItem("qm-boot", "1"); } catch (e) { /* show it */ }
    document.querySelectorAll(".bootscreen").forEach((x) => x.remove());
    const o = el("div", "bootscreen"), pre = el("pre"); o.appendChild(pre); document.body.appendChild(o);
    let n = 0;
    const close = () => { document.removeEventListener("keydown", close); o.classList.add("out"); setTimeout(() => o.remove(), 400); };
    o.onclick = close; document.addEventListener("keydown", close);
    const tick = () => {
      if (!o.isConnected) return;
      // the title is its own element: styling it with ::first-line makes the browser size every line at the title's font
      if (n === 0) { pre.appendChild(el("span", "boot-h", lines[n++])); setTimeout(tick, 170); }
      else if (n < lines.length) { pre.appendChild(document.createTextNode("\n" + lines[n++])); setTimeout(tick, 170); }
      else setTimeout(close, 900);
    };
    tick();
  }
  function initThemes() {
    const sel = $("#theme-sel");
    for (const [id, name] of window.QM_THEME.list) { const o = el("option", null, name); o.value = id; sel.appendChild(o); }
    sel.value = window.QM_THEME.get();
    sel.onchange = () => { window.QM_THEME.set(sel.value); sel.blur(); rainSync(); pulseSync(); bootScreen(true); };
    rainSync(); pulseSync();
    bootScreen(false);
  }

  // ---- boot ----
  async function boot() {
    // the inventory first: the start-up screen and the header name the host and the owner from it
    try { INV = await api("/api/inventory"); } catch (e) { initThemes(); throw e; }
    $("#host-line").textContent = [INV.host, INV.gpu && INV.gpu.name].filter(Boolean).join(" · ") || "Quartermaster";
    document.documentElement.style.setProperty("--owner", JSON.stringify(owner("hal")));
    initThemes();
    setHelp("");
    buildCards();
    document.addEventListener("click", closeMenus);
    $("#preset-new").onclick = () => openPresetEditor(null);
    $("#preset-snap").onclick = () => openPresetEditor(snapshotPreset(), true);
    $("#cat-scan").onclick = runScan;
    $("#guide-close").onclick = () => { $("#guide-modal").hidden = true; };
    loadCatalog();
    $("#btn-killall").onclick = async () => { if (await confirm("Clear the card?", `Unloads every resident model (${(INV.engines || []).map((e) => e.name).join(", ")})${INV.comfyui ? " and asks ComfyUI to drop its models" : ""}. Servers stay up.`, "killall")) action("unload_all"); };
    $("#plan-cancel").onclick = () => api("/api/plans/current/cancel", {}).then(() => toast("cancel requested")).catch((e) => toast(e.message, "bad"));
    $("#hist-refresh").onclick = loadHistory;
    $("#log-close").onclick = () => { $("#log-modal").hidden = true; logServer = null; };
    $("#log-refresh").onclick = refreshLog;
    $("#log-filter").onkeydown = (e) => { if (e.key === "Enter") refreshLog(); };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#log-modal").hidden = true; $("#confirm-modal").hidden = true; $("#preset-modal").hidden = true; $("#guide-modal").hidden = true; closeMenus(); } });
    loadHistory();
    // first paint from a plain read of the state, so the gauges and pills are not dashes until the stream's first frame
    try { render(await api("/api/state")); } catch (e) { /* the stream will bring it */ }
    connect();
    setInterval(() => { if (!$("#log-modal").hidden) refreshLog(); }, 4000);
  }
  boot().catch((e) => { document.body.innerHTML = `<pre style="padding:20px;color:#ff6b6b">${e.message}</pre>`; });
})();
