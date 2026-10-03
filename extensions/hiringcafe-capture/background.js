"use strict";

// Saves hiring.cafe search result pages as the browser receives them, for
// `hiringcafe-toolkit job-shortlist import`.
//
// It only watches. Paging is done by the person at the keyboard; this script
// never requests, clicks, or scrolls anything, and passes every response
// through to the page unchanged.
//
// Each page is stored whole under its searchState and page number, so the
// Python side finds the records with the same code the live client uses.

const FORMAT = "hiringcafe-capture/1";
const PAGE_PREFIX = "page:";

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
  const key = `${PAGE_PREFIX}${canonical(where.searchState)}#${where.page}`;
  await browser.storage.local.set({
    [key]: {
      url,
      searchState: where.searchState,
      page: where.page,
      source,
      build_id: buildId,
      captured_at: new Date().toISOString(),
      payload,
    },
  });
  await updateBadge();
}

async function allPages() {
  const items = await browser.storage.local.get(null);
  return Object.entries(items)
    .filter(([key]) => key.startsWith(PAGE_PREFIX))
    .map(([, page]) => page);
}

async function updateBadge() {
  const count = (await allPages()).length;
  await browser.browserAction.setBadgeText({ text: count ? String(count) : "" });
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

async function summarize() {
  const groups = new Map();
  for (const page of await allPages()) {
    const key = canonical(page.searchState);
    if (!groups.has(key)) {
      groups.set(key, { label: describe(page.searchState), pages: [] });
    }
    groups.get(key).pages.push(page);
  }
  return [...groups.values()].map(({ label, pages }) => {
    pages.sort((a, b) => a.page - b.page);
    const numbers = new Set(pages.map((page) => page.page));
    const last = pages[pages.length - 1];
    const missing = [];
    for (let n = 0; n <= last.page; n += 1) {
      if (!numbers.has(n)) {
        missing.push(n);
      }
    }
    return {
      label,
      firstPage: pages[0].page,
      lastPage: last.page,
      pageCount: pages.length,
      missing,
      records: pages.reduce((sum, page) => sum + recordCount(page.payload), 0),
      lastPageRecords: recordCount(last.payload),
    };
  });
}

function localStamp(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

async function save() {
  const pages = await allPages();
  if (!pages.length) {
    return { saved: false, message: "Nothing captured yet." };
  }
  pages.sort((a, b) => a.captured_at.localeCompare(b.captured_at));
  const body = JSON.stringify({ format: FORMAT, saved_at: new Date().toISOString(), pages });
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
  const keys = Object.keys(await browser.storage.local.get(null)).filter((key) =>
    key.startsWith(PAGE_PREFIX),
  );
  await browser.storage.local.remove(keys);
  await updateBadge();
}

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
    case "summary":
      return summarize();
    case "save":
      return save();
    case "clear":
      return clear();
    default:
      return undefined;
  }
});

updateBadge().catch(console.error);
