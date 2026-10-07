/* TeslaUSB-CN 前端交互：确认框、Toast、多选、摄像头切换、进度轮询 */
(function () {
  "use strict";

  /* ---- Toast ---- */
  var toastEl = null;
  function toast(msg) {
    if (!toastEl) toastEl = document.getElementById("toast");
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.classList.remove("show"); }, 2200);
  }
  window._toast = toast;

  /* ---- 二次确认框：data-confirm="文案" 的表单/按钮统一拦截 ---- */
  var mask = document.getElementById("confirm-mask");
  var msgEl = document.getElementById("confirm-msg");
  var okBtn = document.getElementById("confirm-ok");
  var pending = null;
  if (mask) {
    document.getElementById("confirm-cancel").addEventListener("click", function () {
      mask.classList.remove("show"); pending = null;
    });
    okBtn.addEventListener("click", function () {
      mask.classList.remove("show");
      if (pending) { var f = pending; pending = null; f(); }
    });
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !form._confirmed) {
      e.preventDefault();
      if (!mask) { if (window.confirm(msg)) form.submit(); return; }
      msgEl.textContent = msg;
      mask.classList.add("show");
      pending = function () { form._confirmed = true; form.submit(); };
    }
  });
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-confirm-btn]");
    if (el) {
      var msg = el.getAttribute("data-confirm-btn");
      if (!mask) { if (window.confirm(msg)) window.location = el.getAttribute("href"); return; }
      e.preventDefault();
      msgEl.textContent = msg;
      mask.classList.add("show");
      pending = function () { window.location = el.getAttribute("href"); };
    }
  });

  /* ---- 视频多选工具条 ---- */
  var toolbar = document.getElementById("sel-toolbar");
  if (toolbar) {
    var boxes = Array.prototype.slice.call(document.querySelectorAll(".vcheck"));
    var countEl = document.getElementById("sel-count");
    var namesInput = document.getElementById("sel-names");
    function refresh() {
      var sel = boxes.filter(function (b) { return b.checked; }).map(function (b) {
        return b.getAttribute("data-folder") + ":" + b.value;
      });
      countEl.textContent = sel.length;
      namesInput.value = sel.join(",");
      document.getElementById("sel-names-2").value = sel.join(",");
      toolbar.classList.toggle("show", sel.length > 0);
    }
    boxes.forEach(function (b) { b.addEventListener("change", refresh); });
    document.getElementById("sel-clear").addEventListener("click", function () {
      boxes.forEach(function (b) { b.checked = false; });
      refresh();
    });
    refresh();
  }

  /* ---- 播放页摄像头切换 ---- */
  var camLabel = document.getElementById("cam-label");
  if (camLabel) {
    document.querySelectorAll(".camgrid button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".camgrid button").forEach(function (b) { b.classList.remove("on"); });
        btn.classList.add("on");
        camLabel.textContent = btn.getAttribute("data-label");
      });
    });
    var big = document.getElementById("bigplay");
    if (big) big.addEventListener("click", function () { big.classList.toggle("pause"); });
    var video = document.getElementById("real-video");
    if (video && big) {
      big.addEventListener("click", function () {
        if (video.paused) video.play(); else video.pause();
      });
    }
  }

  /* ---- 长耗时任务进度轮询：data-job-start="/settings/api/backup/start" ---- */
  document.querySelectorAll("[data-job-start]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var wrap = document.getElementById(btn.getAttribute("data-progress-wrap"));
      var bar = wrap.querySelector(".bar > i");
      var lbl = wrap.querySelector(".lbl");
      var cancelBtn = wrap.querySelector("[data-job-cancel]");
      btn.disabled = true;
      wrap.style.display = "block";
      fetch(btn.getAttribute("data-job-start"), { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var jid = data.job_id;
          cancelBtn.onclick = function () {
            fetch("/settings/api/jobs/" + jid + "/cancel", { method: "POST" });
            wrap.style.display = "none"; btn.disabled = false;
            toast(btn.getAttribute("data-cancel-toast") || "");
          };
          var timer = setInterval(function () {
            fetch("/settings/api/jobs/" + jid).then(function (r) { return r.json(); }).then(function (p) {
              bar.style.width = p.percent + "%";
              lbl.textContent = (p.current || 0) + "/" + (p.total || 0) + " · " + p.percent + "%";
              if (p.state !== "running") {
                clearInterval(timer);
                wrap.style.display = "none"; btn.disabled = false;
                toast(p.state === "done"
                  ? (btn.getAttribute("data-done-toast") || "")
                  : (btn.getAttribute("data-cancel-toast") || ""));
                if (p.state === "done" && btn.getAttribute("data-reload") === "1") location.reload();
              }
            });
          }, 600);
        });
    });
  });

  /* ---- 清理预览 ---- */
  var pvBtn = document.getElementById("cleanup-preview-btn");
  if (pvBtn) {
    pvBtn.addEventListener("click", function () {
      fetch("/settings/api/cleanup/preview").then(function (r) { return r.json(); }).then(function (d) {
        document.getElementById("cleanup-preview-result").textContent =
          pvBtn.getAttribute("data-tpl").replace("{files}", d.files).replace("{size}", d.size_gb);
      });
    });
  }

  /* ---- 定时计划表单：按类型显示字段 ---- */
  document.querySelectorAll(".sched-type").forEach(function (radio) {
    radio.addEventListener("change", function () { toggleSchedFields(radio); });
    if (radio.checked) toggleSchedFields(radio);
  });
  function toggleSchedFields(radio) {
    var form = radio.closest("form");
    var t = radio.value;
    ["weekly", "date", "holiday"].forEach(function (k) {
      var el = form.querySelector(".fld-" + k);
      if (el) el.style.display = (t === k) ? "" : "none";
    });
  }

  /* ---- 上传按钮：提交时显示 spinner ---- */
  document.querySelectorAll("form[data-upload]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var b = form.querySelector('button[type="submit"]');
      if (b && !form._confirmed) { b.disabled = true; b.textContent = "…"; }
    });
  });
})();
