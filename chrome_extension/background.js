const BRIDGE_URL = "http://127.0.0.1:8765";
// The Python bridge holds /pending open for up to ~25s waiting for a command,
// so we don't need a high-frequency alarm anymore. A keepalive alarm handles
// the cases where a long-poll fetch is dropped (service worker sleep, network
// blip) — Chrome enforces a 0.5-min minimum for alarms, so we use that floor.
const KEEPALIVE_INTERVAL_MINUTES = 0.5;
const LONG_POLL_TIMEOUT_MS = 28_000;
let pollInFlight = false;

function log(...args) {
  console.log("[TabGroupManager Bridge]", ...args);
}

function bridgeFetch(path, method, body, timeoutMs) {
  const init = {
    method: method || "GET",
    headers: { "Content-Type": "application/json" },
  };
  if (body) {
    init.body = JSON.stringify(body);
  }
  // An AbortController lets us cap the long-poll so a stalled connection
  // doesn't strand the polling loop forever.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || 30000);
  init.signal = controller.signal;
  return fetch(`${BRIDGE_URL}${path}`, init)
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .catch((err) => {
      // Don't spam errors when the Python app is not running, or when a
      // long-poll times out — both are expected and recoverable.
      return null;
    })
    .finally(() => clearTimeout(timer));
}

function ackCommand(command, success, message) {
  return bridgeFetch("/ack", "POST", {
    id: command.id,
    success: !!success,
    message: String(message || ""),
  });
}

// captureLiveTabGroups is now async: it resolves only after the snapshot has
// actually been POSTed to the bridge, so the caller can ack truthfully. Each
// group carries its windowId so the app can rebuild the window layout, and
// ungrouped tabs are bucketed per-window (instead of all merged together).
function captureLiveTabGroups() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({}, (tabs) => {
      if (chrome.runtime.lastError) {
        reject("tabs.query: " + chrome.runtime.lastError.message);
        return;
      }
      chrome.tabGroups.query({}, (groups) => {
        if (chrome.runtime.lastError) {
          reject("tabGroups.query: " + chrome.runtime.lastError.message);
          return;
        }
        const groupMap = new Map();
        // ungrouped[windowId] = [tabItems]
        const ungroupedByWindow = {};

        for (const g of groups) {
          groupMap.set(g.id, {
            title: g.title || "(untitled)",
            color_name: g.color || "grey",
            color_id: -1,
            collapsed: g.collapsed,
            uuid: String(g.id),
            window_id: g.windowId,
            tabs: [],
          });
        }

        for (const tab of tabs) {
          if (!tab.url || !tab.url.startsWith("http")) continue;
          const item = { url: tab.url, title: tab.title || tab.url };
          if (tab.groupId > 0 && groupMap.has(tab.groupId)) {
            groupMap.get(tab.groupId).tabs.push(item);
          } else {
            // Bucket by window so each window gets its own (未分组) group,
            // preserving the tab's true windowId instead of merging windows.
            const wid = tab.windowId;
            if (!ungroupedByWindow[wid]) ungroupedByWindow[wid] = [];
            ungroupedByWindow[wid].push(item);
          }
        }

        const payloadGroups = Array.from(groupMap.values());
        for (const wid of Object.keys(ungroupedByWindow)) {
          const widNum = Number(wid);
          payloadGroups.push({
            title: "(未分组)",
            color_name: "grey",
            color_id: -1,
            collapsed: false,
            uuid: `ungrouped-${wid}`,
            window_id: widNum,
            tabs: ungroupedByWindow[wid],
          });
        }

        const payload = {
          profile_dir: "Live",
          profile_name: "Live Capture",
          email: "",
          groups: payloadGroups,
        };

        bridgeFetch("/snapshot", "POST", payload)
          .then((res) => {
            if (res && res.ok) {
              log("Live snapshot sent");
              resolve(`captured ${payload.groups.length} groups`);
            } else {
              reject("bridge rejected snapshot");
            }
          })
          .catch((err) => reject(err || "snapshot POST failed"));
      });
    });
  });
}

function restoreGroup(payload) {
  const urls = (payload.urls || []).filter((u) => u.startsWith("http"));
  if (urls.length === 0) {
    return Promise.reject("no urls");
  }

  return new Promise((resolve, reject) => {
    chrome.windows.create({ url: urls, focused: true }, (win) => {
      if (chrome.runtime.lastError) {
        reject(chrome.runtime.lastError.message);
        return;
      }
      const tabIds = (win.tabs || []).map((t) => t.id);
      if (tabIds.length === 0) {
        reject("no tabs created");
        return;
      }
      if (tabIds.length === 1) {
        resolve("opened 1 tab");
        return;
      }
      chrome.tabs.group({ tabIds: tabIds }, (groupId) => {
        if (chrome.runtime.lastError) {
          // Grouping failed — clean up the otherwise-orphaned window so we
          // don't leave a stray restored window behind on error.
          chrome.windows.remove(win.id, () => {});
          reject(chrome.runtime.lastError.message);
          return;
        }
        chrome.tabGroups.update(
          groupId,
          { title: payload.title || "Restored", color: payload.color || "blue" },
          () => {
            if (chrome.runtime.lastError) {
              chrome.windows.remove(win.id, () => {});
              reject(chrome.runtime.lastError.message);
            } else {
              resolve(`restored ${tabIds.length} tabs as group "${payload.title}"`);
            }
          }
        );
      });
    });
  });
}

