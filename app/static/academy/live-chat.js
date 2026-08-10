/**
 * Live Academy thread: poll for new messages + send without full page reload.
 * Expects window.AcademyLiveChat = { pollUrl, sendUrl, csrf, meRole, afterId }
 */
(function () {
  const cfg = window.AcademyLiveChat;
  if (!cfg || !cfg.pollUrl || !cfg.sendUrl) return;

  const thread = document.getElementById("live-thread");
  const form = document.getElementById("live-form");
  const input = document.getElementById("live-body");
  const status = document.getElementById("live-status");
  const empty = document.getElementById("live-empty");
  if (!thread || !form || !input) return;

  let afterId = Number(cfg.afterId || 0);
  let busy = false;
  const meRole = cfg.meRole || "learner";
  const POLL_MS = 3000;

  function setStatus(text, isError) {
    if (!status) return;
    status.textContent = text || "";
    status.classList.toggle("is-error", !!isError);
  }

  function appendMessage(msg, animate) {
    if (!msg || !msg.id) return;
    if (thread.querySelector('[data-msg-id="' + msg.id + '"]')) return;
    if (empty) empty.style.display = "none";
    const mine = msg.sender_role === meRole;
    const el = document.createElement("div");
    el.className = "bubble " + (mine ? "me" : "them") + (animate ? " is-new" : "");
    el.setAttribute("data-msg-id", String(msg.id));
    el.textContent = msg.body || "";
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = (msg.sender_role || "") + " · " + (msg.created_label || "");
    el.appendChild(when);
    thread.appendChild(el);
    afterId = Math.max(afterId, Number(msg.id) || 0);
    thread.scrollTop = thread.scrollHeight;
  }

  async function poll() {
    if (document.hidden) return;
    try {
      const url = cfg.pollUrl + (cfg.pollUrl.indexOf("?") >= 0 ? "&" : "?") + "after_id=" + afterId;
      const resp = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!resp.ok) return;
      const data = await resp.json();
      (data.messages || []).forEach(function (m) {
        appendMessage(m, true);
      });
      if ((data.messages || []).length) setStatus("Live · synced", false);
    } catch (e) {
      /* keep polling */
    }
  }

  async function send(e) {
    e.preventDefault();
    if (busy) return;
    const body = (input.value || "").trim();
    if (!body) {
      setStatus("Write a message first.", true);
      input.focus();
      return;
    }
    busy = true;
    setStatus("Sending…", false);
    const btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch(cfg.sendUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "X-CSRFToken": cfg.csrf || "",
        },
        credentials: "same-origin",
        body: JSON.stringify({ body: body }),
      });
      const data = await resp.json().catch(function () {
        return {};
      });
      if (!resp.ok) {
        setStatus(data.error || "Could not send.", true);
      } else {
        appendMessage(data.message, true);
        input.value = "";
        setStatus("Live · sent", false);
      }
    } catch (err) {
      setStatus("Network error — try again.", true);
    } finally {
      busy = false;
      if (btn) btn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", send);
  thread.scrollTop = thread.scrollHeight;
  setStatus("Live · connected", false);
  setInterval(poll, POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });
})();
