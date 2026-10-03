"use strict";

// A page loaded as HTML - the first page of a search, or any page reloaded -
// carries its results in the __NEXT_DATA__ script tag rather than in a request
// the background script can watch. Hand them over once per document load;
// "next page" clicks after that arrive as data requests instead.
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
