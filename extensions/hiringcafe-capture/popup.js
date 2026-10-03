"use strict";

const searchesBox = document.getElementById("searches");
const statusBox = document.getElementById("status");
const clearButton = document.getElementById("clear");

function line(className, text) {
  const div = document.createElement("div");
  div.className = className;
  div.textContent = text;
  return div;
}

async function render() {
  const searches = await browser.runtime.sendMessage({ type: "summary" });
  searchesBox.replaceChildren();
  if (!searches.length) {
    searchesBox.append(
      line("empty", "Nothing captured. Open a link from `job-shortlist urls` and page through it."),
    );
    return;
  }
  for (const search of searches) {
    const box = document.createElement("div");
    box.className = "search";
    box.append(line("label", search.label));
    box.append(
      line(
        "detail",
        `Pages ${search.firstPage}–${search.lastPage} (${search.pageCount} captured), ` +
          `${search.records} postings; last page has ${search.lastPageRecords}`,
      ),
    );
    if (search.missing.length) {
      box.append(line("warn", `Missing pages: ${search.missing.join(", ")}`));
    }
    box.append(
      search.complete
        ? line("done", `Complete - last page reached (${search.endReason})`)
        : line("detail", "In progress - press Alt+N for the next page"),
    );
    searchesBox.append(box);
  }
}

document.getElementById("save").addEventListener("click", async () => {
  try {
    const result = await browser.runtime.sendMessage({ type: "save" });
    statusBox.textContent = result.message;
  } catch (error) {
    statusBox.textContent = `Save failed: ${error}`;
  }
});

// Two clicks rather than a confirm() dialog, which extension popups cannot show.
let clearArmed = false;
clearButton.addEventListener("click", async () => {
  if (!clearArmed) {
    clearArmed = true;
    clearButton.textContent = "Click again to clear";
    statusBox.textContent = "Clearing discards every captured page. Save first if needed.";
    return;
  }
  clearArmed = false;
  clearButton.textContent = "Clear";
  await browser.runtime.sendMessage({ type: "clear" });
  statusBox.textContent = "Cleared.";
  await render();
});

render().catch((error) => {
  statusBox.textContent = `Could not read captured pages: ${error}`;
});
