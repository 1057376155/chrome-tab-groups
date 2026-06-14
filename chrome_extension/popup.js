document.addEventListener("DOMContentLoaded", () => {
  const status = document.getElementById("status");
  const btn = document.getElementById("capture");

  btn.addEventListener("click", () => {
    status.textContent = "正在保存…";
    btn.disabled = true;
    chrome.runtime.sendMessage({ type: "CAPTURE_NOW" }, (resp) => {
      btn.disabled = false;
      if (chrome.runtime.lastError) {
        status.textContent = "错误: " + chrome.runtime.lastError.message;
      } else if (resp && resp.ok) {
        status.textContent = "已保存当前标签组到 App";
      } else {
        status.textContent = "保存失败: " + (resp && resp.error ? resp.error : "未知错误");
      }
    });
  });

  // Show connection status by pinging the Python bridge.
  fetch("http://127.0.0.1:8765/status", { method: "GET" })
    .then((res) => res.json())
    .then(() => {
      status.textContent = "已连接到 App";
    })
    .catch(() => {
      status.textContent = "未连接到 App，请先打开 TabGroupManager";
    });
});
