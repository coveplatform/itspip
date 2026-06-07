/* Cashew — front-end joy + the freemium dig flow */
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

  // A coin flips up with "+$50" each time the scan unearths a stash — small
  // dopamine hits during the dig. The big confetti burst stays reserved for unlock.
  function coinPop(amount, cur) {
    const layer = $("#coin-pops");
    if (!layer) return;
    const el = document.createElement("div");
    el.className = "coin-pop";
    el.innerHTML =
      '<svg viewBox="0 0 40 40" width="34" height="34"><use href="#coin"/></svg>' +
      '<span>+' + money(amount, cur) + "</span>";
    el.style.left = 38 + Math.random() * 24 + "%";
    layer.appendChild(el);
    setTimeout(() => el.remove(), 1500);
  }

  /* ============================================================
     The dig flow
     ============================================================ */
  let CURRENT = null; // { scan_id, ... }
  let CHOSEN = "pro"; // default-selected tier (the upsell)

  const SCAN_MSGS = [
    "Cashew is rummaging…",
    "Sniffing out forgotten cards…",
    "Digging behind the spam…",
    "Counting the acorns…",
    "Almost got it…",
  ];

  // Whimsical, ever-changing dig verbs (à la Claude's playful loaders) — squirrel-themed.
  const DIG_WORDS = [
    "Snuffling…", "Burrowing…", "Rummaging…", "Unearthing…", "Sniffing out cards…",
    "Digging deeper…", "Sorting acorns…", "Nuzzling the spam…", "Pawing through receipts…",
    "Squirrelling…", "Following the money-scent…", "Acorn-quidating…", "Excavacorn-ing…",
    "Tail-flicking through inbox…", "Counting the stash…",
  ];

  // Fade the scan message out, swap text, fade back in.
  function swapMsg(el, text) {
    if (!el) return;
    el.classList.add("dim");
    setTimeout(() => { el.textContent = text; el.classList.remove("dim"); }, 220);
  }

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
        toast(err.message || "Cashew got distracted by a nut. Try again?");
      });
  }

  // Deep scan of the whole inbox with a live progress bar.
  function startScan(email) {
    const overlay = $("#scanning");
    const msg = $("#scan-msg");
    const prog = $("#scan-progress");
    const bar = $("#scan-bar");
    const stats = $("#scan-stats");
    const tally = $("#scan-tally");
    const tallyAmt = $("#scan-tally-amount");
    overlay.hidden = false;
    prog.hidden = false;
    if (tally) tally.hidden = true;
    if (tallyAmt) tallyAmt.textContent = "$0.00";
    bar.style.width = "0%";
    stats.textContent = "Starting…";

    let popped = 0;       // finds already celebrated
    let runTotal = 0;     // running unearthed total

    // rotate the whimsical dig verbs with a fade until the scan finishes
    let wi = 0;
    msg.textContent = DIG_WORDS[wi++];
    const wordTimer = setInterval(() => {
      swapMsg(msg, DIG_WORDS[wi++ % DIG_WORDS.length]);
    }, 2800);
    function stopWords() { clearInterval(wordTimer); }

    fetch("/api/scan/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email || "", source: "gmail" }),
    })
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "couldn't start the dig");
        return d;
      })
      .then((d) => poll(d.job_id))
      .catch((err) => {
        stopWords();
        overlay.hidden = true;
        prog.hidden = true;
        toast(err.message || "Cashew couldn't start digging.");
      });

    function poll(jobId) {
      fetch("/api/scan/progress/" + jobId)
        .then((r) => r.json())
        .then((p) => {
          const total = p.total || 0;
          const scanned = p.scanned || 0;
          const pct = total
            ? Math.min(99, Math.round((scanned / total) * 100))
            : Math.min(95, Math.round(scanned / 40) * 5);
          bar.style.width = pct + "%";

          // New stashes since last poll → pop a coin + climb the tally.
          const finds = p.finds || [];
          if (finds.length > popped) {
            for (let k = popped; k < finds.length; k++) {
              const f = finds[k];
              coinPop(f.amount, f.currency);
              runTotal += f.amount || 0;
            }
            popped = finds.length;
            if (tally) tally.hidden = false;
            if (tallyAmt) countUp(tallyAmt, runTotal, true);
          }

          const foundTxt =
            "found <b>" + p.found + "</b> stash" + (p.found === 1 ? "" : "es");
          stats.innerHTML = total
            ? "Dug through <b>" + scanned.toLocaleString() + "</b> of ~" +
              total.toLocaleString() + " emails · " + foundTxt
            : "Dug through <b>" + scanned.toLocaleString() + "</b> emails · " + foundTxt;

          if (p.done) {
            if (p.error) {
              stopWords();
              overlay.hidden = true;
              prog.hidden = true;
              toast("Scan hiccup: " + p.error);
              return;
            }
            stopWords();
            bar.style.width = "100%";
            msg.textContent = "Done! Tallying your stash…";
            fetch("/api/scan/result/" + p.scan_id)
              .then((r) => r.json())
              .then((data) => {
                overlay.hidden = true;
                prog.hidden = true;
                renderTeaser(data);
              })
              .catch(() => {
                overlay.hidden = true;
                prog.hidden = true;
                toast("Couldn't load results — try again?");
              });
            return;
          }
          setTimeout(() => poll(jobId), 900);
        })
        .catch(() => setTimeout(() => poll(jobId), 1500));
    }
  }

  // meta chips: code present / expiry
  function chips(item) {
    let c = "";
    if (item.code_present) c += '<span class="rc-chip code">✉️ code inside</span>';
    if (item.expires_text) c += '<span class="rc-chip exp">⏰ ' + item.expires_text + "</span>";
    return c ? '<div class="rc-meta">' + c + "</div>" : "";
  }

  // action footer: open the email + check balance
  function actions(item) {
    const open = item.link
      ? '<a class="rc-btn primary" href="' + item.link + '" target="_blank" rel="noopener">Open email</a>'
      : "";
    const bal = item.balance_url
      ? '<a class="rc-btn ghost" href="' + item.balance_url + '" target="_blank" rel="noopener">Check balance</a>'
      : "";
    return open || bal ? '<div class="rc-actions">' + open + bal + "</div>" : "";
  }

  function teaserCard(item, isFree) {
    if (isFree) {
      return (
        '<div class="rcard free">' +
        '<div class="rc-top"><span class="rc-kind">' + (KIND[item.kind] || item.kind) + "</span>" +
          '<span class="free-flag">FREE 🎁</span></div>' +
        '<div class="rc-amount">' + money(item.amount, item.currency) + "</div>" +
        '<div class="rc-brand">' + item.brand + "</div>" +
        chips(item) +
        '<p class="rc-redeem">' + item.redeem + "</p>" +
        actions(item) +
        "</div>"
      );
    }
    // locked
    return (
      '<div class="rcard locked">' +
      '<div class="rc-top"><span class="rc-kind">' + (KIND[item.kind] || item.kind) + "</span>" +
        '<span class="lock-chip"><svg viewBox="0 0 40 40"><use href="#paw"/></svg></span></div>' +
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
        "Cashew dug around but came up empty this time";
      $("#res-total").parentElement.style.display = "none";
      $("#res-sub").innerHTML =
        data.source === "gmail"
          ? "Your inbox is squeaky clean (or your stash is hiding under a different search). Cashew will keep watch! 🐿️"
          : "Nothing here — try the sample dig to see Cashew in action.";
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
    return (
      '<div class="rcard">' +
      '<div class="rc-top"><span class="rc-kind">' + (KIND[item.kind] || item.kind) + "</span></div>" +
      '<div class="rc-amount">' + money(item.amount, item.currency) + "</div>" +
      '<div class="rc-brand">' + item.brand + "</div>" +
      chips(item) +
      '<p class="rc-redeem">' + item.redeem + "</p>" +
      actions(item) +
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
      ? "Every brand revealed below — and Cashew's now watching your inbox for new stashes. 🐿️"
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
      formError(form, "pop in your email so Cashew knows where to dig 🐿️");
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
      startScan(email);
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
    startScan(email);
  } else if (params.get("gmail") === "unconfigured") {
    history.replaceState({}, "", location.pathname);
    toast("Gmail isn't set up on this server yet — see GMAIL_SETUP.md.");
  } else if (params.get("gmail") === "error") {
    history.replaceState({}, "", location.pathname);
    toast("That didn't go through — want to try again?");
  }
})();
