"use strict";

// Saves hiring.cafe search result pages as the browser receives them, for
// `hiringcafe-toolkit job-shortlist import`.
//
// It only watches. Paging is done by the person at the keyboard - by clicking
// "Next page" or pressing Alt+N, which clicks it once per keypress. Nothing
// here requests, clicks, or scrolls on its own, and every response passes
// through to the page unchanged.
//
// Each page is stored whole under its searchState and page number, so the
// Python side finds the records with the same code the live client uses.

const FORMAT = "hiringcafe-capture/1";
const PAGE_PREFIX = "page:";
const END_PREFIX = "end:";

// "Next page" fetches /_next/data/<build id>/classic.json?searchState=...&page=N.
const DATA_ROUTE = "*://hiringcafe.com/_next/data/*/classic.json*";

/** JSON with sorted keys, so one search is one key whatever its key order. */
function canonical(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const fields = Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`);
    return `{${fields.join(",")}}`;
  }
  return JSON.stringify(value);
}

/** The searchState and page number a URL was requested with, or null. */
function parseLocation(url) {
  const params = new URL(url).searchParams;
  const raw = params.get("searchState");
  if (!raw) {
    return null;
  }
  let searchState;
  try {
    searchState = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!searchState || typeof searchState !== "object" || Array.isArray(searchState)) {
    return null;
  }
  const page = Number.parseInt(params.get("page") ?? "0", 10);
  return { searchState, page: Number.isInteger(page) && page >= 0 ? page : 0 };
}

function recordCount(payload) {
  const hits = payload?.pageProps?.ssrHits;
  return Array.isArray(hits) ? hits.length : 0;
}

/** A short, recognizable label for a search in the popup. */
function describe(state) {
  const location = state.locations?.[0];
  const parts = [location?.formatted_address ?? "any location"];
  const types = state.workplaceTypes ?? location?.workplace_types;
  if (Array.isArray(types)) {
    parts.push(types.join("/"));
  }
  if (state.dateFetchedPastNDays !== undefined) {
    parts.push(`${state.dateFetchedPastNDays} days`);
  }
  if (state.maxCompensationLowEnd) {
    parts.push(`max pay ≥ $${Math.round(Number(state.maxCompensationLowEnd) / 1000)}k`);
  }
  return parts.join(" · ");
}

// ----- in-memory index -----------------------------------------------------
//
// Storage holds whole payloads - tens of megabytes for a full capture - so the
// badge and popup read this index of what they need rather than storage
// itself. Storage is read in full only once at startup and again on Save.

/** storage key -> { stateKey, label, page, records } */
const pageIndex = new Map();
/** stateKey -> { searchState, page, reason } */
const endIndex = new Map();

function indexEntry(entry) {
  return {
    stateKey: canonical(entry.searchState),
    label: describe(entry.searchState),
    page: entry.page,
    records: recordCount(entry.payload),
  };
}

const ready = (async () => {
  const items = await browser.storage.local.get(null);
  for (const [key, value] of Object.entries(items)) {
    if (key.startsWith(PAGE_PREFIX)) {
      pageIndex.set(key, indexEntry(value));
    } else if (key.startsWith(END_PREFIX)) {
      endIndex.set(canonical(value.searchState), value);
    }
  }
  await updateBadge();
})();

// ----- capture -------------------------------------------------------------

async function storePage({ url, source, buildId, payload }) {
  const props = payload?.pageProps;
  // A moved page answers with a redirect and no records. Stored, it would
  // read as the end of the results.
  if (!props || typeof props !== "object" || "__N_REDIRECT" in props) {
    return;
  }
  const where = parseLocation(url);
  if (!where) {
    return;
  }
  await ready;
  const key = `${PAGE_PREFIX}${canonical(where.searchState)}#${where.page}`;
  const entry = {
    url,
    searchState: where.searchState,
    page: where.page,
    source,
    build_id: buildId,
    captured_at: new Date().toISOString(),
    payload,
  };
  await browser.storage.local.set({ [key]: entry });
  pageIndex.set(key, indexEntry(entry));
  await updateBadge();
}

/** The search's last page, as seen by the content script: no "Next page" link. */
async function storeEnd(url) {
  if (typeof url !== "string" || !url.startsWith("https://hiringcafe.com/")) {
    return;
  }
  const where = parseLocation(url);
  if (!where) {
    return;
  }
  await ready;
  const stateKey = canonical(where.searchState);
  const end = { searchState: where.searchState, page: where.page, reason: "no next page link" };
  await browser.storage.local.set({ [`${END_PREFIX}${stateKey}`]: end });
  endIndex.set(stateKey, end);
  await updateBadge();
}

// ----- summary, badge, save, clear -----------------------------------------

