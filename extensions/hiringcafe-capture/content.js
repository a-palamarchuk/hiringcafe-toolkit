"use strict";

// Runs on hiring.cafe search pages. Three jobs, none of which pages on its own:
//
// 1. Hand over the results embedded in a page loaded as HTML (the first page
//    of a search, or any page reloaded). "Next page" navigations after that
//    arrive as data requests, which the background script reads instead.
// 2. On the user's Alt+N, click the site's own "Next page" link - once per
//    keypress, never on a timer.
// 3. Tell the background script when the "Next page" link is gone, which is
//    how a search's last page is recognized.

const NEXT_LINK = 'a[aria-label="Next page"]';

// The right-pointing chevron in the pagination bar. On the last page the site
// renders it inside a greyed <span> instead of the "Next page" link.
const NEXT_CHEVRON_PATH = "m8.25 4.5 7.5 7.5-7.5 7.5";

const SETTLE_MS = 500;

(() => {
  const tag = document.getElementById("__NEXT_DATA__");
  if (!tag) {
    return;
  }
  let data;
  try {
    data = JSON.parse(tag.textContent);
  } catch {
    return;
  }
  browser.runtime.sendMessage({
    type: "html-page",
    url: location.href,
    buildId: typeof data.buildId === "string" ? data.buildId : "",
    payload: data.props,
  });
})();

function toast(text) {
  const box = document.createElement("div");
  box.textContent = text;
  Object.assign(box.style, {
    position: "fixed",
    right: "16px",
    bottom: "16px",
    zIndex: "2147483647",
    padding: "8px 14px",
    borderRadius: "6px",
    background: "#1f2328",
    color: "#ffffff",
    font: "13px system-ui, sans-serif",
    boxShadow: "0 2px 8px rgba(0, 0, 0, 0.3)",
  });
  document.body.append(box);
  setTimeout(() => box.remove(), 2000);
}

/**
 * true on the last page, false when a next page exists, null when the
 * pagination bar has not rendered yet. A page still rendering has neither the
 * link nor the greyed chevron, and must not read as the end.
 */
function atEnd() {
  if (document.querySelector(NEXT_LINK)) {
    return false;
  }
  for (const path of document.querySelectorAll("span svg path")) {
    if (path.getAttribute("d") === NEXT_CHEVRON_PATH && !path.closest("a")) {
      return true;
    }
  }
  return null;
}

let lastReport = "";
let settleTimer = null;

function reportPageState() {
  const end = atEnd();
  if (end === null) {
    return;
  }
  const report = `${location.href}|${end}`;
  if (report === lastReport) {
    return;
  }
  lastReport = report;
  browser.runtime.sendMessage({ type: "page-state", url: location.href, atEnd: end });
}

// The site swaps pages in place, so watch for the page to stop changing
// rather than for a load event.
new MutationObserver(() => {
  clearTimeout(settleTimer);
  settleTimer = setTimeout(reportPageState, SETTLE_MS);
}).observe(document.documentElement, { childList: true, subtree: true });
settleTimer = setTimeout(reportPageState, SETTLE_MS);

browser.runtime.onMessage.addListener((message) => {
  if (message?.type !== "next-page") {
    return undefined;
  }
  const link = document.querySelector(NEXT_LINK);
  if (link) {
    link.click();
  } else {
    toast(atEnd() ? "Last page reached" : "No next page link yet - wait for the page to load");
  }
  return undefined;
});
