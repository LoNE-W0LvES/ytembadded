// Opens the link feeder tab by itself when the Course Library app is waiting for Udvash links, and
// closes a tab it opened once nothing has needed a link for a while. Nothing to set up: it asks the
// app on this PC (localhost), and the app says which Udvash course page to open.
const IDLE_CLOSE_MS = 10 * 60 * 1000;

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("check", { periodInMinutes: 0.5 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("check", { periodInMinutes: 0.5 }));
chrome.alarms.onAlarm.addListener(a => { if (a.name === "check") check(); });

// feeder.js says hello when it starts in a tab: remember where the app and the course page are.
chrome.runtime.onMessage.addListener((m, sender) => {
  if (m && m.feeder && sender.tab) chrome.storage.local.set({ app: m.app, start: m.start, tabId: sender.tab.id });
});

async function tabOpen(id) {
  if (id == null) return false;
  try { await chrome.tabs.get(id); return true; } catch { return false; }
}

// Where the app may be: the address a feeder tab last reported, else this PC on port 80 (what
// run_library.ps1 uses when it's free) or 8000.
async function findApp(saved) {
  for (const app of [...new Set([saved, "http://localhost", "http://localhost:8000"].filter(Boolean))]) {
    try { return { app, r: await (await fetch(app + "/api/feeder/wanted/", { cache: "no-store" })).json() }; }
    catch {}
  }
  return null;  // the app isn't running
}

async function check() {
  const s = await chrome.storage.local.get(["app", "start", "tabId", "autoOpened", "idleSince", "revivedAt"]);
  const found = await findApp(s.app);
  if (!found) return;
  const { app, r } = found;
  const start = r.start || s.start;  // the app says which Udvash course page to open
  const open = await tabOpen(s.tabId);
  if (r.needed && start) {
    await chrome.storage.local.set({ idleSince: 0 });
    const url = start + "#coursehub-feeder@" + app;
    if (!open) {
      const tab = await chrome.tabs.create({ url, active: false, pinned: true });
      await chrome.storage.local.set({ tabId: tab.id, autoOpened: true, revivedAt: Date.now() });
    } else if ((r.feeder_ago == null || r.feeder_ago > 90) && Date.now() - (s.revivedAt || 0) > 120000) {
      // The tab is there but its feeder has gone quiet (e.g. the extension was reloaded, which stops
      // the copy running in open tabs): replace the tab so a fresh feeder starts. (Setting the same
      // address again would only be a #hash change, which doesn't reload the page.)
      await chrome.tabs.remove(s.tabId).catch(() => {});
      const tab = await chrome.tabs.create({ url, active: false, pinned: true });
      await chrome.storage.local.set({ tabId: tab.id, autoOpened: true, revivedAt: Date.now() });
    }
  } else if (!r.needed && open && s.autoOpened) {
    if (!s.idleSince) await chrome.storage.local.set({ idleSince: Date.now() });
    else if (Date.now() - s.idleSince > IDLE_CLOSE_MS) {
      await chrome.tabs.remove(s.tabId).catch(() => {});
      await chrome.storage.local.set({ tabId: null, autoOpened: false, idleSince: 0 });
    }
  }
}