function summarize() {
  const groups = new Map();
  for (const item of pageIndex.values()) {
    if (!groups.has(item.stateKey)) {
      groups.set(item.stateKey, { label: item.label, end: endIndex.get(item.stateKey), pages: [] });
    }
    groups.get(item.stateKey).pages.push(item);
  }
  return [...groups.values()].map(({ label, end, pages }) => {
    pages.sort((a, b) => a.page - b.page);
    const numbers = new Set(pages.map((page) => page.page));
    const last = pages[pages.length - 1];
    const missing = [];
    for (let n = 0; n <= last.page; n += 1) {
      if (!numbers.has(n)) {
        missing.push(n);
      }
    }
    // The same two end signals the importer accepts: the "Next page" link
    // gone on a captured page, or a captured page with no results.
    const emptyLast = last.records === 0;
    const complete = emptyLast || (end !== undefined && last.page === end.page);
    return {
      label,
      complete,
      endReason: emptyLast ? "empty page" : (end?.reason ?? null),
      firstPage: pages[0].page,
      lastPage: last.page,
      pageCount: pages.length,
      missing,
      records: pages.reduce((sum, page) => sum + page.records, 0),
      lastPageRecords: last.records,
    };
  });
}

/** Page count on the badge; green once every captured search reached its last page. */
async function updateBadge() {
  const searches = summarize();
  const done = searches.length > 0 && searches.every((search) => search.complete);
  await browser.browserAction.setBadgeText({ text: pageIndex.size ? String(pageIndex.size) : "" });
  await browser.browserAction.setBadgeBackgroundColor({ color: done ? "#1a7f37" : "#0a84ff" });
}

function localStamp(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

async function save() {
  await ready;
  const items = await browser.storage.local.get(null);
  const pages = Object.entries(items)
    .filter(([key]) => key.startsWith(PAGE_PREFIX))
    .map(([, page]) => page);
  if (!pages.length) {
    return { saved: false, message: "Nothing captured yet." };
  }
  pages.sort((a, b) => a.captured_at.localeCompare(b.captured_at));
  const ends = [...endIndex.values()];
  const body = JSON.stringify({ format: FORMAT, saved_at: new Date().toISOString(), pages, ends });
  const url = URL.createObjectURL(new Blob([body], { type: "application/json" }));
  const filename = `hiringcafe-capture/capture-${localStamp(new Date())}.json`;
  try {
    await browser.downloads.download({ url, filename, conflictAction: "uniquify", saveAs: false });
  } finally {
    // The download reads the blob asynchronously; release it once it surely has.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
  return { saved: true, message: `Saved ${pages.length} pages to Downloads/${filename}` };
}

async function clear() {
  await ready;
  const keys = [...pageIndex.keys(), ...[...endIndex.keys()].map((key) => END_PREFIX + key)];
  await browser.storage.local.remove(keys);
  pageIndex.clear();
  endIndex.clear();
  await updateBadge();
}

// ----- wiring --------------------------------------------------------------

browser.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.method !== "GET") {
      return {};
    }
    const filter = browser.webRequest.filterResponseData(details.requestId);
    const decoder = new TextDecoder("utf-8");
    let text = "";
    filter.ondata = (event) => {
      text += decoder.decode(event.data, { stream: true });
      filter.write(event.data);
    };
    filter.onstop = () => {
      filter.close();
      text += decoder.decode();
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        return;
      }
      // /_next/data/<build id>/classic.json
      const buildId = new URL(details.url).pathname.split("/")[3] ?? "";
      storePage({ url: details.url, source: "data", buildId, payload }).catch(console.error);
    };
    filter.onerror = () => {
      console.warn("hiring.cafe capture: could not read", details.url, filter.error);
    };
    return {};
  },
  { urls: [DATA_ROUTE] },
  ["blocking"],
);

browser.runtime.onMessage.addListener((message, sender) => {
  switch (message?.type) {
    case "html-page":
      return storePage({
        url: sender.url ?? message.url,
        source: "html",
        buildId: message.buildId,
        payload: message.payload,
      });
    case "page-state":
      // The page's own current address: the site's "Next page" changes it
      // without a reload, and sender.url stays at the address the tab loaded.
      return message.atEnd ? storeEnd(message.url) : undefined;
    case "summary":
      return ready.then(summarize);
    case "save":
      return save();
    case "clear":
      return clear();
    default:
      return undefined;
  }
});

// Alt+N (changeable in about:addons -> Manage Extension Shortcuts). Forwarded to
// the page, which clicks the site's own "Next page" link: one click per keypress.
browser.commands.onCommand.addListener(async (command) => {
  if (command !== "next-page") {
    return;
  }
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url?.startsWith("https://hiringcafe.com/")) {
    return;
  }
  // No content script on the page (not a search page) rejects; nothing to do.
  browser.tabs.sendMessage(tab.id, { type: "next-page" }).catch(() => {});
});
