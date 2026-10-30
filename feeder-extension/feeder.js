// Link feeder. Runs in a Udvash page marked #coursehub-feeder@<app address>: the app's own hidden
// browser (web/library/portal.py) starts it there, or the optional Chrome extension does. Every few seconds it asks the app which PDFs need a fresh
// signed link, reads those note cards with your logged-in session, and sends the links to the app.
(() => {
  if (!location.hash.includes("coursehub-feeder")) return;

  // The app can run on another PC on the home network; the Downloads page passes its address.
  const m = location.hash.match(/coursehub-feeder@(https?:\/\/[^#&\s]+)/);
  const APP = m ? m[1].replace(/\/$/, "") : "http://localhost:8000";
  const START = location.href.split("#")[0];
  // Tell the extension's background part where the app and this course page are, so it can reopen
  // this tab by itself next time the app needs links (background.js).
  try { chrome.runtime.sendMessage({ feeder: true, app: APP, start: START }); } catch {}
  // Started by the extension (not the app's browser): it can be reloaded, leaving this copy behind.
  const viaExtension = !!globalThis.chrome?.runtime?.id;
  const replaced = () => viaExtension && !globalThis.chrome?.runtime?.id;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const card = {};  // portal content id -> its note card page (filled at start and by Load course)
  const uniq = a => [...new Set(a)];
  const getDoc = async u => {
    const res = await fetch(u, { cache: "no-store" });
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    doc.landedOn = new URL(res.url).pathname;  // where Udvash actually sent us (a redirect to its login, say)
    return doc;
  };
  // Why a course page has no subjects: an empty course is still the subject page; anything else
  // (the login page, a "logged in on another device" notice) is a problem to show, not an empty course.
  const notCoursePage = doc => {
    if (doc.querySelector('input[type="password"]') || /login|signin|account/i.test(doc.landedOn))
      return "Chrome isn't logged in to Udvash on this PC — log in there and press Load course again";
    if (doc.landedOn.toLowerCase() !== "/content/contentsubject")
      return `Udvash showed "${textOf(doc.querySelector("title")) || "a page"}" (${doc.landedOn}) instead of the course — ` +
             "open the course in that Chrome, check it's your course and you're logged in, then press Load course again";
    return "";
  };
  const hrefs = (doc, path) => [...doc.querySelectorAll("a[href]")].map(a => a.getAttribute("href")).filter(h => h.split("?")[0] === path);
  const linkIn = doc => [...doc.querySelectorAll('[onclick*="forceDownload"]')]
    .map(e => (e.getAttribute("onclick").match(/forceDownload\(\s*['"]([^'"]+)['"]/) || [])[1]).find(Boolean);
  // A video card's player lists every resolution in matching comma-separated attributes, e.g.
  // data-all-resolution="720,480" and data-all-video-source="<720p link>,<480p link>". Best first.
  const videoSources = doc => [...doc.querySelectorAll("[data-all-video-source]")].flatMap(el => {
    const urls = el.getAttribute("data-all-video-source").split(",").map(s => s.trim());
    const res = (el.getAttribute("data-all-resolution") || "").split(",").map(Number);
    return urls.map((u, i) => ({ u, r: res[i] || 0 })).filter(x => /^https:\/\//.test(x.u))
      .sort((a, b) => b.r - a.r).slice(0, 1).map(x => x.u);
  });
  async function pool(items, n, fn) {
    const out = []; let i = 0;
    await Promise.all(Array.from({ length: n }, async () => {
      while (i < items.length) { const k = i++; try { out[k] = await fn(items[k]); } catch (e) { setStatus("⚠ " + e); } }
    }));
    return out;
  }

  // The YouTube id a video card's player carries, if the class is on YouTube.
  const youtubeIdIn = doc => {
    const id = ((doc.querySelector("[data-all-video-source][data-youtube-video]") || { getAttribute: () => "" })
      .getAttribute("data-youtube-video") || "").trim();
    return /^[\w-]{11}$/.test(id) ? id : "";
  };
  // A card in Bangla first, then the same card in English.
  const bothLanguages = h => {
    const bn = /ln=(Bn|En)/.test(h) ? h.replace(/ln=(Bn|En)/, "ln=Bn") : h + (h.includes("?") ? "&" : "?") + "ln=Bn";
    return [bn, bn.replace("ln=Bn", "ln=En")];
  };
  const param = (h, k) => new URLSearchParams(h.split("?")[1] || "").get(k);
  const textOf = el => {
    if (!el) return "";
    const c = el.cloneNode(true);
    c.querySelectorAll("style,script").forEach(x => x.remove());
    return c.textContent.replace(/\s+/g, " ").trim();
  };
  const linksTo = (doc, path) => {
    const seen = new Set();
    return [...doc.querySelectorAll("a[href]")]
      .map(a => ({ href: a.getAttribute("href"), name: textOf(a) }))
      .filter(l => l.href.split("?")[0] === path && !seen.has(l.href) && seen.add(l.href));
  };

  // "⟳ Load course" in the app: read every lecture card of the course and send what they say.
  // Cards the app already knows only need their place in the course; new ones also get their
  // YouTube id (from the video card's player) and a fresh note link.
  async function loadCourse(job) {
    const known = new Set(job.known);
    const send = body => fetch(APP + "/api/course/feed/", { method: "POST", headers: { "Content-Type": "application/json" },
                                                            body: JSON.stringify(body) });
    try {
      setStatus("loading the course: finding its class lists…");
      const startDoc = await getDoc(job.start);
      const subjects = linksTo(startDoc, "/Content/ContentChapter");
      if (!subjects.length) {
        const problem = notCoursePage(startDoc);
        if (problem) throw new Error(problem);
        await send({ done: true, done_types: 0, total_types: 0 });
        return;
      }
      const lists = [];  // one per class type of each chapter, in course order
      for (const s of subjects) {
        const chapters = linksTo(await getDoc(s.href), "/Content/DisplayContentType");
        const perChapter = await pool(chapters, 4, async c =>
          linksTo(await getDoc(c.href), "/Content/DisplayContentCard")
            .map(t => ({ href: t.href, subject: s.name, chapter: c.name, type: t.name.replace(/\s*\(\d+\)\s*$/, "") })));
        perChapter.forEach(l => lists.push(...(l || [])));
      }
      let done = 0;
      for (const list of lists) {
        if (replaced()) return;  // the extension was reloaded: the new copy takes over
        const doc = await getDoc(list.href);
        const boxes = [...doc.querySelectorAll(".card")].filter(c => c.querySelector('a[href^="/Content/DisplayContentCardDetails"]'));
        const lectures = (await pool(boxes, 3, async box => {
          const links = [...box.querySelectorAll('a[href^="/Content/DisplayContentCardDetails"]')].map(a => a.getAttribute("href"));
          const video = links.find(h => param(h, "contentButtonType") === "video");
          const note = links.find(h => param(h, "contentButtonType") === "note");
          const id = param(video || note, "masterContentId");
          if (!id) return null;
          if (note) card[id] = note;
          const item = { content_id: id, subject: list.subject, chapter: list.chapter, class_type: list.type,
                         title: [textOf(box.querySelector(".subject-name")), textOf(box.querySelector(".uuu-wrap-title")),
                                 textOf(box.querySelector(".card-body .content"))].filter(Boolean).join(" ") };
          if (!known.has(id)) {
            if (video) {
              item.youtube_id = youtubeIdIn(await getDoc(bothLanguages(video)[0]));
              item.video_card = video;
            }
            if (note) for (const c of bothLanguages(note)) if (!item.note_url) item.note_url = linkIn(await getDoc(c)) || "";
          }
          return item;
        })).filter(Boolean);
        done++;
        await send({ lectures, done_types: done, total_types: lists.length });
        setStatus(`loading the course: ${done} of ${lists.length} class lists`);
      }
      await send({ done: true, done_types: done, total_types: lists.length });
    } catch (e) {
      await send({ error: String(e.message || e) }).catch(() => {});
    }
  }

  // Small badge on the page so it's obvious this tab is the feeder.
  const badge = document.createElement("div");
  badge.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:99999;background:#18181b;color:#ececf1;" +
    "border:1px solid #3b82f6;border-radius:8px;padding:8px 12px;font:13px system-ui,sans-serif;box-shadow:0 4px 16px #0006";
  document.body.appendChild(badge);
  const setStatus = t => { badge.textContent = "🔗 Course Library link feeder · " + t + " · keep this tab open"; };
  document.title = "🔗 Link feeder · " + document.title;

  (async () => {
    // 1. Map portal content id -> its note card page, starting from the course's subject list.
    setStatus("finding note cards…");
    // Keeps going even when this finds nothing, so a Load course still gets its answer (and reason).
    const subjects = uniq(hrefs(await getDoc(START), "/Content/ContentChapter"));
    const chapters = uniq((await pool(subjects, 3, async u => hrefs(await getDoc(u), "/Content/DisplayContentType"))).flat());
    const types = uniq((await pool(chapters, 4, async u => hrefs(await getDoc(u), "/Content/DisplayContentCard"))).flat());
    (await pool(types, 4, async u => hrefs(await getDoc(u), "/Content/DisplayContentCardDetails"))).flat().forEach(h => {
      const p = new URLSearchParams(h.split("?")[1]);
      if (p.get("contentButtonType") === "note") card[p.get("masterContentId")] = h;
    });
    const cards = Object.keys(card).length;

    // 2. Feed links on demand.
    let sent = 0;
    setStatus(`running · ${cards} note cards · 0 links sent`);
    for (;;) {
      // Reloading or updating the extension leaves this old copy running in the tab, still able to
      // reach the app; step aside so the new copy (in a new tab) does the work.
      if (replaced()) { setStatus("⚠ replaced by a newer version — this tab can be closed"); return; }
      try {
        const job = await (await fetch(APP + "/api/course/feed/")).json();
        if (job.load) await loadCourse(job);
        const { content_ids } = await (await fetch(APP + "/api/note-links/needed/")).json();
        const ids = content_ids.filter(id => card[id]);
        if (ids.length) {
          // Both language cards: the app keeps the Bangla link if it works, else the English one.
          const links = (await pool(ids, 3, async id => {
            const urls = [];
            for (const c of bothLanguages(card[id])) urls.push(linkIn(await getDoc(c)));
            return { content_id: id, urls: uniq(urls.filter(Boolean)) };
          })).filter(l => l && l.urls.length);
          if (links.length) {
            await fetch(APP + "/api/note-links/", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ links }) });
            sent += links.length;
          }
        }
        // Portal-only videos: the app names the video card. The card's Download button is the note PDF;
        // the video is the .mp4 its player loads. Bangla card first, English as the fallback (they're
        // often on different storage servers); the app keeps the first link that works.
        const { videos } = await (await fetch(APP + "/api/video-links/")).json();
        const links = [];
        for (const v of videos) {
          const found = [];
          let youtube_id = "";
          for (const c of bothLanguages(v.card)) {
            const doc = await getDoc(c);
            found.push(...videoSources(doc));
            youtube_id = youtube_id || youtubeIdIn(doc);
          }
          if (found.length || youtube_id) links.push({ pk: v.pk, urls: uniq(found), youtube_id });
        }
        if (links.length) {
          await fetch(APP + "/api/video-links/", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ links }) });
          sent += links.length;
        }
        setStatus(`running · ${cards} note cards · ${sent} links sent · last check ${new Date().toLocaleTimeString()}`);
      } catch (e) {
        setStatus("⚠ can't reach the app at " + APP + " — is it running? (" + e.message + ")");
      }
      await sleep(5000);
    }
  })();
})();
