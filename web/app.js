/* Pip — front-end joy + the freemium dig flow */
(function () {
  "use strict";

  /* ---- scroll reveals ---- */
  const obs = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          obs.unobserve(e.target);
        }
      });
    },
    { threshold: 0.15 }
  );
  document.querySelectorAll(".reveal").forEach((el) => obs.observe(el));

  /* ---- helpers ---- */
  const $ = (s) => document.querySelector(s);
  const money = (n, cur) => {
    const sym = { USD: "$", GBP: "£", EUR: "€" }[cur] || "$";
    return sym + Number(n).toFixed(2);
  };
  const KIND = {
    gift_card: "Gift card",
    store_credit: "Store credit",
    referral_reward: "Referral reward",
    coupon: "Coupon",
  };

  /* ---- animated counter ---- */
  function countUp(el, target, decimals) {
    if (!el) return;
    const dur = 1100, start = performance.now();
    function tick(now) {
      const p = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      const val = target * eased;
      el.textContent = decimals ? val.toFixed(2) : Math.round(val).toLocaleString();
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  const diggerEl = $("#digger-count");
  fetch("/api/stats")
    .then((r) => r.json())
    .then((d) => {
      countUp(diggerEl, d.diggers || 0);
      countUp($("#avg-stash"), d.avg_stash || 175);
    })
    .catch(() => { if (diggerEl) diggerEl.textContent = "1,283"; });

  /* ---- coin confetti ---- */
  function celebrate(n) {
    for (let i = 0; i < (n || 16); i++) {
      const c = document.createElement("div");
      c.className = "confetti";
      c.innerHTML = '<svg viewBox="0 0 40 40" width="22" height="22"><use href="#coin"/></svg>';
      c.style.left = Math.random() * 100 + "vw";
      c.style.animationDuration = 1.6 + Math.random() * 1.4 + "s";
      c.style.animationDelay = Math.random() * 0.3 + "s";
      c.style.width = 14 + Math.random() * 16 + "px";
      document.body.appendChild(c);
      setTimeout(() => c.remove(), 3400);
    }
  }

  /* ============================================================
     The dig flow
     ============================================================ */
  let CURRENT = null; // { scan_id, ... }
  let CHOSEN = "pro"; // default-selected tier (the upsell)

  const SCAN_MSGS = [
    "Pip is rummaging…",
    "Sniffing out forgotten cards…",
    "Digging behind the spam…",
    "Counting the acorns…",
    "Almost got it…",
  ];

  function runScan(email, source) {
    const overlay = $("#scanning");
    const msg = $("#scan-msg");
    overlay.hidden = false;
    let i = 0;
    msg.textContent = SCAN_MSGS[0];
    const cycle = setInterval(() => {
      i = (i + 1) % SCAN_MSGS.length;
      msg.textContent = SCAN_MSGS[i];
    }, 520);

    const minWait = new Promise((res) => setTimeout(res, 2000));
    const req = fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email || "", source: source || "demo" }),
    }).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "scan failed");
      return data;
    });

    Promise.all([req, minWait])
      .then(([data]) => {
        clearInterval(cycle);
        overlay.hidden = true;
        renderTeaser(data);
        if (data.diggers && diggerEl) diggerEl.textContent = Number(data.diggers).toLocaleString();
      })
      .catch((err) => {
        clearInterval(cycle);
        overlay.hidden = true;
        toast(err.message || "Pip got distracted by a nut. Try again?");
      });
  }

  function teaserCard(item, isFree) {
    if (isFree) {
      const code = item.code_present
        ? '<span class="rc-code">✉️ code in your email</span>'
        : "";
      return (
        '<div class="rcard free">' +
        '<span class="free-flag">FREE 🎁</span>' +
        '<div class="rc-kind">' + (KIND[item.kind] || item.kind) + "</div>" +
        '<div class="rc-amount">' + money(item.amount, item.currency) + "</div>" +
        '<div class="rc-brand">' + item.brand + "</div>" +
        '<div class="rc-redeem">' + item.redeem + "</div>" +
        code +
        "</div>"
      );
    }
    // locked
    return (
      '<div class="rcard locked">' +
      '<span class="lock-chip"><svg viewBox="0 0 40 40"><use href="#paw"/></svg></span>' +
      '<div class="rc-kind">' + (KIND[item.kind] || item.kind) + "</div>" +
      '<div class="rc-amount">' + money(item.amount, item.currency) + "</div>" +
      '<div class="rc-brand-blur"></div>' +
      '<div class="rc-redeem-blur"></div>' +
      '<div class="rc-redeem-blur short"></div>' +
      "</div>"
    );
  }

  function renderTeaser(data) {
    CURRENT = data;
    const sec = $("#results");
    sec.hidden = false;

    // sample vs real-inbox framing
    const tag = sec.querySelector(".sample-tag");
    if (tag) tag.style.display = data.source === "gmail" ? "none" : "";

    if (data.empty || data.count === 0) {
      $("#result-grid").innerHTML = "";
      $("#paywall").style.display = "none";
      $("#unlocked-banner").hidden = true;
      sec.querySelector(".results-head h2").textContent =
        "Pip dug around but came up empty this time";
      $("#res-total").parentElement.style.display = "none";
      $("#res-sub").innerHTML =
        data.source === "gmail"
          ? "Your inbox is squeaky clean (or your stash is hiding under a different search). Pip will keep watch! 🐿️"
          : "Nothing here — try the sample dig to see Pip in action.";
      sec.scrollIntoView({ behavior: "smooth" });
      return;
    }
    $("#res-total").parentElement.style.display = "";

    $("#res-count").textContent = data.count;
    $("#res-locked-n").textContent = data.locked.length;
    countUp($("#res-total"), data.total, true);

    const lockedTotal = data.locked.reduce((s, i) => s + (i.amount || 0), 0);
    $("#pw-locked-total").textContent = lockedTotal.toFixed(2);
    $("#pw-locked-count").textContent = data.locked.length;

    // cards: free first, then locked
    const grid = $("#result-grid");
    let html = teaserCard(data.free, true);
    data.locked.forEach((i) => (html += teaserCard(i, false)));
    grid.innerHTML = html;

    // tiers
    renderTiers(data.tiers);

    // reset paywall/banner state
    $("#paywall").style.display = "";
    $("#unlocked-banner").hidden = false;
    $("#unlocked-banner").classList.remove("show");

    sec.scrollIntoView({ behavior: "smooth" });
  }

  function renderTiers(tiers) {
    const order = ["once", "pro", "forever"];
    const wrap = $("#tiers");
    wrap.innerHTML = order
      .map((key) => {
        const t = tiers[key];
        if (!t) return "";
        const rec = t.recommended;
        return (
          '<div class="tier' + (rec ? " rec" : "") + (key === CHOSEN ? " sel" : "") +
          '" data-tier="' + key + '">' +
          (rec ? '<span class="rec-badge">most diggers 🐿️</span>' : "") +
          '<div class="t-name">' + t.label + "</div>" +
          '<div class="t-price">' + t.price + "</div>" +
          '<div class="t-blurb">' + t.blurb + "</div>" +
          "</div>"
        );
      })
      .join("");
    wrap.querySelectorAll(".tier").forEach((el) => {
      el.addEventListener("click", () => {
        CHOSEN = el.dataset.tier;
        wrap.querySelectorAll(".tier").forEach((t) => t.classList.remove("sel"));
        el.classList.add("sel");
      });
    });
  }

  function unlock() {
    if (!CURRENT) return;
    const btn = $("#unlock-btn");
    btn.disabled = true;
    btn.textContent = "unlocking…";
    fetch("/api/unlock/" + CURRENT.scan_id, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tier: CHOSEN }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (!data.ok) throw new Error();
        revealAll(data);
      })
      .catch(() => {
        btn.disabled = false;
        btn.innerHTML = 'Unlock everything<svg class="btn-paw" viewBox="0 0 40 40"><use href="#paw"/></svg>';
        alert("Payment hiccup — try again?");
      });
  }

  function fullCard(item) {
    const code = item.code_present
      ? '<span class="rc-code">✉️ code in your email</span>'
      : "";
    const exp = item.expires_text
      ? '<span class="rc-code">⏰ expires ' + item.expires_text + "</span>"
      : "";
    return (
      '<div class="rcard">' +
      '<div class="rc-kind">' + (KIND[item.kind] || item.kind) + "</div>" +
      '<div class="rc-amount">' + money(item.amount, item.currency) + "</div>" +
      '<div class="rc-brand">' + item.brand + "</div>" +
      '<div class="rc-redeem">' + item.redeem + "</div>" +
      code + exp +
      "</div>"
    );
  }

  function revealAll(data) {
    const grid = $("#result-grid");
    grid.innerHTML = data.items
      .slice()
      .sort((a, b) => (b.amount || 0) - (a.amount || 0))
      .map(fullCard)
      .join("");

    $("#paywall").style.display = "none";
    const banner = $("#unlocked-banner");
    banner.hidden = false;
    banner.classList.add("show");
    $("#ub-note").textContent = data.monitoring
      ? "Every brand revealed below — and Pip's now watching your inbox for new stashes. 🐿️"
      : "Every brand + how to claim it is revealed below.";
    $("#res-sub").innerHTML =
      "All <b>" + data.items.length + "</b> stashes unlocked — go spend " +
      money(data.total, data.currency) + " you forgot you had!";

    celebrate(28);
  }

  $("#unlock-btn") && $("#unlock-btn").addEventListener("click", unlock);

  /* ---- toast ---- */
  function toast(text) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = text;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => t.remove(), 400);
    }, 4600);
  }

  function formError(form, msg) {
    const old = form.parentElement.querySelector(".form-error");
    if (old) old.remove();
    const p = document.createElement("p");
    p.className = "form-error";
    p.textContent = msg;
    form.insertAdjacentElement("afterend", p);
  }

  /* ---- the one real flow: email → connect Gmail → scan ---- */
  async function startDig(form) {
    const input = form.querySelector('input[type="email"]');
    const btn = form.querySelector("button");
    const email = (input.value || "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      formError(form, "pop in your email so Pip knows where to dig 🐿️");
      input.focus();
      return;
    }
    try { localStorage.setItem("pip_email", email); } catch (e) {}
    // capture the lead immediately (survives if they bail at consent)
    fetch("/api/waitlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    }).catch(() => {});

    btn.disabled = true;
    btn.textContent = "connecting…";

    // already connected this session? scan straight away. otherwise → consent.
    let connected = false;
    try {
      const m = await (await fetch("/api/me")).json();
      connected = m.connected;
      if (!m.configured && !connected) {
        btn.disabled = false;
        btn.innerHTML = digLabel();
        toast("Gmail isn't set up on this server yet — set GOOGLE_CLIENT_ID/SECRET (GMAIL_SETUP.md).");
        return;
      }
    } catch (e) {}

    if (connected) {
      runScan(email, "gmail");
      btn.disabled = false;
      btn.innerHTML = digLabel();
    } else {
      window.location.href = "/auth/google/start";
    }
  }

  function digLabel() {
    return 'Dig up my money<svg class="btn-paw" viewBox="0 0 40 40"><use href="#paw"/></svg>';
  }

  function wireDig(form) {
    if (!form) return;
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      startDig(form);
    });
  }
  wireDig($("#hero-form"));
  wireDig($("#cta-form"));

  /* ---- returning from Google consent → auto-scan ---- */
  const params = new URLSearchParams(location.search);
  if (params.get("connected") === "1") {
    history.replaceState({}, "", location.pathname);
    let email = "";
    try { email = localStorage.getItem("pip_email") || ""; } catch (e) {}
    toast("Gmail connected! 🐿️ Digging through your inbox…");
    runScan(email, "gmail");
  } else if (params.get("gmail") === "unconfigured") {
    history.replaceState({}, "", location.pathname);
    toast("Gmail isn't set up on this server yet — see GMAIL_SETUP.md.");
  } else if (params.get("gmail") === "error") {
    history.replaceState({}, "", location.pathname);
    toast("That didn't go through — want to try again?");
  }
})();