// Restore a whole window: open every group's tabs in one new Chrome window,
// then rebuild each non-virtual group as a native colored tab group inside it.
// payload = { window_title, groups: [{ title, color, urls: [...] }, ...] }
function restoreWindow(payload) {
  const windowGroups = (payload && payload.groups) || [];
  // Flatten all urls across every group to open them in one windows.create.
  const allUrls = [];
  // urlOffsets[g] = [startIndex, endIndex) into allUrls for group g, so we can
  // map the created tab ids back to their owning group after creation.
  const urlOffsets = [];
  for (const g of windowGroups) {
    const urls = (g.urls || []).filter((u) => u.startsWith("http"));
    const start = allUrls.length;
    for (const u of urls) allUrls.push(u);
    urlOffsets.push([start, allUrls.length]);
  }
  if (allUrls.length === 0) {
    return Promise.reject("no urls");
  }

  return new Promise((resolve, reject) => {
    chrome.windows.create({ url: allUrls, focused: true }, (win) => {
      if (chrome.runtime.lastError) {
        reject(chrome.runtime.lastError.message);
        return;
      }
      const tabIds = (win.tabs || []).map((t) => t.id);
      if (tabIds.length === 0) {
        reject("no tabs created");
        return;
      }
      // For each group with >=2 tabs (and a non-virtual title), recreate it as
      // a native colored group. Single tabs and "(未分组)" virtual groups are
      // left as plain tabs. Errors are collected but don't abort the window.
      const restoreTasks = [];
      for (let i = 0; i < windowGroups.length; i++) {
        const g = windowGroups[i];
        const [start, end] = urlOffsets[i];
        const groupTabIds = tabIds.slice(start, end);
        const isVirtual = !g.title || g.title === "(未分组)" || g.title === "(ungrouped)";
        if (isVirtual || groupTabIds.length < 2) {
          continue;
        }
        restoreTasks.push(
          new Promise((res) => {
            chrome.tabs.group({ tabIds: groupTabIds }, (groupId) => {
              if (chrome.runtime.lastError) {
                res(`group "${g.title}" failed: ${chrome.runtime.lastError.message}`);
                return;
              }
              chrome.tabGroups.update(
                groupId,
                { title: g.title, color: g.color || "grey" },
                () => {
                  if (chrome.runtime.lastError) {
                    res(`group "${g.title}" color failed: ${chrome.runtime.lastError.message}`);
                  } else {
                    res(`group "${g.title}" ok`);
                  }
                }
              );
            });
          })
        );
      }
      Promise.all(restoreTasks).then((details) => {
        const okCount = details.filter((d) => d.endsWith("ok")).length;
        const msg =
          `restored window "${payload.window_title || ""}" with ${allUrls.length} tabs, ` +
          `${okCount}/${restoreTasks.length} groups`;
        // If at least one group was attempted but none succeeded, report
        // failure so the user is told the rebuild was partial, not "success".
        if (restoreTasks.length > 0 && okCount === 0) {
          reject(msg);
        } else {
          resolve(msg);
        }
      });
    });
  });
}

function handleCommand(command) {
  if (!command || command.type === "NONE") return;

  if (command.type === "CAPTURE") {
    // Only ack after the capture promise settles, so the success flag is honest.
    captureLiveTabGroups()
      .then((msg) => ackCommand(command, true, msg))
      .catch((err) => ackCommand(command, false, String(err)));
  } else if (command.type === "RESTORE") {
    restoreGroup(command.payload || {})
      .then((msg) => ackCommand(command, true, msg))
      .catch((err) => ackCommand(command, false, String(err)));
  } else if (command.type === "RESTORE_WINDOW") {
    restoreWindow(command.payload || {})
      .then((msg) => ackCommand(command, true, msg))
      .catch((err) => ackCommand(command, false, String(err)));
  }
}

function pollPending() {
  // Guard against overlapping long-polls (e.g. an alarm firing while a fetch
  // is still in flight). One outstanding request is all we need.
  if (pollInFlight) return;
  pollInFlight = true;
  bridgeFetch("/pending", "GET", null, LONG_POLL_TIMEOUT_MS)
    .then((command) => {
      if (command && command.type && command.type !== "NONE") {
        handleCommand(command);
      }
    })
    .finally(() => {
      pollInFlight = false;
      // Immediately re-arm the long-poll so the loop keeps running. The alarm
      // only serves as a safety net to restart the loop if it ever stalls.
      schedulePoll();
    });
}

function schedulePoll() {
  // setTimeout within a service worker is fine here because the outstanding
  // fetch keeps the worker alive; if the worker sleeps, the alarm wakes us.
  setTimeout(pollPending, 1000);
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "tabgroupmanager-keepalive") {
    pollPending();
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("tabgroupmanager-keepalive", {
    periodInMinutes: KEEPALIVE_INTERVAL_MINUTES,
  });
  schedulePoll();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("tabgroupmanager-keepalive", {
    periodInMinutes: KEEPALIVE_INTERVAL_MINUTES,
  });
  schedulePoll();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "CAPTURE_NOW") {
    captureLiveTabGroups()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // keep the message channel open for the async sendResponse
  }
});
