/* 学习通待办中心 - 前端逻辑（原生 JS，无外部依赖） */
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var state = { data: null, filter: "all", q: "", loadedAt: 0, polling: null };

  // ---------------------------------------------------------------- 工具
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toast(msg, type) {
    var el = $("#toast");
    el.textContent = msg;
    el.className = "toast on" + (type ? " " + type : "");
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.className = "toast"; }, 3200);
  }

  function fmtDuration(sec) {
    var neg = sec < 0;
    var s = Math.abs(Math.round(sec));
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    var body;
    if (d > 0) body = d + " 天 " + h + " 小时";
    else if (h > 0) body = h + " 小时 " + m + " 分";
    else body = Math.max(m, 1) + " 分钟";
    return neg ? "已逾期 " + body : "剩 " + body;
  }

  function urgency(sec) {
    if (sec == null) return "";
    if (sec <= 0) return "over";
    if (sec <= 3 * 3600) return "u3";
    if (sec <= 24 * 3600) return "u24";
    if (sec <= 72 * 3600) return "u72";
    return "";
  }

  function remainingOf(task) {
    if (task.remaining_seconds == null) return null;
    var drift = (Date.now() - state.loadedAt) / 1000;
    return task.remaining_seconds - drift;
  }

  // ---------------------------------------------------------------- API
  async function api(path, options) {
    var opts = options || {};
    opts.credentials = "same-origin";
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    var resp = await fetch(path, opts);
    if (resp.status === 401) {
      showLogin();
      throw new Error("未登录");
    }
    var data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (!resp.ok) throw new Error(data.detail || ("HTTP " + resp.status));
    return data;
  }

  // ---------------------------------------------------------------- 视图切换
  function showLogin() {
    $("#app").classList.add("hidden");
    $("#login").classList.remove("hidden");
    var input = $("#login-pwd");
    if (input) input.focus();
  }

  function showApp() {
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
  }

  // ---------------------------------------------------------------- 渲染
  function renderStats(c) {
    $("#s-total").textContent = c.total;
    $("#s-homework").textContent = c.homework;
    $("#s-exam").textContent = c.exam;
    $("#s-due").textContent = c.due_24h;
    $("#s-overdue").textContent = c.overdue;
  }

  function renderBanner(d) {
    var banner = $("#banner");
    var msgs = [];
    var cls = "banner";
    if (!d.account_ready) {
      msgs.push("尚未配置学习通账号，点右上角「设置」填写账号密码后才能采集待办。");
    }
    if (d.run && d.run.last_ok === false && d.run.error) {
      msgs.push("上次检测异常：" + d.run.error);
      cls += " error";
    }
    if (!msgs.length) { banner.className = "banner hidden"; return; }
    banner.className = cls;
    banner.textContent = msgs.join(" ｜ ");
  }

  function renderTopbar(d) {
    var run = d.run || {};
    var parts = [];
    if (run.last_run) parts.push("上次检测 " + run.last_run.slice(11));
    if (run.running) parts.push("正在检测…");
    else if (run.next_run) parts.push("下一轮 " + run.next_run.slice(11));
    var sum = run.summary || {};
    if (sum.all != null) {
      parts.push("课程 " + (sum.courses || 0) + " · 记录 " + sum.all +
                 " · 待办 " + (sum.pending || 0));
    }
    parts.push("间隔 " + Math.round((run.interval || 0) / 60) + " 分钟");
    $("#topbar-sub").textContent = parts.join(" · ");
    $("#refresh-spin").classList.toggle("on", !!run.running);
    $("#refresh-label").textContent = run.running ? "检测中…" : "立即检测";
    $("#btn-refresh").disabled = !!run.running;
  }

  function rowHtml(t, opts) {
    var ignored = opts && opts.ignored;
    var r = ignored
      ? (t.remaining_seconds == null ? null : t.remaining_seconds - (Date.now() - state.loadedAt) / 1000)
      : remainingOf(t);
    var u = urgency(r);
    var chipText = r == null ? "无截止时间" : fmtDuration(r);
    var chipCls = "chip" + (u ? " " + u : "");
    var rowCls = "row" + (u ? " " + u : "");
    var kind = t.kind || "homework";
    var title = t.title || "";
    var course = ignored ? (t.course || "") : (t.course_name || "");
    var deadline = t.deadline || "—";
    var status = ignored ? "已忽略" : (t.status || "");
    var act = ignored
      ? '<button class="row-act restore" data-act="unignore">恢复</button>'
      : '<button class="row-act" data-act="ignore">忽略</button>';
    return '<div class="' + rowCls + '" data-key="' + esc(t.key || "") + '">' +
      '<span class="tag' + (kind === "exam" ? " exam" : "") + '">' +
      (kind === "exam" ? "考试" : "作业") + "</span>" +
      '<div class="row-main">' +
      '<div class="row-title" title="' + esc(title) + '">' + esc(title) + "</div>" +
      '<div class="row-course">' + esc(course) +
      (t.source === "yuketang" ? '<span class="src">雨课堂</span>' : "") +
      "</div></div>" +
      '<div class="cell-time">' + esc(deadline) + "</div>" +
      '<div><span class="' + chipCls + '" data-sec="' +
      (r == null ? "" : Math.round(r + (Date.now() - state.loadedAt) / 1000)) + '">' +
      esc(chipText) + "</span></div>" +
      '<div class="status">' + esc(status) + "</div>" +
      "<div>" + act + "</div>" +
      "</div>";
  }

  function renderTasks(d) {
    var list = $("#task-list");

    if (state.filter === "ignored") {
      var ig = d.ignored || [];
      if (!ig.length) {
        list.innerHTML = '<div class="empty">还没有忽略任何任务。<br>在任务行右侧点「忽略」即可隐藏并停止提醒。</div>';
        return;
      }
      list.innerHTML = ig.map(function (t) { return rowHtml(t, { ignored: true }); }).join("");
      return;
    }

    var items = (d.tasks || []).filter(function (t) {
      if (state.filter === "homework" && t.kind !== "homework") return false;
      if (state.filter === "exam" && t.kind !== "exam") return false;
      if (state.filter === "urgent") {
        var r = remainingOf(t);
        if (r == null || r > 72 * 3600) return false;
      }
      if (state.q) {
        var hay = (t.course_name || "") + " " + (t.title || "");
        if (hay.toLowerCase().indexOf(state.q.toLowerCase()) === -1) return false;
      }
      return true;
    });

    if (!items.length) {
      list.innerHTML = '<div class="empty">' +
        (d.account_ready ? "当前没有待办任务，保持关注。" : "配置账号后这里会显示作业与考试。") +
        "</div>";
      return;
    }
    list.innerHTML = items.map(function (t) { return rowHtml(t, {}); }).join("");
  }

  function renderEvents(d) {
    var box = $("#events");
    var items = d.events || [];
    $("#events-count").textContent = items.length ? items.length + " 条" : "";
    if (!items.length) {
      box.innerHTML = '<div class="empty small">暂无提醒记录</div>';
      return;
    }
    var labels = { new: "新任务", due: "即将到期", overdue: "已逾期" };
    box.innerHTML = items.map(function (e) {
      return '<div class="ev">' +
        '<span class="ev-time">' + esc((e.ts || "").slice(5, 16)) + "</span>" +
        '<span class="ev-kind ' + esc(e.kind) + '">' + (labels[e.kind] || e.kind) + "</span>" +
        '<span class="ev-text" title="' + esc(e.course + " / " + e.title) + '">' +
        esc(e.course) + " / " + esc(e.title) + "</span></div>";
    }).join("");
  }

  function render(d) {
    renderStats(d.counts || {});
    renderBanner(d);
    renderTopbar(d);
    renderTasks(d);
    renderEvents(d);
  }

  // ---------------------------------------------------------------- 倒计时
  function tick() {
    if (!state.data) return;
    document.querySelectorAll(".chip[data-sec]").forEach(function (chip) {
      var raw = chip.getAttribute("data-sec");
      if (raw === "") return;
      var sec = Number(raw) - (Date.now() - state.loadedAt) / 1000;
      chip.textContent = fmtDuration(sec);
      var u = urgency(sec);
      chip.className = "chip" + (u ? " " + u : "");
    });
  }

  // ---------------------------------------------------------------- 详情 / 忽略
  var detail = { key: "", ignored: false, meta: null };

  function openDetail(key, meta) {
    detail.key = key;
    detail.meta = meta || null;
    detail.ignored = !!(meta && meta.ignored);
    $("#detail").classList.add("open");
    $("#detail-mask").classList.remove("hidden");
    $("#d-title").textContent = (meta && meta.title) || "作业详情";
    $("#d-body").innerHTML = '<div class="empty small">正在获取作业内容…</div>';
    $("#d-ignore").textContent = detail.ignored ? "取消忽略" : "忽略此任务";
    $("#d-open").href = (meta && meta.url) || "#";
    loadDetail(false);
  }

  function closeDetail() {
    $("#detail").classList.remove("open");
    $("#detail-mask").classList.add("hidden");
    detail.key = "";
  }

  async function loadDetail(refresh) {
    if (!detail.key) return;
    try {
      var d = await api("/api/task/" + encodeURIComponent(detail.key) + (refresh ? "?refresh=1" : ""));
      renderDetail(d);
    } catch (e) {
      $("#d-body").innerHTML = '<div class="d-empty">获取失败：' + esc(e.message || "未知错误") + "</div>";
    }
  }

  function renderDetail(d) {
    var html = '<div class="d-meta">' +
      "<span>课程：<b>" + esc(d.course_name || "-") + "</b></span>" +
      "<span>状态：<b>" + esc(d.status || "-") + "</b></span>" +
      "<span>截止：<b>" + esc(d.deadline || "未设置") + "</b></span>" +
      (d.total_score ? "<span>总分：<b>" + d.total_score + " 分</b></span>" : "") +
      (d.question_count ? "<span>题目：<b>" + d.question_count + " 道</b></span>" : "") +
      "</div>";

    if (d.groups && d.groups.length) {
      html += '<div class="d-sec"><h3>题目结构</h3><div class="d-empty">' +
        esc(d.groups.join("　/　")) + "</div></div>";
    }

    if (d.questions && d.questions.length) {
      html += '<div class="d-sec"><h3>题目内容</h3>';
      d.questions.forEach(function (q) {
        html += '<div class="q"><div class="q-head"><span>' + q.no + ". " +
          esc(q.type || "题目") + "</span><span>" + (q.score ? q.score + " 分" : "") + "</span></div>" +
          '<div class="q-stem">' + esc(q.stem || "（题干为空）") + "</div>";
        if (q.options && q.options.length) {
          html += '<div class="q-opts">' + q.options.map(function (o) {
            return '<div class="q-opt">' + esc(o) + "</div>";
          }).join("") + "</div>";
        }
        html += "</div>";
      });
      html += "</div>";
    } else {
      html += '<div class="d-sec"><h3>题目内容</h3><div class="d-empty">' +
        "该作业没有可解析的题目（可能是附件作业、线下作业，或需要进入作答页查看）。</div></div>";
    }

    if (d.attachments && d.attachments.length) {
      html += '<div class="d-sec"><h3>附件</h3>' + d.attachments.map(function (a) {
        return '<a class="d-attach" href="' + esc(a.url) + '" target="_blank" rel="noopener">' +
          esc(a.name) + "</a>";
      }).join("") + "</div>";
    }

    html += '<div class="d-empty">内容获取时间：' + esc(d.fetched_at || "-") +
      (d.cached ? "（缓存）" : "") + "</div>";

    $("#d-body").innerHTML = html;
    if (d.url) $("#d-open").href = d.url;
  }

  async function toggleIgnore(key, ignored, meta) {
    var body = { key: key };
    if (!ignored) {
      body.title = (meta && meta.title) || "";
      body.course_name = (meta && (meta.course_name || meta.course)) || "";
      body.kind = (meta && meta.kind) || "homework";
      body.deadline = (meta && meta.deadline) || null;
    }
    await api(ignored ? "/api/unignore" : "/api/ignore", {
      method: "POST", body: JSON.stringify(body)
    });
    toast(ignored ? "已恢复该任务" : "已忽略，不再提醒", "ok");
    await load();
    if (detail.key === key) {
      detail.ignored = !ignored;
      $("#d-ignore").textContent = detail.ignored ? "取消忽略" : "忽略此任务";
    }
  }

  // ---------------------------------------------------------------- 加载
  async function load() {
    var d = await api("/api/overview");
    state.data = d;
    state.loadedAt = Date.now();
    render(d);
    return d;
  }

  async function refresh() {
    try {
      var r = await api("/api/refresh", { method: "POST" });
      toast(r.message || "已触发检测");
      await load();
      clearInterval(state.polling);
      var started = Date.now();
      state.polling = setInterval(async function () {
        try {
          var d = await load();
          if (!d.run.running) {
            clearInterval(state.polling);
            toast("检测完成", "ok");
          }
        } catch (e) { clearInterval(state.polling); }
        if (Date.now() - started > 180000) clearInterval(state.polling);
      }, 2500);
    } catch (e) {
      toast(e.message || "触发失败", "error");
    }
  }

  // ---------------------------------------------------------------- 设置
  async function openSettings() {
    try {
      var s = await api("/api/settings");
      $("#f-username").value = s.username || "";
      $("#f-password").value = "";
      $("#f-cookie").value = "";
      $("#f-interval").value = s.interval_seconds;
      $("#f-thresholds").value = (s.thresholds_hours || []).join(",");
      $("#f-daily").value = s.daily_summary_at || "";
      $("#f-wecom").value = s.wecom_webhook || "";
      $("#f-serverchan").value = s.serverchan_key || "";
      $("#f-dryrun").checked = !!s.dry_run;
      $("#f-ytcookie").value = "";
      $("#f-ytenable").checked = !!s.yuketang_enabled;
      $("#hint-yt").textContent = s.yuketang_has_cookie
        ? "已配置 Cookie（尾号 …" + (s.yuketang_cookie_tail || "") + "），留空表示不修改"
        : "未配置。雨课堂只能用 Cookie 登录：浏览器打开 changjiang.yuketang.cn 后按 F12 复制 sessionid";
      $("#f-newpwd").value = "";
      $("#hint-account").textContent = s.has_password
        ? "已保存账号与密码；密码留空表示不修改。通道：" + (s.channels || []).join(" / ")
        : "尚未保存密码。";
      $("#drawer").classList.add("open");
      $("#drawer-mask").classList.remove("hidden");
    } catch (e) {
      toast(e.message || "读取设置失败", "error");
    }
  }

  function closeSettings() {
    $("#drawer").classList.remove("open");
    $("#drawer-mask").classList.add("hidden");
  }

  async function saveSettings(event) {
    event.preventDefault();
    var thresholds = $("#f-thresholds").value.split(/[,，\s]+/)
      .filter(Boolean).map(Number).filter(function (n) { return n > 0; });
    var body = {
      username: $("#f-username").value,
      interval_seconds: Number($("#f-interval").value) || 900,
      thresholds_hours: thresholds.length ? thresholds : [72, 24, 3],
      daily_summary_at: $("#f-daily").value,
      yuketang_enabled: $("#f-ytenable").checked,
      wecom_webhook: $("#f-wecom").value,
      serverchan_key: $("#f-serverchan").value,
      dry_run: $("#f-dryrun").checked
    };
    if ($("#f-password").value) body.password = $("#f-password").value;
    if ($("#f-cookie").value) body.cookie = $("#f-cookie").value;
    if ($("#f-newpwd").value) body.new_password = $("#f-newpwd").value;
    if ($("#f-ytcookie").value) body.yuketang_cookie = $("#f-ytcookie").value;

    var btn = $("#btn-save");
    btn.disabled = true;
    $("#save-hint").textContent = "保存中…";
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
      $("#save-hint").textContent = "已保存";
      toast("设置已保存并生效", "ok");
      closeSettings();
      await load();
      if (body.password || body.username) setTimeout(refresh, 400);
    } catch (e) {
      $("#save-hint").textContent = "";
      toast(e.message || "保存失败", "error");
    } finally {
      btn.disabled = false;
      setTimeout(function () { $("#save-hint").textContent = ""; }, 2500);
    }
  }

  // ---------------- 雨课堂扫码登录 ----------------
  var qrState = { id: "", timer: null };

  function stopQrPolling() {
    if (qrState.timer) clearInterval(qrState.timer);
    qrState.timer = null;
  }

  function closeQrModal() {
    stopQrPolling();
    qrState.id = "";
    $("#qr-modal").classList.add("hidden");
    $("#qr-mask").classList.add("hidden");
  }

  async function startYtLogin() {
    stopQrPolling();
    $("#qr-mask").classList.remove("hidden");
    $("#qr-modal").classList.remove("hidden");
    $("#qr-status").textContent = "正在生成二维码…";
    $("#qr-img").removeAttribute("src");
    try {
      var r = await api("/api/yt/login/start", { method: "POST" });
      qrState.id = r.id;
      $("#qr-img").src = "/api/yt/login/qr?id=" + encodeURIComponent(r.id) + "&t=" + Date.now();
      $("#qr-status").textContent = "请用微信扫码，并在手机上确认登录";
      qrState.timer = setInterval(pollYtLogin, 3000);
    } catch (e) {
      $("#qr-status").textContent = "生成失败：" + (e.message || "未知错误");
    }
  }

  async function pollYtLogin() {
    if (!qrState.id) return;
    try {
      var s = await api("/api/yt/login/status?id=" + encodeURIComponent(qrState.id));
      if (s.status === "waiting") {
        $("#qr-status").textContent = "等待扫码…";
      } else if (s.status === "scanned") {
        $("#qr-status").textContent = "已扫码，请在手机上点「确认登录」";
      } else if (s.status === "ok") {
        stopQrPolling();
        $("#qr-status").textContent = "登录成功" + (s.user ? "：" + s.user : "");
        toast("雨课堂登录成功" + (s.user ? "：" + s.user : ""), "ok");
        setTimeout(function () {
          closeQrModal();
          load();
          openSettings();
        }, 800);
      } else if (s.status === "expired") {
        stopQrPolling();
        $("#qr-status").textContent = "二维码已过期，点「刷新二维码」重试";
      } else if (s.status === "failed") {
        stopQrPolling();
        $("#qr-status").textContent = s.detail || "登录失败，请重试";
      }
    } catch (e) {
      /* 轮询失败不打断，等下一次 */
    }
  }

  async function testNotify() {
    try {
      var r = await api("/api/test-notify", { method: "POST" });
      var ok = (r.results || []).filter(function (x) { return x.ok; });
      var bad = (r.results || []).filter(function (x) { return !x.ok; });
      if (bad.length) {
        toast("部分通道失败：" + bad.map(function (b) { return b.channel; }).join(","), "error");
      } else {
        toast("测试消息已发送（" + ok.length + " 个通道）", "ok");
      }
    } catch (e) {
      toast(e.message || "发送失败", "error");
    }
  }

  async function runDiagnose() {
    var panel = $("#diag");
    var body = $("#diag-body");
    panel.classList.remove("hidden");
    $("#diag-summary").textContent = "诊断中，请稍候…";
    body.innerHTML = '<div class="empty small">正在依次检查账号、登录、课程列表…</div>';
    try {
      var r = await api("/api/diagnose", { method: "POST" });
      $("#diag-summary").textContent = r.ok ? "连接正常" : "存在问题";
      body.innerHTML = (r.steps || []).map(function (s) {
        return '<div class="dstep ' + (s.ok ? "ok" : "bad") + '">' +
          '<span class="mark">' + (s.ok ? "OK" : "X") + "</span>" +
          '<span class="name">' + esc(s.name) + "</span>" +
          '<span class="detail">' + esc(s.detail) + "</span>" +
          '<span class="cost">' + (s.cost ? s.cost + "s" : "") + "</span></div>";
      }).join("");
      toast(r.ok ? "诊断完成：连接正常" : "诊断完成：发现问题", r.ok ? "ok" : "error");
    } catch (e) {
      $("#diag-summary").textContent = "诊断失败";
      body.innerHTML = '<div class="empty small">' + esc(e.message || "请求失败") + "</div>";
    }
  }

  async function showLogs() {
    var panel = $("#logs-panel");
    panel.classList.remove("hidden");
    $("#logs").textContent = "加载中…";
    try {
      var r = await api("/api/logs?lines=150");
      $("#logs").textContent = (r.lines || []).join("\n") || "（暂无日志）";
      $("#logs").scrollTop = $("#logs").scrollHeight;
    } catch (e) {
      $("#logs").textContent = e.message || "读取失败";
    }
  }

  // ---------------------------------------------------------------- 绑定
  function bind() {
    $("#login-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var pwd = $("#login-pwd").value;
      var btn = $("#login-btn");
      btn.disabled = true;
      try {
        await api("/api/login", { method: "POST", body: JSON.stringify({ password: pwd }) });
        $("#login-pwd").value = "";
        showApp();
        await load();
      } catch (err) {
        $("#login-hint").textContent = err.message || "登录失败";
      } finally {
        btn.disabled = false;
      }
    });

    $("#btn-logout").addEventListener("click", async function () {
      try { await api("/api/logout", { method: "POST" }); } catch (e) { /* ignore */ }
      showLogin();
    });

    $("#btn-refresh").addEventListener("click", refresh);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-diag").addEventListener("click", runDiagnose);
    $("#btn-diag-close").addEventListener("click", function () {
      $("#diag").classList.add("hidden");
    });
    $("#btn-close-drawer").addEventListener("click", closeSettings);
    $("#drawer-mask").addEventListener("click", closeSettings);
    $("#settings-form").addEventListener("submit", saveSettings);
    $("#btn-test-notify").addEventListener("click", testNotify);
    $("#btn-yt-qr").addEventListener("click", startYtLogin);
    $("#qr-close").addEventListener("click", closeQrModal);
    $("#qr-mask").addEventListener("click", closeQrModal);
    $("#qr-refresh").addEventListener("click", startYtLogin);
    $("#btn-logs").addEventListener("click", showLogs);
    $("#btn-logs-close").addEventListener("click", function () {
      $("#logs-panel").classList.add("hidden");
    });

    $("#filter-tabs").addEventListener("click", function (e) {
      var btn = e.target.closest(".tab");
      if (!btn) return;
      document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
      btn.classList.add("active");
      state.filter = btn.getAttribute("data-filter");
      if (state.data) renderTasks(state.data);
    });

    $("#task-list").addEventListener("click", function (e) {
      var row = e.target.closest(".row");
      if (!row) return;
      var key = row.getAttribute("data-key");
      var btn = e.target.closest(".row-act");
      var d = state.data || {};
      if (btn && key) {
        e.stopPropagation();
        var restoring = btn.getAttribute("data-act") === "unignore";
        var meta = restoring
          ? (d.ignored || []).filter(function (x) { return x.key === key; })[0]
          : (d.tasks || []).filter(function (x) { return x.key === key; })[0];
        toggleIgnore(key, restoring, meta).catch(function (err) {
          toast(err.message || "操作失败", "error");
        });
        return;
      }
      if (!key) return;
      var item = (d.tasks || []).filter(function (x) { return x.key === key; })[0];
      openDetail(key, item || null);
    });

    $("#btn-close-detail").addEventListener("click", closeDetail);
    $("#detail-mask").addEventListener("click", closeDetail);
    $("#d-reload").addEventListener("click", function () { loadDetail(true); });
    $("#d-ignore").addEventListener("click", function () {
      if (!detail.key) return;
      toggleIgnore(detail.key, detail.ignored, detail.meta).catch(function (err) {
        toast(err.message || "操作失败", "error");
      });
    });

    $("#search").addEventListener("input", function (e) {
      state.q = e.target.value.trim();
      if (state.data) renderTasks(state.data);
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeSettings(); closeDetail(); }
    });
  }

  // ---------------------------------------------------------------- 启动
  async function boot() {
    bind();
    setInterval(tick, 1000);
    setInterval(function () {
      if (!state.data) return;
      if ($("#app").classList.contains("hidden")) return;
      load().catch(function () { /* 网络抖动忽略 */ });
    }, 20000);

    try {
      var s = await api("/api/session");
      if (s.authenticated) {
        showApp();
        await load();
      } else {
        showLogin();
      }
    } catch (e) {
      showLogin();
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
