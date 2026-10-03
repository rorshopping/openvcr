/* OpenVCR waitlist form + counter. No frameworks, no trackers. */
(function () {
  "use strict";

  var form = document.getElementById("waitlist-form");
  var success = document.getElementById("wl-success");
  var statusEl = document.getElementById("wl-status");
  var submitBtn = document.getElementById("wl-submit");
  var emailEl = document.getElementById("wl-email");
  var roleEl = document.getElementById("wl-role");
  var buildingEl = document.getElementById("wl-building");
  var hpEl = document.getElementById("wl-website");
  var positionEl = document.getElementById("wl-position");
  var countEl = document.getElementById("waitlist-count");

  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

  function setStatus(msg, isError) {
    statusEl.textContent = msg || "";
    statusEl.className = "form-status" + (isError ? " error" : "");
  }

  function alreadyJoined() {
    try { return localStorage.getItem("openvcr:waitlist"); } catch (e) { return null; }
  }

  function showSuccess(position) {
    form.hidden = true;
    success.hidden = false;
    if (position) positionEl.textContent = " — #" + position;
  }

  // Restore joined state within this browser.
  var saved = alreadyJoined();
  if (saved) {
    try { showSuccess(JSON.parse(saved).position); } catch (e) { /* fall through */ }
  }

  // Public counter (no PII).
  fetch("/api/waitlist").then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
    if (data && typeof data.count === "number" && data.count > 0) {
      countEl.textContent = data.count + " builders already on the list";
      countEl.hidden = false;
    }
  }).catch(function () { /* counter is decorative */ });

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var email = emailEl.value.trim();
    if (!EMAIL_RE.test(email)) {
      emailEl.setAttribute("aria-invalid", "true");
      setStatus("That email doesn't look right — mind checking it?", true);
      emailEl.focus();
      return;
    }
    emailEl.removeAttribute("aria-invalid");
    setStatus("");
    submitBtn.disabled = true;
    submitBtn.textContent = "Joining…";

    fetch("/api/waitlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: email,
        role: roleEl.value,
        building: buildingEl.value.trim(),
        website: hpEl.value, // honeypot: must stay empty
      }),
    }).then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) {
          var msg = (res.data && res.data.error && res.data.error.message) || "Something went wrong — please try again.";
          setStatus(msg, true);
          submitBtn.disabled = false;
          submitBtn.textContent = "Join the waitlist";
          return;
        }
        try { localStorage.setItem("openvcr:waitlist", JSON.stringify({ position: res.data.position })); } catch (e) {}
        showSuccess(res.data.position);
      })
      .catch(function () {
        setStatus("Network hiccup — please try again.", true);
        submitBtn.disabled = false;
        submitBtn.textContent = "Join the waitlist";
      });
  });
})();
