/* Twitch Recorder Pro — Studio Web UI Controller */
(function () {
  "use strict";

  const QUALITIES = ["best", "1440p60", "1080p60", "1080p", "720p60", "720p", "480p", "360p", "worst"];
  const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

  const state = {
    backend: null,
    page: "live",
    vodSub: "direct",
    studioSub: "trim",
    settingsSub: "general",
    config: {},
    diagnostics: {},
    exportPresets: {},
    channels: [],
    jobs: {},
    library: [],
    logs: [],
    catalogVods: [],
    catalogClips: [],
    catalogLoading: false,
    catalogError: "",
    highlights: { mode: "chat", heatmap: [], highlights: [] },
    sparkHistory: Array(28).fill(8),
    previewBust: {},
    // Draft selections
    manualBlurFile: "",
    manualBlurRegions: [],
    vodDraftRegions: [],
    studioFile: "",
    studioChatFile: "",
    studioMeta: null,
    studioIn: 0,
    studioOut: 60,
    studioTime: 0,
    studioSeekOffset: 0,
    studioSpeed: 1,
    studioFx: { speed: 1, color_preset: "none", brightness: 0, contrast: 1, saturation: 1, sharpen: false, fade_in: 0, fade_out: 0, volume_db: 0, denoise: false, voice_enhance: false, normalize_audio: false },
    studioThumb: { headline: "ЛУЧШИЙ МОМЕНТ СТРИМА", subtitle: "TWITCH HIGHLIGHTS", accent: "#8b5cf6" },
    aiHubSub: "viral",
    aiViralClips: [],
    aiChapters: "",
    aiSocialPost: "",
    aiCritique: null,
    aiCopilot: null,
    aiBanHits: [],
    aiBestFrames: [],
    aiThumbIdeas: [],
    aiQuotePath: "",
    aiChatSentiment: null,
    speechSearchQuery: "",
    speechSearchHits: [],
    localAi: null,
    multiviewSolo: "",
    scheduledPosts: [],
    analytics: null,
    obsScenes: [],
    mobileRemote: null,
    workspaces: ["Основной"],
    activeWorkspace: "Основной",
    subPreview: { show: false, text: "ЭТО БЫЛ ЛУЧШИЙ КЛАТЧ! 🔥", size: 28, color: "#fde047", pos: "bottom" },
    pub: {
      videoPath: "",
      formatMode: "full", // "full" | "clip" | "vertical"
      platforms: ["youtube", "telegram"],
      title: "",
      titleVariants: [],
      description: "",
      hashtags: "#twitch #стрим #нарезка #хайлайты #игры",
      thumbPath: "",
      thumbHeadline: "ЛУЧШИЙ МОМЕНТ СТРИМА",
      thumbSubtitle: "TWITCH HIGHLIGHTS",
      thumbAccent: "#8b5cf6",
      thumbTs: "00:15",
      privacy: "unlisted",
      clipStart: "00:00:00",
      clipEnd: "00:01:00",
      verticalLayout: "blur_bg",
      aiEngine: "",
      results: [],
    },
    shortsCam: null,
    shortsGame: null,
    libSearch: "",
    libFilter: "all",
    libStats: null,
    multiCutSegments: [],
    ytMeta: null,
    streamDigest: "",
    lastThumbPath: "",
    activeGame: "",
    settingsDraft: null,
    update: { status: "idle", info: null, progress: 0, progress_text: "", downloaded_path: "" },
  };

  // ==================== HELPERS ====================
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const appendFileSuffix = (filePath, suffix, forceExt = null) => {
    const str = String(filePath || "");
    const slashIdx = Math.max(str.lastIndexOf("/"), str.lastIndexOf("\\"));
    const dir = slashIdx >= 0 ? str.slice(0, slashIdx + 1) : "";
    const base = slashIdx >= 0 ? str.slice(slashIdx + 1) : str;
    const dotIdx = base.lastIndexOf(".");
    if (dotIdx > 0) {
      const stem = base.slice(0, dotIdx);
      const ext = forceExt || base.slice(dotIdx);
      return `${dir}${stem}${suffix}${ext}`;
    }
    return `${dir}${base}${suffix}${forceExt || ".mp4"}`;
  };
  const fmtDur = (sec) => {
    const s = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return [h, m, r].map((v) => String(v).padStart(2, "0")).join(":");
  };
  const parseSec = (txt) => {
    const s = String(txt || "").trim().replace(",", ".");
    if (!s) return null;
    if (s.includes(":")) {
      const p = s.split(":").map(Number);
      if (p.some(isNaN)) return null;
      return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1];
    }
    const n = Number(s);
    return isNaN(n) ? null : n;
  };
  const mediaUrl = (path) => `/media/file?path=${encodeURIComponent(path)}`;
  const videoPlayUrl = (path, ss = 0) => `/media/webm?path=${encodeURIComponent(path)}&ss=${Math.max(0, Number(ss) || 0).toFixed(2)}`;
  const thumbUrl = (path) => `/media/thumb?path=${encodeURIComponent(path)}`;

  function showToast(title, message = "", level = "info") {
    const stack = $("#toastStack");
    const el = document.createElement("div");
    el.className = `toast ${level}`;
    el.innerHTML = `<div class="toast-title">${esc(title)}</div>${message ? `<div class="toast-msg">${esc(message)}</div>` : ""}`;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateX(20px)";
      el.style.transition = "all 0.2s ease";
      setTimeout(() => el.remove(), 220);
    }, 3400);
  }

  function callBackend(method, ...args) {
    return new Promise((resolve) => {
      if (!state.backend || typeof state.backend[method] !== "function") return resolve(null);
      state.backend[method](...args, (res) => resolve(res));
    });
  }

  // ==================== NAVIGATION & BOOTSTRAP ====================
  function switchPage(page) {
    if (state.page === "settings" && page !== "settings") {
      state.settingsDraft = null;
    }
    if (state.page === "studio" && page !== "studio") {
      const mon = $("#studioMonitor");
      if (mon) {
        mon.pause();
        mon.removeAttribute("src");
        mon.load();
      }
      _studioLoadedSrc = "";
    }
    if (state.page === "multiview" && page !== "multiview") {
      document.querySelectorAll("#page-multiview video").forEach((v) => {
        v.pause();
        v.removeAttribute("src");
        v.load();
      });
    }
    state.page = page;
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
    document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
    if (page === "library") callBackend("scanLibrary");
    renderCurrentPage();
  }

  function renderCurrentPage() {
    if (state.page === "live") renderLivePage();
    else if (state.page === "multiview") renderMultiviewPage();
    else if (state.page === "vod") renderVodPage();
    else if (state.page === "library") renderLibraryPage();
    else if (state.page === "blur") renderBlurPage();
    else if (state.page === "studio") renderStudioPage();
    else if (state.page === "publish") renderPublishPage();
    else if (state.page === "analytics") renderAnalyticsPage();
    else if (state.page === "settings") renderSettingsPage();
    else if (state.page === "logs") renderLogsPage();
    updateNavBadges();
  }

  function updateNavBadges() {
    const recCnt = state.channels.filter((c) => c.is_recording).length;
    const allJobs = Object.values(state.jobs);
    const vodCnt = allJobs.filter((j) => j.category === "vod" && j.state === "running").length;
    const blurCnt = allJobs.filter((j) => j.category === "blur" && (j.state === "running" || j.state === "queued")).length;
    const stCnt = allJobs.filter((j) => j.category === "studio" && j.state === "running").length;

    const setB = (id, n) => {
      const el = $(id);
      if (!el) return;
      el.textContent = n;
      el.classList.toggle("hidden", n <= 0);
    };
    setB("#badgeRec", recCnt);
    setB("#badgeVod", vodCnt);
    setB("#badgeBlur", blurCnt);
    setB("#badgeStudio", stCnt);
  }

  // ==================== PAGE 1: LIVE CHANNELS ====================
  function renderLivePage() {
    const root = $("#page-live");
    const onlineCnt = state.channels.filter((c) => c.status?.is_live).length;
    const recCnt = state.channels.filter((c) => c.is_recording).length;

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Прямые эфиры</div>
          <div class="page-sub">Каналов: ${state.channels.length} · В эфире: ${onlineCnt} · Идёт запись: ${recCnt}</div>
        </div>
        <div class="header-actions">
          <label class="switch-inline" title="Автоматически начинать запись, когда стример выходит в эфир" style="background:rgba(255,255,255,0.03);border:1px solid var(--border);padding:7px 12px;border-radius:10px;">
            <input type="checkbox" id="liveAutoMon" ${state.config.auto_monitor_enabled ? "checked" : ""}/>
            <span class="switch-track"></span>
            <span>Автомониторинг</span>
          </label>
          <label class="switch-inline" title="Во время записи ноутбук не уснёт, а после завершения всех записей и блюра автоматически уйдёт в сон" style="background:rgba(255,255,255,0.03);border:1px solid var(--border);padding:7px 12px;border-radius:10px;">
            <input type="checkbox" id="liveAutoSleep" ${state.config.auto_sleep_after_finish ? "checked" : ""}/>
            <span class="switch-track"></span>
            <span>🌙 Сон после записи</span>
          </label>
          <button class="btn btn-subtle" id="btnCheckNow">${iconSvg("refresh", 15)}<span>Обновить</span></button>
          <button class="btn ${onlineCnt > 0 ? "btn-success" : "btn-subtle"}" id="btnRecAll" ${onlineCnt === 0 ? "disabled" : ""}>${iconSvg("record", 13)}<span>Записать всех${onlineCnt > 0 ? ` (${onlineCnt})` : ""}</span></button>
          <button class="btn ${recCnt > 0 ? "btn-danger" : "btn-subtle"}" id="btnStopAll" ${recCnt === 0 ? "disabled" : ""}>${iconSvg("stop", 13)}<span>Стоп всем${recCnt > 0 ? ` (${recCnt})` : ""}</span></button>
        </div>
      </div>

      <div class="live-kpi-strip">
        <div class="kpi-card">
          <div>
            <div class="kpi-label">Отслеживается каналов</div>
            <div class="kpi-val">${state.channels.length}</div>
          </div>
          <div class="kpi-icon">${iconSvg("radio", 18)}</div>
        </div>
        <div class="kpi-card">
          <div>
            <div class="kpi-label">Сейчас в эфире</div>
            <div class="kpi-val live">${onlineCnt}</div>
          </div>
          <div class="kpi-icon live">${iconSvg("play", 16)}</div>
        </div>
        <div class="kpi-card">
          <div>
            <div class="kpi-label">Идёт запись</div>
            <div class="kpi-val rec">${recCnt}</div>
          </div>
          <div class="kpi-icon rec">${iconSvg("record", 16)}</div>
        </div>
      </div>

      <div class="card add-bar">
        <select class="input-select" id="addPlat">
          <option value="twitch">Twitch</option>
          <option value="youtube">YouTube Live</option>
          <option value="kick">Kick</option>
          <option value="vkplay">VK Play Live</option>
          <option value="trovo">Trovo</option>
        </select>
        <input type="text" class="input" id="addChInput" placeholder="Введите ник стримера или вставьте ссылку (можно несколько через запятую)..." />
        <button class="btn btn-primary" id="btnAddCh">${iconSvg("plus", 16)}<span>Добавить канал</span></button>
      </div>

      ${state.channels.length === 0 ? `
        <div class="empty-state">
          ${iconSvg("radio", 38)}
          <h3>Добавьте каналы для записи</h3>
          <p>Поддерживаются Twitch, YouTube Live, Kick, VK Play Live и Trovo. Настраивайте зоны блюра, сцены, запись чата и автостарт для каждого стримера.</p>
        </div>
      ` : `<div class="channels-grid ${state.config.compact_cards ? "compact" : ""}" id="channelsGrid">${state.channels.map(renderChannelCard).join("")}</div>`}
    `;

    $("#liveAutoMon").onchange = async (e) => {
      const res = await callBackend("setAutoMonitor", e.target.checked);
      if (res) {
        try { state.config = JSON.parse(res); } catch {}
      }
    };
    $("#liveAutoSleep").onchange = async (e) => {
      const res = await callBackend("setAutoSleepAfterFinish", e.target.checked);
      if (res) {
        try { state.config = JSON.parse(res); } catch {}
      }
      showToast("Ночной режим сна", e.target.checked ? "Компьютер уснёт сам, когда все записи и блюр завершатся" : "Авто-сон после записи выключен", "info");
    };
    $("#btnCheckNow").onclick = () => { callBackend("checkChannels"); showToast("Проверка эфиров", "Обновляем статусы каналов...", "info"); };
    $("#btnRecAll").onclick = () => callBackend("recordAllOnline");
    $("#btnStopAll").onclick = () => callBackend("stopAllRecordings");

    const applyChannelsReturn = (raw) => {
      if (!raw) return;
      try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed.channels)) {
          state.channels = parsed.channels;
          if (state.page === "live") renderLivePage();
          updateNavBadges();
        }
      } catch {}
    };

    const doAdd = async () => {
      const val = $("#addChInput").value.trim();
      if (!val) return;
      $("#addChInput").value = "";
      const raw = await callBackend("addChannels", val, $("#addPlat").value);
      applyChannelsReturn(raw);
    };
    $("#btnAddCh").onclick = doAdd;
    $("#addChInput").onkeydown = (e) => { if (e.key === "Enter") doAdd(); };

    bindChannelCardEvents(root, applyChannelsReturn);
  }

  const SPLIT_PRESETS = [
    { val: 0, label: "1 файл" },
    { val: 15, label: "По 15 мин" },
    { val: 30, label: "По 30 мин" },
    { val: 60, label: "По 1 часу" },
    { val: 120, label: "По 2 часа" },
    { val: 180, label: "По 3 часа" },
  ];

  function renderChannelCard(ch) {
    const st = ch.status || {};
    const live = !!st.is_live;
    const rec = !!ch.is_recording;
    const tel = ch.telemetry || {};
    const zonesCnt = (ch.blur_regions || []).length;
    const presets = Object.keys(ch.blur_presets || { "Основной": [] });
    const segMin = Number(ch.segment_minutes || 0);
    const hasCustomSeg = segMin > 0 && !SPLIT_PRESETS.some((p) => p.val === segMin);
    const bust = state.previewBust[ch.key] || 1;
    const thumbHtml = live && ch.has_preview
      ? `<img src="/media/live/${encodeURIComponent(ch.key)}?t=${bust}" alt="" />`
      : iconSvg("radio", 24);

    return `
      <div class="channel-card ${rec ? "is-recording" : ""}" data-key="${esc(ch.key)}">
        <div class="ch-top">
          <div class="ch-thumb ${live ? "act-watch-live" : ""}" title="${live ? "Смотреть эфир в мини-плеере без рекламы" : ""}" style="${live ? "cursor:pointer;" : ""}">
            ${thumbHtml}
            ${live ? `<div class="media-play-hover"><div class="play-circle" style="width:36px;height:36px;">${iconSvg("play", 16)}</div><span style="font-size:10.5px;font-weight:700;color:#fff;">Смотреть</span></div>` : ""}
            <span class="ch-status-badge ${rec ? "rec" : (live ? "live" : "off")}">
              ${rec ? `<span class="pulse-dot"></span>ЗАПИСЬ` : (live ? `<span class="pulse-dot"></span>В ЭФИРЕ` : "ОФФЛАЙН")}
            </span>
            ${live && st.viewers ? `<span class="ch-viewers-badge">${st.viewers.toLocaleString("ru-RU")} зрит.</span>` : ""}
          </div>
          <div class="ch-info">
            <div class="ch-name-row">
              <span class="ch-name">${esc(ch.username)}</span>
              <span class="plat-tag plat-${esc(ch.platform)}">${esc(ch.platform)}</span>
              ${segMin > 0 ? `<span class="plat-tag" style="background:rgba(139,92,246,0.18);color:var(--accent-light);">✂ ${segMin >= 60 && segMin % 60 === 0 ? `${segMin / 60}ч` : `${segMin}м`}</span>` : ""}
              ${ch.schedule_enabled ? `<span class="plat-tag" style="background:rgba(255,255,255,0.06);color:var(--text-secondary)">${esc(ch.schedule_start)}–${esc(ch.schedule_end)}</span>` : ""}
            </div>
            <div class="ch-game">${live ? esc(st.game || "Прямой эфир") : (st.error ? esc(st.error) : "Стример не в сети")}</div>
            <div class="ch-title" title="${esc(st.title || "")}">${live && st.title ? esc(st.title) : "Ожидание начала трансляции"}</div>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end;">
            ${rec
              ? `<button class="btn btn-danger btn-sm act-stop">${iconSvg("stop", 12)}<span>Стоп</span></button>`
              : `<button class="btn ${live ? "btn-success" : "btn-subtle"} btn-sm act-rec">${iconSvg("record", 12)}<span>Запись</span></button>`}
            <div style="display:flex;gap:4px;">
              ${live ? `<button class="btn-icon act-watch-live" title="Смотреть эфир в мини-плеере без рекламы">${iconSvg("play", 14)}</button>` : ""}
              <button class="btn-icon act-settings" title="Параметры канала, расписание, чат, авто-клипы">${iconSvg("gear", 15)}</button>
              <button class="btn-icon danger act-del" title="Удалить канал">${iconSvg("trash", 15)}</button>
            </div>
          </div>
        </div>

        ${rec ? `
          <div class="ch-rec-strip">
            <span class="ch-rec-stats" data-tel-key="${esc(ch.key)}">${esc(tel.state || "Запись")} · ${esc(tel.dur || "00:00:00")} · ${esc(tel.size || "0 КБ")} · ${esc(tel.speed || "1.0x")}</span>
            <div style="display:flex;gap:6px;">
              <button class="btn btn-subtle btn-sm act-dvr" title="Откат: сохранить последние 60 секунд эфира в AutoClips/">${iconSvg("scissors", 13)}<span>Откат 60с</span></button>
              <button class="btn btn-soft btn-sm act-marker" title="Поставить маркер хайлайта (также ${esc(state.config.marker_hotkey || "F9")})">${iconSvg("bookmark", 13)}<span>Маркер</span></button>
            </div>
          </div>
        ` : ""}

        <div class="ch-controls">
          <select class="input-select compact act-quality" title="Качество записи">
            ${QUALITIES.map((q) => `<option value="${q}" ${ch.quality === q ? "selected" : ""}>${q}</option>`).join("")}
          </select>
          <select class="input-select compact act-segmin" title="Разбивка записи на части">
            ${SPLIT_PRESETS.map((p) => `<option value="${p.val}" ${segMin === p.val ? "selected" : ""}>${p.label}</option>`).join("")}
            ${hasCustomSeg ? `<option value="${segMin}" selected>По ${segMin} мин</option>` : ""}
          </select>
          <select class="input-select compact act-blurmode" title="Режим блюра">
            <option value="postprocess" ${ch.blur_mode === "postprocess" ? "selected" : ""}>Пост-блюр</option>
            <option value="realtime" ${ch.blur_mode === "realtime" ? "selected" : ""}>Реалтайм</option>
            <option value="none" ${ch.blur_mode === "none" ? "selected" : ""}>Без блюра</option>
          </select>
          ${presets.length > 1 ? `
            <select class="input-select compact act-preset" title="Активная сцена блюра">
              ${presets.map((p) => `<option value="${esc(p)}" ${ch.active_preset === p ? "selected" : ""}>${esc(p)}</option>`).join("")}
            </select>
          ` : ""}
          <button class="btn ${zonesCnt > 0 ? "btn-soft" : "btn-subtle"} btn-sm act-blur">
            ${iconSvg("crop", 14)}<span>${zonesCnt > 0 ? `Зоны: ${zonesCnt}` : "Блюр"}</span>
          </button>
          <div class="ch-controls-right">
            <label class="switch-inline" title="Автоматически записывать при выходе в эфир">
              <input type="checkbox" class="act-auto" ${ch.auto_record ? "checked" : ""}/>
              <span class="switch-track"></span>
              <span style="font-size:12px;">Авто</span>
            </label>
          </div>
        </div>
      </div>
    `;
  }

  function bindChannelCardEvents(root, applyChannelsReturn) {
    root.querySelectorAll(".channel-card").forEach((card) => {
      const key = card.dataset.key;
      const ch = state.channels.find((c) => c.key === key);
      if (!ch) return;
      card.querySelector(".act-rec")?.addEventListener("click", () => callBackend("startRecording", key));
      card.querySelector(".act-stop")?.addEventListener("click", () => callBackend("stopRecording", key));
      card.querySelector(".act-marker")?.addEventListener("click", () => callBackend("dropMarker", key));
      card.querySelector(".act-dvr")?.addEventListener("click", () => callBackend("clipLastSeconds", key, 60));
      card.querySelectorAll(".act-watch-live").forEach((el) => {
        el.addEventListener("click", () => openLiveStreamPlayer(ch));
      });
      card.querySelector(".act-del")?.addEventListener("click", async () => {
        const raw = await callBackend("deleteChannel", key);
        if (applyChannelsReturn) applyChannelsReturn(raw);
      });
      card.querySelector(".act-settings")?.addEventListener("click", () => openChannelDrawer(ch));
      card.querySelector(".act-blur")?.addEventListener("click", () => openBlurStudio({ mode: "channel", channel: ch }));
      card.querySelector(".act-quality")?.addEventListener("change", async (e) => {
        const raw = await callBackend("updateChannelField", key, JSON.stringify({ quality: e.target.value }));
        if (applyChannelsReturn) applyChannelsReturn(raw);
      });
      card.querySelector(".act-segmin")?.addEventListener("change", async (e) => {
        const mins = Math.max(0, Number(e.target.value) || 0);
        const raw = await callBackend("updateChannelField", key, JSON.stringify({ segment_minutes: mins }));
        if (applyChannelsReturn) applyChannelsReturn(raw);
        showToast("Разбивка записи", mins > 0 ? `Канал ${ch.username}: части по ${mins} мин` : `Канал ${ch.username}: одним файлом`, "info");
      });
      card.querySelector(".act-blurmode")?.addEventListener("change", async (e) => {
        const raw = await callBackend("updateChannelField", key, JSON.stringify({ blur_mode: e.target.value }));
        if (applyChannelsReturn) applyChannelsReturn(raw);
      });
      card.querySelector(".act-preset")?.addEventListener("change", async (e) => {
        const raw = await callBackend("updateChannelField", key, JSON.stringify({ active_preset: e.target.value }));
        if (applyChannelsReturn) applyChannelsReturn(raw);
      });
      card.querySelector(".act-auto")?.addEventListener("change", async (e) => {
        const raw = await callBackend("updateChannelField", key, JSON.stringify({ auto_record: e.target.checked }));
        if (applyChannelsReturn) applyChannelsReturn(raw);
      });
    });
  }

  // ==================== PAGE 2: VOD & CLIPS ====================
  function renderVodPage() {
    const root = $("#page-vod");
    const vodJobs = Object.values(state.jobs).filter((j) => j.category === "vod").reverse();
    const chPresets = state.channels.filter((c) => (c.blur_regions || []).length > 0);

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">VOD и Клипы</div>
          <div class="page-sub">Многопоточная загрузка прошлых эфиров, встроенный архив канала и авто-подборки топ-клипов</div>
        </div>
      </div>

      <div class="subnav" id="vodSubnav">
        <button data-sub="direct" class="${state.vodSub === "direct" ? "active" : ""}">По ссылке</button>
        <button data-sub="archive" class="${state.vodSub === "archive" ? "active" : ""}">Архив VOD канала</button>
        <button data-sub="clips" class="${state.vodSub === "clips" ? "active" : ""}">Топ-клипы и Подборки</button>
      </div>

      ${state.vodSub === "direct" ? `
        <div class="card">
          <div class="form-grid-3" style="grid-template-columns: 3fr 1fr 1fr; margin-bottom: 12px;">
            <div class="field">
              <label>Ссылка на Twitch VOD / клип / YouTube</label>
              <input class="input" id="vodUrl" placeholder="https://www.twitch.tv/videos/1234567890" />
            </div>
            <div class="field">
              <label>Качество</label>
              <select class="input-select" id="vodQual">
                <option>best</option><option>1080p</option><option>720p</option><option>480p</option><option>360p</option>
              </select>
            </div>
            <div class="field">
              <label>Авто-блюр после загрузки</label>
              <select class="input-select" id="vodBlurPreset">
                <option value="">Без блюра</option>
                ${state.vodDraftRegions.length ? `<option value="__custom__" selected>Свои зоны (${state.vodDraftRegions.length})</option>` : ""}
                ${chPresets.map((c) => `<option value="${esc(c.key)}">Канал ${esc(c.username)} (${c.blur_regions.length})</option>`).join("")}
              </select>
            </div>
          </div>
          <div class="input-row">
            <div class="field" style="width:130px;"><label>Начало (опц.)</label><input class="input" id="vodStart" placeholder="00:00:00" /></div>
            <div class="field" style="width:130px;"><label>Конец (опц.)</label><input class="input" id="vodEnd" placeholder="01:00:00" /></div>
            <div style="flex:1"></div>
            <button class="btn btn-soft" id="btnVodBlurPick">${iconSvg("crop", 15)}<span>Зоны на кадре (${state.vodDraftRegions.length})</span></button>
            <button class="btn btn-primary" id="btnVodStart">${iconSvg("download", 16)}<span>Скачать VOD</span></button>
          </div>
        </div>
      ` : state.vodSub === "archive" ? `
        <div class="card">
          <div class="input-row" style="margin-bottom:14px;">
            <input class="input" id="catVodInput" placeholder="Ник канала Twitch (напр. shroud)..." value="${esc(state.channels[0]?.username || "")}" />
            <button class="btn btn-primary" id="btnFetchVods">${iconSvg("search", 15)}<span>Загрузить архив</span></button>
          </div>
          ${state.catalogLoading ? `<div class="empty-state"><p>Загрузка архива VOD с Twitch...</p></div>` :
            state.catalogError ? `<div class="empty-state"><p>${esc(state.catalogError)}</p></div>` :
            state.catalogVods.length === 0 ? `<div class="empty-state"><p>Введите ник стримера Twitch, чтобы увидеть его последние записи.</p></div>` :
            `<div class="media-grid">${state.catalogVods.map((v) => `
              <div class="media-card">
                <div class="media-thumb">
                  <img src="${esc(v.thumbnail_url)}" alt="" />
                  <span class="media-dur">${fmtDur(v.duration_sec)}</span>
                </div>
                <div class="media-body">
                  <div class="media-title">${esc(v.title)}</div>
                  <div class="media-meta">${esc(v.created_at)} · ${(v.views || 0).toLocaleString("ru-RU")} просм. · ${esc(v.game || "")}</div>
                  <div class="media-actions">
                    <button class="btn btn-primary btn-sm btn-full act-dl-vod" data-url="${esc(v.url)}" data-title="${esc(v.title)}">${iconSvg("download", 14)}<span>Скачать VOD</span></button>
                  </div>
                </div>
              </div>
            `).join("")}</div>`}
        </div>
      ` : `
        <div class="card">
          <div class="input-row" style="margin-bottom:14px;flex-wrap:wrap;">
            <input class="input" style="flex:1;min-width:180px;" id="catClipInput" placeholder="Ник канала Twitch..." value="${esc(state.channels[0]?.username || "")}" />
            <select class="input-select" style="width:155px;" id="catClipPeriod">
              <option value="LAST_DAY">За день</option>
              <option value="LAST_WEEK" selected>За неделю</option>
              <option value="LAST_MONTH">За месяц</option>
              <option value="ALL_TIME">За всё время</option>
            </select>
            <button class="btn btn-primary" id="btnFetchClips">${iconSvg("search", 15)}<span>Найти клипы</span></button>
            <button class="btn btn-subtle" id="btnDlClips">${iconSvg("download", 15)}<span>Скачать выбранные</span></button>
            <button class="btn btn-soft" id="btnMergeClips">${iconSvg("sparkles", 15)}<span>Склеить в подборку</span></button>
          </div>
          ${state.catalogLoading ? `<div class="empty-state"><p>Загрузка топ-клипов...</p></div>` :
            state.catalogClips.length === 0 ? `<div class="empty-state"><p>Найдите популярные клипы любого канала и склейте их в готовую нарезку одной кнопкой.</p></div>` :
            `<div class="media-grid">${state.catalogClips.map((c, idx) => `
              <div class="media-card">
                <div class="media-thumb">
                  <img src="${esc(c.thumbnail_url)}" alt="" />
                  <span class="media-dur">${Math.round(c.duration_sec)}с</span>
                  <label style="position:absolute;top:8px;left:8px;background:rgba(9,10,15,0.8);padding:4px 8px;border-radius:7px;cursor:pointer;display:flex;align-items:center;gap:6px;font-size:11.5px;">
                    <input type="checkbox" class="clip-cb" data-url="${esc(c.url)}" ${idx < 5 ? "checked" : ""}/> Выбрать
                  </label>
                </div>
                <div class="media-body">
                  <div class="media-title">${esc(c.title)}</div>
                  <div class="media-meta">${(c.views || 0).toLocaleString("ru-RU")} просм. · ${esc(c.created_at)}</div>
                </div>
              </div>
            `).join("")}</div>`}
        </div>
      `}

      <div class="section-heading"><span>Загрузки VOD и клипов (${vodJobs.length})</span></div>
      ${vodJobs.length ? `<div class="jobs-list">${vodJobs.map(renderJobCard).join("")}</div>` : `<div class="empty-state">${iconSvg("film", 34)}<h3>Активных загрузок нет</h3><p>Запущенные загрузки VOD и подборки клипов отобразятся здесь с живым прогрессом.</p></div>`}
    `;

    root.querySelectorAll("#vodSubnav button").forEach((b) => {
      b.onclick = () => { state.vodSub = b.dataset.sub; renderVodPage(); };
    });

    $("#btnVodBlurPick")?.addEventListener("click", () => {
      openBlurStudio({ mode: "vod", vodUrl: $("#vodUrl")?.value.trim() || "", regions: state.vodDraftRegions });
    });
    $("#btnVodStart")?.addEventListener("click", () => {
      const url = $("#vodUrl").value.trim();
      if (!url) return showToast("Укажите ссылку", "Вставьте ссылку на Twitch VOD", "error");
      const preset = $("#vodBlurPreset").value;
      callBackend("startVodDownload", JSON.stringify({
        url, quality: $("#vodQual").value, start_time: $("#vodStart").value, end_time: $("#vodEnd").value,
        preset_channel_key: preset && preset !== "__custom__" ? preset : "",
        regions: preset === "__custom__" ? state.vodDraftRegions : [],
      }));
      $("#vodUrl").value = "";
    });
    $("#btnFetchVods")?.addEventListener("click", () => {
      const u = $("#catVodInput").value.trim();
      if (!u) return;
      state.catalogLoading = true; state.catalogError = ""; renderVodPage();
      callBackend("fetchCatalog", "vods", u, "");
    });
    $("#btnFetchClips")?.addEventListener("click", () => {
      const u = $("#catClipInput").value.trim();
      if (!u) return;
      state.catalogLoading = true; state.catalogError = ""; renderVodPage();
      callBackend("fetchCatalog", "clips", u, $("#catClipPeriod").value);
    });
    root.querySelectorAll(".act-dl-vod").forEach((btn) => {
      btn.onclick = () => {
        callBackend("startVodDownload", JSON.stringify({ url: btn.dataset.url, title: btn.dataset.title, quality: "best" }));
        showToast("Загрузка VOD запущена", btn.dataset.title, "success");
      };
    });
    const runBatch = (merge) => {
      const urls = [...root.querySelectorAll(".clip-cb:checked")].map((cb) => cb.dataset.url);
      if (!urls.length) return showToast("Клипы не выбраны", "Отметьте нужные клипы галочкой", "error");
      callBackend("downloadClipsBatch", JSON.stringify({ channel: $("#catClipInput").value.trim() || "twitch", urls, merge }));
      showToast(merge ? "Сборка подборки" : "Загрузка клипов", `Выбрано клипов: ${urls.length}`, "success");
    };
    $("#btnDlClips")?.addEventListener("click", () => runBatch(false));
    $("#btnMergeClips")?.addEventListener("click", () => runBatch(true));

    bindJobEvents(root);
  }

  // ==================== PAGE 3: MEDIA LIBRARY ====================
  function getFilteredLibrary() {
    const q = state.libSearch.toLowerCase();
    return state.library.filter((f) => {
      if (q && !f.name.toLowerCase().includes(q) && !f.folder.toLowerCase().includes(q)) return false;
      if (state.libFilter === "blurred" && !f.is_blurred) return false;
      if (state.libFilter === "raw" && f.is_blurred) return false;
      if (state.libFilter === "chat" && !f.has_chat && !f.has_markers) return false;
      return true;
    });
  }

  function renderLibraryGrid() {
    const filtered = getFilteredLibrary();
    const subEl = $("#libCountSub");
    if (subEl) subEl.textContent = `Найдено видеофайлов: ${filtered.length} — встроенный просмотр, отправка в блюр и монтаж в 1 клик`;
    const statsWrap = $("#libStatsWrap");
    if (statsWrap && state.libStats) {
      const st = state.libStats;
      const topHtml = (st.top_folders || []).map((f) => `<span class="diag-pill off">${esc(f.name)}: ${esc(f.size_str)}</span>`).join("");
      statsWrap.innerHTML = `
        <div class="stats-strip">
          <div class="stats-pills">
            <span class="diag-pill">Всего: ${esc(st.total_size_str)} (${st.total_files} видео)</span>
            ${topHtml}
          </div>
          ${st.redundant_raw_count > 0 ? `
            <button class="btn btn-soft btn-sm" id="btnCleanRaw">${iconSvg("trash", 13)}<span>Очистить ${st.redundant_raw_count} лишних _raw (${esc(st.redundant_raw_size_str)})</span></button>
          ` : ""}
        </div>
      `;
      $("#btnCleanRaw")?.addEventListener("click", () => {
        if (confirm(`Удалить ${st.redundant_raw_count} исходников _raw, для которых уже готов _blurred (${st.redundant_raw_size_str})?`)) {
          callBackend("cleanRedundantRawFiles");
        }
      });
    }
    const wrap = $("#libGridWrap");
    if (!wrap) return;
    wrap.innerHTML = filtered.length === 0 ? `
      <div class="empty-state">${iconSvg("grid", 38)}<h3>Записей пока нет</h3><p>После записи стримов или загрузки VOD все файлы появятся здесь с превью.</p></div>
    ` : `
      <div class="media-grid">
        ${filtered.map((f, idx) => `
          <div class="media-card">
            <div class="media-thumb act-play" data-idx="${idx}" data-path="${esc(f.path)}" data-name="${esc(f.name)}">
              <img src="${thumbUrl(f.path)}" loading="lazy" alt="" />
              <div class="media-play-hover"><div class="play-circle">${iconSvg("play", 20)}</div></div>
              <div class="media-badges">
                ${f.resolution ? `<span class="tag-chip" style="background:rgba(15,23,42,0.82)">${esc(f.resolution)}</span>` : ""}
                ${f.is_blurred ? `<span class="tag-chip">БЛЮР</span>` : ""}
                ${f.has_chat ? `<span class="tag-chip green">ЧАТ</span>` : ""}
                ${f.has_markers ? `<span class="tag-chip blue">МАРКЕРЫ</span>` : ""}
              </div>
              <span class="media-dur">${f.duration_str ? `${esc(f.duration_str)} · ` : ""}${esc(f.size_str)}</span>
            </div>
            <div class="media-body">
              <div class="media-title" title="${esc(f.title || f.name)}">${esc(f.name)}</div>
              <div class="media-meta">${esc(f.folder)}${f.game ? ` · ${esc(f.game)}` : ""} · ${esc(f.mtime)}</div>
              <div class="media-actions">
                <button class="btn btn-soft btn-sm act-to-blur" data-path="${esc(f.path)}">${iconSvg("blur", 13)}<span>Блюр</span></button>
                <button class="btn btn-subtle btn-sm act-to-studio" data-path="${esc(f.path)}">${iconSvg("scissors", 13)}<span>Монтаж</span></button>
                <button class="btn-icon act-to-pub" data-path="${esc(f.path)}" title="Отправить в Автопостинг (YouTube / TikTok / Telegram)">${iconSvg("upload", 14)}</button>
                <button class="btn-icon act-repair" data-path="${esc(f.path)}" title="MP4 / MKV Doctor: проверить и починить файл для монтажа">${iconSvg("refresh", 14)}</button>
                <button class="btn-icon act-tg-pub" data-path="${esc(f.path)}" data-name="${esc(f.name)}" title="Быстро отправить в Telegram">${iconSvg("bell", 14)}</button>
                <button class="btn-icon act-reveal" data-path="${esc(f.path)}" title="Показать в проводнике">${iconSvg("folder", 14)}</button>
                <button class="btn-icon danger act-rm" data-path="${esc(f.path)}" data-name="${esc(f.name)}" title="Удалить">${iconSvg("trash", 14)}</button>
              </div>
            </div>
          </div>
        `).join("")}
      </div>
    `;

    wrap.querySelectorAll(".act-play").forEach((el) => {
      el.onclick = () => {
        const item = filtered[Number(el.dataset.idx)];
        openLightboxPlayer(el.dataset.path, el.dataset.name, item?.markers || []);
      };
    });
    wrap.querySelectorAll(".act-to-blur").forEach((el) => {
      el.onclick = () => { state.manualBlurFile = el.dataset.path; switchPage("blur"); };
    });
    wrap.querySelectorAll(".act-to-studio").forEach((el) => {
      el.onclick = () => { switchPage("studio"); loadStudioVideo(el.dataset.path); };
    });
    wrap.querySelectorAll(".act-to-pub").forEach((el) => {
      el.onclick = () => { state.pub.videoPath = el.dataset.path; switchPage("publish"); };
    });
    wrap.querySelectorAll(".act-repair").forEach((el) => {
      el.onclick = () => callBackend("repairVideoFile", el.dataset.path);
    });
    wrap.querySelectorAll(".act-reveal").forEach((el) => {
      el.onclick = () => callBackend("openInExplorer", el.dataset.path);
    });
    wrap.querySelectorAll(".act-tg-pub").forEach((el) => {
      el.onclick = () => callBackend("publishVideoToTelegram", el.dataset.path, `🎬 ${el.dataset.name}`);
    });
    wrap.querySelectorAll(".act-rm").forEach((el) => {
      el.onclick = () => {
        if (confirm(`Удалить файл «${el.dataset.name}» с диска?`)) callBackend("deleteRecordingFile", el.dataset.path);
      };
    });
  }

  function renderLibraryPage() {
    const root = $("#page-library");
    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Библиотека записей</div>
          <div class="page-sub" id="libCountSub"></div>
        </div>
        <div class="header-actions">
          <button class="btn btn-subtle" id="btnLibRefresh">${iconSvg("refresh", 15)}<span>Обновить</span></button>
          <button class="btn btn-subtle" id="btnLibFolder">${iconSvg("folder", 15)}<span>Открыть папку</span></button>
        </div>
      </div>

      <div id="libStatsWrap"></div>

      <div class="card add-bar" style="margin-bottom:12px;">
        <input class="input" id="libSearch" placeholder="Поиск по каналу или имени файла..." value="${esc(state.libSearch)}" />
        <select class="input-select" id="libFilter" style="width:200px;">
          <option value="all" ${state.libFilter === "all" ? "selected" : ""}>Все видео</option>
          <option value="blurred" ${state.libFilter === "blurred" ? "selected" : ""}>Только с блюром</option>
          <option value="raw" ${state.libFilter === "raw" ? "selected" : ""}>Только исходники</option>
          <option value="chat" ${state.libFilter === "chat" ? "selected" : ""}>Есть чат / маркеры</option>
        </select>
      </div>

      <div class="card add-bar" style="margin-bottom:16px;padding:10px 14px;">
        <input class="input" id="libSpeechInp" placeholder="🔍 AI Поиск по речи и чату внутри всех записей (напр. «клатч», «подарил», «смешно»)..." value="${esc(state.speechSearchQuery)}" />
        <button class="btn btn-soft btn-sm" id="btnLibSpeechSearch">${iconSvg("sparkles", 14)}<span>Найти момент</span></button>
      </div>
      <div id="libSpeechResults"></div>

      <div id="libGridWrap"></div>
    `;

    $("#btnLibRefresh").onclick = () => callBackend("scanLibrary");
    $("#btnLibFolder").onclick = () => callBackend("openInExplorer", "");
    $("#libSearch").oninput = (e) => { state.libSearch = e.target.value; renderLibraryGrid(); };
    $("#libFilter").onchange = (e) => { state.libFilter = e.target.value; renderLibraryGrid(); };
    const doSpeechSearch = () => {
      const q = $("#libSpeechInp").value.trim();
      state.speechSearchQuery = q;
      if (!q) { state.speechSearchHits = []; renderLibrarySpeechHits(); return; }
      callBackend("runAiSuiteTask", "speech_search", JSON.stringify({ query: q }));
      showToast("AI Поиск по речи", `Ищем «${q}» по всем записям...`, "info");
    };
    $("#btnLibSpeechSearch").onclick = doSpeechSearch;
    $("#libSpeechInp").onkeydown = (e) => { if (e.key === "Enter") doSpeechSearch(); };

    renderLibrarySpeechHits();
    renderLibraryGrid();
  }

  function renderLibrarySpeechHits() {
    const box = $("#libSpeechResults");
    if (!box) return;
    if (!state.speechSearchHits.length) { box.innerHTML = ""; return; }
    box.innerHTML = `
      <div class="card" style="margin-bottom:16px;padding:12px 14px;">
        <div class="input-row" style="justify-content:space-between;margin-bottom:8px;">
          <strong style="font-size:13px;">🔍 Найдено совпадений по «${esc(state.speechSearchQuery)}»: ${state.speechSearchHits.length}</strong>
          <button class="btn btn-subtle btn-sm" id="btnClearSpHits">Скрыть</button>
        </div>
        <div class="hl-list" style="max-height:190px;">
          ${state.speechSearchHits.map((h) => `
            <div class="hl-row">
              <span class="mono" style="color:var(--accent-light);font-weight:700;">${fmtDur(h.ts)}</span>
              <span style="font-size:11.5px;color:var(--text-muted);">${esc(h.source)}</span>
              <span style="flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(h.text)}"><strong>${esc(h.name)}:</strong> ${esc(h.text)}</span>
              <button class="btn btn-soft btn-sm act-jump-sp" data-path="${esc(h.video_path)}" data-ts="${h.ts}">Открыть</button>
            </div>
          `).join("")}
        </div>
      </div>
    `;
    $("#btnClearSpHits").onclick = () => { state.speechSearchHits = []; renderLibrarySpeechHits(); };
    box.querySelectorAll(".act-jump-sp").forEach((b) => {
      b.onclick = () => {
        state.studioFile = b.dataset.path;
        switchPage("studio");
        setTimeout(() => {
          const mon = $("#studioMonitor");
          if (mon) mon.currentTime = Number(b.dataset.ts) || 0;
        }, 400);
      };
    });
  }

  // ==================== PAGE 4: BLUR & GPU QUEUE ====================
  function renderBlurPage() {
    const root = $("#page-blur");
    const blurJobs = Object.values(state.jobs).filter((j) => j.category === "blur").reverse();
    const chPresets = state.channels.filter((c) => (c.blur_regions || []).length > 0);
    const gmNote = state.activeGame ? ` · 🎮 Игровой режим (${state.activeGame})` : "";

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Блюр и Умная очередь GPU</div>
          <div class="page-sub">Лимит потоков GPU: ${state.config.max_concurrent_blurs || 1} · Ночной рендер: ${state.config.night_render_enabled ? `ВКЛ (${state.config.night_render_start}–${state.config.night_render_end})` : "ВЫКЛ"}${esc(gmNote)}</div>
        </div>
        <div class="header-actions">
          <button class="btn btn-soft" id="btnBatchBlur">${iconSvg("plus", 14)}<span>Пакетный блюр (2+ файлов)</span></button>
          <button class="btn btn-success" id="btnQueueNow">${iconSvg("play", 13)}<span>Обработать очередь сейчас</span></button>
        </div>
      </div>

      <div class="card drop-zone" id="blurDropZone">
        <div class="input-row" style="margin-bottom:14px;">
          <input class="input" id="blurFileInp" value="${esc(state.manualBlurFile)}" placeholder="Перетащите видео из Finder/Проводника сюда или выберите файл (.mp4 / .mkv)..." />
          <button class="btn btn-subtle" id="btnPickBlurFile">${iconSvg("folder", 15)}<span>Выбрать файл</span></button>
        </div>
        <div class="input-row" style="flex-wrap:wrap;">
          <select class="input-select" style="width:230px;" id="blurPresetSel">
            <option value="">Свои зоны (${state.manualBlurRegions.length})</option>
            ${chPresets.map((c) => `<option value="${esc(c.key)}">Пресет ${esc(c.username)} (${c.blur_regions.length} зон)</option>`).join("")}
          </select>
          <button class="btn btn-soft" id="btnOpenBlurStudio">${iconSvg("crop", 15)}<span>Открыть студию блюра (${state.manualBlurRegions.length})</span></button>
          <label class="switch-inline"><input type="checkbox" id="blurDelOrig"/><span class="switch-track"></span><span>Удалить исходник</span></label>
          <label class="switch-inline"><input type="checkbox" id="blurNormAudio" ${state.config.normalize_audio_default ? "checked" : ""}/><span class="switch-track"></span><span>Выровнять звук</span></label>
          <div style="flex:1"></div>
          <button class="btn btn-primary" id="btnEnqueueBlur">${iconSvg("sparkles", 15)}<span>Запустить блюр</span></button>
        </div>
      </div>

      <div class="section-heading"><span>Очередь обработки (${blurJobs.length})</span></div>
      ${blurJobs.length ? `<div class="jobs-list">${blurJobs.map(renderJobCard).join("")}</div>` : `
        <div class="empty-state">${iconSvg("blur", 36)}<h3>Очередь блюра пуста</h3><p>Перетащите видеофайл прямо в окно или выберите вручную. Сюда также попадают задачи после стримов и VOD.</p></div>
      `}
    `;

    $("#btnQueueNow").onclick = () => callBackend("processBlurQueueNow");
    $("#blurFileInp").oninput = (e) => { state.manualBlurFile = e.target.value; };
    const pickBlurVideo = async () => {
      const p = await callBackend("pickFile", "Видео (*.mp4 *.mkv *.ts *.flv *.mov)");
      if (p) { state.manualBlurFile = p; renderBlurPage(); }
    };
    $("#btnPickBlurFile").onclick = pickBlurVideo;
    $("#btnBatchBlur").onclick = async () => {
      const raw = await callBackend("pickVideoFiles");
      const files = JSON.parse(raw || "[]");
      if (!files.length) return;
      const presetKey = $("#blurPresetSel").value;
      callBackend("batchEnqueueBlur", JSON.stringify({
        input_paths: files,
        preset_channel_key: presetKey,
        regions: presetKey ? [] : state.manualBlurRegions,
        delete_original: $("#blurDelOrig").checked,
        normalize_audio: $("#blurNormAudio").checked,
        force_now: true,
      }));
    };
    const dz = $("#blurDropZone");
    if (dz) {
      dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
      dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
      dz.addEventListener("drop", () => dz.classList.remove("dragover"));
    }
    $("#btnOpenBlurStudio").onclick = () => {
      openBlurStudio({ mode: "manual", videoPath: state.manualBlurFile, regions: state.manualBlurRegions });
    };
    $("#btnEnqueueBlur").onclick = () => {
      const presetKey = $("#blurPresetSel").value;
      callBackend("enqueueBlur", JSON.stringify({
        input_path: state.manualBlurFile,
        preset_channel_key: presetKey,
        regions: presetKey ? [] : state.manualBlurRegions,
        delete_original: $("#blurDelOrig").checked,
        normalize_audio: $("#blurNormAudio").checked,
        force_now: true,
      }));
    };

    bindJobEvents(root);
  }

  // ==================== PAGE 5: PRO STUDIO 3.0 NLE WORKSPACE ====================
  let _studioLoadedSrc = "";
  let _studioProbedFile = "";
  let _studioTlMoveHandler = null;
  let _studioTlUpHandler = null;

  function getStudioDuration() {
    const mon = $("#studioMonitor");
    const mDur = Number(state.studioMeta?.duration || 0);
    const vDur = (mon && isFinite(mon.duration) && mon.duration > 0) ? mon.duration : 0;
    return Math.max(1, mDur || vDur || 60);
  }

  function getStudioTime() {
    const mon = $("#studioMonitor");
    return Math.max(0, (state.studioSeekOffset || 0) + (mon ? (mon.currentTime || 0) : 0));
  }

  function seekStudioMonitor(targetSec, autoplay = true) {
    const mon = $("#studioMonitor");
    if (!mon || !state.studioFile) return;
    const dur = getStudioDuration();
    const clamped = Math.max(0, Math.min(dur, Number(targetSec) || 0));
    state.studioSeekOffset = clamped;
    state.studioTime = clamped;
    const tc = $("#stTimecode");
    if (tc) tc.textContent = `${fmtDur(clamped)} / ${fmtDur(dur)}`;
    const ph = $("#tlPlayhead");
    if (ph && dur > 0) ph.style.left = `${Math.min(100, Math.max(0, (clamped / dur) * 100))}%`;
    mon.src = videoPlayUrl(state.studioFile, clamped);
    mon.playbackRate = state.studioSpeed || 1;
    mon.load();
    if (autoplay) mon.play().catch(() => {});
  }

  async function loadStudioVideo(path) {
    if (!path) return;
    state.studioFile = path;
    state.studioTime = 0;
    state.studioSeekOffset = 0;
    state.studioMeta = null;
    state.multiCutSegments = [];
    renderStudioPage();
    _studioProbedFile = path;
    const raw = await callBackend("probeStudioVideo", path);
    if (_studioProbedFile !== path) return;
    try {
      const m = JSON.parse(raw || "{}");
      if (m.ok) {
        state.studioMeta = m;
        state.studioIn = 0;
        state.studioOut = m.duration > 0 ? Math.min(m.duration, Math.max(10, m.duration)) : 60;
        if (m.chat_path) state.studioChatFile = m.chat_path;
        if (state.page === "studio") renderStudioPage(true);
      }
    } catch {}
  }

  function studioFxCssFilter() {
    const fx = state.studioFx;
    let b = 1 + (Number(fx.brightness) || 0);
    let c = Number(fx.contrast) || 1;
    let s = Number(fx.saturation) || 1;
    if (fx.color_preset === "vibrant") { b += 0.03; c *= 1.08; s *= 1.24; }
    else if (fx.color_preset === "cinema") { b -= 0.03; c *= 1.14; s *= 0.88; }
    else if (fx.color_preset === "crisp") { c *= 1.10; s *= 1.10; }
    return `brightness(${b.toFixed(2)}) contrast(${c.toFixed(2)}) saturate(${s.toFixed(2)})`;
  }

  function renderStudioPage(forceRebuild = false) {
    const root = $("#page-studio");
    const stJobs = Object.values(state.jobs).filter((j) => j.category === "studio").reverse();
    const hasVid = !!state.studioFile;

    if (hasVid && _studioProbedFile !== state.studioFile) {
      loadStudioVideo(state.studioFile);
      return;
    }

    const existingMon = $("#studioMonitor");
    if (!forceRebuild && hasVid && existingMon && _studioLoadedSrc === state.studioFile && $("#studioInspectorHost")) {
      updateStudioToolTabsUI();
      renderStudioInspector();
      syncStudioMonitorOverlays();
      renderStudioTimelineUI();
      renderStudioJobsDock(stJobs);
      return;
    }

    if (!hasVid) {
      _studioLoadedSrc = "";
      if (!state.library.length) callBackend("scanLibrary");
      const recent = state.library.slice(0, 6);
      root.innerHTML = `
        <div class="page-header">
          <div>
            <div class="page-title">Монтаж и AI-студия</div>
            <div class="page-sub">Полноценная NLE-студия: интерактивный таймлайн, Shorts 9:16, хайлайты, цветокор, Whisper AI и обложки</div>
          </div>
          <div class="header-actions">
            <button class="btn btn-primary" id="btnStPick">${iconSvg("folder", 15)}<span>Открыть видео</span></button>
          </div>
        </div>

        <div class="studio-dropzone" id="stDropzone">
          <div class="studio-file-icon" style="width:52px;height:52px;border-radius:14px;">${iconSvg("scissors", 24)}</div>
          <h3>Откройте запись или перетащите видео сюда</h3>
          <p>Поддерживаются MP4, MKV, TS и MOV. Внутри доступен живой монитор, интерактивный таймлайн с маркерами, Shorts 9:16, Whisper AI, цветокор и экспорт.</p>
          <div class="input-row" style="margin-top:6px;">
            <button class="btn btn-primary" id="btnStPickBig">${iconSvg("folder", 15)}<span>Выбрать файл на диске</span></button>
            <button class="btn btn-subtle" id="btnStGoLib">${iconSvg("grid", 15)}<span>Открыть Библиотеку</span></button>
          </div>
        </div>

        ${recent.length ? `
          <div class="section-heading"><span>Последние записи — открыть в 1 клик</span></div>
          <div class="studio-recent-grid">
            ${recent.map((it) => `
              <div class="studio-recent-card" data-open-path="${esc(it.path)}">
                <div class="studio-recent-thumb">
                  <img src="${thumbUrl(it.path)}" alt="" loading="lazy" />
                  ${it.duration ? `<span class="media-dur">${fmtDur(it.duration)}</span>` : ""}
                </div>
                <div class="studio-recent-info">
                  <div class="studio-recent-name" title="${esc(it.name)}">${esc(it.name)}</div>
                  <div class="studio-recent-sub">${esc(it.size_str || "")}${it.markers?.length ? ` · 🔖 ${it.markers.length}` : ""}</div>
                </div>
              </div>
            `).join("")}
          </div>
        ` : ""}
        <div id="studioJobsDock"></div>
      `;
      const pickFn = async () => {
        const p = await callBackend("pickFile", "Видео (*.mp4 *.mkv *.ts *.mov *.webm)");
        if (p) loadStudioVideo(p);
      };
      $("#btnStPick").onclick = pickFn;
      $("#btnStPickBig").onclick = pickFn;
      $("#btnStGoLib").onclick = () => switchPage("library");
      root.querySelectorAll(".studio-recent-card").forEach((c) => {
        c.onclick = () => loadStudioVideo(c.dataset.openPath);
      });
      renderStudioJobsDock(stJobs);
      return;
    }

    _studioLoadedSrc = state.studioFile;
    const m = state.studioMeta || {};
    const fname = state.studioFile.split(/[\\/]/).pop();

    root.innerHTML = `
      <div class="studio-topbar">
        <div class="studio-file-meta">
          <div class="studio-file-icon">${iconSvg("film", 18)}</div>
          <div style="min-width:0;">
            <div class="studio-file-name" title="${esc(state.studioFile)}">${esc(fname)}</div>
            <div class="studio-meta-pills">
              ${m.width ? `<span class="studio-meta-pill">${m.width}×${m.height}</span>` : ""}
              ${m.fps ? `<span class="studio-meta-pill">${Math.round(m.fps)} FPS</span>` : ""}
              ${m.duration ? `<span class="studio-meta-pill">${fmtDur(m.duration)}</span>` : ""}
              ${m.size_str ? `<span class="studio-meta-pill">${esc(m.size_str)}</span>` : ""}
              ${m.markers?.length ? `<span class="studio-meta-pill" style="color:#fbbf24;">🔖 ${m.markers.length} марк.</span>` : ""}
            </div>
          </div>
        </div>
        <div class="input-row" style="flex-wrap:wrap;">
          <button class="btn btn-soft btn-sm" id="btnStToPublish">${iconSvg("upload", 14)}<span>В Автопостинг</span></button>
          <button class="btn btn-subtle btn-sm" id="btnStPick">${iconSvg("folder", 14)}<span>Другое видео</span></button>
          <button class="btn btn-subtle btn-sm" id="btnStReveal">${iconSvg("grid", 14)}<span>В папке</span></button>
          <button class="btn btn-danger-ghost btn-sm" id="btnStCloseFile">${iconSvg("x", 14)}<span>Закрыть</span></button>
        </div>
      </div>

      <div class="studio-nle-grid">
        <div class="studio-stage-col">
          <div class="studio-monitor-card">
            <div class="studio-monitor-screen">
              <video id="studioMonitor" src="${videoPlayUrl(state.studioFile, state.studioSeekOffset || 0)}" playsinline autoplay muted></video>
              <div class="monitor-shorts-guide hidden" id="monShortsGuide"><span>SHORTS 9:16</span></div>
              <div class="monitor-sub-preview hidden" id="monSubPreview"></div>
              <div class="monitor-thumb-preview hidden" id="monThumbPreview">
                <span class="monitor-thumb-pill" id="monThumbSub"></span>
                <div class="monitor-thumb-head" id="monThumbHead"></div>
              </div>
            </div>
            <div class="studio-transport">
              <button class="btn btn-primary btn-sm" id="stBtnPlay" title="Пауза / Старт (Space)">${iconSvg("play", 14)}</button>
              <button class="btn-icon" id="stBtnBack5" title="-5 сек (←)">-5с</button>
              <button class="btn-icon" id="stBtnFrameB" title="-1 кадр">&lt;</button>
              <button class="btn-icon" id="stBtnFrameF" title="+1 кадр">&gt;</button>
              <button class="btn-icon" id="stBtnFwd5" title="+5 сек (→)">+5с</button>
              <span class="transport-tc" id="stTimecode">00:00:00 / ${fmtDur(m.duration || 0)}</span>
              <div class="speed-seg" id="stSpeedSeg">
                ${[0.5, 1, 1.5, 2].map((sp) => `<button data-spd="${sp}" class="${state.studioSpeed === sp ? "active" : ""}">${sp}×</button>`).join("")}
              </div>
              <div style="flex:1"></div>
              <button class="btn btn-subtle btn-sm" id="stBtnSnap" title="Сохранить текущий кадр в JPG">${iconSvg("camera", 14)}<span>Кадр</span></button>
              <button class="btn btn-soft btn-sm" id="stBtnUseThumb" title="Открыть текущий кадр в Конструкторе обложек">${iconSvg("image", 14)}<span>На обложку</span></button>
            </div>
          </div>

          <div class="studio-timeline-card">
            <div class="tl-header">
              <div class="input-row" style="flex-wrap:wrap;gap:6px;">
                <button class="btn btn-subtle btn-sm" id="tlBtnIn" title="Установить начало выделения (I)">От [I]</button>
                <button class="btn btn-subtle btn-sm" id="tlBtnOut" title="Установить конец выделения (O)">До [O]</button>
                <button class="btn btn-soft btn-sm" id="tlBtnAddSeg">${iconSvg("plus", 13)}<span>В нарезку</span></button>
                <button class="btn btn-primary btn-sm" id="tlBtnQuickTrim">${iconSvg("scissors", 13)}<span>Вырезать</span></button>
              </div>
              <span class="mono" id="tlRangeLbl" style="font-size:12px;color:var(--accent-light);font-weight:600;">00:00:00 — 00:01:00</span>
            </div>
            <div class="tl-ruler" id="tlRuler"><span>00:00</span><span>—</span><span>—</span><span>—</span><span>${fmtDur(m.duration || 0)}</span></div>
            <div class="tl-track" id="tlTrack">
              <div class="tl-selection" id="tlSelection"></div>
              <div class="tl-handle left" id="tlHandleL" title="Потяните начало отрезка"></div>
              <div class="tl-handle right" id="tlHandleR" title="Потяните конец отрезка"></div>
              <div id="tlSegsLayer"></div>
              <div id="tlPinsLayer"></div>
              <div class="tl-playhead" id="tlPlayhead"></div>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);flex-wrap:wrap;gap:6px;">
              <span>Тяните фиолетовые ручки для выделения · Клик по точкам — переход к маркеру/хайлайту</span>
              <span><kbd>Space</kbd> пауза · <kbd>I</kbd>/<kbd>O</kbd> границы · <kbd>J</kbd>/<kbd>L</kbd> ±10с</span>
            </div>
          </div>
        </div>

        <div class="studio-inspector-col">
          <div class="studio-tool-tabs" id="studioToolTabs">
            <button class="studio-tool-tab" data-sub="trim">${iconSvg("scissors", 15)}<span>Монтаж</span></button>
            <button class="studio-tool-tab" data-sub="shorts">${iconSvg("smartphone", 15)}<span>Shorts</span></button>
            <button class="studio-tool-tab" data-sub="highlights">${iconSvg("flame", 15)}<span>Хайлайты</span></button>
            <button class="studio-tool-tab" data-sub="ai">${iconSvg("sparkles", 15)}<span>AI Текст</span></button>
            <button class="studio-tool-tab" data-sub="thumbnail">${iconSvg("image", 15)}<span>Обложка</span></button>
            <button class="studio-tool-tab" data-sub="effects">${iconSvg("sliders", 15)}<span>Эффекты</span></button>
            <button class="studio-tool-tab" data-sub="export">${iconSvg("download", 15)}<span>Экспорт</span></button>
          </div>
          <div id="studioInspectorHost"></div>
        </div>
      </div>

      <div id="studioJobsDock" style="margin-top:16px;"></div>
    `;

    $("#btnStToPublish").onclick = () => { state.pub.videoPath = state.studioFile; switchPage("publish"); };
    $("#btnStPick").onclick = async () => {
      const p = await callBackend("pickFile", "Видео (*.mp4 *.mkv *.ts *.mov *.webm)");
      if (p) loadStudioVideo(p);
    };
    $("#btnStReveal").onclick = () => callBackend("openInExplorer", state.studioFile);
    $("#btnStCloseFile").onclick = () => {
      state.studioFile = "";
      state.studioMeta = null;
      _studioLoadedSrc = "";
      _studioProbedFile = "";
      renderStudioPage(true);
    };

    const mon = $("#studioMonitor");
    mon.playbackRate = state.studioSpeed;
    mon.muted = false;
    const syncPlayBtn = () => {
      const b = $("#stBtnPlay");
      if (b) b.innerHTML = iconSvg(mon.paused ? "play" : "pause", 14);
    };
    mon.onplay = syncPlayBtn;
    mon.onpause = syncPlayBtn;
    mon.onloadedmetadata = () => {
      if (isFinite(mon.duration) && mon.duration > 0) {
        if (!state.studioMeta) state.studioMeta = { duration: mon.duration };
        else if (!state.studioMeta.duration) state.studioMeta.duration = mon.duration;
      }
      renderStudioTimelineUI();
    };
    mon.ontimeupdate = () => {
      const dur = getStudioDuration();
      state.studioTime = Math.min(dur, getStudioTime());
      const tc = $("#stTimecode");
      if (tc) tc.textContent = `${fmtDur(state.studioTime)} / ${fmtDur(dur)}`;
      const ph = $("#tlPlayhead");
      if (ph && dur > 0) ph.style.left = `${Math.min(100, Math.max(0, (state.studioTime / dur) * 100))}%`;
    };
    $("#stBtnPlay").onclick = () => { if (mon.paused) mon.play().catch(() => {}); else mon.pause(); };
    $("#stBtnBack5").onclick = () => seekStudioMonitor(getStudioTime() - 5, !mon.paused);
    $("#stBtnFwd5").onclick = () => seekStudioMonitor(getStudioTime() + 5, !mon.paused);
    $("#stBtnFrameB").onclick = () => seekStudioMonitor(getStudioTime() - 0.5, false);
    $("#stBtnFrameF").onclick = () => seekStudioMonitor(getStudioTime() + 0.5, false);
    $("#stSpeedSeg").querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        state.studioSpeed = Number(b.dataset.spd) || 1;
        mon.playbackRate = state.studioSpeed;
        $("#stSpeedSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      };
    });
    $("#stBtnSnap").onclick = () => callBackend("saveStudioFrame", state.studioFile, getStudioTime());
    $("#stBtnUseThumb").onclick = () => {
      state.studioSub = "thumbnail";
      updateStudioToolTabsUI();
      renderStudioInspector();
      syncStudioMonitorOverlays();
      if ($("#thTime")) $("#thTime").value = fmtDur(getStudioTime());
    };

    const track = $("#tlTrack");
    let tlDrag = null;
    const xToTime = (clientX) => {
      const rect = track.getBoundingClientRect();
      const dur = getStudioDuration();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
      return ratio * dur;
    };
    track.addEventListener("mousedown", (e) => {
      if (e.target.closest(".tl-pin")) return;
      if (e.target.id === "tlHandleL") tlDrag = "in";
      else if (e.target.id === "tlHandleR") tlDrag = "out";
      else {
        tlDrag = "seek";
        seekStudioMonitor(xToTime(e.clientX), true);
      }
    });
    const onTlMove = (e) => {
      if (!tlDrag) return;
      if (!$("#tlTrack")) { tlDrag = null; return; }
      const t = xToTime(e.clientX);
      const dur = getStudioDuration();
      if (tlDrag === "in") {
        state.studioIn = Math.max(0, Math.min(state.studioOut - 0.5, t));
        renderStudioTimelineUI();
      } else if (tlDrag === "out") {
        state.studioOut = Math.min(dur, Math.max(state.studioIn + 0.5, t));
        renderStudioTimelineUI();
      }
    };
    if (_studioTlMoveHandler) window.removeEventListener("mousemove", _studioTlMoveHandler);
    if (_studioTlUpHandler) window.removeEventListener("mouseup", _studioTlUpHandler);
    _studioTlMoveHandler = onTlMove;
    _studioTlUpHandler = () => {
      if (tlDrag === "in") seekStudioMonitor(state.studioIn, false);
      else if (tlDrag === "out") seekStudioMonitor(state.studioOut, false);
      tlDrag = null;
    };
    window.addEventListener("mousemove", _studioTlMoveHandler);
    window.addEventListener("mouseup", _studioTlUpHandler);

    $("#tlBtnIn").onclick = () => {
      state.studioIn = Math.min(getStudioTime(), Math.max(0, state.studioOut - 0.5));
      renderStudioTimelineUI();
    };
    $("#tlBtnOut").onclick = () => {
      state.studioOut = Math.max(getStudioTime(), state.studioIn + 0.5);
      renderStudioTimelineUI();
    };
    $("#tlBtnAddSeg").onclick = () => {
      if (state.studioOut <= state.studioIn) return showToast("Отрезок", "Конец должен быть больше начала", "error");
      state.multiCutSegments.push([state.studioIn, state.studioOut]);
      renderStudioTimelineUI();
      if (state.studioSub === "trim") renderStudioInspector();
      showToast("Добавлено в нарезку", `${fmtDur(state.studioIn)} – ${fmtDur(state.studioOut)}`, "success");
    };
    $("#tlBtnQuickTrim").onclick = () => {
      if (state.studioOut <= state.studioIn) return showToast("Обрезка", "Выберите корректный интервал", "error");
      const out = appendFileSuffix(state.studioFile, `_cut_${Math.floor(state.studioIn)}-${Math.floor(state.studioOut)}`);
      callBackend("runStudioAction", JSON.stringify({
        action: "trim", title: `Обрезка ${fmtDur(state.studioIn)}–${fmtDur(state.studioOut)}`, subtitle: fname,
        params: { input_path: state.studioFile, start_sec: state.studioIn, end_sec: state.studioOut, output_path: out, accurate: $("#trimAcc")?.checked ?? true }
      }));
    };

    $("#studioToolTabs").querySelectorAll(".studio-tool-tab").forEach((b) => {
      b.onclick = () => {
        state.studioSub = b.dataset.sub;
        updateStudioToolTabsUI();
        renderStudioInspector();
        syncStudioMonitorOverlays();
      };
    });

    updateStudioToolTabsUI();
    renderStudioInspector();
    syncStudioMonitorOverlays();
    renderStudioTimelineUI();
    renderStudioJobsDock(stJobs);
  }

  function updateStudioToolTabsUI() {
    $("#studioToolTabs")?.querySelectorAll(".studio-tool-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.sub === state.studioSub);
    });
  }

  function syncStudioMonitorOverlays() {
    const mon = $("#studioMonitor");
    if (!mon) return;
    mon.style.filter = state.studioSub === "effects" ? studioFxCssFilter() : "";
    $("#monShortsGuide")?.classList.toggle("hidden", state.studioSub !== "shorts");
    const sp = $("#monSubPreview");
    if (sp) {
      const showSub = state.studioSub === "ai" && state.aiHubSub === "speech" && state.subPreview.show;
      sp.classList.toggle("hidden", !showSub);
      if (showSub) {
        sp.textContent = state.subPreview.text || "";
        sp.style.fontSize = `${state.subPreview.size || 28}px`;
        sp.style.color = state.subPreview.color || "#fde047";
        sp.style.top = state.subPreview.pos === "top" ? "10%" : (state.subPreview.pos === "middle" ? "44%" : "auto");
        sp.style.bottom = state.subPreview.pos === "bottom" ? "10%" : "auto";
      }
    }
    const tp = $("#monThumbPreview");
    if (tp) {
      const showTh = state.studioSub === "thumbnail";
      tp.classList.toggle("hidden", !showTh);
      if (showTh) {
        tp.style.setProperty("--thumb-accent", state.studioThumb.accent || "#8b5cf6");
        $("#monThumbHead").textContent = state.studioThumb.headline || "ЛУЧШИЙ МОМЕНТ СТРИМА";
        $("#monThumbSub").textContent = state.studioThumb.subtitle || "TWITCH HIGHLIGHTS";
      }
    }
  }

  function renderStudioTimelineUI() {
    const mon = $("#studioMonitor");
    const dur = Math.max(1, mon?.duration || state.studioMeta?.duration || 60);
    const inPct = Math.min(100, Math.max(0, (state.studioIn / dur) * 100));
    const outPct = Math.min(100, Math.max(inPct, (state.studioOut / dur) * 100));

    const sel = $("#tlSelection"), hl = $("#tlHandleL"), hr = $("#tlHandleR"), lbl = $("#tlRangeLbl");
    if (sel) { sel.style.left = `${inPct}%`; sel.style.width = `${Math.max(0.5, outPct - inPct)}%`; }
    if (hl) hl.style.left = `${inPct}%`;
    if (hr) hr.style.left = `${outPct}%`;
    if (lbl) lbl.textContent = `${fmtDur(state.studioIn)} — ${fmtDur(state.studioOut)} (${fmtDur(Math.max(0, state.studioOut - state.studioIn))})`;

    if ($("#trimStart")) $("#trimStart").value = fmtDur(state.studioIn);
    if ($("#trimEnd")) $("#trimEnd").value = fmtDur(state.studioOut);

    const ruler = $("#tlRuler");
    if (ruler) {
      ruler.innerHTML = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${fmtDur(dur * f)}</span>`).join("");
    }

    const segsEl = $("#tlSegsLayer");
    if (segsEl) {
      segsEl.innerHTML = (state.multiCutSegments || []).map((s) => {
        const l = Math.min(100, Math.max(0, (s[0] / dur) * 100));
        const w = Math.max(0.8, Math.min(100 - l, ((s[1] - s[0]) / dur) * 100));
        return `<div class="tl-seg-bar" style="left:${l}%;width:${w}%;"></div>`;
      }).join("");
    }

    const pinsEl = $("#tlPinsLayer");
    if (pinsEl) {
      const mPins = (state.studioMeta?.markers || []).map((m) => ({
        t: Number(m.t) || 0, note: m.note || "Маркер", cls: "marker"
      }));
      const hPins = (state.highlights?.highlights || []).map((h) => ({
        t: Number(h.timestamp_sec) || 0, note: h.reason || h.label || "Хайлайт", cls: "hl"
      }));
      pinsEl.innerHTML = [...mPins, ...hPins].map((p) => {
        const left = Math.min(99.5, Math.max(0.5, (p.t / dur) * 100));
        return `<div class="tl-pin ${p.cls}" data-t="${p.t}" style="left:${left}%;" title="${fmtDur(p.t)} · ${esc(p.note)}"></div>`;
      }).join("");
      pinsEl.querySelectorAll(".tl-pin").forEach((el) => {
        el.onclick = (e) => {
          e.stopPropagation();
          seekStudioMonitor(Number(el.dataset.t) || 0, true);
        };
      });
    }
  }

  function renderStudioJobsDock(stJobs) {
    const dock = $("#studioJobsDock");
    if (!dock) return;
    if (!stJobs.length) { dock.innerHTML = ""; return; }
    dock.innerHTML = `
      <div class="section-heading"><span>Очередь рендера Студии (${stJobs.length})</span></div>
      ${stJobs.slice(0, 6).map(renderJobCard).join("")}
    `;
    bindJobEvents(dock);
  }

  function renderStudioInspector() {
    const host = $("#studioInspectorHost");
    if (!host) return;
    const chPresets = state.channels.filter((c) => (c.blur_regions || []).length > 0);
    const fname = state.studioFile.split(/[\\/]/).pop();
    const mon = $("#studioMonitor");

    if (state.studioSub === "trim") {
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Обрезка и AI-чистка</span></div>
          <div class="form-grid-2">
            <div class="field"><label>Начало (I)</label><input class="input mono" id="trimStart" value="${fmtDur(state.studioIn)}" /></div>
            <div class="field"><label>Конец (O)</label><input class="input mono" id="trimEnd" value="${fmtDur(state.studioOut)}" /></div>
          </div>
          <label class="switch-inline"><input type="checkbox" id="trimAcc" checked/><span class="switch-track"></span><span style="font-size:12.5px;">Точный кадр (перекодирование)</span></label>
          <button class="btn btn-primary btn-full" id="btnDoTrim">${iconSvg("scissors", 14)}<span>Вырезать отрезок</span></button>
          <div class="form-grid-3" style="gap:6px;">
            <button class="btn btn-soft btn-sm" id="btnRemoveAfk" title="Удалить тишину и AFK">⚡ Анти-AFK</button>
            <button class="btn btn-soft btn-sm" id="btnRemoveFillers" title="AI удаление слов-паразитов и пауз (Descript)">🧹 Паразиты</button>
            <button class="btn btn-subtle btn-sm" id="btnSmartSceneCut" title="AI вырезка экранов «Скоро начнём» / BRB">✂ BRB/Загр.</button>
          </div>
        </div>

        ${state.multiCutSegments.length > 0 ? `
          <div class="studio-inspector-card">
            <div class="inspector-group-title"><span>Multi-Cut нарезка (${state.multiCutSegments.length})</span><button class="btn btn-danger-ghost btn-sm" id="btnClearMultiCut">Очистить</button></div>
            <div class="multicut-list">
              ${state.multiCutSegments.map((seg, i) => `
                <span class="multicut-chip">#${i + 1} ${fmtDur(seg[0])}–${fmtDur(seg[1])} <button data-rm-seg="${i}">×</button></span>
              `).join("")}
            </div>
            <button class="btn btn-success btn-full" id="btnRunMultiCut">${iconSvg("film", 14)}<span>Собрать нарезку (${state.multiCutSegments.length})</span></button>
          </div>
        ` : ""}

        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Разбить видео на равные части</span></div>
          <div class="split-pills">
            <button class="split-pill st-split-preset" data-min="15">15 мин</button>
            <button class="split-pill st-split-preset active" data-min="30">30 мин</button>
            <button class="split-pill st-split-preset" data-min="60">1 час</button>
            <button class="split-pill st-split-preset" data-min="120">2 часа</button>
          </div>
          <div class="input-row">
            <input type="number" class="input mono" id="stSplitMin" value="30" min="1" max="600" style="width:84px;" />
            <span style="font-size:12px;color:var(--text-muted);">мин</span>
            <div style="flex:1"></div>
            <button class="btn btn-primary btn-sm" id="btnSplitParts">${iconSvg("scissors", 14)}<span>Разбить</span></button>
          </div>
        </div>

        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Склейка файлов</span></div>
          <label class="switch-inline"><input type="checkbox" id="catNorm"/><span class="switch-track"></span><span style="font-size:12.5px;">Выровнять в 1080p60</span></label>
          <button class="btn btn-subtle btn-full" id="btnDoConcat">${iconSvg("plus", 14)}<span>Выбрать 2+ файлов и склеить</span></button>
        </div>
      `;
      $("#trimStart").onchange = (e) => { const v = parseSec(e.target.value); if (v != null) { state.studioIn = v; renderStudioTimelineUI(); } };
      $("#trimEnd").onchange = (e) => { const v = parseSec(e.target.value); if (v != null) { state.studioOut = v; renderStudioTimelineUI(); } };
      $("#btnDoTrim").onclick = () => $("#tlBtnQuickTrim")?.click();
      $("#btnRemoveAfk").onclick = () => {
        callBackend("runStudioAction", JSON.stringify({ action: "remove_silence_afk", title: "Удаление тишины и AFK", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_no_afk") } }));
      };
      $("#btnRemoveFillers").onclick = () => {
        callBackend("runStudioAction", JSON.stringify({ action: "remove_fillers", title: "AI Удаление слов-паразитов", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_clean_speech"), model: state.config.whisper_model || "small", language: "ru" } }));
      };
      $("#btnSmartSceneCut").onclick = () => {
        callBackend("runStudioAction", JSON.stringify({ action: "smart_scene_cut", title: "AI Вырезка BRB / Загрузок", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_no_brb") } }));
      };
      host.querySelectorAll("[data-rm-seg]").forEach((b) => {
        b.onclick = () => { state.multiCutSegments.splice(Number(b.dataset.rmSeg), 1); renderStudioInspector(); renderStudioTimelineUI(); };
      });
      $("#btnClearMultiCut")?.addEventListener("click", () => { state.multiCutSegments = []; renderStudioInspector(); renderStudioTimelineUI(); });
      $("#btnRunMultiCut")?.addEventListener("click", () => {
        callBackend("runStudioAction", JSON.stringify({ action: "multi_cut", title: `Нарезка (${state.multiCutSegments.length})`, subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, `_montage_${state.multiCutSegments.length}cuts`, ".mp4"), segments: state.multiCutSegments, accurate: $("#trimAcc")?.checked ?? true } }));
      });
      host.querySelectorAll(".st-split-preset").forEach((b) => {
        b.onclick = () => {
          host.querySelectorAll(".st-split-preset").forEach((x) => x.classList.remove("active"));
          b.classList.add("active");
          $("#stSplitMin").value = b.dataset.min;
        };
      });
      $("#btnSplitParts").onclick = () => {
        const mins = Math.max(1, Number($("#stSplitMin").value) || 30);
        callBackend("runStudioAction", JSON.stringify({ action: "split_parts", title: `Разбивка по ${mins} мин`, subtitle: fname, params: { input_path: state.studioFile, segment_minutes: mins } }));
      };
      $("#btnDoConcat").onclick = async () => {
        const files = JSON.parse((await callBackend("pickVideoFiles")) || "[]");
        if (files.length < 2) return;
        callBackend("runStudioAction", JSON.stringify({ action: "concat", title: `Склейка (${files.length})`, subtitle: files[0].split(/[\\/]/).pop(), params: { input_paths: files, output_path: appendFileSuffix(files[0], "_merged", ".mp4"), normalize: $("#catNorm").checked } }));
      };
    } else if (state.studioSub === "shorts") {
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Shorts / TikTok 9:16 + AI</span></div>
          <div class="field">
            <label>Шаблон компоновки (1080×1920)</label>
            <select class="input-select" id="shortsMode">
              <option value="split">Вебка сверху + Геймплей снизу</option>
              <option value="pip_circle">Круглая вебка (PiP) поверх геймплея</option>
              <option value="pip_rounded">Скруглённая вебка (PiP) поверх геймплея</option>
              <option value="blur_bg">Видео по центру + размытый фон</option>
            </select>
          </div>
          <div class="field">
            <label>Пресет блюра баннера (опционально)</label>
            <select class="input-select" id="shortsBlurCh">
              <option value="">Без блюра</option>
              ${chPresets.map((c) => `<option value="${esc(c.key)}">Канал ${esc(c.username)} (${c.blur_regions.length} зон)</option>`).join("")}
            </select>
          </div>
          <label class="switch-inline"><input type="checkbox" id="shortsProgBar" checked/><span class="switch-track"></span><span style="font-size:12.5px;">Анимированный прогресс-бар снизу</span></label>
          <label class="switch-inline"><input type="checkbox" id="shortsKillcam"/><span class="switch-track"></span><span style="font-size:12.5px;">Killcam 1.35× зум на центр</span></label>
          <div class="form-grid-2" style="gap:8px;">
            <button class="btn btn-soft btn-sm" id="btnAiFaceTrack" title="Автоматически найти лицо стримера на видео">🎯 AI Face Track</button>
            <button class="btn btn-subtle btn-sm" id="btnPickShortsZones">${iconSvg("crop", 13)}<span>Зоны вручную</span></button>
          </div>
          <button class="btn btn-primary btn-full" id="btnRunShorts">${iconSvg("smartphone", 14)}<span>Создать Shorts 9:16</span></button>
          <div class="form-grid-2" style="gap:8px;border-top:1px solid var(--border);padding-top:10px;">
            <button class="btn btn-soft btn-sm" id="btnVirtGs" title="Удалить фон вебки без хромакея и наложить поверх геймплея">🪄 Virtual Green Screen</button>
            <button class="btn btn-soft btn-sm" id="btnAutoHook" title="Поставить текущий момент (2.6с) в начало ролика как тизер-хук">⚡ AI Авто-хук 3с</button>
          </div>
        </div>
      `;
      $("#btnAiFaceTrack").onclick = () => {
        callBackend("runAiSuiteTask", "face_track", JSON.stringify({ video_path: state.studioFile }));
        showToast("AI Face Tracking", "Ищем лицо стримера на кадрах...", "info");
      };
      $("#btnPickShortsZones").onclick = () => {
        const init = [
          state.shortsCam || { x: 0.02, y: 0.55, w: 0.28, h: 0.35, mode: "blur", strength: 10, label: "1. Вебка (верх)" },
          state.shortsGame || { x: 0.22, y: 0.05, w: 0.56, h: 0.90, mode: "blur", strength: 10, label: "2. Геймплей (низ)" },
        ];
        openBlurStudio({ mode: "shorts", videoPath: state.studioFile, regions: init });
      };
      $("#btnRunShorts").onclick = () => {
        const mode = $("#shortsMode").value;
        callBackend("runStudioAction", JSON.stringify({
          action: "shorts", title: "Рендер Shorts 9:16", subtitle: "1080×1920",
          params: {
            input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, `_shorts_${mode}`, ".mp4"), layout: mode,
            cam_region: mode !== "blur_bg" ? (state.shortsCam || { x: 0.02, y: 0.55, w: 0.28, h: 0.35 }) : null,
            game_region: mode !== "blur_bg" ? (state.shortsGame || { x: 0.22, y: 0.05, w: 0.56, h: 0.90 }) : null,
            blur_channel_key: $("#shortsBlurCh").value, progress_bar: $("#shortsProgBar").checked, killcam_zoom: $("#shortsKillcam").checked,
          }
        }));
      };
      $("#btnVirtGs").onclick = () => {
        callBackend("runStudioAction", JSON.stringify({
          action: "virtual_greenscreen", title: "Shorts Virtual Green Screen", subtitle: fname,
          params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_vgs_9x16", ".mp4"), cam_region: state.shortsCam, game_region: state.shortsGame }
        }));
      };
      $("#btnAutoHook").onclick = () => {
        const ts = mon?.currentTime || state.studioIn || 15;
        callBackend("runStudioAction", JSON.stringify({
          action: "auto_hook", title: `AI Авто-хук (${fmtDur(ts)})`, subtitle: fname,
          params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_hooked", ".mp4"), hook_ts: ts }
        }));
      };
    } else if (state.studioSub === "highlights") {
      const hls = state.highlights.highlights || [];
      const sent = state.aiChatSentiment;
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Поиск хайлайтов и эмоций чата</span></div>
          <div class="input-row">
            <input class="input" id="hlChatInp" value="${esc(state.studioChatFile)}" placeholder="Файл .chat.json..." />
            <button class="btn btn-subtle btn-sm" id="btnPickChat">${iconSvg("folder", 14)}</button>
          </div>
          <div class="form-grid-2" style="gap:6px;">
            <button class="btn btn-primary btn-sm" id="btnScanChat">${iconSvg("flame", 13)}<span>Пики чата</span></button>
            <button class="btn btn-soft btn-sm" id="btnScanAudio">${iconSvg("volume", 13)}<span>Громкие пики</span></button>
            <button class="btn btn-soft btn-sm" id="btnScanEvents">${iconSvg("sparkles", 13)}<span>Смех / Крики</span></button>
            <button class="btn btn-soft btn-sm" id="btnScanGameVision">🎯 Игровые (Vision)</button>
            <button class="btn btn-subtle btn-sm" id="btnChatSentiment">📊 Настроение чата</button>
            <button class="btn btn-subtle btn-sm" id="btnBurnChat">${iconSvg("video", 13)}<span>Вшить чат</span></button>
          </div>
          <div class="input-row">
            <input class="input" id="hlVodUrl" placeholder="Ссылка на Twitch VOD для чата..." />
            <button class="btn btn-subtle btn-sm" id="btnFetchVodChat">${iconSvg("download", 14)}</button>
          </div>
          ${sent?.ok ? `
            <div class="yt-meta-box" style="margin-top:4px;padding:10px;">
              <div style="font-size:12px;font-weight:700;">📊 Настроение чата (${sent.totals.messages} сообщ.)</div>
              <div class="input-row" style="gap:6px;flex-wrap:wrap;font-size:11.5px;">
                <span class="diag-pill">😂 Смех: ${sent.totals.laugh} (пик ${sent.peak_laugh_min}м)</span>
                <span class="diag-pill" style="background:rgba(139,92,246,0.2);color:#c4b5fd;">🔥 Хайп: ${sent.totals.hype} (пик ${sent.peak_hype_min}м)</span>
                <span class="diag-pill off">💀 Горение: ${sent.totals.salt}</span>
              </div>
            </div>
          ` : ""}
          <div class="heatmap-box" style="height:96px;">${renderHeatmapSvg()}</div>
          ${hls.length ? `
            <div class="input-row" style="flex-wrap:wrap;gap:6px;">
              <button class="btn btn-primary btn-sm" id="btnBatchShortsHl">📱 Все в Shorts (${hls.length})</button>
              <button class="btn btn-soft btn-sm" id="btnBatchCutHl">✂ Нарезать все</button>
              <button class="btn btn-subtle btn-sm" id="btnExportEdl">EDL</button>
            </div>
            <div class="hl-list">
              ${hls.map((h) => `
                <div class="hl-row">
                  <span class="mono act-jump-hl" data-ts="${h.timestamp_sec}" style="cursor:pointer;color:var(--accent-light);font-weight:700;">${fmtDur(h.timestamp_sec)}</span>
                  <span style="flex:1;font-size:11.5px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(h.reason || h.label || "")}</span>
                  <button class="btn btn-soft btn-sm act-cut-hl" data-ts="${h.timestamp_sec}">±20с</button>
                </div>
              `).join("")}
            </div>
          ` : ""}
        </div>
      `;
      $("#btnPickChat").onclick = async () => { const p = await callBackend("pickFile", "Чат JSON (*.json)"); if (p) { state.studioChatFile = p; renderStudioInspector(); } };
      $("#btnScanChat").onclick = () => { const path = $("#hlChatInp").value.trim() || appendFileSuffix(state.studioFile, "", ".chat.json"); state.studioChatFile = path; callBackend("analyzeHighlights", "chat", path); };
      $("#btnScanAudio").onclick = () => callBackend("analyzeHighlights", "audio", state.studioFile);
      $("#btnScanEvents").onclick = () => callBackend("analyzeHighlights", "events", state.studioFile);
      $("#btnScanGameVision").onclick = () => { callBackend("runAiSuiteTask", "game_events", JSON.stringify({ video_path: state.studioFile })); showToast("AI Vision", "Сканируем Killfeed и игровые события...", "info"); };
      $("#btnChatSentiment").onclick = () => { callBackend("runAiSuiteTask", "chat_sentiment", JSON.stringify({ video_path: state.studioFile, chat_path: $("#hlChatInp").value.trim() })); };
      $("#btnFetchVodChat").onclick = () => { const u = $("#hlVodUrl").value.trim(); if (u) callBackend("downloadVodChat", u, state.studioFile); };
      $("#btnExportEdl")?.addEventListener("click", () => callBackend("exportHighlightsEdl", state.studioFile, JSON.stringify(hls)));
      $("#btnBatchCutHl")?.addEventListener("click", () => callBackend("runStudioAction", JSON.stringify({ action: "batch_highlights", title: `Пакет хайлайтов (${hls.length})`, subtitle: fname, params: { input_path: state.studioFile, timestamps: hls.map((h) => h.timestamp_sec), pad_sec: 20 } })));
      $("#btnBatchShortsHl")?.addEventListener("click", () => callBackend("runStudioAction", JSON.stringify({ action: "batch_shorts", title: `Пакет Shorts 9:16 (${hls.length})`, subtitle: fname, params: { input_path: state.studioFile, timestamps: hls.map((h) => h.timestamp_sec), pad_sec: 20, layout: "split", cam_region: state.shortsCam || { x: 0.02, y: 0.55, w: 0.28, h: 0.35 }, game_region: state.shortsGame || { x: 0.22, y: 0.05, w: 0.56, h: 0.90 }, progress_bar: true } })));
      $("#btnBurnChat").onclick = () => {
        const baseChat = ($("#hlChatInp").value.trim() || state.studioChatFile || state.studioFile);
        const assPath = baseChat.endsWith(".chat.json") ? baseChat.replace(/\.chat\.json$/, ".chat.ass") : appendFileSuffix(baseChat, "", ".chat.ass");
        callBackend("runStudioAction", JSON.stringify({ action: "burn_chat", title: "Наложение чата", subtitle: fname, params: { input_path: state.studioFile, ass_path: assPath, output_path: appendFileSuffix(state.studioFile, "_with_chat") } }));
      };
      host.querySelectorAll(".act-jump-hl").forEach((el) => { el.onclick = () => seekStudioMonitor(Number(el.dataset.ts) || 0, true); });
      host.querySelectorAll(".act-cut-hl").forEach((b) => {
        b.onclick = () => {
          const t = Number(b.dataset.ts);
          callBackend("runStudioAction", JSON.stringify({ action: "trim", title: `Хайлайт ${fmtDur(t)}`, subtitle: "±20 сек", params: { input_path: state.studioFile, start_sec: Math.max(0, t - 20), end_sec: t + 20, output_path: appendFileSuffix(state.studioFile, `_hl_${Math.floor(t)}s`), accurate: false } }));
        };
      });
    } else if (state.studioSub === "ai") {
      const sub = state.aiHubSub || "viral";
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>AI Центр (25 инструментов)</span></div>
          <div class="split-pills" id="aiHubNav" style="margin-top:0;">
            <button class="split-pill ${sub === "viral" ? "active" : ""}" data-as="viral">🔥 Клипы</button>
            <button class="split-pill ${sub === "copilot" ? "active" : ""}" data-as="copilot">💬 Копилот</button>
            <button class="split-pill ${sub === "speech" ? "active" : ""}" data-as="speech">🎙 Речь/Дубляж</button>
            <button class="split-pill ${sub === "seo" ? "active" : ""}" data-as="seo">📈 SEO/Посты</button>
          </div>

          ${sub === "viral" ? `
            <div style="font-size:12px;color:var(--text-muted);">AI объединяет чат, маркеры и эмоции речи, ставит оценку вирусности (0–100) и собирает клипы в 1 клик.</div>
            <div class="form-grid-2" style="gap:8px;">
              <button class="btn btn-primary btn-sm" id="btnFindViral">🔥 Найти вирусные клипы</button>
              <button class="btn btn-soft btn-sm" id="btnAutoMontage">🎬 Авто-монтаж (10м)</button>
            </div>
            ${state.aiViralClips.length ? `
              <div class="hl-list" style="max-height:260px;">
                ${state.aiViralClips.map((c, idx) => `
                  <div class="hl-row" style="flex-direction:column;align-items:stretch;gap:6px;">
                    <div class="input-row" style="justify-content:space-between;">
                      <span class="mono act-vjump" data-ts="${c.timestamp_sec}" style="cursor:pointer;color:var(--accent-light);font-weight:700;">${fmtDur(c.timestamp_sec)}</span>
                      <span class="diag-pill" style="padding:2px 7px;font-size:11px;">Вирусность ${c.score}/100</span>
                    </div>
                    <div style="font-size:12.5px;font-weight:700;">${esc(c.title)}</div>
                    <div class="input-row" style="justify-content:space-between;">
                      <span style="font-size:11px;color:var(--text-muted);">${esc(c.reason)}</span>
                      <button class="btn btn-primary btn-sm act-vshort" data-idx="${idx}">📱 В Shorts</button>
                    </div>
                  </div>
                `).join("")}
              </div>
            ` : ""}
          ` : sub === "copilot" ? `
            <div style="font-size:12px;color:var(--text-muted);">Напишите задачу обычным языком — AI-копилот подскажет и даст кнопки запуска в 1 клик.</div>
            <div class="input-row">
              <input class="input" id="aiCopilotInp" placeholder="Напр.: найди вирусные клипы и сделай главы YouTube..." />
              <button class="btn btn-primary btn-sm" id="btnRunCopilot">Отправить</button>
            </div>
            ${state.aiCopilot ? `
              <div class="yt-meta-box" style="margin-top:4px;">
                <div style="font-size:12.5px;line-height:1.45;">${esc(state.aiCopilot.reply)}</div>
                <div class="split-pills">
                  ${(state.aiCopilot.actions || []).map((a) => `<button class="btn btn-soft btn-sm act-cop-run" data-act="${esc(a.id)}">${esc(a.label)}</button>`).join("")}
                </div>
              </div>
            ` : ""}
          ` : sub === "speech" ? `
            <div class="form-grid-2">
              <div class="field"><label>Модель Whisper</label><select class="input-select" id="aiModel">${["tiny", "base", "small", "medium"].map((m) => `<option value="${m}" ${(state.config.whisper_model || "small") === m ? "selected" : ""}>${m}</option>`).join("")}</select></div>
              <div class="field"><label>Язык / Перевод</label><select class="input-select" id="aiLang"><option value="ru">Русский + Эмодзи</option><option value="en">English + Эмодзи</option><option value="translate_en">RU → EN Перевод</option><option value="auto">Авто</option></select></div>
            </div>
            <div class="field"><label>Словарь сленга / ников</label><input class="input" id="aiPrompt" value="${esc(state.config.whisper_prompt || "")}" placeholder="skywhywalker, клатч..." /></div>
            <div class="field"><label>Бан-лист слов (Ban-Word Shield)</label><input class="input" id="aiBanned" value="${esc(state.config.banned_words || "")}" /></div>
            <div class="input-row" style="flex-wrap:wrap;">
              <label class="switch-inline"><input type="checkbox" id="aiBurn" checked/><span class="switch-track"></span><span style="font-size:12px;">Вшить субтитры</span></label>
              <label class="switch-inline"><input type="checkbox" id="aiBeep" checked/><span class="switch-track"></span><span style="font-size:12px;">Писк цензуры</span></label>
            </div>
            <div class="form-grid-2" style="gap:6px;">
              <button class="btn btn-primary btn-sm" id="btnAiSubs">✨ Субтитры + Эмодзи</button>
              <button class="btn btn-soft btn-sm" id="btnAiCensor">🔇 Запикать мат</button>
              <button class="btn btn-subtle btn-sm" id="btnBanShield">🛡 Ban-Word Shield</button>
              <button class="btn btn-soft btn-sm" id="btnAiDubEn">🌍 AI Дубляж → EN</button>
            </div>
            <details class="adv-group" ${state.subPreview.show ? "open" : ""}>
              <summary>Предпросмотр субтитров на мониторе</summary>
              <div class="adv-body" style="padding-top:8px;display:flex;flex-direction:column;gap:8px;">
                <label class="switch-inline"><input type="checkbox" id="subPrevOn" ${state.subPreview.show ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12px;">Показать субтитры поверх видео</span></label>
                <input class="input" id="subPrevTxt" value="${esc(state.subPreview.text)}" placeholder="Пример текста субтитров..." />
                <div class="form-grid-3" style="gap:6px;">
                  <div class="field"><label>Размер</label><input type="number" class="input mono" id="subPrevSz" value="${state.subPreview.size}" min="16" max="54" /></div>
                  <div class="field"><label>Цвет</label><input type="color" class="input" id="subPrevCol" value="${state.subPreview.color}" style="height:34px;padding:2px;" /></div>
                  <div class="field"><label>Позиция</label><select class="input-select" id="subPrevPos"><option value="bottom" ${state.subPreview.pos === "bottom" ? "selected" : ""}>Снизу</option><option value="middle" ${state.subPreview.pos === "middle" ? "selected" : ""}>Центр</option><option value="top" ${state.subPreview.pos === "top" ? "selected" : ""}>Сверху</option></select></div>
                </div>
              </div>
            </details>
            ${state.aiBanHits.length ? `
              <div class="hl-list" style="max-height:140px;">
                ${state.aiBanHits.map((h) => `<div class="hl-row"><span class="mono act-bjump" data-ts="${h.t}" style="cursor:pointer;color:#f87171;font-weight:700;">${fmtDur(h.t)}</span><span>🛡 ${esc(h.word)}</span></div>`).join("")}
              </div>
            ` : ""}
          ` : `
            <div class="form-grid-2" style="gap:6px;">
              <button class="btn btn-primary btn-sm" id="btnAiChapters">📑 Главы YouTube</button>
              <button class="btn btn-soft btn-sm" id="btnGenYtMeta">✨ YouTube SEO</button>
              <button class="btn btn-soft btn-sm" id="btnGenDigest">📝 AI Конспект</button>
              <button class="btn btn-subtle btn-sm" id="btnAiCritique">⭐ Оценка ролика</button>
            </div>
            <div class="input-row" style="gap:6px;">
              <select class="input-select compact" id="aiPostPlat"><option value="telegram">Telegram</option><option value="vk">VK</option><option value="discord">Discord</option></select>
              <button class="btn btn-soft btn-sm" style="flex:1;" id="btnAiSocialPost">📣 Написать пост-анонс</button>
            </div>
            ${state.aiCritique ? `<div class="yt-meta-box"><strong style="font-size:12px;color:#4ade80;">⭐ Оценка: ${state.aiCritique.score}/100</strong><textarea readonly style="min-height:85px;">${esc(state.aiCritique.critique)}</textarea></div>` : ""}
            ${state.aiChapters ? `<div class="yt-meta-box"><div class="input-row" style="justify-content:space-between;"><strong style="font-size:12px;">📑 Главы YouTube</strong><button class="btn btn-subtle btn-sm" id="btnCopyChap">Копировать</button></div><textarea readonly>${esc(state.aiChapters)}</textarea></div>` : ""}
            ${state.aiSocialPost ? `<div class="yt-meta-box"><div class="input-row" style="justify-content:space-between;"><strong style="font-size:12px;">📣 Пост для соцсетей</strong><button class="btn btn-subtle btn-sm" id="btnCopyPost">Копировать</button></div><textarea readonly>${esc(state.aiSocialPost)}</textarea></div>` : ""}
            ${state.ytMeta ? `<div class="yt-meta-box"><div class="input-row" style="justify-content:space-between;"><strong style="font-size:12px;">YouTube SEO</strong><button class="btn btn-subtle btn-sm" id="btnCopyYtMeta">Копировать</button></div><textarea readonly>${esc((state.ytMeta.titles || []).join("\n") + "\n\n" + (state.ytMeta.description || ""))}</textarea></div>` : ""}
            ${state.streamDigest ? `<div class="yt-meta-box"><div class="input-row" style="justify-content:space-between;"><strong style="font-size:12px;">Конспект стрима</strong><button class="btn btn-subtle btn-sm" id="btnCopyDigest">Копировать</button></div><textarea readonly>${esc(state.streamDigest)}</textarea></div>` : ""}
          `}
        </div>
      `;
      $("#aiHubNav").querySelectorAll(".split-pill").forEach((b) => {
        b.onclick = () => { state.aiHubSub = b.dataset.as; renderStudioInspector(); };
      });
      $("#btnFindViral")?.addEventListener("click", () => { callBackend("runAiSuiteTask", "viral_clips", JSON.stringify({ video_path: state.studioFile, chat_path: state.studioChatFile })); showToast("AI Вирусные клипы", "Анализируем стрим...", "info"); });
      $("#btnAutoMontage")?.addEventListener("click", () => { callBackend("runStudioAction", JSON.stringify({ action: "auto_montage", title: "AI Авто-монтаж «Лучшее за стрим»", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_best_of", ".mp4"), clips: state.aiViralClips, chat_path: state.studioChatFile } })); });
      host.querySelectorAll(".act-vjump").forEach((el) => { el.onclick = () => seekStudioMonitor(Number(el.dataset.ts) || 0, true); });
      host.querySelectorAll(".act-vshort").forEach((b) => {
        b.onclick = () => {
          const c = state.aiViralClips[Number(b.dataset.idx)];
          if (!c) return;
          callBackend("runStudioAction", JSON.stringify({ action: "batch_shorts", title: `Shorts: ${c.title}`, subtitle: fmtDur(c.timestamp_sec), params: { input_path: state.studioFile, timestamps: [c.timestamp_sec], pad_sec: 20, layout: "split", cam_region: state.shortsCam || { x: 0.02, y: 0.55, w: 0.28, h: 0.35 }, game_region: state.shortsGame || { x: 0.22, y: 0.05, w: 0.56, h: 0.90 }, progress_bar: true } }));
        };
      });
      const sendCopilot = () => { const q = $("#aiCopilotInp")?.value.trim(); if (q) callBackend("runAiSuiteTask", "copilot", JSON.stringify({ prompt: q, video_path: state.studioFile })); };
      $("#btnRunCopilot")?.addEventListener("click", sendCopilot);
      $("#aiCopilotInp")?.addEventListener("keydown", (e) => { if (e.key === "Enter") sendCopilot(); });
      host.querySelectorAll(".act-cop-run").forEach((b) => {
        b.onclick = () => {
          const a = b.dataset.act;
          if (a === "viral_clips") { state.aiHubSub = "viral"; renderStudioInspector(); callBackend("runAiSuiteTask", "viral_clips", JSON.stringify({ video_path: state.studioFile })); }
          else if (a === "shorts") { state.studioSub = "shorts"; updateStudioToolTabsUI(); renderStudioInspector(); syncStudioMonitorOverlays(); }
          else if (a === "subtitles") { state.aiHubSub = "speech"; renderStudioInspector(); }
          else if (a === "remove_afk") callBackend("runStudioAction", JSON.stringify({ action: "remove_silence_afk", title: "Анти-AFK", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_no_afk") } }));
          else if (a === "remove_fillers") callBackend("runStudioAction", JSON.stringify({ action: "remove_fillers", title: "AI Слова-паразиты", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_clean_speech") } }));
          else if (a === "best_frames" || a === "vision_thumb") { state.studioSub = "thumbnail"; updateStudioToolTabsUI(); renderStudioInspector(); syncStudioMonitorOverlays(); callBackend("runAiSuiteTask", a, JSON.stringify({ video_path: state.studioFile, ts: mon?.currentTime || 15 })); }
          else if (a === "split_30" || a === "split_60") callBackend("runStudioAction", JSON.stringify({ action: "split_parts", title: `Разбивка по ${a === "split_60" ? 60 : 30} мин`, subtitle: fname, params: { input_path: state.studioFile, segment_minutes: a === "split_60" ? 60 : 30 } }));
          else if (a === "ban_shield") { state.aiHubSub = "speech"; renderStudioInspector(); callBackend("runAiSuiteTask", "ban_shield", JSON.stringify({ video_path: state.studioFile })); }
          else if (a === "chapters" || a === "social_post") { state.aiHubSub = "seo"; renderStudioInspector(); callBackend("runAiSuiteTask", a, JSON.stringify({ video_path: state.studioFile, platform: "telegram" })); }
          else if (a === "auto_montage") callBackend("runStudioAction", JSON.stringify({ action: "auto_montage", title: "AI Авто-монтаж", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_best_of", ".mp4") } }));
          else if (a === "voice_iso") callBackend("runStudioAction", JSON.stringify({ action: "voice_isolate", title: "Изоляция голоса", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_voice_only", ".mp4") } }));
        };
      });
      $("#btnAiSubs")?.addEventListener("click", () => {
        const lv = $("#aiLang").value, isTr = lv === "translate_en";
        callBackend("runStudioAction", JSON.stringify({ action: "subtitles", title: isTr ? "Whisper Перевод RU → EN" : "Whisper Субтитры + Эмодзи", subtitle: $("#aiModel").value, input_path: state.studioFile, model: $("#aiModel").value, language: isTr ? "ru" : lv, task: isTr ? "translate" : "transcribe", burn_subtitles: $("#aiBurn").checked, shorts_style: true, whisper_prompt: $("#aiPrompt").value.trim() }));
      });
      $("#btnAiCensor")?.addEventListener("click", () => {
        const lv = $("#aiLang").value;
        callBackend("runStudioAction", JSON.stringify({ action: "censor", title: "AI Аудио-цензура", subtitle: $("#aiModel").value, input_path: state.studioFile, model: $("#aiModel").value, language: lv === "translate_en" ? "ru" : lv, banned_words: $("#aiBanned").value, censor_beep: $("#aiBeep").checked, whisper_prompt: $("#aiPrompt").value.trim() }));
      });
      $("#btnBanShield")?.addEventListener("click", () => { callBackend("runAiSuiteTask", "ban_shield", JSON.stringify({ video_path: state.studioFile, banned_words: $("#aiBanned").value, model: $("#aiModel").value })); showToast("AI Ban-Word Shield", "Сканируем речь на бан-ворды...", "info"); });
      $("#btnAiDubEn")?.addEventListener("click", () => { callBackend("runStudioAction", JSON.stringify({ action: "ai_dub", title: "AI Дубляж (EN)", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_dub_en", ".mp4"), target_lang: "en" } })); });
      $("#subPrevOn")?.addEventListener("change", (e) => { state.subPreview.show = e.target.checked; syncStudioMonitorOverlays(); });
      $("#subPrevTxt")?.addEventListener("input", (e) => { state.subPreview.text = e.target.value; syncStudioMonitorOverlays(); });
      $("#subPrevSz")?.addEventListener("input", (e) => { state.subPreview.size = Number(e.target.value) || 28; syncStudioMonitorOverlays(); });
      $("#subPrevCol")?.addEventListener("input", (e) => { state.subPreview.color = e.target.value; syncStudioMonitorOverlays(); });
      $("#subPrevPos")?.addEventListener("change", (e) => { state.subPreview.pos = e.target.value; syncStudioMonitorOverlays(); });
      host.querySelectorAll(".act-bjump").forEach((el) => { el.onclick = () => seekStudioMonitor(Number(el.dataset.ts) || 0, true); });
      $("#btnAiChapters")?.addEventListener("click", () => callBackend("runAiSuiteTask", "chapters", JSON.stringify({ video_path: state.studioFile })));
      $("#btnGenYtMeta")?.addEventListener("click", () => callBackend("generateYouTubeMeta", state.studioFile));
      $("#btnGenDigest")?.addEventListener("click", () => callBackend("generateStreamDigest", state.studioFile));
      $("#btnAiCritique")?.addEventListener("click", () => callBackend("runAiSuiteTask", "virality_critique", JSON.stringify({ video_path: state.studioFile, title: state.studioThumb.headline })));
      $("#btnAiSocialPost")?.addEventListener("click", () => callBackend("runAiSuiteTask", "social_post", JSON.stringify({ video_path: state.studioFile, platform: $("#aiPostPlat").value })));
      $("#btnCopyChap")?.addEventListener("click", () => { navigator.clipboard?.writeText(state.aiChapters); showToast("Скопировано", "", "success"); });
      $("#btnCopyPost")?.addEventListener("click", () => { navigator.clipboard?.writeText(state.aiSocialPost); showToast("Скопировано", "", "success"); });
      $("#btnCopyYtMeta")?.addEventListener("click", () => { navigator.clipboard?.writeText(`${(state.ytMeta.titles || []).join("\n")}\n\n${state.ytMeta.description || ""}`); showToast("Скопировано", "", "success"); });
      $("#btnCopyDigest")?.addEventListener("click", () => { navigator.clipboard?.writeText(state.streamDigest); showToast("Скопировано", "", "success"); });
    } else if (state.studioSub === "thumbnail") {
      const swatches = ["#8b5cf6", "#ef4444", "#22c55e", "#f59e0b", "#3b82f6", "#ec4899"];
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Обложка 1280×720 + AI Vision</span></div>
          <div class="form-grid-2" style="gap:6px;">
            <button class="btn btn-soft btn-sm" id="btnBestFrames">🖼 AI 5 лучших кадров</button>
            <button class="btn btn-soft btn-sm" id="btnVisionIdeas">🤖 AI Идеи по кадру</button>
          </div>
          ${state.aiBestFrames.length ? `
            <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:5px;">
              ${state.aiBestFrames.map((f) => `<img src="${mediaUrl(f.path)}&t=${Date.now()}" class="act-bf-pick" data-ts="${f.ts}" title="${fmtDur(f.ts)} · Оценка ${f.score}" style="width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:6px;border:1px solid var(--border);cursor:pointer;" />`).join("")}
            </div>
          ` : ""}
          ${state.aiThumbIdeas.length ? `
            <div class="split-pills">
              ${state.aiThumbIdeas.map((id, i) => `<button type="button" class="split-pill act-idea-pick" data-idx="${i}" style="font-size:11px;">${esc(id.headline)}</button>`).join("")}
            </div>
          ` : ""}
          <div class="field"><label>Крупный заголовок</label><input class="input" id="thHeadline" value="${esc(state.studioThumb.headline)}" /></div>
          <div class="field"><label>Плашка-подзаголовок</label><input class="input" id="thSubtitle" value="${esc(state.studioThumb.subtitle)}" /></div>
          <div class="form-grid-2">
            <div class="field"><label>Таймкод кадра</label><input class="input mono" id="thTime" value="${fmtDur(mon?.currentTime || 15)}" /></div>
            <div class="field"><label>Цвет акцента</label>
              <div class="color-swatch-row" style="margin-top:6px;">
                ${swatches.map((col) => `<button type="button" class="color-swatch ${state.studioThumb.accent === col ? "active" : ""}" data-col="${col}" style="background:${col};"></button>`).join("")}
              </div>
            </div>
          </div>
          <div class="form-grid-2" style="gap:8px;">
            <button class="btn btn-primary btn-sm" id="btnCreateThumb">🖼 Обложка .JPG</button>
            <button class="btn btn-soft btn-sm" id="btnQuoteCard">💬 Карточка-цитата</button>
          </div>
          ${state.lastThumbPath ? `<img src="${mediaUrl(state.lastThumbPath)}&t=${Date.now()}" style="width:100%;border-radius:10px;border:1px solid var(--border);cursor:pointer;" id="btnRevealThumb" title="Открыть в папке" />` : ""}
          ${state.aiQuotePath ? `<img src="${mediaUrl(state.aiQuotePath)}&t=${Date.now()}" style="width:100%;border-radius:10px;border:1px solid var(--border);cursor:pointer;" id="btnRevealQuote" title="Открыть цитату в папке" />` : ""}
        </div>
      `;
      $("#btnBestFrames").onclick = () => { callBackend("runAiSuiteTask", "best_frames", JSON.stringify({ video_path: state.studioFile })); showToast("AI Подбор кадров", "Ищем 5 самых чётких и эмоциональных кадров...", "info"); };
      $("#btnVisionIdeas").onclick = () => { callBackend("runAiSuiteTask", "vision_thumb", JSON.stringify({ video_path: state.studioFile, ts: mon?.currentTime || 15 })); showToast("AI Vision", "Анализируем кадр и генерируем заголовки...", "info"); };
      host.querySelectorAll(".act-bf-pick").forEach((img) => { img.onclick = () => { const ts = Number(img.dataset.ts) || 0; seekStudioMonitor(ts, false); if ($("#thTime")) $("#thTime").value = fmtDur(ts); }; });
      host.querySelectorAll(".act-idea-pick").forEach((b) => {
        b.onclick = () => {
          const it = state.aiThumbIdeas[Number(b.dataset.idx)];
          if (!it) return;
          state.studioThumb = { headline: it.headline, subtitle: it.subtitle, accent: it.accent || state.studioThumb.accent };
          renderStudioInspector();
          syncStudioMonitorOverlays();
        };
      });
      $("#thHeadline").oninput = (e) => { state.studioThumb.headline = e.target.value; syncStudioMonitorOverlays(); };
      $("#thSubtitle").oninput = (e) => { state.studioThumb.subtitle = e.target.value; syncStudioMonitorOverlays(); };
      host.querySelectorAll(".color-swatch").forEach((s) => { s.onclick = () => { state.studioThumb.accent = s.dataset.col; host.querySelectorAll(".color-swatch").forEach((x) => x.classList.toggle("active", x === s)); syncStudioMonitorOverlays(); }; });
      $("#btnCreateThumb").onclick = async () => {
        const raw = await callBackend("createThumbnail", JSON.stringify({ video_path: state.studioFile, timestamp_sec: parseSec($("#thTime").value) ?? (mon?.currentTime || 15), headline: state.studioThumb.headline.trim() || "ЛУЧШИЙ МОМЕНТ", subtitle: state.studioThumb.subtitle.trim() || "TWITCH HIGHLIGHTS", accent_color: state.studioThumb.accent }));
        try { const res = JSON.parse(raw || "{}"); if (res.ok && res.output_path) { state.lastThumbPath = res.output_path; renderStudioInspector(); } } catch {}
      };
      $("#btnQuoteCard").onclick = () => {
        callBackend("runAiSuiteTask", "quote_card", JSON.stringify({ video_path: state.studioFile, ts: parseSec($("#thTime").value) ?? (mon?.currentTime || 15), quote: state.studioThumb.headline, author: state.studioThumb.subtitle, accent: state.studioThumb.accent }));
      };
      $("#btnRevealThumb")?.addEventListener("click", () => callBackend("openInExplorer", state.lastThumbPath));
      $("#btnRevealQuote")?.addEventListener("click", () => callBackend("openInExplorer", state.aiQuotePath));
    } else if (state.studioSub === "effects") {
      const fx = state.studioFx;
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Цветокор и Эффекты</span><button class="btn btn-subtle btn-sm" id="btnFxReset">Сброс</button></div>
          <div class="split-pills" id="fxPresets" style="margin-top:0;">
            ${[["none", "Оригинал"], ["vibrant", "Яркий"], ["cinema", "Кино"], ["crisp", "Чёткий HD"]].map(([id, lbl]) =>
              `<button type="button" class="split-pill ${fx.color_preset === id ? "active" : ""}" data-cp="${id}">${lbl}</button>`
            ).join("")}
          </div>
          <div class="field"><div class="field-head"><label>Яркость</label><span class="mono" id="fxBrV">${fx.brightness.toFixed(2)}</span></div><input type="range" id="fxBr" min="-0.3" max="0.3" step="0.02" value="${fx.brightness}" /></div>
          <div class="field"><div class="field-head"><label>Контраст</label><span class="mono" id="fxCtV">${fx.contrast.toFixed(2)}</span></div><input type="range" id="fxCt" min="0.7" max="1.5" step="0.02" value="${fx.contrast}" /></div>
          <div class="field"><div class="field-head"><label>Насыщенность</label><span class="mono" id="fxStV">${fx.saturation.toFixed(2)}</span></div><input type="range" id="fxSt" min="0.5" max="1.8" step="0.02" value="${fx.saturation}" /></div>
          <div class="form-grid-3" style="gap:8px;">
            <div class="field"><label>Скорость</label><select class="input-select" id="fxSpd">${[0.5, 0.75, 1, 1.25, 1.5, 2].map((v) => `<option value="${v}" ${fx.speed === v ? "selected" : ""}>${v}×</option>`).join("")}</select></div>
            <div class="field"><label>Fade In</label><input type="number" class="input mono" id="fxFi" value="${fx.fade_in}" min="0" max="10" step="0.5" /></div>
            <div class="field"><label>Fade Out</label><input type="number" class="input mono" id="fxFo" value="${fx.fade_out}" min="0" max="10" step="0.5" /></div>
          </div>
          <div class="field"><div class="field-head"><label>Громкость (дБ)</label><span class="mono" id="fxVolV">${fx.volume_db > 0 ? "+" : ""}${fx.volume_db} дБ</span></div><input type="range" id="fxVol" min="-12" max="12" step="1" value="${fx.volume_db}" /></div>
          <div class="input-row" style="flex-wrap:wrap;">
            <label class="switch-inline"><input type="checkbox" id="fxSharp" ${fx.sharpen ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12px;">Резкость</span></label>
            <label class="switch-inline"><input type="checkbox" id="fxDenoise" ${fx.denoise ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12px;">Шумодав</span></label>
            <label class="switch-inline"><input type="checkbox" id="fxVoice" ${fx.voice_enhance ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12px;">Голос</span></label>
            <label class="switch-inline"><input type="checkbox" id="fxNorm" ${fx.normalize_audio ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12px;">EBU R128</span></label>
          </div>
          <button class="btn btn-primary btn-full" id="btnApplyFx">${iconSvg("sparkles", 14)}<span>Применить цветокор и эффекты</span></button>
        </div>

        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>AI Аудио: DMCA, Музыка и SFX</span></div>
          <button class="btn btn-soft btn-full btn-sm" id="btnVoiceIso" title="Убрать фоновую музыку и оставить чистый голос (защита от DMCA)">🎙 Изолировать голос (DMCA Safe)</button>
          <div class="form-grid-2" style="gap:6px;">
            <button class="btn btn-subtle btn-sm" id="btnMusicDuck" title="Подложить музыку с авто-приглушением на голосе">🎵 Музыка + Ducking</button>
            <button class="btn btn-subtle btn-sm" id="btnAutoSfx" title="Добавить Vine Boom на пиковые моменты">💥 Авто SFX (Boom)</button>
          </div>
        </div>
      `;
      const syncFx = () => {
        fx.brightness = Number($("#fxBr").value); $("#fxBrV").textContent = fx.brightness.toFixed(2);
        fx.contrast = Number($("#fxCt").value); $("#fxCtV").textContent = fx.contrast.toFixed(2);
        fx.saturation = Number($("#fxSt").value); $("#fxStV").textContent = fx.saturation.toFixed(2);
        fx.volume_db = Number($("#fxVol").value); $("#fxVolV").textContent = `${fx.volume_db > 0 ? "+" : ""}${fx.volume_db} дБ`;
        syncStudioMonitorOverlays();
      };
      ["#fxBr", "#fxCt", "#fxSt", "#fxVol"].forEach((id) => $(id).oninput = syncFx);
      $("#fxPresets").querySelectorAll(".split-pill").forEach((b) => { b.onclick = () => { fx.color_preset = b.dataset.cp; $("#fxPresets").querySelectorAll(".split-pill").forEach((x) => x.classList.toggle("active", x === b)); syncStudioMonitorOverlays(); }; });
      $("#btnFxReset").onclick = () => { state.studioFx = { speed: 1, color_preset: "none", brightness: 0, contrast: 1, saturation: 1, sharpen: false, fade_in: 0, fade_out: 0, volume_db: 0, denoise: false, voice_enhance: false, normalize_audio: false }; renderStudioInspector(); syncStudioMonitorOverlays(); };
      $("#btnApplyFx").onclick = () => {
        fx.speed = Number($("#fxSpd").value) || 1; fx.fade_in = Number($("#fxFi").value) || 0; fx.fade_out = Number($("#fxFo").value) || 0;
        fx.sharpen = $("#fxSharp").checked; fx.denoise = $("#fxDenoise").checked; fx.voice_enhance = $("#fxVoice").checked; fx.normalize_audio = $("#fxNorm").checked;
        callBackend("runStudioAction", JSON.stringify({ action: "studio_effects", title: "Эффекты и цветокор", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_fx", ".mp4"), ...fx } }));
      };
      $("#btnVoiceIso").onclick = () => callBackend("runStudioAction", JSON.stringify({ action: "voice_isolate", title: "AI Изоляция голоса (DMCA)", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_voice_safe", ".mp4") } }));
      $("#btnMusicDuck").onclick = async () => {
        const mus = await callBackend("pickFile", "Аудио (*.mp3 *.wav *.m4a *.ogg)");
        if (!mus) return;
        callBackend("runStudioAction", JSON.stringify({ action: "music_ducking", title: "Фоновая музыка (Smart Ducking)", subtitle: mus.split(/[\\/]/).pop(), params: { input_path: state.studioFile, music_path: mus, output_path: appendFileSuffix(state.studioFile, "_music", ".mp4"), music_vol: 0.24 } }));
      };
      $("#btnAutoSfx").onclick = () => {
        const tsList = (state.highlights?.highlights || []).map((h) => h.timestamp_sec);
        if (!tsList.length) tsList.push(mon?.currentTime || 5);
        callBackend("runStudioAction", JSON.stringify({ action: "auto_sfx", title: "AI Авто SFX (Vine Boom)", subtitle: `${tsList.length} эффектов`, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_sfx", ".mp4"), timestamps: tsList, sfx_type: "boom" } }));
      };
    } else if (state.studioSub === "export") {
      host.innerHTML = `
        <div class="studio-inspector-card">
          <div class="inspector-group-title"><span>Экспорт и публикация</span></div>
          <div class="field"><label>Целевой пресет</label><select class="input-select" id="expPreset">${Object.entries(state.exportPresets).map(([id, pr]) => `<option value="${esc(id)}">${esc(pr.label)}</option>`).join("")}</select></div>
          <div class="field"><label>Водяной знак (PNG, опционально)</label><div class="input-row"><input class="input" id="expWmPath" value="${esc(state.config.watermark_path || "")}" placeholder="Логотип в углу..." /><button class="btn btn-subtle btn-sm" id="btnPickWm">${iconSvg("image", 14)}</button></div></div>
          <div class="input-row" style="flex-wrap:wrap;">
            <label class="switch-inline"><input type="checkbox" id="expNorm" checked/><span class="switch-track"></span><span style="font-size:12px;">Нормализация -14 LUFS</span></label>
            <label class="switch-inline"><input type="checkbox" id="expVoice"/><span class="switch-track"></span><span style="font-size:12px;">Улучшение голоса</span></label>
          </div>
          <button class="btn btn-primary btn-full" id="btnDoExport">${iconSvg("download", 15)}<span>Экспортировать видео</span></button>
          <div class="form-grid-2" style="gap:8px;border-top:1px solid var(--border);padding-top:10px;">
            <button class="btn btn-subtle btn-sm" id="btnExtractMp3">${iconSvg("volume", 14)}<span>Скачать MP3</span></button>
            <button class="btn btn-soft btn-sm" id="btnPubTgNow">${iconSvg("bell", 14)}<span>В Telegram</span></button>
          </div>
        </div>
      `;
      $("#btnPickWm").onclick = async () => { const p = await callBackend("pickFile", "Картинки PNG/WebP (*.png *.webp *.jpg)"); if (p) $("#expWmPath").value = p; };
      $("#btnDoExport").onclick = () => {
        const pid = $("#expPreset").value;
        callBackend("runStudioAction", JSON.stringify({ action: "export_preset", title: `Экспорт (${state.exportPresets[pid]?.label || pid})`, subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, `_${pid}`, ".mp4"), preset_id: pid, normalize_audio: $("#expNorm").checked, voice_enhance: $("#expVoice").checked, watermark_path: $("#expWmPath").value.trim() } }));
      };
      $("#btnExtractMp3").onclick = () => callBackend("runStudioAction", JSON.stringify({ action: "extract_audio", title: "Извлечение MP3", subtitle: fname, params: { input_path: state.studioFile, output_path: appendFileSuffix(state.studioFile, "_audio", ".mp3"), format: "mp3" } }));
      $("#btnPubTgNow").onclick = () => callBackend("publishVideoToTelegram", state.studioFile, "");
    }
  }

  function renderHeatmapSvg() {
    const hm = state.highlights.heatmap || [];
    if (!hm.length) return `<div style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:13px;">Нажмите «Анализ чата» или «Громкие моменты» для построения графика активности</div>`;
    const maxV = Math.max(...hm.map((x) => x[1]), 1);
    const W = 800, H = 130;
    const pts = hm.map((x, i) => `${(i / Math.max(1, hm.length - 1)) * W},${H - (x[1] / maxV) * (H - 12)}`);
    const lineD = `M ${pts.join(" L ")}`;
    const areaD = `M 0,${H} L ${pts.join(" L ")} L ${W},${H} Z`;
    const col = state.highlights.mode === "audio" ? "#22c55e" : "#8b5cf6";
    return `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <defs><linearGradient id="hmGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${col}" stop-opacity="0.45"/><stop offset="100%" stop-color="${col}" stop-opacity="0.02"/></linearGradient></defs>
        <path d="${areaD}" fill="url(#hmGrad)"/>
        <path d="${lineD}" fill="none" stroke="${col}" stroke-width="2.2"/>
      </svg>
    `;
  }

  // ==================== PAGE 5b: SOCIAL AUTO-POSTING & LOCAL AI ====================
  function renderPublishPage() {
    const root = $("#page-publish");
    const pub = state.pub;
    if (!pub.videoPath && state.studioFile) pub.videoPath = state.studioFile;
    if (!state.localAi) {
      callBackend("getLocalAiStatus").then((raw) => {
        try {
          state.localAi = JSON.parse(raw || "{}");
          if (state.page === "publish") renderPublishPage();
        } catch {}
      });
    }
    const lai = state.localAi || { engine_label: "Встроенный оффлайн AI (без API)", ollama_installed: false, ollama_running: false, models: [] };
    const hasPlat = (id) => pub.platforms.includes(id);

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Автопостинг в соцсети</div>
          <div class="page-sub">Публикация полноценных роликов, нарезок и вертикальных Shorts/TikTok с локальным AI (обложка, заголовок, описание, хэштеги)</div>
        </div>
        <div class="header-actions">
          <button class="btn btn-soft" id="btnPubAiAll">${iconSvg("sparkles", 15)}<span>Сгенерировать всё (AI)</span></button>
          <button class="btn btn-primary" id="btnPubRunNow">${iconSvg("upload", 15)}<span>Опубликовать (${pub.platforms.length})</span></button>
        </div>
      </div>

      <div class="local-ai-banner">
        <div>
          <div style="font-size:13px;font-weight:700;color:#4ade80;">🤖 Локальный AI активен: ${esc(pub.aiEngine || lai.engine_label)}</div>
          <div style="font-size:12px;color:var(--text-secondary);">
            ${lai.ollama_running && lai.models?.length
              ? `Найдены локальные модели Ollama: ${esc(lai.models.slice(0, 4).join(", "))} — всё работает на 100% бесплатно без API!`
              : (lai.ollama_installed
                ? "Ollama установлен! Нажмите «Скачать qwen2.5:3b», чтобы включить мощную локальную нейросеть (или используйте встроенный оффлайн AI)."
                : "Встроенный оффлайн AI уже работает без ключей. Для ещё более умных текстов можно бесплатно поставить Ollama (ollama.com).")}
          </div>
        </div>
        <div class="input-row">
          ${lai.ollama_installed
            ? `<button class="btn btn-soft btn-sm" id="btnPubPullOllama">${iconSvg("download", 14)}<span>Скачать qwen2.5:3b</span></button>`
            : `<button class="btn btn-subtle btn-sm" id="btnPubGetOllama">Скачать Ollama (бесплатно)</button>`}
        </div>
      </div>

      <div class="pub-grid">
        <!-- LEFT COLUMN: VIDEO, FORMAT, PLATFORMS, TEXTS -->
        <div style="display:flex;flex-direction:column;gap:14px;">
          <div class="card">
            <div class="field" style="margin-bottom:14px;">
              <label>1. Видеофайл для публикации</label>
              <div class="input-row">
                <input class="input" id="pubVidInp" value="${esc(pub.videoPath)}" placeholder="Выберите запись или готовый ролик..." />
                <button class="btn btn-subtle" id="btnPubPickVid">${iconSvg("folder", 15)}<span>Выбрать</span></button>
              </div>
            </div>

            <div class="field" style="margin-bottom:14px;">
              <label>2. Формат публикации</label>
              <div class="pub-format-cards" id="pubFormatCards">
                <button type="button" class="pub-format-card ${pub.formatMode === "full" ? "active" : ""}" data-fmt="full">
                  <div style="font-size:18px;">🎬</div>
                  <div class="pub-format-title">Полноценный ролик</div>
                  <div class="pub-format-sub">Горизонтальный 16:9 целиком</div>
                </button>
                <button type="button" class="pub-format-card ${pub.formatMode === "clip" ? "active" : ""}" data-fmt="clip">
                  <div style="font-size:18px;">✂</div>
                  <div class="pub-format-title">Нарезка / Хайлайт</div>
                  <div class="pub-format-sub">Вырезать отрезок От — До</div>
                </button>
                <button type="button" class="pub-format-card ${pub.formatMode === "vertical" ? "active" : ""}" data-fmt="vertical">
                  <div style="font-size:18px;">📱</div>
                  <div class="pub-format-title">Вертикальный 9:16</div>
                  <div class="pub-format-sub">Авто-рендер Shorts / TikTok</div>
                </button>
              </div>
            </div>

            ${pub.formatMode === "clip" ? `
              <div class="form-grid-2" style="margin-bottom:14px;">
                <div class="field"><label>Начало нарезки</label><input class="input mono" id="pubClipS" value="${esc(pub.clipStart)}" placeholder="00:00:00" /></div>
                <div class="field"><label>Конец нарезки</label><input class="input mono" id="pubClipE" value="${esc(pub.clipEnd)}" placeholder="00:01:00" /></div>
              </div>
            ` : pub.formatMode === "vertical" ? `
              <div class="field" style="margin-bottom:14px;">
                <label>Компоновка вертикального видео 9:16 (1080×1920)</label>
                <select class="input-select" id="pubVertLayout">
                  <option value="blur_bg" ${pub.verticalLayout === "blur_bg" ? "selected" : ""}>Видео по центру + размытый фон (универсально)</option>
                  <option value="split" ${pub.verticalLayout === "split" ? "selected" : ""}>Вебка сверху + Геймплей снизу</option>
                  <option value="pip_circle" ${pub.verticalLayout === "pip_circle" ? "selected" : ""}>Круглая вебка PiP поверх геймплея</option>
                </select>
              </div>
            ` : ""}

            <div class="field">
              <label>3. Куда выкладываем (можно выбрать несколько)</label>
              <div class="pub-plat-grid" id="pubPlatGrid">
                <button type="button" class="pub-plat-btn ${hasPlat("youtube") ? "active" : ""}" data-plat="youtube">▶ YouTube</button>
                <button type="button" class="pub-plat-btn ${hasPlat("tiktok") ? "active" : ""}" data-plat="tiktok">🎵 TikTok</button>
                <button type="button" class="pub-plat-btn ${hasPlat("telegram") ? "active" : ""}" data-plat="telegram">✈ Telegram</button>
                <button type="button" class="pub-plat-btn ${hasPlat("vk") ? "active" : ""}" data-plat="vk">💙 VK / Клипы</button>
                <button type="button" class="pub-plat-btn ${hasPlat("discord") ? "active" : ""}" data-plat="discord">💬 Discord</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="input-row" style="justify-content:space-between;margin-bottom:10px;">
              <strong style="font-size:14px;">4. Заголовок, описание и хэштеги</strong>
              <button class="btn btn-soft btn-sm" id="btnPubAiText">${iconSvg("sparkles", 13)}<span>Сгенерировать AI</span></button>
            </div>
            <div class="field" style="margin-bottom:10px;">
              <label>Заголовок ролика</label>
              <input class="input" id="pubTitleInp" value="${esc(pub.title)}" placeholder="Введите заголовок или нажмите «Сгенерировать AI»..." />
            </div>
            ${pub.titleVariants.length ? `
              <div class="split-pills" style="margin-bottom:10px;">
                ${pub.titleVariants.map((t, i) => `<button type="button" class="split-pill act-pub-tvar" data-idx="${i}" style="font-size:11.5px;">${esc(t)}</button>`).join("")}
              </div>
            ` : ""}
            <div class="field" style="margin-bottom:10px;">
              <label>Описание (с таймкодами)</label>
              <textarea class="input" id="pubDescInp" style="min-height:110px;resize:vertical;font-family:inherit;">${esc(pub.description)}</textarea>
            </div>
            <div class="field" style="margin-bottom:10px;">
              <label>Хэштеги</label>
              <input class="input" id="pubTagsInp" value="${esc(pub.hashtags)}" placeholder="#shorts #twitch #игры #нарезка" />
              <div class="split-pills" id="pubTagQuick">
                ${["#shorts", "#tiktok", "#reels", "#twitch", "#стрим", "#нарезка", "#хайлайты", "#игры", "#cs2", "#gta"].map((tg) =>
                  `<button type="button" class="split-pill" data-tag="${tg}" style="padding:3px 9px;font-size:11.5px;">+ ${tg}</button>`
                ).join("")}
              </div>
            </div>
            <div class="input-row" style="justify-content:space-between;flex-wrap:wrap;">
              <div class="input-row">
                <span style="font-size:12px;color:var(--text-muted);">Доступ YouTube:</span>
                <select class="input-select compact" id="pubPrivacy">
                  <option value="public" ${pub.privacy === "public" ? "selected" : ""}>Открытый (Public)</option>
                  <option value="unlisted" ${pub.privacy === "unlisted" ? "selected" : ""}>По ссылке (Unlisted)</option>
                  <option value="private" ${pub.privacy === "private" ? "selected" : ""}>Личный (Private)</option>
                </select>
              </div>
              <button class="btn btn-subtle btn-sm" id="btnPubCopyAll">${iconSvg("copy", 13)}<span>Копировать весь текст</span></button>
            </div>
          </div>
        </div>

        <!-- RIGHT COLUMN: COVER GENERATOR + PUBLISH RESULTS -->
        <div style="display:flex;flex-direction:column;gap:14px;">
          <div class="card">
            <div class="input-row" style="justify-content:space-between;margin-bottom:10px;">
              <strong style="font-size:14px;">5. Обложка 1280×720 (Превью)</strong>
              <button class="btn btn-subtle btn-sm" id="btnPubPickCover">${iconSvg("image", 13)}<span>Своя картинка</span></button>
            </div>
            <div class="pub-cover-box" style="margin-bottom:12px;">
              ${pub.thumbPath
                ? `<img src="${mediaUrl(pub.thumbPath)}&t=${Date.now()}" alt="Обложка" />`
                : `<div style="text-align:center;padding:20px;">${iconSvg("image", 32)}<div style="font-size:12.5px;margin-top:6px;">Нажмите «Сгенерировать обложку» ниже</div></div>`}
            </div>
            <div class="field" style="margin-bottom:8px;"><label>Заголовок на обложке</label><input class="input" id="pubThHead" value="${esc(pub.thumbHeadline)}" /></div>
            <div class="form-grid-2" style="margin-bottom:10px;">
              <div class="field"><label>Плашка сверху</label><input class="input" id="pubThSub" value="${esc(pub.thumbSubtitle)}" /></div>
              <div class="field"><label>Кадр (таймкод)</label><input class="input mono" id="pubThTs" value="${esc(pub.thumbTs)}" placeholder="00:15" /></div>
            </div>
            <div class="input-row" style="justify-content:space-between;margin-bottom:10px;">
              <div class="color-swatch-row">
                ${["#8b5cf6", "#ef4444", "#22c55e", "#f59e0b", "#3b82f6", "#ec4899"].map((col) =>
                  `<button type="button" class="color-swatch ${pub.thumbAccent === col ? "active" : ""}" data-pcol="${col}" style="background:${col};"></button>`
                ).join("")}
              </div>
              <button class="btn btn-primary btn-sm" id="btnPubGenCover">${iconSvg("sparkles", 14)}<span>Сгенерировать обложку</span></button>
            </div>
          </div>

          <div class="card">
            <div class="input-row" style="justify-content:space-between;margin-bottom:8px;">
              <strong style="font-size:13.5px;">📅 Контент-календарь (Отложенный постинг)</strong>
            </div>
            <div class="input-row" style="margin-bottom:10px;">
              <input type="datetime-local" class="input mono" id="pubSchedAt" style="flex:1;" />
              <button class="btn btn-soft btn-sm" id="btnPubAddSched">📅 Запланировать</button>
            </div>
            ${state.scheduledPosts.length ? `
              <div class="hl-list">
                ${state.scheduledPosts.map((sp) => `
                  <div class="hl-row">
                    <span class="mono" style="font-size:11.5px;color:var(--accent-light);">${esc(sp.publish_at)}</span>
                    <span style="flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(sp.title)} (${esc((sp.platforms || []).join(", "))})</span>
                    <span class="diag-pill ${sp.status === "published" ? "" : "off"}" style="font-size:10.5px;">${sp.status === "published" ? "Готово" : "Ждёт"}</span>
                    <button class="btn-icon danger act-del-sp" data-id="${esc(sp.id)}">${iconSvg("trash", 13)}</button>
                  </div>
                `).join("")}
              </div>
            ` : `<div style="font-size:12px;color:var(--text-muted);">Нет запланированных публикаций — выберите дату выше и нажмите «Запланировать».</div>`}
          </div>

          ${pub.results.length ? `
            <div class="card">
              <strong style="font-size:13.5px;display:block;margin-bottom:8px;">Результаты публикации</strong>
              <div class="hl-list">
                ${pub.results.map((r) => `
                  <div class="hl-row">
                    <span class="diag-pill ${r.ok ? "" : "off"}" style="${r.ok ? "" : "color:#f87171;"}">${esc(r.platform)}</span>
                    <span style="flex:1;font-size:12px;color:var(--text-secondary);">${esc(r.message)}</span>
                  </div>
                `).join("")}
              </div>
            </div>
          ` : ""}
        </div>
      </div>
    `;

    const syncPubInputs = () => {
      pub.videoPath = $("#pubVidInp")?.value.trim() || pub.videoPath;
      pub.title = $("#pubTitleInp")?.value || "";
      pub.description = $("#pubDescInp")?.value || "";
      pub.hashtags = $("#pubTagsInp")?.value || "";
      pub.privacy = $("#pubPrivacy")?.value || "unlisted";
      pub.thumbHeadline = $("#pubThHead")?.value || "";
      pub.thumbSubtitle = $("#pubThSub")?.value || "";
      pub.thumbTs = $("#pubThTs")?.value || "00:15";
      if ($("#pubClipS")) pub.clipStart = $("#pubClipS").value;
      if ($("#pubClipE")) pub.clipEnd = $("#pubClipE").value;
      if ($("#pubVertLayout")) pub.verticalLayout = $("#pubVertLayout").value;
    };

    $("#btnPubPullOllama")?.addEventListener("click", () => callBackend("pullOllamaModel", state.config.local_ai_model || "qwen2.5:3b"));
    $("#btnPubGetOllama")?.addEventListener("click", () => callBackend("openExternalUrl", "https://ollama.com/download"));
    $("#btnPubPickVid").onclick = async () => {
      const p = await callBackend("pickFile", "Видео (*.mp4 *.mkv *.ts *.mov *.webm)");
      if (p) { pub.videoPath = p; renderPublishPage(); }
    };
    $("#btnPubPickCover").onclick = async () => {
      const p = await callBackend("pickFile", "Картинки (*.jpg *.jpeg *.png *.webp)");
      if (p) { pub.thumbPath = p; renderPublishPage(); }
    };
    $("#pubFormatCards").querySelectorAll(".pub-format-card").forEach((b) => {
      b.onclick = () => { syncPubInputs(); pub.formatMode = b.dataset.fmt; renderPublishPage(); };
    });
    $("#pubPlatGrid").querySelectorAll(".pub-plat-btn").forEach((b) => {
      b.onclick = () => {
        syncPubInputs();
        const pl = b.dataset.plat;
        if (pub.platforms.includes(pl)) pub.platforms = pub.platforms.filter((x) => x !== pl);
        else pub.platforms.push(pl);
        renderPublishPage();
      };
    });
    root.querySelectorAll(".act-pub-tvar").forEach((b) => {
      b.onclick = () => {
        pub.title = pub.titleVariants[Number(b.dataset.idx)] || pub.title;
        if ($("#pubTitleInp")) $("#pubTitleInp").value = pub.title;
      };
    });
    $("#pubTagQuick").querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        const tg = b.dataset.tag;
        const cur = $("#pubTagsInp").value.trim();
        if (!cur.toLowerCase().includes(tg.toLowerCase())) {
          $("#pubTagsInp").value = (cur ? `${cur} ${tg}` : tg);
          pub.hashtags = $("#pubTagsInp").value;
        }
      };
    });
    root.querySelectorAll("[data-pcol]").forEach((s) => {
      s.onclick = () => { syncPubInputs(); pub.thumbAccent = s.dataset.pcol; renderPublishPage(); };
    });

    const genCover = async () => {
      syncPubInputs();
      if (!pub.videoPath) return showToast("Обложка", "Сначала выберите видеофайл", "error");
      const raw = await callBackend("createThumbnail", JSON.stringify({
        video_path: pub.videoPath,
        timestamp_sec: parseSec(pub.thumbTs) ?? 15,
        headline: pub.thumbHeadline.trim() || pub.title.slice(0, 32) || "ЛУЧШИЙ МОМЕНТ",
        subtitle: pub.thumbSubtitle.trim() || "TWITCH HIGHLIGHTS",
        accent_color: pub.thumbAccent,
      }));
      try {
        const res = JSON.parse(raw || "{}");
        if (res.ok && res.output_path) { pub.thumbPath = res.output_path; renderPublishPage(); }
      } catch {}
    };
    $("#btnPubGenCover").onclick = genCover;

    const genAiPkg = (alsoCover) => {
      syncPubInputs();
      if (!pub.videoPath) return showToast("Автопостинг AI", "Сначала выберите видеофайл", "error");
      pub._autoGenCoverNext = !!alsoCover;
      callBackend("runAiSuiteTask", "publish_package", JSON.stringify({
        video_path: pub.videoPath,
        format_mode: pub.formatMode,
        platforms: pub.platforms,
      }));
      showToast("Локальный AI", "Генерируем заголовок, описание и хэштеги...", "info");
    };
    $("#btnPubAiText").onclick = () => genAiPkg(false);
    $("#btnPubAiAll").onclick = () => genAiPkg(true);

    $("#btnPubCopyAll").onclick = () => {
      syncPubInputs();
      navigator.clipboard?.writeText(`${pub.title}\n\n${pub.description}\n\n${pub.hashtags}`.trim());
      showToast("Скопировано", "Заголовок, описание и хэштеги в буфере обмена", "success");
    };

    $("#btnPubRunNow").onclick = () => {
      syncPubInputs();
      if (!pub.videoPath) return showToast("Автопостинг", "Выберите видеофайл", "error");
      if (!pub.platforms.length) return showToast("Автопостинг", "Выберите хотя бы одну соцсеть", "error");
      navigator.clipboard?.writeText(`${pub.title}\n\n${pub.description}\n\n${pub.hashtags}`.trim());
      callBackend("executeAutoPublish", JSON.stringify({
        video_path: pub.videoPath,
        thumb_path: pub.thumbPath,
        format_mode: pub.formatMode,
        platforms: pub.platforms,
        title: pub.title || pub.videoPath.split(/[\\/]/).pop(),
        description: pub.description,
        hashtags: pub.hashtags,
        privacy: pub.privacy,
        clip_start: parseSec(pub.clipStart) ?? 0,
        clip_end: parseSec(pub.clipEnd) ?? 60,
        vertical_layout: pub.verticalLayout,
      }));
    };

    if (!state._schedLoaded) {
      state._schedLoaded = true;
      callBackend("getScheduledPosts").then((raw) => {
        try { state.scheduledPosts = JSON.parse(raw || "[]"); if (state.page === "publish") renderPublishPage(); } catch {}
      });
    }
    $("#btnPubAddSched").onclick = async () => {
      syncPubInputs();
      const dt = ($("#pubSchedAt")?.value || "").replace("T", " ").trim();
      if (!pub.videoPath) return showToast("Календарь", "Выберите видеофайл", "error");
      if (!dt) return showToast("Календарь", "Укажите дату и время публикации", "error");
      const raw = await callBackend("addScheduledPost", JSON.stringify({
        publish_at: dt,
        video_path: pub.videoPath,
        thumb_path: pub.thumbPath,
        format_mode: pub.formatMode,
        platforms: pub.platforms,
        title: pub.title || pub.videoPath.split(/[\\/]/).pop(),
        description: pub.description,
        hashtags: pub.hashtags,
        privacy: pub.privacy,
        clip_start: parseSec(pub.clipStart) ?? 0,
        clip_end: parseSec(pub.clipEnd) ?? 60,
        vertical_layout: pub.verticalLayout,
      }));
      try { state.scheduledPosts = JSON.parse(raw || "[]"); renderPublishPage(); } catch {}
    };
    root.querySelectorAll(".act-del-sp").forEach((b) => {
      b.onclick = async () => {
        const raw = await callBackend("deleteScheduledPost", b.dataset.id);
        try { state.scheduledPosts = JSON.parse(raw || "[]"); renderPublishPage(); } catch {}
      };
    });
  }

  // ==================== PAGE 1b: MULTIVIEW WALL (2-4 LIVE STREAMS) ====================
  function renderMultiviewPage() {
    const root = $("#page-multiview");
    const online = state.channels.filter((c) => c.status?.is_live).slice(0, 4);
    if (!state.multiviewSolo && online.length) state.multiviewSolo = online[0].key;

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Мультистрим-стена (Multiview)</div>
          <div class="page-sub">До 4 прямых эфиров одновременно без рекламы Twitch · Клик по 🔊 включает соло-звук</div>
        </div>
        <div class="header-actions">
          <button class="btn btn-subtle" id="btnMvRefresh">${iconSvg("refresh", 15)}<span>Обновить потоки</span></button>
        </div>
      </div>

      ${online.length === 0 ? `
        <div class="empty-state">
          ${iconSvg("video", 40)}
          <h3>Нет активных стримов онлайн</h3>
          <p>Когда отслеживаемые стримеры выйдут в эфир, здесь автоматически появятся их живые плееры (до 4 одновременно).</p>
        </div>
      ` : `
        <div class="multiview-grid">
          ${online.map((ch) => {
            const isSolo = state.multiviewSolo === ch.key;
            const rec = !!ch.is_recording;
            return `
              <div class="mv-tile ${isSolo ? "solo" : ""}" data-key="${esc(ch.key)}">
                <div class="mv-video-wrap">
                  <video src="/media/stream?url=${encodeURIComponent(ch.stream_url)}&q=480p,720p,best&t=${Date.now()}" autoplay playsinline ${isSolo ? "" : "muted"} controls></video>
                </div>
                <div class="mv-bar">
                  <div style="display:flex;align-items:center;gap:6px;">
                    <strong>${esc(ch.username)}</strong>
                    <span class="plat-tag plat-${esc(ch.platform)}">${esc(ch.platform)}</span>
                  </div>
                  <div class="input-row" style="gap:5px;">
                    <button class="btn ${isSolo ? "btn-primary" : "btn-subtle"} btn-sm act-mv-solo" data-key="${esc(ch.key)}">${isSolo ? "🔊 Соло" : "🔇 Звук"}</button>
                    ${rec
                      ? `<button class="btn btn-subtle btn-sm act-mv-dvr" data-key="${esc(ch.key)}">✂ 60с</button>
                         <button class="btn btn-soft btn-sm act-mv-mk" data-key="${esc(ch.key)}">🔖</button>
                         <button class="btn btn-danger btn-sm act-mv-stop" data-key="${esc(ch.key)}">■</button>`
                      : `<button class="btn btn-success btn-sm act-mv-rec" data-key="${esc(ch.key)}">● Запись</button>`}
                  </div>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      `}
    `;

    $("#btnMvRefresh").onclick = () => renderMultiviewPage();
    root.querySelectorAll(".act-mv-solo").forEach((b) => {
      b.onclick = () => {
        state.multiviewSolo = b.dataset.key;
        root.querySelectorAll(".mv-tile").forEach((t) => {
          const isS = t.dataset.key === state.multiviewSolo;
          t.classList.toggle("solo", isS);
          const v = t.querySelector("video");
          if (v) v.muted = !isS;
          const btn = t.querySelector(".act-mv-solo");
          if (btn) { btn.textContent = isS ? "🔊 Соло" : "🔇 Звук"; btn.className = `btn ${isS ? "btn-primary" : "btn-subtle"} btn-sm act-mv-solo`; }
        });
      };
    });
    root.querySelectorAll(".act-mv-rec").forEach((b) => { b.onclick = () => callBackend("startRecording", b.dataset.key); });
    root.querySelectorAll(".act-mv-stop").forEach((b) => { b.onclick = () => callBackend("stopRecording", b.dataset.key); });
    root.querySelectorAll(".act-mv-dvr").forEach((b) => { b.onclick = () => callBackend("clipLastSeconds", b.dataset.key, 60); });
    root.querySelectorAll(".act-mv-mk").forEach((b) => { b.onclick = () => callBackend("dropMarker", b.dataset.key); });
  }

  // ==================== PAGE 5c: ANALYTICS DASHBOARD ====================
  function renderAnalyticsPage() {
    const root = $("#page-analytics");
    if (!state.analytics) {
      root.innerHTML = `<div class="page-header"><div><div class="page-title">Аналитика записей и стримов</div><div class="page-sub">Собираем статистику по вашей библиотеке...</div></div></div>`;
      callBackend("getAnalyticsData").then((raw) => {
        try { state.analytics = JSON.parse(raw || "{}"); if (state.page === "analytics") renderAnalyticsPage(); } catch {}
      });
      return;
    }
    const a = state.analytics;
    const maxChH = Math.max(...(a.top_channels || []).map((x) => x.hours), 0.1);
    const maxGmH = Math.max(...(a.top_games || []).map((x) => x.hours), 0.1);
    const daily = a.daily || [];
    const maxDayH = Math.max(...daily.map((x) => x.hours), 0.1);

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Аналитика записей и стримов</div>
          <div class="page-sub">Статистика часов записи, объёма хранилища, топ стримеров, игр и активности по дням</div>
        </div>
        <div class="header-actions">
          <button class="btn btn-subtle" id="btnAnRefresh">${iconSvg("refresh", 15)}<span>Обновить</span></button>
        </div>
      </div>

      <div class="live-kpi-strip" style="grid-template-columns:repeat(4,minmax(0,1fr));">
        <div class="kpi-card"><div><div class="kpi-label">Всего записано</div><div class="kpi-val">${a.total_hours || 0} ч</div></div><div class="kpi-icon live">${iconSvg("film", 18)}</div></div>
        <div class="kpi-card"><div><div class="kpi-label">Видеофайлов</div><div class="kpi-val">${a.total_files || 0}</div></div><div class="kpi-icon">${iconSvg("grid", 18)}</div></div>
        <div class="kpi-card"><div><div class="kpi-label">Объём на диске</div><div class="kpi-val">${a.total_gb || 0} ГБ</div></div><div class="kpi-icon">${iconSvg("folder", 18)}</div></div>
        <div class="kpi-card"><div><div class="kpi-label">Маркеров / Чата</div><div class="kpi-val">${a.total_markers || 0} / ${(a.total_chat_msgs || 0).toLocaleString("ru-RU")}</div></div><div class="kpi-icon rec">${iconSvg("flame", 18)}</div></div>
      </div>

      <div class="form-grid-2" style="margin-bottom:14px;">
        <div class="card">
          <strong style="font-size:14px;">Топ стримеров (по часам записи)</strong>
          <div class="analytics-bars">
            ${(a.top_channels || []).length ? a.top_channels.map((c) => `
              <div class="an-bar-row">
                <div class="an-bar-top"><span>${esc(c.name)}</span><span class="mono" style="color:var(--text-muted);">${c.hours} ч · ${c.files} видео · ${c.gb} ГБ</span></div>
                <div class="an-bar-track"><div class="an-bar-fill" style="width:${Math.min(100, (c.hours / maxChH) * 100)}%;"></div></div>
              </div>
            `).join("") : `<div style="color:var(--text-muted);font-size:12.5px;">Пока нет записанных стримов</div>`}
          </div>
        </div>
        <div class="card">
          <strong style="font-size:14px;">Топ категорий / игр</strong>
          <div class="analytics-bars">
            ${(a.top_games || []).length ? a.top_games.map((g) => `
              <div class="an-bar-row">
                <div class="an-bar-top"><span>${esc(g.name)}</span><span class="mono" style="color:var(--text-muted);">${g.hours} ч</span></div>
                <div class="an-bar-track"><div class="an-bar-fill" style="width:${Math.min(100, (g.hours / maxGmH) * 100)}%;background:linear-gradient(90deg,#22c55e,#38bdf8);"></div></div>
              </div>
            `).join("") : `<div style="color:var(--text-muted);font-size:12.5px;">Игры появятся после записи прямых эфиров</div>`}
          </div>
        </div>
      </div>

      <div class="card">
        <strong style="font-size:14px;display:block;margin-bottom:10px;">Активность записей по дням (часов)</strong>
        ${daily.length ? `
          <div style="display:flex;align-items:flex-end;gap:10px;height:130px;padding-top:10px;">
            ${daily.map((d) => `
              <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end;">
                <span class="mono" style="font-size:10.5px;color:var(--accent-light);">${d.hours}ч</span>
                <div style="width:100%;max-width:42px;height:${Math.max(8, (d.hours / maxDayH) * 85)}%;border-radius:7px 7px 3px 3px;background:linear-gradient(180deg,#a78bfa,#6d28d9);"></div>
                <span class="mono" style="font-size:10.5px;color:var(--text-muted);">${esc(d.day)}</span>
              </div>
            `).join("")}
          </div>
        ` : `<div style="color:var(--text-muted);font-size:12.5px;">Нет данных по дням</div>`}
      </div>
    `;

    $("#btnAnRefresh").onclick = () => { state.analytics = null; renderAnalyticsPage(); };
  }

  // ==================== PAGE 6: SETTINGS ====================
  function renderSettingsPage() {
    const root = $("#page-settings");
    if (!state.settingsDraft) {
      state.settingsDraft = { ...state.config };
    }
    const c = state.settingsDraft;
    const d = state.diagnostics;
    const isMac = d.platform === "darwin";
    const encs = d.encoders || ["libx264"];
    const hwEncName = isMac ? "h264_videotoolbox" : "h264_nvenc";
    const hwEncLabel = isMac ? "Apple VideoToolbox" : "NVIDIA NVENC";
    const hasHwEnc = encs.includes(hwEncName);

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Настройки</div>
          <div class="page-sub">Хранилище, лимит GPU-очереди, ночной рендер, Telegram/Discord, Twitch OAuth, прокси, трей и автозапуск</div>
        </div>
        <div class="header-actions">
          <button class="btn btn-primary" id="btnSaveAllSettings">${iconSvg("save", 15)}<span>Сохранить настройки</span></button>
        </div>
      </div>

      <div class="subnav" id="setSubnav">
        <button data-sub="general" class="${state.settingsSub === "general" ? "active" : ""}">Файлы и GPU</button>
        <button data-sub="network" class="${state.settingsSub === "network" ? "active" : ""}">Сеть и OAuth</button>
        <button data-sub="notify" class="${state.settingsSub === "notify" ? "active" : ""}">Telegram / Discord</button>
        <button data-sub="system" class="${state.settingsSub === "system" ? "active" : ""}">Система и Трей</button>
      </div>

      <div class="card ${state.settingsSub === "general" ? "" : "hidden"}">
        <div class="field" style="margin-bottom:14px;">
          <label>Папка сохранения записей</label>
          <div class="input-row">
            <input class="input" id="cfgOutDir" value="${esc(c.output_dir || "")}" />
            <button class="btn btn-subtle" id="btnPickOutDir">${iconSvg("folder", 15)}<span>Обзор</span></button>
          </div>
        </div>
        <div class="form-grid-4" style="margin-bottom:14px;">
          <div class="field"><label>Формат</label><select class="input-select" id="cfgCont"><option ${c.container === "mp4" ? "selected" : ""}>mp4</option><option ${c.container === "mkv" ? "selected" : ""}>mkv</option></select></div>
          <div class="field">
            <label>Разбивка новых каналов</label>
            <select class="input-select" id="cfgDefSeg">
              ${SPLIT_PRESETS.map((p) => `<option value="${p.val}" ${Number(c.default_segment_minutes || 0) === p.val ? "selected" : ""}>${p.label}</option>`).join("")}
            </select>
          </div>
          <div class="field"><label>Проверка эфиров (сек)</label><input type="number" class="input" id="cfgInterval" value="${c.check_interval_sec || 30}" /></div>
          <div class="field"><label>Предупр. диска (&lt; ГБ)</label><input type="number" class="input" id="cfgMinGb" value="${c.min_free_gb || 15}" /></div>
        </div>
        <div class="form-grid-4" style="margin-bottom:14px;">
          <div class="field">
            <label>Видеокодек (GPU / CPU)</label>
            <select class="input-select" id="cfgEnc">
              <option value="auto" ${c.encoder === "auto" ? "selected" : ""}>Авто (${esc(d.active_encoder || "libx264")})</option>
              ${encs.map((e) => `<option value="${esc(e)}" ${c.encoder === e ? "selected" : ""}>${esc(e)}</option>`).join("")}
            </select>
          </div>
          <div class="field"><label>Качество CQ/CRF (19 оптимум)</label><input type="number" class="input" id="cfgCq" value="${c.video_quality || 19}" /></div>
          <div class="field"><label>Потоков VOD / Лимит МБ/с</label>
            <div class="input-row" style="gap:6px;">
              <input type="number" class="input" id="cfgVodFrag" value="${c.vod_concurrent_fragments || 4}" min="1" max="16" title="Количество потоков загрузки VOD" />
              <input type="number" class="input" id="cfgVodLimit" value="${c.vod_rate_limit_mbps || 0}" min="0" max="1000" step="1" title="Лимит скорости VOD в МБ/с (0 = без лимита)" placeholder="МБ/с" />
            </div>
          </div>
          <div class="field"><label>Лимит одноврем. блюров GPU</label><input type="number" class="input" id="cfgMaxBlurs" value="${c.max_concurrent_blurs || 1}" min="1" max="6" /></div>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Smart Segment Blur (умный точечный блюр)</div><div class="setting-row-desc">Перекодирует ТОЛЬКО секунды, где виден баннер, а остальной стрим копирует без потерь (в 5–10 раз быстрее)</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgSmartBlur" ${c.smart_segment_blur !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Hard Disk Guard (авто-стоп при нехватке места)</div><div class="setting-row-desc">Блокирует новые записи и безопасно останавливает текущие, если свободно меньше порога</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgDiskGuard" ${c.hard_disk_guard !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Низкий приоритет фонового рендера</div><div class="setting-row-desc">Снижает приоритет CPU для FFmpeg, чтобы ПК и игры не тормозили</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgLowPrio" ${c.low_priority_render !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Игровой режим (Game Mode)</div><div class="setting-row-desc">Авто-пауза очереди GPU-блюра, пока запущена игра из списка ниже</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgGameMode" ${c.game_mode_enabled !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="field" style="margin:8px 0 12px;">
          <label>Процессы игр (через запятую)</label>
          <input class="input" id="cfgGameProcs" value="${esc(c.game_processes || "")}" />
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Нормализация громкости по умолчанию (EBU R128)</div><div class="setting-row-desc">Выравнивать звук при автоматическом блюре записей</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgNormAudio" ${c.normalize_audio_default ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Ночной режим рендера (отложенный блюр)</div><div class="setting-row-desc">Днём только запись без нагрузки, а блюр запускается в указанное окно</div></div>
          <div class="input-row">
            <input class="input mono" style="width:82px;" id="cfgNightS" value="${esc(c.night_render_start || "02:00")}"/>
            <span>—</span>
            <input class="input mono" style="width:82px;" id="cfgNightE" value="${esc(c.night_render_end || "08:00")}"/>
            <label class="switch-inline"><input type="checkbox" id="cfgNight" ${c.night_render_enabled ? "checked" : ""}/><span class="switch-track"></span></label>
          </div>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Авто-очистка старых записей</div><div class="setting-row-desc">Удалять видео старше указанного числа дней при запуске</div></div>
          <div class="input-row">
            <input type="number" class="input" style="width:90px;" id="cfgMaxDays" value="${c.max_age_days || 30}"/>
            <label class="switch-inline"><input type="checkbox" id="cfgAutoClean" ${c.auto_cleanup_old ? "checked" : ""}/><span class="switch-track"></span></label>
          </div>
        </div>
        <div class="form-grid-3" style="margin:14px 0;">
          <div class="field">
            <label>Позиция оверлея чата</label>
            <select class="input-select" id="cfgChatPos">
              <option value="right" ${c.chat_position !== "left" ? "selected" : ""}>Справа</option>
              <option value="left" ${c.chat_position === "left" ? "selected" : ""}>Слева</option>
            </select>
          </div>
          <div class="field"><label>Шрифт чата (px)</label><input type="number" class="input" id="cfgChatFont" value="${c.chat_font_size || 28}" min="16" max="48" /></div>
          <div class="field"><label>Фильтр чата</label><label class="switch-inline" style="margin-top:8px;"><input type="checkbox" id="cfgChatBots" ${c.chat_hide_bots !== false ? "checked" : ""}/><span class="switch-track"></span><span>Скрывать ботов и !команды</span></label></div>
        </div>
        <div class="form-grid-3" style="margin:14px 0;">
          <div class="field">
            <label>Модель Whisper AI</label>
            <select class="input-select" id="cfgWhisper">
              ${["tiny", "base", "small", "medium"].map((m) => `<option value="${m}" ${(c.whisper_model || "small") === m ? "selected" : ""}>${m}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label>Словарь Whisper (ники, сленг)</label>
            <input class="input" id="cfgWhisperPrompt" value="${esc(c.whisper_prompt || "")}" placeholder="skywhywalker, клатч..." />
          </div>
          <div class="field">
            <label>Словарь авто-цензуры</label>
            <input class="input" id="cfgBanned" value="${esc(c.banned_words || "")}" />
          </div>
        </div>
        <div class="diag-pills">
          <span class="diag-pill ${d.ffmpeg ? "" : "off"}">FFmpeg: ${d.ffmpeg ? "OK" : "Нет"}</span>
          <span class="diag-pill ${d.streamlink ? "" : "off"}">Streamlink: ${d.streamlink ? "OK" : "Нет"}</span>
          <span class="diag-pill ${d.ytdlp ? "" : "off"}">yt-dlp: ${d.ytdlp ? "OK" : "Нет"}</span>
          <span class="diag-pill ${hasHwEnc ? "" : "off"}">${hwEncLabel}: ${hasHwEnc ? "OK" : "—"}</span>
        </div>
      </div>

      <div class="card ${state.settingsSub === "network" ? "" : "hidden"}">
        <div class="field" style="margin-bottom:14px;">
          <label>Twitch OAuth / auth-token (для саб-онли VOD и отключения рекламы)</label>
          <input type="password" class="input" id="cfgOauth" value="${esc(c.twitch_oauth_token || "")}" placeholder="Вставьте auth-token из cookies Twitch..." />
        </div>
        <div class="field" style="margin-bottom:14px;">
          <label>HTTP / SOCKS5 Прокси (опционально)</label>
          <input class="input" id="cfgProxy" value="${esc(c.proxy_url || "")}" placeholder="http://user:pass@host:port или socks5://127.0.0.1:1080" />
        </div>
        <div style="border-top:1px solid var(--border);padding-top:14px;margin-top:6px;">
          <div class="setting-row-title" style="margin-bottom:4px;">LLM AI для заголовков и описаний YouTube (опционально)</div>
          <div class="setting-row-desc" style="margin-bottom:12px;">Поддерживаются OpenAI, OpenRouter, Gemini OpenAI-compat и локальный Ollama (http://localhost:11434/v1)</div>
          <div class="form-grid-3" style="margin-bottom:12px;">
            <div class="field"><label>API Base URL</label><input class="input" id="cfgLlmUrl" value="${esc(c.llm_api_url || "")}" placeholder="https://api.openai.com/v1" /></div>
            <div class="field"><label>API Key</label><input type="password" class="input" id="cfgLlmKey" value="${esc(c.llm_api_key || "")}" placeholder="sk-..." /></div>
            <div class="field"><label>Модель</label><input class="input" id="cfgLlmModel" value="${esc(c.llm_model || "gpt-4o-mini")}" /></div>
          </div>
          <div class="field" style="margin-bottom:12px;">
            <label>AI Brand Voice — персональный стиль вашего канала (используется во всех AI-генераторах)</label>
            <input class="input" id="cfgBrandVoice" value="${esc(c.ai_brand_voice || "")}" placeholder="Дерзкий молодёжный стиль, короткие кликбейтные заголовки, эмодзи 🔥💀, аудитория CS2/GTA..." />
          </div>
          <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:6px;">
            <div class="setting-row-title" style="margin-bottom:6px;">Ключи автопостинга YouTube и VK (опционально)</div>
            <div class="form-grid-3">
              <div class="field"><label>YouTube client_secrets.json</label><input class="input" id="cfgYtSecrets" value="${esc(c.youtube_client_secrets || "")}" placeholder="Путь к OAuth JSON..." /></div>
              <div class="field"><label>VK Access Token</label><input type="password" class="input" id="cfgVkTok" value="${esc(c.vk_access_token || "")}" placeholder="vk1.a...." /></div>
              <div class="field"><label>VK Group ID (опц.)</label><input class="input" id="cfgVkGid" value="${esc(c.vk_group_id || "")}" placeholder="123456789" /></div>
            </div>
          </div>
        </div>
      </div>

      <div class="card ${state.settingsSub === "notify" ? "" : "hidden"}">
        <div class="form-grid-2" style="margin-bottom:14px;">
          <div class="field"><label>Telegram Bot Token (@BotFather)</label><input class="input" id="cfgTgTok" value="${esc(c.telegram_bot_token || "")}" placeholder="123456:AAH..." /></div>
          <div class="field"><label>Telegram Chat ID</label><input class="input" id="cfgTgChat" value="${esc(c.telegram_chat_id || "")}" placeholder="123456789" /></div>
        </div>
        <div class="setting-row" style="margin-bottom:12px;">
          <div>
            <div class="setting-row-title">Интерактивный Telegram-бот (удалённое управление с телефона)</div>
            <div class="setting-row-desc">Команды только от вашего Chat ID: /status, /rec &lt;ник&gt;, /stop &lt;ник&gt;, /stopall, /marker, /vod &lt;url&gt;, /snap &lt;ник&gt;</div>
          </div>
          <label class="switch-inline"><input type="checkbox" id="cfgTgBot" ${c.telegram_bot_enabled ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row" style="margin-bottom:12px;">
          <div>
            <div class="setting-row-title">Авто-публикация готовых Shorts и экспортов в Telegram</div>
            <div class="setting-row-desc">Автоматически отправлять отрендеренные в Студии ролики (&lt;50 МБ) прямо в ваш Telegram-чат</div>
          </div>
          <label class="switch-inline"><input type="checkbox" id="cfgAutoPubTg" ${c.auto_publish_telegram ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row" style="margin-bottom:12px;">
          <div>
            <div class="setting-row-title">Нативные системные уведомления ${isMac ? "macOS Notification Center" : "Windows"} со звуком</div>
            <div class="setting-row-desc">Показывать всплывающие баннеры ОС при старте эфира, авто-клипах и завершении блюра</div>
          </div>
          <label class="switch-inline"><input type="checkbox" id="cfgNativeOsNotif" ${c.native_os_notifications !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="field" style="margin-bottom:14px;">
          <label>Discord Webhook URL</label>
          <input class="input" id="cfgDcWh" value="${esc(c.discord_webhook_url || "")}" placeholder="https://discord.com/api/webhooks/..." />
        </div>
        <div class="input-row" style="flex-wrap:wrap;">
          <label class="switch-inline"><input type="checkbox" id="cfgNLive" ${c.notify_on_live ? "checked" : ""}/><span class="switch-track"></span><span>Старт эфира</span></label>
          <label class="switch-inline"><input type="checkbox" id="cfgNFin" ${c.notify_on_finish ? "checked" : ""}/><span class="switch-track"></span><span>Конец записи</span></label>
          <label class="switch-inline"><input type="checkbox" id="cfgNBlur" ${c.notify_on_blur ? "checked" : ""}/><span class="switch-track"></span><span>Блюр готов</span></label>
          <div style="flex:1"></div>
          <button class="btn btn-soft" id="btnTestNotify">${iconSvg("bell", 15)}<span>Отправить тест</span></button>
        </div>
      </div>

      <div class="card ${state.settingsSub === "system" ? "" : "hidden"}">
        <div class="setting-row">
          <div><div class="setting-row-title">Тема оформления</div><div class="setting-row-desc">Применяется мгновенно ко всему интерфейсу</div></div>
          <select class="input-select compact" id="cfgTheme">
            <option value="purple" ${(c.theme || "purple") === "purple" ? "selected" : ""}>Фиолетовая (Pro)</option>
            <option value="oled" ${c.theme === "oled" ? "selected" : ""}>OLED Чёрная</option>
            <option value="blue" ${c.theme === "blue" ? "selected" : ""}>Синяя</option>
            <option value="emerald" ${c.theme === "emerald" ? "selected" : ""}>Изумрудная</option>
            <option value="light" ${c.theme === "light" ? "selected" : ""}>Светлая</option>
          </select>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Компактные карточки каналов</div><div class="setting-row-desc">Удобно, если отслеживается 10+ стримеров</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgCompact" ${c.compact_cards ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">${isMac ? "Сворачивать в строку меню macOS" : "Сворачивать в системный трей"}</div><div class="setting-row-desc">При закрытии окна программа остаётся работать в фоне</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgTray" ${c.minimize_to_tray ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">${isMac ? "Автозапуск при входе в macOS (LaunchAgent)" : "Автозапуск вместе с Windows"}</div><div class="setting-row-desc">Тихий запуск свёрнутым в трей при старте системы</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgAutostart" ${c.start_with_windows ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Не давать ноутбуку засыпать во время записи и блюра</div><div class="setting-row-desc">Блокирует сон системы (macOS caffeinate / Windows), пока идёт запись, и снимает блокировку сразу после завершения</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgPreventSleep" ${c.prevent_sleep_while_recording !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Усыпить компьютер после завершения всех записей (ночной режим)</div><div class="setting-row-desc">Когда все активные стримы и блюр закончатся, компьютер автоматически уйдёт в сон через 30 сек</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgAutoSleepAfter" ${c.auto_sleep_after_finish ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Глобальная клавиша маркера хайлайта</div><div class="setting-row-desc">Работает на Windows и macOS даже поверх полноэкранных игр и браузера</div></div>
          <select class="input-select compact" id="cfgHotkey">
            ${Array.from({ length: 12 }, (_, i) => `F${i + 1}`).map((k) => `<option ${c.marker_hotkey === k ? "selected" : ""}>${k}</option>`).join("")}
          </select>
        </div>
        <div class="setting-row">
          <div><div class="setting-row-title">Перенос профиля между устройствами (Windows ↔ Mac)</div><div class="setting-row-desc">Экспорт/импорт всех каналов, сцен блюра и шаблонов баннеров в .zip</div></div>
          <div class="input-row">
            <button class="btn btn-subtle btn-sm" id="btnExportProf">${iconSvg("download", 14)}<span>Экспорт .zip</span></button>
            <button class="btn btn-soft btn-sm" id="btnImportProf">${iconSvg("folder", 14)}<span>Импорт .zip</span></button>
          </div>
        </div>
        <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:8px;">
          <div class="setting-row-title" style="margin-bottom:4px;">Облачная синхронизация профиля и сцен блюра (iCloud / Dropbox / GitHub Gist)</div>
          <div class="setting-row-desc" style="margin-bottom:10px;">Синхронизирует каналы и зоны блюра в 1 клик между вашим Mac и ПК</div>
          <div class="form-grid-2" style="margin-bottom:10px;">
            <div class="field">
              <label>Облачная папка (iCloud Drive / Dropbox / Google Drive)</label>
              <div class="input-row">
                <input class="input" id="cfgCloudPath" value="${esc(c.cloud_sync_path || "")}" placeholder="/Users/.../iCloud Drive/TwitchRecorder" />
                <button class="btn btn-subtle btn-sm" id="btnPickCloudDir">${iconSvg("folder", 14)}</button>
              </div>
            </div>
            <div class="field">
              <label>GitHub Gist Token (опционально, scope: gist)</label>
              <input type="password" class="input" id="cfgGistTok" value="${esc(c.cloud_gist_token || "")}" placeholder="ghp_..." />
            </div>
          </div>
          <div class="input-row">
            <button class="btn btn-primary btn-sm" id="btnCloudPush">${iconSvg("upload", 14)}<span>Выгрузить профиль в облако</span></button>
            <button class="btn btn-soft btn-sm" id="btnCloudPull">${iconSvg("download", 14)}<span>Загрузить профиль из облака</span></button>
            ${c.cloud_gist_id ? `<span class="mono" style="font-size:11px;color:var(--text-muted);">Gist ID: ${esc(c.cloud_gist_id)}</span>` : ""}
          </div>
        </div>
        <div style="border-top:1px solid var(--border);padding-top:14px;margin-top:14px;">
          <div class="setting-row">
            <div>
              <div class="setting-row-title">☁️ Авто-выгрузка готовых записей в облако (Яндекс.Диск WebDAV / Nextcloud / NAS)</div>
              <div class="setting-row-desc">После завершения записи автоматически копирует видео в облачную папку или на WebDAV-сервер</div>
            </div>
            <label class="switch-inline"><input type="checkbox" id="cfgCloudUpOn" ${c.cloud_upload_enabled ? "checked" : ""}/><span class="switch-track"></span></label>
          </div>
          <div class="form-grid-2" style="margin:8px 0;">
            <div class="field">
              <label>Локальная облачная папка (Яндекс.Диск / Google Drive / NAS)</label>
              <div class="input-row">
                <input class="input" id="cfgCloudUpDir" value="${esc(c.cloud_upload_dir || "")}" placeholder="/Volumes/NAS/Streams или C:\YandexDisk\Streams" />
                <button class="btn btn-subtle btn-sm" id="btnPickCloudUpDir">${iconSvg("folder", 14)}</button>
              </div>
            </div>
            <div class="field">
              <label>WebDAV URL (напр. https://webdav.yandex.ru/TwitchRec)</label>
              <input class="input" id="cfgWebdavUrl" value="${esc(c.cloud_webdav_url || "")}" placeholder="https://webdav.yandex.ru/TwitchRec" />
            </div>
          </div>
          <div class="form-grid-2" style="margin-bottom:8px;">
            <div class="field"><label>WebDAV Логин</label><input class="input" id="cfgWebdavUser" value="${esc(c.cloud_webdav_user || "")}" placeholder="user@yandex.ru" /></div>
            <div class="field"><label>WebDAV Пароль приложения</label><input type="password" class="input" id="cfgWebdavPass" value="${esc(c.cloud_webdav_pass || "")}" placeholder="Пароль приложения" /></div>
          </div>
          <label class="switch-inline"><input type="checkbox" id="cfgCloudDelLoc" ${c.cloud_delete_local_after ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12.5px;">Удалять локальный файл после успешной выгрузки в облако</span></label>
        </div>

        <div style="border-top:1px solid var(--border);padding-top:14px;margin-top:14px;">
          <div class="setting-row-title" style="margin-bottom:4px;">🎥 Интеграция с OBS Studio (WebSocket v5)</div>
          <div class="setting-row-desc" style="margin-bottom:10px;">Управляйте сценами и записью OBS прямо из Twitch Recorder Pro (OBS → Сервис → Настройки сервера WebSocket)</div>
          <div class="form-grid-3" style="margin-bottom:10px;">
            <div class="field"><label>Хост OBS</label><input class="input mono" id="cfgObsHost" value="${esc(c.obs_ws_host || "127.0.0.1")}" /></div>
            <div class="field"><label>Порт</label><input type="number" class="input mono" id="cfgObsPort" value="${c.obs_ws_port || 4455}" /></div>
            <div class="field"><label>Пароль WebSocket</label><input type="password" class="input" id="cfgObsPass" value="${esc(c.obs_ws_password || "")}" placeholder="Если задан в OBS" /></div>
          </div>
          <div class="input-row" style="flex-wrap:wrap;gap:8px;">
            <button class="btn btn-primary btn-sm" id="btnObsConnect">🔗 Проверить OBS и сцены</button>
            <button class="btn btn-soft btn-sm" id="btnObsStart">⏺ Старт записи OBS</button>
            <button class="btn btn-danger-ghost btn-sm" id="btnObsStop">⏹ Стоп записи OBS</button>
            ${(state.obsScenes || []).length ? `
              <select class="input-select compact" id="obsSceneSel">${state.obsScenes.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select>
              <button class="btn btn-subtle btn-sm" id="btnObsSwitchScene">Переключить сцену</button>
            ` : ""}
          </div>
        </div>

        <div style="border-top:1px solid var(--border);padding-top:14px;margin-top:14px;">
          <div class="setting-row">
            <div>
              <div class="setting-row-title">📱 Веб-пульт с телефона по Wi-Fi (Mobile Remote)</div>
              <div class="setting-row-desc">Запускайте/останавливайте запись, ставьте маркеры и сохраняйте DVR 60с с iPhone/Android в домашней сети</div>
            </div>
            <label class="switch-inline"><input type="checkbox" id="cfgMobOn" ${c.mobile_remote_enabled ? "checked" : ""}/><span class="switch-track"></span></label>
          </div>
          <div class="form-grid-2" style="margin-top:8px;">
            <div class="field"><label>Порт Wi-Fi пульта</label><input type="number" class="input mono" id="cfgMobPort" value="${c.mobile_remote_port || 8765}" min="1024" max="65535" /></div>
            <div class="field"><label>PIN-код доступа (опционально)</label><input class="input mono" id="cfgMobPin" value="${esc(c.mobile_remote_pin || "")}" placeholder="Напр. 1234" /></div>
          </div>
          <div id="mobRemoteUrl" style="margin-top:8px;font-size:12.5px;color:var(--accent-light);"></div>
        </div>
      </div>

      <div class="card ${state.settingsSub === "system" ? "" : "hidden"}">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;gap:10px;flex-wrap:wrap;">
          <div>
            <div class="setting-row-title">Облачные обновления (GitHub Releases)</div>
            <div class="setting-row-desc">Автоматическая загрузка с проверкой SHA-256 и установка на Windows и macOS</div>
          </div>
          <div class="input-row">
            <span class="diag-pill">Текущая версия: v${esc(d.app_version || "2.4.0")}</span>
            <button class="btn btn-soft btn-sm" id="btnOpenClSettings">${iconSvg("sparkles", 14)}<span>История изменений</span></button>
          </div>
        </div>
        <div class="field" style="margin-bottom:12px;">
          <label>Репозиторий GitHub (owner/repo или ссылка)</label>
          <input class="input" id="cfgGhRepo" value="${esc(c.github_repo || "")}" placeholder="g0dspeedbtw-crypto/twitch-rec" />
        </div>
        <div class="setting-row" style="margin-bottom:10px;">
          <div><div class="setting-row-title">Автоматически проверять обновления при запуске</div><div class="setting-row-desc">Покажет уведомление, как только на GitHub выйдет новый релиз</div></div>
          <label class="switch-inline"><input type="checkbox" id="cfgAutoUpd" ${c.auto_check_updates !== false ? "checked" : ""}/><span class="switch-track"></span></label>
        </div>
        <div id="settingsUpdateBox">${renderSettingsUpdateBoxHtml()}</div>
      </div>
    `;

    const collectSettings = () => ({
      output_dir: $("#cfgOutDir").value.trim(),
      container: $("#cfgCont").value,
      default_segment_minutes: Math.max(0, Number($("#cfgDefSeg")?.value) || 0),
      check_interval_sec: Math.max(10, Number($("#cfgInterval").value) || 30),
      vod_concurrent_fragments: Math.max(1, Number($("#cfgVodFrag").value) || 4),
      vod_rate_limit_mbps: Math.max(0, Number($("#cfgVodLimit")?.value) || 0),
      min_free_gb: Math.max(1, Number($("#cfgMinGb").value) || 15),
      smart_segment_blur: $("#cfgSmartBlur").checked,
      hard_disk_guard: $("#cfgDiskGuard").checked,
      encoder: $("#cfgEnc").value,
      video_quality: Number($("#cfgCq").value) || 23,
      max_concurrent_blurs: Math.max(1, Number($("#cfgMaxBlurs").value) || 1),
      normalize_audio_default: $("#cfgNormAudio").checked,
      low_priority_render: $("#cfgLowPrio").checked,
      game_mode_enabled: $("#cfgGameMode").checked,
      game_processes: $("#cfgGameProcs").value.trim(),
      night_render_enabled: $("#cfgNight").checked,
      night_render_start: $("#cfgNightS").value.trim() || "02:00",
      night_render_end: $("#cfgNightE").value.trim() || "08:00",
      auto_cleanup_old: $("#cfgAutoClean").checked,
      max_age_days: Math.max(1, Number($("#cfgMaxDays").value) || 30),
      chat_position: $("#cfgChatPos").value,
      chat_font_size: Math.max(16, Math.min(48, Number($("#cfgChatFont").value) || 28)),
      chat_hide_bots: $("#cfgChatBots").checked,
      whisper_model: $("#cfgWhisper").value,
      whisper_prompt: $("#cfgWhisperPrompt").value.trim(),
      banned_words: $("#cfgBanned").value.trim(),
      twitch_oauth_token: $("#cfgOauth").value.trim(),
      proxy_url: $("#cfgProxy").value.trim(),
      llm_api_url: $("#cfgLlmUrl").value.trim(),
      llm_api_key: $("#cfgLlmKey").value.trim(),
      llm_model: $("#cfgLlmModel").value.trim() || "gpt-4o-mini",
      ai_brand_voice: $("#cfgBrandVoice")?.value.trim() || "",
      youtube_client_secrets: $("#cfgYtSecrets")?.value.trim() || "",
      vk_access_token: $("#cfgVkTok")?.value.trim() || "",
      vk_group_id: $("#cfgVkGid")?.value.trim() || "",
      telegram_bot_token: $("#cfgTgTok").value.trim(),
      telegram_chat_id: $("#cfgTgChat").value.trim(),
      telegram_bot_enabled: $("#cfgTgBot").checked,
      auto_publish_telegram: $("#cfgAutoPubTg").checked,
      native_os_notifications: $("#cfgNativeOsNotif").checked,
      discord_webhook_url: $("#cfgDcWh").value.trim(),
      notify_on_live: $("#cfgNLive").checked,
      notify_on_finish: $("#cfgNFin").checked,
      notify_on_blur: $("#cfgNBlur").checked,
      theme: $("#cfgTheme").value,
      compact_cards: $("#cfgCompact").checked,
      minimize_to_tray: $("#cfgTray").checked,
      start_with_windows: $("#cfgAutostart").checked,
      prevent_sleep_while_recording: $("#cfgPreventSleep").checked,
      auto_sleep_after_finish: $("#cfgAutoSleepAfter").checked,
      marker_hotkey: $("#cfgHotkey").value,
      cloud_sync_path: $("#cfgCloudPath")?.value.trim() || "",
      cloud_gist_token: $("#cfgGistTok")?.value.trim() || "",
      cloud_upload_enabled: $("#cfgCloudUpOn")?.checked || false,
      cloud_upload_dir: $("#cfgCloudUpDir")?.value.trim() || "",
      cloud_webdav_url: $("#cfgWebdavUrl")?.value.trim() || "",
      cloud_webdav_user: $("#cfgWebdavUser")?.value.trim() || "",
      cloud_webdav_pass: $("#cfgWebdavPass")?.value || "",
      cloud_delete_local_after: $("#cfgCloudDelLoc")?.checked || false,
      obs_ws_host: $("#cfgObsHost")?.value.trim() || "127.0.0.1",
      obs_ws_port: Math.max(1, Number($("#cfgObsPort")?.value) || 4455),
      obs_ws_password: $("#cfgObsPass")?.value || "",
      mobile_remote_enabled: $("#cfgMobOn")?.checked || false,
      mobile_remote_port: Math.max(1024, Number($("#cfgMobPort")?.value) || 8765),
      mobile_remote_pin: $("#cfgMobPin")?.value.trim() || "",
      github_repo: $("#cfgGhRepo").value.trim(),
      auto_check_updates: $("#cfgAutoUpd").checked,
    });

    $("#cfgTheme").onchange = (e) => { applyTheme(e.target.value); };
    $("#btnExportProf").onclick = () => callBackend("exportProfileZip");
    $("#btnImportProf").onclick = () => callBackend("importProfileZip");
    $("#btnOpenClSettings")?.addEventListener("click", openChangelogModal);
    $("#btnPickCloudDir")?.addEventListener("click", async () => {
      const folder = await callBackend("pickFolder");
      if (folder && $("#cfgCloudPath")) {
        $("#cfgCloudPath").value = folder;
        state.settingsDraft.cloud_sync_path = folder;
      }
    });
    $("#btnPickCloudUpDir")?.addEventListener("click", async () => {
      const folder = await callBackend("pickFolder");
      if (folder && $("#cfgCloudUpDir")) {
        $("#cfgCloudUpDir").value = folder;
        state.settingsDraft.cloud_upload_dir = folder;
      }
    });
    const saveAndObs = async (action, arg = "") => {
      const data = collectSettings();
      state.settingsDraft = { ...data };
      await callBackend("saveSettings", JSON.stringify(data));
      await callBackend("obsAction", action, arg);
    };
    $("#btnObsConnect")?.addEventListener("click", () => saveAndObs("status"));
    $("#btnObsStart")?.addEventListener("click", () => saveAndObs("start_record"));
    $("#btnObsStop")?.addEventListener("click", () => saveAndObs("stop_record"));
    $("#btnObsSwitchScene")?.addEventListener("click", () => {
      const sc = $("#obsSceneSel")?.value || "";
      if (sc) saveAndObs("set_scene", sc);
    });
    callBackend("getMobileRemoteInfo").then((raw) => {
      try {
        const info = JSON.parse(raw || "{}");
        const box = $("#mobRemoteUrl");
        if (box) {
          box.innerHTML = info.running
            ? `✅ Пульт запущен! Откройте в браузере телефона: <strong class="mono">${esc(info.url)}</strong>${info.pin_required ? " (защищён PIN)" : ""}`
            : `Включите переключатель выше и нажмите «Сохранить настройки» — адрес появится здесь (${esc(info.url || "")})`;
        }
      } catch {}
    });
    $("#btnCloudPush")?.addEventListener("click", async () => {
      const data = collectSettings();
      state.settingsDraft = { ...data };
      await callBackend("saveSettings", JSON.stringify(data));
      await callBackend("syncCloudProfile", "push");
    });
    $("#btnCloudPull")?.addEventListener("click", async () => {
      const data = collectSettings();
      state.settingsDraft = { ...data };
      await callBackend("saveSettings", JSON.stringify(data));
      await callBackend("syncCloudProfile", "pull");
    });

    root.querySelectorAll("#setSubnav button").forEach((b) => {
      b.onclick = () => {
        state.settingsDraft = { ...state.settingsDraft, ...collectSettings() };
        state.settingsSub = b.dataset.sub;
        renderSettingsPage();
      };
    });
    $("#btnPickOutDir").onclick = async () => {
      const folder = await callBackend("pickFolder");
      if (folder) {
        $("#cfgOutDir").value = folder;
        state.settingsDraft.output_dir = folder;
      }
    };
    $("#btnSaveAllSettings").onclick = async () => {
      const data = collectSettings();
      state.settingsDraft = { ...data };
      applyTheme(data.theme);
      await callBackend("saveSettings", JSON.stringify(data));
    };
    $("#btnTestNotify").onclick = async () => {
      const data = collectSettings();
      state.settingsDraft = { ...data };
      await callBackend("saveSettings", JSON.stringify(data));
      await callBackend("sendTestNotification");
    };
    bindSettingsUpdateEvents(collectSettings);
  }

  function renderSettingsUpdateBoxHtml() {
    const u = state.update || {};
    const info = u.info || {};
    const st = u.status || "idle";
    const statusText = u.progress_text || (state.config.github_repo ? "Нажмите «Проверить обновления»" : "Укажите репозиторий GitHub и сохраните настройки");
    const isDl = st === "downloading";
    const isReady = st === "ready";
    const isAvail = st === "available";

    return `
      <div class="input-row" style="flex-wrap:wrap;gap:10px;">
        <span class="job-status ${isReady || st === "up_to_date" ? "done" : (st === "error" ? "error" : "")}" style="flex:1;">${esc(statusText)}</span>
        <button class="btn btn-subtle btn-sm" id="btnCheckUpd" ${st === "checking" || isDl ? "disabled" : ""}>${iconSvg("refresh", 14)}<span>Проверить</span></button>
        ${isAvail ? `<button class="btn btn-primary btn-sm" id="btnStartUpdDl">${iconSvg("download", 14)}<span>Скачать v${esc(info.latest_version || "")}</span></button>` : ""}
        ${isDl ? `<button class="btn btn-danger-ghost btn-sm" id="btnCancelUpdDl">${iconSvg("x", 14)}<span>Отмена</span></button>` : ""}
        ${isReady ? `<button class="btn btn-success btn-sm" id="btnApplyUpd">${iconSvg("sparkles", 14)}<span>${info.can_self_update ? "Установить и перезапустить" : "Открыть установщик"}</span></button>` : ""}
        ${info.html_url ? `<button class="btn btn-subtle btn-sm" id="btnOpenGhRelease" data-url="${esc(info.html_url)}">GitHub</button>` : ""}
      </div>
      ${isDl || isReady ? `<div class="prog-track" style="margin-top:10px;"><div class="prog-fill ${isReady ? "done" : ""}" style="width:${Math.min(100, Math.max(0, u.progress || 0))}%"></div></div>` : ""}
      ${(isAvail || isReady) && info.notes ? `<div class="update-notes">${esc(info.notes)}</div>` : ""}
    `;
  }

  function bindSettingsUpdateEvents(collectSettings) {
    $("#btnCheckUpd")?.addEventListener("click", async () => {
      if (typeof collectSettings === "function") {
        const data = collectSettings();
        state.settingsDraft = { ...data };
        await callBackend("saveSettings", JSON.stringify(data));
      }
      await callBackend("checkForUpdates", true);
    });
    $("#btnStartUpdDl")?.addEventListener("click", () => callBackend("startUpdateDownload"));
    $("#btnCancelUpdDl")?.addEventListener("click", () => callBackend("cancelUpdateDownload"));
    $("#btnApplyUpd")?.addEventListener("click", () => callBackend("applyDownloadedUpdate"));
    $("#btnOpenGhRelease")?.addEventListener("click", (e) => callBackend("openExternalUrl", e.currentTarget.dataset.url));
  }

  function renderUpdateBanner() {
    const banner = $("#updateBanner");
    if (!banner) return;
    const u = state.update || {};
    const st = u.status || "idle";
    const info = u.info || {};
    const visible = st === "available" || st === "downloading" || st === "ready";
    banner.classList.toggle("hidden", !visible);
    if (!visible) return;

    $("#updVer").textContent = `v${info.latest_version || ""}`;
    $("#updText").textContent = u.progress_text || `Доступна версия v${info.latest_version || ""}`;
    const progWrap = $("#updProgWrap");
    const progBar = $("#updProgBar");
    progWrap.classList.toggle("hidden", st !== "downloading" && st !== "ready");
    progBar.style.width = `${Math.min(100, Math.max(0, u.progress || 0))}%`;
    progBar.classList.toggle("done", st === "ready");

    const btn = $("#btnSidebarUpdate");
    if (st === "available") {
      btn.textContent = "Скачать обновление";
      btn.className = "btn btn-primary btn-sm btn-full";
      btn.disabled = false;
      btn.onclick = () => callBackend("startUpdateDownload");
    } else if (st === "downloading") {
      btn.textContent = "Отменить загрузку";
      btn.className = "btn btn-danger-ghost btn-sm btn-full";
      btn.disabled = false;
      btn.onclick = () => callBackend("cancelUpdateDownload");
    } else if (st === "ready") {
      btn.textContent = info.can_self_update ? "Установить и перезапустить" : "Открыть установщик";
      btn.className = "btn btn-success btn-sm btn-full";
      btn.disabled = false;
      btn.onclick = () => callBackend("applyDownloadedUpdate");
    }
  }

  // ==================== PAGE 7: LOGS ====================
  function renderLogsPage() {
    const root = $("#page-logs");
    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Журнал событий</div>
          <div class="page-sub">Логи записей, чата, FFmpeg NVENC, Whisper AI и уведомлений</div>
        </div>
        <div class="header-actions">
          <button class="btn btn-subtle" id="btnOpenLogFile">${iconSvg("folder", 15)}<span>Файл app.log</span></button>
          <button class="btn btn-subtle" id="btnCopyLogs">${iconSvg("copy", 15)}<span>Копировать</span></button>
          <button class="btn btn-danger-ghost" id="btnClearLogs">${iconSvg("trash", 15)}<span>Очистить</span></button>
        </div>
      </div>
      <div class="log-box" id="logConsole">${esc(state.logs.join("\n"))}</div>
    `;
    const box = $("#logConsole");
    box.scrollTop = box.scrollHeight;
    $("#btnOpenLogFile").onclick = () => callBackend("openLogFile");
    $("#btnCopyLogs").onclick = () => { navigator.clipboard?.writeText(state.logs.join("\n")); showToast("Скопировано", "Логи в буфере обмена", "info"); };
    $("#btnClearLogs").onclick = () => { state.logs = []; callBackend("clearLogs"); renderLogsPage(); };
  }

  // ==================== JOB CARD RENDERER ====================
  function renderJobCard(j) {
    return `
      <div class="job-card" data-job-id="${esc(j.id)}">
        <div class="job-top">
          <div>
            <div class="job-title">${esc(j.title)}</div>
            <div class="job-sub">${esc(j.subtitle)}</div>
          </div>
          <div class="input-row">
            <span class="job-status ${esc(j.state)}">${esc(j.status)}</span>
            ${j.state === "running" || j.state === "queued"
              ? `<button class="btn btn-danger-ghost btn-sm act-job-cancel" data-id="${esc(j.id)}">${iconSvg("x", 14)}<span>Отмена</span></button>`
              : (j.output_path ? `<button class="btn btn-subtle btn-sm act-job-reveal" data-path="${esc(j.output_path)}">${iconSvg("folder", 14)}<span>Показать</span></button>` : "")}
          </div>
        </div>
        <div class="prog-track"><div class="prog-fill ${esc(j.state)}" style="width:${Math.min(100, Math.max(0, j.progress || 0))}%"></div></div>
      </div>
    `;
  }

  function bindJobEvents(root) {
    root.querySelectorAll(".act-job-cancel").forEach((b) => { b.onclick = () => callBackend("cancelJob", b.dataset.id); });
    root.querySelectorAll(".act-job-reveal").forEach((b) => { b.onclick = () => callBackend("openInExplorer", b.dataset.path); });
  }

  // ==================== FULLSCREEN BLUR STUDIO MODAL ====================
  const bs = {
    mode: "manual",      // "channel" | "manual" | "vod" | "shorts"
    channel: null,
    presets: { "Основной": [] },
    activePreset: "Основной",
    regions: [],
    selected: -1,
    preview: true,
    mediaPath: "",
    mediaType: "none",   // "none" | "image" | "video"
    pendingSnapReq: null,
    drag: null,
  };

  function openBlurStudio(opts) {
    bs.mode = opts.mode || "manual";
    bs.channel = opts.channel || null;
    bs.mediaPath = "";
    bs.mediaType = "none";
    bs.preview = bs.mode !== "shorts";
    $("#bsPreviewToggle").checked = bs.preview;

    if (bs.mode === "channel" && bs.channel) {
      bs.presets = JSON.parse(JSON.stringify(bs.channel.blur_presets || { "Основной": [] }));
      bs.activePreset = bs.channel.active_preset || Object.keys(bs.presets)[0];
      bs.regions = bs.presets[bs.activePreset] || [];
      $("#bsTitle").textContent = `Студия блюра — ${bs.channel.username}`;
      $("#bsSnapLive").classList.remove("hidden");
      $("#bsSceneWrap").classList.remove("hidden");
      refreshBsSceneSelect();
    } else {
      bs.presets = { "Основной": JSON.parse(JSON.stringify(opts.regions || [])) };
      bs.activePreset = "Основной";
      bs.regions = bs.presets["Основной"];
      $("#bsTitle").textContent = bs.mode === "shorts" ? "Выбор зон для Shorts 9:16 (1 = Вебка, 2 = Геймплей)" : "Студия блюра видео";
      $("#bsSnapLive").classList.add("hidden");
      $("#bsSceneWrap").classList.add("hidden");
    }

    bs.selected = bs.regions.length ? 0 : -1;
    setBsMedia("none", "");
    $("#blurModal").classList.remove("hidden");
    renderBsZones();

    if (opts.videoPath) {
      loadBsLocalMedia(opts.videoPath);
    } else if (bs.mode === "channel" && bs.channel) {
      requestBsLiveSnap();
    } else if (bs.mode === "vod" && opts.vodUrl) {
      requestBsVodSnap(opts.vodUrl);
    }
  }

  function setBsMedia(type, path) {
    bs.mediaType = type;
    bs.mediaPath = path;
    const img = $("#bsImage"), vid = $("#bsVideo"), empty = $("#bsEmptyGrid"), bar = $("#bsPlayerBar");
    vid.pause();
    img.classList.add("hidden");
    vid.classList.add("hidden");
    bar.classList.add("hidden");
    empty.classList.remove("hidden");

    if (type === "image" && path) {
      img.src = `${mediaUrl(path)}&t=${Date.now()}`;
      img.classList.remove("hidden");
      empty.classList.add("hidden");
    } else if (type === "video" && path) {
      bs.seekOffset = 0;
      bs.duration = 0;
      vid.src = videoPlayUrl(path, 0);
      vid.classList.remove("hidden");
      bar.classList.remove("hidden");
      empty.classList.add("hidden");
      vid.load();
      vid.play().catch(() => {});
      callBackend("probeStudioVideo", path).then((raw) => {
        try {
          const m = JSON.parse(raw || "{}");
          if (m.ok && m.duration > 0) bs.duration = m.duration;
        } catch {}
      });
    }
  }

  function loadBsLocalMedia(path) {
    const ext = (path.split(".").pop() || "").toLowerCase();
    if (["png", "jpg", "jpeg", "webp", "bmp"].includes(ext)) setBsMedia("image", path);
    else setBsMedia("video", path);
  }

  async function requestBsLiveSnap() {
    if (!bs.channel) return;
    $("#bsSubtitle").textContent = "Захват живого кадра со стрима...";
    bs.pendingSnapReq = await callBackend("requestSnapshot", "live", bs.channel.key, 0);
  }

  async function requestBsVodSnap(vodUrl) {
    $("#bsSubtitle").textContent = "Извлечение кадра из VOD...";
    bs.pendingSnapReq = await callBackend("requestSnapshot", "vod", vodUrl, 30);
  }

  function refreshBsSceneSelect() {
    const sel = $("#bsSceneSelect");
    sel.innerHTML = Object.keys(bs.presets).map((n) => `<option value="${esc(n)}" ${n === bs.activePreset ? "selected" : ""}>${esc(n)}</option>`).join("");
  }

  function renderBsZones() {
    const overlay = $("#bsOverlay");
    overlay.innerHTML = "";
    bs.regions.forEach((r, idx) => {
      const box = document.createElement("div");
      box.className = `blur-box ${idx === bs.selected ? "selected" : ""} ${!bs.preview ? "no-preview" : ""}`;
      box.style.left = `${r.x * 100}%`;
      box.style.top = `${r.y * 100}%`;
      box.style.width = `${r.w * 100}%`;
      box.style.height = `${r.h * 100}%`;
      box.dataset.idx = idx;

      const fx = document.createElement("div");
      fx.className = `blur-box-fx fx-${r.mode || "blur"}`;
      fx.style.setProperty("--blur-px", `${Math.max(4, Math.round((r.strength || 25) * 0.65))}px`);
      if (r.feather > 0 && bs.preview && r.mode !== "image") {
        const f = Math.min(35, r.feather);
        fx.style.borderRadius = `${Math.round(f * 0.6)}px`;
        fx.style.opacity = "0.94";
      }
      if (r.mode === "image" && r.image_path) {
        const ext = (r.image_path.split(".").pop() || "").toLowerCase();
        if (["mp4", "webm", "mov"].includes(ext)) {
          fx.innerHTML = `<video src="${mediaUrl(r.image_path)}" autoplay loop muted playsinline style="width:100%;height:100%;object-fit:fill;pointer-events:none;"></video>`;
        } else {
          fx.innerHTML = `<img src="${mediaUrl(r.image_path)}" draggable="false"/>`;
        }
      }
      box.appendChild(fx);

      const tag = document.createElement("div");
      tag.className = "blur-box-tag";
      tag.textContent = `#${idx + 1} ${r.label || "Баннер"}${r.auto_detect ? (r.track_motion ? " [AI+Track]" : " [AI]") : ""}`;
      box.appendChild(tag);

      ["tl", "tr", "bl", "br"].forEach((h) => {
        const hd = document.createElement("div");
        hd.className = `blur-box-handle ${h}`;
        hd.dataset.handle = h;
        box.appendChild(hd);
      });
      overlay.appendChild(box);
    });

    $("#bsZoneCount").textContent = bs.regions.length;
    const zList = $("#bsZoneList");
    zList.innerHTML = bs.regions.length === 0
      ? `<div style="font-size:12px;color:var(--text-muted);padding:8px;">Нарисуйте прямоугольник мышкой на кадре</div>`
      : bs.regions.map((r, i) => `
          <div class="zone-item ${i === bs.selected ? "active" : ""}" data-idx="${i}">
            <span>#${i + 1} ${esc(r.label || "Баннер")}${r.auto_detect ? " [AI]" : ""}</span>
            <span class="mono" style="font-size:11px;color:var(--text-muted);">${Math.round(r.w * 100)}×${Math.round(r.h * 100)}%</span>
          </div>
        `).join("");
    zList.querySelectorAll(".zone-item").forEach((el) => {
      el.onclick = () => { bs.selected = Number(el.dataset.idx); renderBsZones(); };
    });

    syncBsInspector();
  }

  function syncBsInspector() {
    const r = bs.regions[bs.selected];
    const panel = $("#bsPropPanel");
    if (!r) { panel.style.opacity = "0.45"; panel.style.pointerEvents = "none"; return; }
    panel.style.opacity = "1"; panel.style.pointerEvents = "auto";

    $("#bsZoneLabel").value = r.label || "";
    $("#bsModeSeg").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.mode === (r.mode || "blur")));
    $("#bsLogoField").classList.toggle("hidden", r.mode !== "image");
    $("#bsLogoPath").value = r.image_path || "";
    if ($("#bsChromaKey")) $("#bsChromaKey").checked = !!r.chromakey;
    $("#bsStrengthField").classList.toggle("hidden", r.mode === "solid" || r.mode === "image");
    $("#bsFeatherField").classList.toggle("hidden", r.mode === "solid" || r.mode === "image");
    $("#bsStrength").value = r.strength ?? 25;
    $("#bsStrengthVal").textContent = r.strength ?? 25;
    $("#bsFeather").value = r.feather ?? 0;
    $("#bsFeatherVal").textContent = `${r.feather ?? 0} px`;
    $("#bsStartSec").value = r.start_sec != null ? r.start_sec : "";
    $("#bsEndSec").value = r.end_sec != null ? r.end_sec : "";
    $("#bsAutoDetect").checked = !!r.auto_detect;
    if ($("#bsTrackMotion")) $("#bsTrackMotion").checked = !!r.track_motion;
    $("#bsTplStatus").textContent = r.template_path ? `Образец: ${r.template_path.split(/[\\/]/).pop()}` : "Блюрит только когда баннер в кадре";
  }

  function initBlurStudioEvents() {
    const overlay = $("#bsOverlay");
    const vid = $("#bsVideo");

    overlay.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const rect = overlay.getBoundingClientRect();
      const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const ny = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));

      const handleEl = e.target.closest(".blur-box-handle");
      const boxEl = e.target.closest(".blur-box");
      if (handleEl && boxEl) {
        const idx = Number(boxEl.dataset.idx);
        bs.selected = idx;
        bs.drag = { type: "resize", handle: handleEl.dataset.handle, idx, startX: nx, startY: ny, orig: { ...bs.regions[idx] } };
        renderBsZones();
        return;
      }
      if (boxEl) {
        const idx = Number(boxEl.dataset.idx);
        bs.selected = idx;
        bs.drag = { type: "move", idx, startX: nx, startY: ny, orig: { ...bs.regions[idx] } };
        renderBsZones();
        return;
      }
      // Draw new zone
      const newR = { x: nx, y: ny, w: 0.01, h: 0.01, mode: "blur", strength: 25, feather: 0, label: `Баннер ${bs.regions.length + 1}`, auto_detect: false, template_path: "", image_path: "", start_sec: null, end_sec: null };
      bs.regions.push(newR);
      bs.selected = bs.regions.length - 1;
      bs.drag = { type: "draw", idx: bs.selected, startX: nx, startY: ny };
      renderBsZones();
    });

    window.addEventListener("mousemove", (e) => {
      if (!bs.drag) return;
      const rect = overlay.getBoundingClientRect();
      const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const ny = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
      const idx = bs.drag.idx;
      const r = bs.regions[idx];
      if (!r) return;

      if (bs.drag.type === "draw") {
        r.x = Math.min(bs.drag.startX, nx);
        r.y = Math.min(bs.drag.startY, ny);
        r.w = Math.max(0.015, Math.abs(nx - bs.drag.startX));
        r.h = Math.max(0.015, Math.abs(ny - bs.drag.startY));
      } else if (bs.drag.type === "move") {
        const o = bs.drag.orig;
        r.x = Math.max(0, Math.min(1 - o.w, o.x + (nx - bs.drag.startX)));
        r.y = Math.max(0, Math.min(1 - o.h, o.y + (ny - bs.drag.startY)));
      } else if (bs.drag.type === "resize") {
        const o = bs.drag.orig;
        let l = o.x, t = o.y, ri = o.x + o.w, b = o.y + o.h;
        if (bs.drag.handle.includes("l")) l = Math.max(0, Math.min(ri - 0.02, nx));
        if (bs.drag.handle.includes("r")) ri = Math.min(1, Math.max(l + 0.02, nx));
        if (bs.drag.handle.includes("t")) t = Math.max(0, Math.min(b - 0.02, ny));
        if (bs.drag.handle.includes("b")) b = Math.min(1, Math.max(t + 0.02, ny));
        r.x = l; r.y = t; r.w = ri - l; r.h = b - t;
      }

      // Fast in-place DOM update during drag (avoids rebuilding overlay & reloading images)
      const boxEl = overlay.querySelector(`.blur-box[data-idx="${idx}"]`);
      if (boxEl) {
        boxEl.style.left = `${r.x * 100}%`;
        boxEl.style.top = `${r.y * 100}%`;
        boxEl.style.width = `${r.w * 100}%`;
        boxEl.style.height = `${r.h * 100}%`;
      }
      const listItem = $("#bsZoneList")?.querySelector(`.zone-item[data-idx="${idx}"] .mono`);
      if (listItem) {
        listItem.textContent = `${Math.round(r.w * 100)}×${Math.round(r.h * 100)}%`;
      }
    });

    window.addEventListener("mouseup", () => {
      if (!bs.drag) return;
      if (bs.drag.type === "draw") {
        const r = bs.regions[bs.drag.idx];
        if (r && (r.w < 0.02 || r.h < 0.02)) {
          bs.regions.splice(bs.drag.idx, 1);
          bs.selected = bs.regions.length - 1;
          renderBsZones();
        }
      }
      bs.drag = null;
    });

    // Video player controls inside Blur Studio
    $("#bsPlayBtn").onclick = () => {
      if (vid.paused) { vid.play(); $("#bsPlayBtn").innerHTML = iconSvg("pause", 16); }
      else { vid.pause(); $("#bsPlayBtn").innerHTML = iconSvg("play", 16); }
    };
    vid.ontimeupdate = () => {
      const dur = bs.duration || (isFinite(vid.duration) ? vid.duration : 0);
      const cur = (bs.seekOffset || 0) + (vid.currentTime || 0);
      if (dur > 0 && document.activeElement !== $("#bsScrubber")) {
        $("#bsScrubber").value = Math.min(100, (cur / dur) * 100);
      }
      $("#bsTimeLbl").textContent = `${fmtDur(cur)} / ${fmtDur(dur)}`;
    };
    $("#bsScrubber").onchange = (e) => {
      const dur = bs.duration || (isFinite(vid.duration) ? vid.duration : 0);
      if (dur > 0 && bs.mediaPath) {
        const target = (Number(e.target.value) / 100) * dur;
        bs.seekOffset = target;
        vid.src = videoPlayUrl(bs.mediaPath, target);
        vid.load();
        vid.play().catch(() => {});
      }
    };

    // Inspector bindings
    $("#bsPreviewToggle").onchange = (e) => { bs.preview = e.target.checked; renderBsZones(); };
    $("#bsSnapLive").onclick = requestBsLiveSnap;
    $("#bsLoadFile").onclick = async () => {
      const p = await callBackend("pickFile", "Видео и фото (*.mp4 *.mkv *.ts *.png *.jpg *.jpeg *.webp)");
      if (p) loadBsLocalMedia(p);
    };
    $("#bsZoneLabel").oninput = (e) => {
      const r = bs.regions[bs.selected];
      if (!r) return;
      r.label = e.target.value;
      const text = `#${bs.selected + 1} ${r.label || "Баннер"}${r.auto_detect ? " [AI]" : ""}`;
      const tag = overlay.querySelector(`.blur-box[data-idx="${bs.selected}"] .blur-box-tag`);
      if (tag) tag.textContent = text;
      const itemSpan = $("#bsZoneList")?.querySelector(`.zone-item[data-idx="${bs.selected}"] span:first-child`);
      if (itemSpan) itemSpan.textContent = text;
    };
    $("#bsModeSeg").querySelectorAll("button").forEach((b) => {
      b.onclick = () => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].mode = b.dataset.mode; renderBsZones(); } };
    });
    $("#bsPickLogo").onclick = async () => {
      const p = await callBackend("pickFile", "Медиа и картинки (*.png *.jpg *.jpeg *.webp *.gif *.mp4 *.webm)");
      if (p && bs.regions[bs.selected]) { bs.regions[bs.selected].image_path = p; renderBsZones(); }
    };
    $("#bsChromaKey")?.addEventListener("change", (e) => {
      if (bs.regions[bs.selected]) bs.regions[bs.selected].chromakey = e.target.checked;
    });
    $("#bsStrength").oninput = (e) => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].strength = Number(e.target.value); renderBsZones(); } };
    $("#bsFeather").oninput = (e) => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].feather = Number(e.target.value); renderBsZones(); } };
    const syncStartSec = (e) => { if (bs.regions[bs.selected]) bs.regions[bs.selected].start_sec = parseSec(e.target.value); };
    const syncEndSec = (e) => { if (bs.regions[bs.selected]) bs.regions[bs.selected].end_sec = parseSec(e.target.value); };
    $("#bsStartSec").oninput = syncStartSec;
    $("#bsStartSec").onchange = syncStartSec;
    $("#bsEndSec").oninput = syncEndSec;
    $("#bsEndSec").onchange = syncEndSec;
    $("#bsAutoDetect").onchange = (e) => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].auto_detect = e.target.checked; renderBsZones(); } };
    $("#bsTrackMotion")?.addEventListener("change", (e) => {
      if (bs.regions[bs.selected]) { bs.regions[bs.selected].track_motion = e.target.checked; renderBsZones(); }
    });
    $("#bsSaveTpl").onclick = async () => {
      const r = bs.regions[bs.selected];
      if (!r || !bs.mediaPath) return showToast("Нужен кадр", "Сначала загрузите кадр со стрима или видео", "error");
      const currentTs = bs.mediaType === "video" ? ((bs.seekOffset || 0) + (Number(vid.currentTime) || 0)) : 0;
      const raw = await callBackend("captureBannerTemplate", bs.mediaPath, JSON.stringify(r), currentTs);
      const res = JSON.parse(raw || "{}");
      if (res.ok) {
        r.template_path = res.template_path;
        r.auto_detect = true;
        renderBsZones();
        showToast("Образец сохранён", "AI-детектор готов для этой зоны", "success");
      } else showToast("Ошибка", res.error || "Не удалось сохранить", "error");
    };
    $("#bsTestDetect")?.addEventListener("click", () => {
      const r = bs.regions[bs.selected];
      if (!r || bs.mediaType !== "video" || !bs.mediaPath) {
        return showToast("Тест детекции", "Откройте видеофайл и сохраните образец баннера", "info");
      }
      callBackend("testBannerDetect", bs.mediaPath, JSON.stringify(r));
    });
    $("#bsDeleteZone").onclick = () => {
      if (bs.selected >= 0) { bs.regions.splice(bs.selected, 1); bs.selected = Math.min(bs.selected, bs.regions.length - 1); renderBsZones(); }
    };
    $("#bsPrivacyFaces")?.addEventListener("click", () => {
      if (!bs.mediaPath) return showToast("AI Лица", "Сначала загрузите видео или кадр", "info");
      callBackend("runAiSuiteTask", "privacy_blur", JSON.stringify({ video_path: bs.mediaPath }));
      showToast("AI Приватность", "Ищем лица в кадре...", "info");
    });
    $("#bsClearZones").onclick = () => { bs.regions = []; bs.selected = -1; renderBsZones(); };

    // Scene preset switching
    $("#bsSceneSelect").onchange = (e) => {
      bs.presets[bs.activePreset] = bs.regions;
      bs.activePreset = e.target.value;
      bs.regions = bs.presets[bs.activePreset] || [];
      bs.selected = bs.regions.length ? 0 : -1;
      renderBsZones();
    };
    $("#bsAddScene").onclick = () => {
      const name = prompt("Название новой сцены (напр. Just Chatting или Игра):");
      if (!name || !name.trim()) return;
      bs.presets[bs.activePreset] = bs.regions;
      bs.activePreset = name.trim();
      if (!bs.presets[bs.activePreset]) bs.presets[bs.activePreset] = [];
      bs.regions = bs.presets[bs.activePreset];
      bs.selected = -1;
      refreshBsSceneSelect();
      renderBsZones();
    };
    $("#bsDelScene").onclick = () => {
      if (Object.keys(bs.presets).length <= 1) return showToast("Сцены", "Нельзя удалить единственную сцену", "info");
      delete bs.presets[bs.activePreset];
      bs.activePreset = Object.keys(bs.presets)[0];
      bs.regions = bs.presets[bs.activePreset];
      refreshBsSceneSelect();
      renderBsZones();
    };

    const closeModal = () => {
      vid.pause();
      vid.removeAttribute("src");
      vid.load();
      $("#blurModal").classList.add("hidden");
    };
    $("#bsCloseBtn").onclick = closeModal;
    $("#bsCancelBtn").onclick = closeModal;
    $("#bsSaveBtn").onclick = async () => {
      bs.presets[bs.activePreset] = bs.regions;
      if (bs.mode === "channel" && bs.channel) {
        const raw = await callBackend("saveChannelBlurPresets", bs.channel.key, JSON.stringify(bs.presets), bs.activePreset);
        if (raw) {
          try {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed.channels)) {
              state.channels = parsed.channels;
              if (state.page === "live") renderLivePage();
            }
          } catch {}
        }
      } else if (bs.mode === "manual") {
        state.manualBlurRegions = JSON.parse(JSON.stringify(bs.regions));
        renderBlurPage();
      } else if (bs.mode === "vod") {
        state.vodDraftRegions = JSON.parse(JSON.stringify(bs.regions));
        renderVodPage();
      } else if (bs.mode === "shorts") {
        if (bs.regions[0]) state.shortsCam = bs.regions[0];
        if (bs.regions[1]) state.shortsGame = bs.regions[1];
        showToast("Области Shorts сохранены", "Вебка и геймплей настроены", "success");
      }
      closeModal();
    };
  }

  // ==================== CHANNEL SETTINGS DRAWER ====================
  let drawerChKey = null;
  function openChannelDrawer(ch) {
    drawerChKey = ch.key;
    $("#chDrawerTitle").textContent = `Настройки канала — ${ch.username}`;
    const body = $("#chDrawerBody");
    const days = new Set(ch.schedule_days || [0, 1, 2, 3, 4, 5, 6]);
    const curSeg = Number(ch.segment_minutes || 0);
    body.innerHTML = `
      <div class="field">
        <label>Разбивка записи на части (по длительности)</label>
        <div class="split-pills" id="cdSegPills">
          ${SPLIT_PRESETS.map((p) => `<button type="button" class="split-pill ${curSeg === p.val ? "active" : ""}" data-min="${p.val}">${p.label}</button>`).join("")}
        </div>
        <div class="input-row" style="margin-top:8px;">
          <span style="font-size:12px;color:var(--text-muted);">Свой интервал (минут, 0 = одним файлом):</span>
          <input type="number" class="input mono" style="width:92px;" id="cdSeg" value="${curSeg}" min="0" max="720"/>
        </div>
      </div>

      <div>
        <div class="setting-row"><div><div class="setting-row-title">Авто-переподключение при обрыве</div><div class="setting-row-desc">Мгновенно возобновит запись при падении стрима</div></div><label class="switch-inline"><input type="checkbox" id="cdReconn" ${ch.auto_reconnect ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Параллельная запись чата Twitch</div><div class="setting-row-desc">Создаёт .chat.json и .chat.ass для хайлайтов</div></div><label class="switch-inline"><input type="checkbox" id="cdChat" ${ch.record_chat ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Пропуск рекламы Twitch</div><div class="setting-row-desc">Флаг --twitch-disable-ads</div></div><label class="switch-inline"><input type="checkbox" id="cdAds" ${ch.disable_ads ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Авто-сцена блюра по названию игры</div><div class="setting-row-desc">Автоматически переключает сцену блюра под текущую игру</div></div><label class="switch-inline"><input type="checkbox" id="cdAutoScene" ${ch.auto_scene_by_game !== false ? "checked" : ""}/><span class="switch-track"></span></label></div>
      </div>

      <details class="adv-group">
        <summary>Авто-клиппер, MP3-подкаст и доп. опции</summary>
        <div class="adv-body">
          <div class="setting-row"><div><div class="setting-row-title">Авто-склейка частей после обрыва</div><div class="setting-row-desc">Склеит куски в 1 файл (если разбивка выключена)</div></div><label class="switch-inline"><input type="checkbox" id="cdConcat" ${ch.auto_concat_parts ? "checked" : ""}/><span class="switch-track"></span></label></div>
          <div class="setting-row"><div><div class="setting-row-title">Сохранять исходник после блюра</div><div class="setting-row-desc">Не удалять _raw файл после постобработки</div></div><label class="switch-inline"><input type="checkbox" id="cdKeep" ${ch.keep_original ? "checked" : ""}/><span class="switch-track"></span></label></div>
          <div class="setting-row"><div><div class="setting-row-title">Отдельная MP3-дорожка (подкаст)</div><div class="setting-row-desc">Авто-извлечение чистого аудио .mp3 после записи</div></div><label class="switch-inline"><input type="checkbox" id="cdSepAudio" ${ch.separate_audio_track ? "checked" : ""}/><span class="switch-track"></span></label></div>
          <div class="setting-row">
            <div><div class="setting-row-title">Авто-клиппер 45с при взрыве чата</div><div class="setting-row-desc">Вырезает 45 сек в AutoClips/ при пороге сообщ./10с</div></div>
            <div class="input-row">
              <input type="number" class="input" style="width:70px;" id="cdClipThr" value="${ch.auto_clip_threshold || 18}" min="5" max="200"/>
              <label class="switch-inline"><input type="checkbox" id="cdAutoClip" ${ch.live_auto_clip ? "checked" : ""}/><span class="switch-track"></span></label>
            </div>
          </div>
          <div class="field" style="margin-top:8px;"><label>Ключевые слова в чате для авто-маркера</label><input class="input" id="cdTrigWords" value="${esc(ch.chat_trigger_words || "")}" placeholder="КЛАТЧ, ОРУ, ЭЙС, КЛИП"/></div>
        </div>
      </details>

      <details class="adv-group" ${ch.autopilot_enabled ? "open" : ""}>
        <summary>🤖 Автопилот после завершения эфира</summary>
        <div class="adv-body" style="padding-top:10px;display:flex;flex-direction:column;gap:8px;">
          <div class="setting-row">
            <div>
              <div class="setting-row-title">Включить Автопилот после стрима</div>
              <div class="setting-row-desc">Автоматически найдёт лучшие моменты, нарежет Shorts 9:16 и отправит в Telegram</div>
            </div>
            <label class="switch-inline"><input type="checkbox" id="cdAutoPilot" ${ch.autopilot_enabled ? "checked" : ""}/><span class="switch-track"></span></label>
          </div>
          <div class="form-grid-2">
            <div class="field"><label>Кол-во Shorts (1–10)</label><input type="number" class="input mono" id="cdApCount" value="${ch.autopilot_shorts_count || 3}" min="1" max="10" /></div>
            <div class="field" style="justify-content:flex-end;">
              <label class="switch-inline" style="margin-top:18px;"><input type="checkbox" id="cdApTg" ${ch.autopilot_send_telegram !== false ? "checked" : ""}/><span class="switch-track"></span><span style="font-size:12px;">Отправлять в Telegram</span></label>
            </div>
          </div>
        </div>
      </details>

      <details class="adv-group" ${ch.schedule_enabled || ch.category_filter || ch.title_filter ? "open" : ""}>
        <summary>Фильтры игр и Расписание записи</summary>
        <div class="adv-body" style="padding-top:10px;display:flex;flex-direction:column;gap:10px;">
          <div class="field"><label>Фильтр категорий / игр (пусто = все)</label><input class="input" id="cdCat" value="${esc(ch.category_filter || "")}" placeholder="Just Chatting, Counter-Strike 2"/></div>
          <div class="field"><label>Обязательные слова в названии стрима</label><input class="input" id="cdTitle" value="${esc(ch.title_filter || "")}" placeholder="турнир, марафон"/></div>
          <div>
            <div class="setting-row"><div><div class="setting-row-title">Запись по расписанию</div><div class="setting-row-desc">Автостарт только в выбранные дни и часы</div></div><label class="switch-inline"><input type="checkbox" id="cdSched" ${ch.schedule_enabled ? "checked" : ""}/><span class="switch-track"></span></label></div>
            <div class="input-row" style="margin:10px 0;">
              <input class="input mono" id="cdSStart" value="${esc(ch.schedule_start || "18:00")}"/><span>—</span><input class="input mono" id="cdSEnd" value="${esc(ch.schedule_end || "03:00")}"/>
            </div>
            <div class="day-pills" id="cdDays">
              ${DAYS.map((d, i) => `<button type="button" class="day-pill ${days.has(i) ? "active" : ""}" data-day="${i}">${d}</button>`).join("")}
            </div>
          </div>
        </div>
      </details>
    `;
    body.querySelectorAll("#cdSegPills .split-pill").forEach((b) => {
      b.onclick = () => {
        body.querySelectorAll("#cdSegPills .split-pill").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        $("#cdSeg").value = b.dataset.min;
      };
    });
    body.querySelectorAll(".day-pill").forEach((b) => { b.onclick = () => b.classList.toggle("active"); });
    $("#chDrawer").classList.remove("hidden");
  }

  function initDrawerEvents() {
    const close = () => $("#chDrawer").classList.add("hidden");
    $("#chDrawerClose").onclick = close;
    $("#chDrawerCancel").onclick = close;
    $("#chDrawerSave").onclick = async () => {
      if (!drawerChKey) return;
      const days = [...$("#cdDays").querySelectorAll(".day-pill.active")].map((b) => Number(b.dataset.day));
      const raw = await callBackend("updateChannelField", drawerChKey, JSON.stringify({
        auto_reconnect: $("#cdReconn").checked,
        auto_concat_parts: $("#cdConcat").checked,
        record_chat: $("#cdChat").checked,
        disable_ads: $("#cdAds").checked,
        keep_original: $("#cdKeep").checked,
        auto_scene_by_game: $("#cdAutoScene").checked,
        separate_audio_track: $("#cdSepAudio").checked,
        live_auto_clip: $("#cdAutoClip").checked,
        auto_clip_threshold: Math.max(5, Number($("#cdClipThr").value) || 18),
        chat_trigger_words: $("#cdTrigWords").value.trim(),
        segment_minutes: Number($("#cdSeg").value) || 0,
        category_filter: $("#cdCat").value.trim(),
        title_filter: $("#cdTitle").value.trim(),
        schedule_enabled: $("#cdSched").checked,
        schedule_start: $("#cdSStart").value.trim() || "00:00",
        schedule_end: $("#cdSEnd").value.trim() || "23:59",
        schedule_days: days,
        autopilot_enabled: $("#cdAutoPilot")?.checked || false,
        autopilot_shorts_count: Math.max(1, Math.min(10, Number($("#cdApCount")?.value) || 3)),
        autopilot_send_telegram: $("#cdApTg")?.checked ?? true,
      }));
      if (raw) {
        try {
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed.channels)) {
            state.channels = parsed.channels;
            if (state.page === "live") renderLivePage();
          }
        } catch {}
      }
      showToast("Параметры канала сохранены", "", "success");
      close();
    };
  }

  // ==================== LIGHTBOX PLAYER & CHANGELOG ====================
  let _lastLivePlayerCh = null;

  function openLiveStreamPlayer(ch) {
    _lastLivePlayerCh = ch;
    $("#playerTitle").textContent = `🔴 Прямой эфир (без рекламы) — ${ch.username} · ${ch.status?.game || ch.platform.toUpperCase()}`;
    const v = $("#lightboxVideo");
    const ov = $("#playerOverlay");
    const sp = $("#playerSpinner");
    const txt = $("#playerOverlayText");
    const retryBtn = $("#btnPlayerRetry");

    ov.classList.remove("hidden");
    sp.classList.remove("hidden");
    retryBtn.classList.add("hidden");
    txt.textContent = `Подключение к потоку ${ch.username} (H.264 без рекламы)...`;

    v.onplaying = () => { ov.classList.add("hidden"); };
    v.oncanplay = () => { ov.classList.add("hidden"); v.play().catch(() => {}); };
    v.onerror = () => {
      if ($("#playerModal").classList.contains("hidden")) return;
      sp.classList.add("hidden");
      txt.textContent = "Не удалось запустить поток. Возможно, канал оффлайн или Twitch временно ограничил доступ.";
      retryBtn.classList.remove("hidden");
    };
    retryBtn.onclick = () => { if (_lastLivePlayerCh) openLiveStreamPlayer(_lastLivePlayerCh); };

    const q = ch.quality && ch.quality !== "1440p60" ? ch.quality : "720p60";
    v.src = `/media/stream?url=${encodeURIComponent(ch.stream_url)}&q=${encodeURIComponent(q)}&t=${Date.now()}`;
    v.load();
    v.play().catch(() => {});

    const row = $("#playerMarkersRow");
    if (row) {
      row.innerHTML = `<span style="color:var(--success);font-weight:600;">⚡ Прямой поток через Streamlink H.264 (без рекламы Twitch)</span>`
        + `<span style="margin-left:auto;">Буферизация занимает 2–4 сек</span>`;
    }
    $("#playerModal").classList.remove("hidden");
  }

  let _lbPath = "";
  let _lbDur = 0;
  let _lbOffset = 0;

  function seekLightbox(sec) {
    if (!_lbPath) return;
    const v = $("#lightboxVideo");
    const clamped = Math.max(0, Math.min(_lbDur || 999999, Number(sec) || 0));
    _lbOffset = clamped;
    v.src = videoPlayUrl(_lbPath, clamped);
    v.load();
    v.play().catch(() => {});
  }

  function openLightboxPlayer(path, name, markers = []) {
    _lastLivePlayerCh = null;
    _lbPath = path;
    _lbOffset = 0;
    _lbDur = 0;
    $("#playerTitle").textContent = name || path;
    const v = $("#lightboxVideo");
    const ov = $("#playerOverlay");
    const sp = $("#playerSpinner");
    const txt = $("#playerOverlayText");
    const retryBtn = $("#btnPlayerRetry");
    ov?.classList.remove("hidden");
    sp?.classList.remove("hidden");
    retryBtn?.classList.add("hidden");
    if (txt) txt.textContent = "Загрузка видео...";

    v.onplaying = () => { ov?.classList.add("hidden"); };
    v.oncanplay = () => { ov?.classList.add("hidden"); v.play().catch(() => {}); };
    v.onerror = () => {
      if ($("#playerModal").classList.contains("hidden")) return;
      sp?.classList.add("hidden");
      if (txt) txt.textContent = "Не удалось открыть видео. Попробуйте повторить или открыть во внешнем плеере.";
      retryBtn?.classList.remove("hidden");
    };
    if (retryBtn) retryBtn.onclick = () => seekLightbox(_lbOffset);

    v.src = videoPlayUrl(path, 0);
    v.load();
    v.play().catch(() => {});

    const row = $("#playerMarkersRow");
    if (row) {
      const mkHtml = (markers || []).map((m) =>
        `<button class="btn btn-soft btn-sm act-seek-mk" data-t="${Number(m.t) || 0}">${fmtDur(m.t)} · ${esc(m.note || "Маркер")}</button>`
      ).join("");
      row.innerHTML = `
        <div class="input-row" style="width:100%;gap:10px;margin-bottom:${mkHtml ? "6px" : "0"};">
          <span class="mono" id="lbTimeLbl" style="font-size:12px;color:var(--text-primary);">00:00 / --:--</span>
          <input type="range" id="lbSeekSlider" min="0" max="100" step="0.2" value="0" style="flex:1;" />
          <button class="btn btn-subtle btn-sm" id="lbOpenExt" title="Открыть в системном плеере">Внешний плеер</button>
        </div>
        ${mkHtml ? `<div class="input-row" style="width:100%;flex-wrap:wrap;gap:6px;"><span style="font-weight:600;color:var(--text-primary);">Маркеры:</span> ${mkHtml}</div>` : ""}
      `;
      row.querySelectorAll(".act-seek-mk").forEach((b) => {
        b.onclick = () => seekLightbox(Number(b.dataset.t) || 0);
      });
      $("#lbOpenExt").onclick = () => callBackend("openExternalPlayer", path);
      const sl = $("#lbSeekSlider");
      sl.onchange = (e) => { if (_lbDur > 0) seekLightbox((Number(e.target.value) / 100) * _lbDur); };
      v.ontimeupdate = () => {
        const cur = _lbOffset + (v.currentTime || 0);
        const lbl = $("#lbTimeLbl");
        if (lbl) lbl.textContent = `${fmtDur(cur)} / ${fmtDur(_lbDur)}`;
        if (sl && _lbDur > 0 && document.activeElement !== sl) {
          sl.value = Math.min(100, (cur / _lbDur) * 100);
        }
      };
    }

    callBackend("probeStudioVideo", path).then((raw) => {
      try {
        const m = JSON.parse(raw || "{}");
        if (m.ok && m.duration > 0) {
          _lbDur = m.duration;
          const lbl = $("#lbTimeLbl");
          if (lbl) lbl.textContent = `${fmtDur(_lbOffset)} / ${fmtDur(_lbDur)}`;
        }
      } catch {}
    });

    $("#playerModal").classList.remove("hidden");
  }

  async function openChangelogModal() {
    const body = $("#changelogBody");
    body.innerHTML = `<div style="color:var(--text-muted);padding:16px;">Загрузка истории изменений...</div>`;
    $("#changelogModal").classList.remove("hidden");
    const md = (await callBackend("getChangelog")) || "";
    const sections = md.split(/^##\s+/m).slice(1);
    if (!sections.length) {
      body.innerHTML = `<div class="cl-version-card"><pre style="white-space:pre-wrap;font-family:inherit;font-size:13px;">${esc(md)}</pre></div>`;
      return;
    }
    const fmtInline = (line) => esc(line).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<kbd>$1</kbd>");
    body.innerHTML = sections.map((sec, idx) => {
      const lines = sec.trim().split("\n");
      const head = lines[0] || "";
      const [verPart, ...rest] = head.split("—");
      const ver = (verPart || "").trim();
      const title = rest.join("—").trim();
      let html = `<div class="cl-version-card ${idx === 0 ? "latest" : ""}"><div class="cl-ver-head"><span class="cl-ver-badge">${esc(ver)}</span><span class="cl-ver-title">${esc(title)}</span></div>`;
      let inList = false;
      for (let i = 1; i < lines.length; i++) {
        const l = lines[i].trim();
        if (!l || l === "---") continue;
        if (l.startsWith("### ")) {
          if (inList) { html += "</ul>"; inList = false; }
          html += `<div class="cl-sec-title">${esc(l.slice(4))}</div>`;
        } else if (l.startsWith("- ")) {
          if (!inList) { html += '<ul class="cl-list">'; inList = true; }
          html += `<li>${fmtInline(l.slice(2))}</li>`;
        }
      }
      if (inList) html += "</ul>";
      html += "</div>";
      return html;
    }).join("");
  }

  function openCmdPalette() {
    $("#cmdModal").classList.remove("hidden");
    const inp = $("#cmdInput");
    inp.value = "";
    renderCmdResults("");
    setTimeout(() => inp.focus(), 20);
  }

  function renderCmdResults(query) {
    const q = query.trim().toLowerCase();
    const cmds = [
      { label: "Открыть: Прямые эфиры", sub: "Страница", run: () => switchPage("live") },
      { label: "Открыть: Мультивью (2×2)", sub: "Страница", run: () => switchPage("multiview") },
      { label: "Открыть: VOD и Клипы", sub: "Страница", run: () => switchPage("vod") },
      { label: "Открыть: Библиотека записей", sub: "Страница", run: () => switchPage("library") },
      { label: "Открыть: Блюр и Очередь GPU", sub: "Страница", run: () => switchPage("blur") },
      { label: "Открыть: Монтаж и AI-студия", sub: "Страница", run: () => switchPage("studio") },
      { label: "Открыть: Автопостинг и Календарь", sub: "Страница", run: () => switchPage("publish") },
      { label: "Открыть: Аналитика и Графики", sub: "Страница", run: () => switchPage("analytics") },
      { label: "Открыть: Настройки", sub: "Страница", run: () => switchPage("settings") },
      { label: "Проверить статусы всех каналов", sub: "Действие", run: () => callBackend("checkChannels") },
      { label: "Записать всех онлайн", sub: "Действие", run: () => callBackend("recordAllOnline") },
      { label: "Остановить все записи", sub: "Действие", run: () => callBackend("stopAllRecordings") },
      { label: "Обработать очередь блюра сейчас", sub: "GPU", run: () => callBackend("processBlurQueueNow") },
      { label: "Проверить обновления (GitHub Releases)", sub: "Облако", run: () => callBackend("checkForUpdates", true) },
      { label: "Открыть папку записей", sub: "Проводник", run: () => callBackend("openInExplorer", "") },
      ...state.channels.map((ch) => ({
        label: `${ch.is_recording ? "Остановить" : "Записать"} канал ${ch.username}`,
        sub: ch.platform.toUpperCase(),
        run: () => callBackend(ch.is_recording ? "stopRecording" : "startRecording", ch.key),
      })),
    ].filter((c) => !q || c.label.toLowerCase().includes(q) || c.sub.toLowerCase().includes(q));

    const box = $("#cmdResults");
    box.innerHTML = cmds.slice(0, 12).map((c, i) => `
      <div class="cmd-item" data-idx="${i}"><span>${esc(c.label)}</span><span class="cmd-item-sub">${esc(c.sub)}</span></div>
    `).join("");
    box.querySelectorAll(".cmd-item").forEach((el) => {
      el.onclick = () => { $("#cmdModal").classList.add("hidden"); cmds[Number(el.dataset.idx)].run(); };
    });
  }

  function applyTheme(theme) {
    const t = theme || "purple";
    if (t === "purple") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = t;
  }

  // ==================== TELEMETRY SPARKLINE ====================
  function updateTelemetry(snap) {
    const val = snap.gpu_pct != null ? snap.gpu_pct : Math.round(snap.cpu_pct || 0);
    state.sparkHistory.push(Math.max(2, Math.min(100, val)));
    if (state.sparkHistory.length > 28) state.sparkHistory.shift();

    const prevGame = state.activeGame;
    state.activeGame = snap.active_game || "";
    if (prevGame !== state.activeGame && state.page === "blur") renderBlurPage();

    const W = 200, H = 34;
    const pts = state.sparkHistory.map((v, i) => `${(i / 27) * W},${H - (v / 100) * (H - 4) - 2}`);
    $("#sparkLine").setAttribute("d", `M ${pts.join(" L ")}`);
    $("#sparkArea").setAttribute("d", `M 0,${H} L ${pts.join(" L ")} L ${W},${H} Z`);

    $("#telCpu").textContent = `CPU ${Math.round(snap.cpu_pct || 0)}%`;
    $("#telGpu").textContent = snap.gpu_pct != null ? `GPU ${snap.gpu_pct}% · NVENC ${snap.nvenc_pct}%` : `RAM ${Math.round(snap.ram_pct || 0)}%`;
    const diskEl = $("#telDisk");
    diskEl.textContent = snap.sleep_inhibited
      ? `☕ Анти-сон · ${(snap.disk_free_gb || 0).toFixed(1)} ГБ`
      : (state.activeGame
        ? `🎮 Игра: ${state.activeGame} · ${(snap.disk_free_gb || 0).toFixed(0)} ГБ`
        : `Свободно: ${(snap.disk_free_gb || 0).toFixed(1)} ГБ`);
    const low = (snap.disk_free_gb || 0) < (state.config.min_free_gb || 15);
    diskEl.parentElement.classList.toggle("low", low);
    $("#telDiskBar").style.width = `${Math.min(100, Math.max(6, ((snap.disk_free_gb || 0) / 500) * 100))}%`;
  }

  function applyDiagnosticsUi() {
    const enc = state.diagnostics.active_encoder || "libx264";
    const encLabels = {
      h264_nvenc: "RTX NVENC готов",
      hevc_nvenc: "RTX HEVC NVENC",
      h264_videotoolbox: "Apple VideoToolbox",
      hevc_videotoolbox: "Apple HEVC VideoToolbox",
      h264_qsv: "Intel QSV готов",
      h264_amf: "AMD AMF готов",
    };
    const gpuLbl = $("#telGpuLabel");
    if (gpuLbl) gpuLbl.textContent = encLabels[enc] || `Кодек: ${enc}`;
    const kbd = $("#cmdShortcutKbd");
    if (kbd) kbd.textContent = state.diagnostics.platform === "darwin" ? "⌘ K" : "Ctrl K";
    const bv = $("#brandVerText");
    if (bv) bv.textContent = `v${state.diagnostics.app_version || "2.4.0"} · Что нового`;
  }

  // ==================== BACKEND EVENT DISPATCHER ====================
  let _lastEventRaw = "";
  let _lastEventTime = 0;

  function handleBackendEvent(raw) {
    const now = Date.now();
    if (raw === _lastEventRaw && now - _lastEventTime < 45) return;
    _lastEventRaw = raw;
    _lastEventTime = now;

    let ev;
    try { ev = JSON.parse(raw); } catch { return; }
    const { type, data } = ev;

    if (type === "channels") {
      state.channels = data || [];
      if (state.page === "live") renderLivePage();
      updateNavBadges();
    } else if (type === "preview_updated") {
      state.previewBust[data.key] = data.ts;
      const ch = state.channels.find((c) => c.key === data.key);
      if (ch) ch.has_preview = true;
      if (state.page === "live") renderLivePage();
    } else if (type === "rec_progress") {
      const ch = state.channels.find((c) => c.key === data.key);
      if (ch) ch.telemetry = data;
      const el = document.querySelector(`[data-tel-key="${CSS.escape(data.key)}"]`);
      if (el) el.textContent = `${data.state || "Запись"} · ${data.dur || "00:00:00"} · ${data.size || "0 КБ"} · ${data.speed || "1.0x"}`;
    } else if (type === "job") {
      state.jobs[data.id] = data;
      const card = document.querySelector(`[data-job-id="${CSS.escape(data.id)}"]`);
      if (card && data.state === "running") {
        const st = card.querySelector(".job-status");
        const bar = card.querySelector(".prog-fill");
        if (st) st.textContent = data.status;
        if (bar) bar.style.width = `${Math.min(100, Math.max(0, data.progress || 0))}%`;
      } else {
        if ((data.category === "vod" && state.page === "vod") || (data.category === "blur" && state.page === "blur") || (data.category === "studio" && state.page === "studio")) {
          renderCurrentPage();
        }
      }
      updateNavBadges();
    } else if (type === "library") {
      if (Array.isArray(data)) {
        state.library = data;
      } else {
        state.library = data?.items || [];
        state.libStats = data?.stats || null;
      }
      if (state.page === "library") renderLibraryGrid();
      else if (state.page === "studio" && !state.studioFile) renderStudioPage(true);
    } else if (type === "files_dropped") {
      // #49 Native OS Drag & Drop from Finder/Explorer
      const paths = data || [];
      if (!paths.length) return;
      const first = paths[0];
      if (!$("#blurModal").classList.contains("hidden")) {
        loadBsLocalMedia(first);
        showToast("Файл загружен в Студию блюра", first.split(/[\\/]/).pop(), "success");
      } else if (state.page === "studio") {
        loadStudioVideo(first);
        showToast("Видео открыто в Студии", first.split(/[\\/]/).pop(), "success");
      } else {
        state.manualBlurFile = first;
        switchPage("blur");
        showToast("Файл добавлен", paths.length > 1 ? `Выбрано ${paths.length} файлов (первый загружен)` : first.split(/[\\/]/).pop(), "success");
      }
    } else if (type === "banner_test") {
      const ivs = data?.intervals || [];
      const summary = ivs.length
        ? ivs.slice(0, 8).map((p) => `${fmtDur(p[0])}–${fmtDur(p[1])}`).join(", ") + (ivs.length > 8 ? ` (+${ivs.length - 8})` : "")
        : "Баннер в кадре не найден (попробуйте снизить порог или переснять образец)";
      showToast(`AI-детектор: ${data?.label || "Баннер"} (${ivs.length} интерв.)`, summary, ivs.length ? "success" : "info");
    } else if (type === "vod_chat_ready") {
      if (data?.ok && data.json_path) {
        state.studioChatFile = data.json_path;
        if (state.page === "studio") renderStudioPage();
        callBackend("analyzeHighlights", "chat", data.json_path);
      }
    } else if (type === "yt_meta") {
      if (data?.ok) {
        state.ytMeta = data;
        if (state.page === "studio") renderStudioPage();
        showToast("AI YouTube готов", "Заголовки, описание и таймкоды сгенерированы", "success");
      } else {
        showToast("AI YouTube", data?.error || "Ошибка генерации", "error");
      }
    } else if (type === "stream_digest") {
      state.streamDigest = data?.text || "";
      if (state.page === "studio") renderStudioPage();
    } else if (type === "ai_suite_result") {
      const t = data?.task;
      const d = data?.data || {};
      if (t === "viral_clips") { state.aiViralClips = d.clips || []; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "face_track" && d.ok && d.region) { state.shortsCam = d.region; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "speech_search") { state.speechSearchHits = d.hits || []; if (state.page === "library") renderLibrarySpeechHits(); }
      else if (t === "privacy_blur" && d.ok && Array.isArray(d.regions)) {
        d.regions.forEach((r) => bs.regions.push(r));
        bs.selected = bs.regions.length - 1;
        renderBsZones();
      }
      else if (t === "chapters") { state.aiChapters = d.text || ""; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "copilot") { state.aiCopilot = d; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "vision_thumb") { state.aiThumbIdeas = d.ideas || []; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "ban_shield") { state.aiBanHits = d.hits || []; if (state.page === "studio") { renderStudioInspector(); renderStudioTimelineUI(); } }
      else if (t === "quote_card" && d.ok) { state.aiQuotePath = d.path || ""; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "game_events") {
        state.highlights = { mode: "events", heatmap: state.highlights.heatmap || [], highlights: d.events || [] };
        if (state.page === "studio") { renderStudioInspector(); renderStudioTimelineUI(); }
      }
      else if (t === "best_frames") { state.aiBestFrames = d.frames || []; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "social_post") { state.aiSocialPost = d.text || ""; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "virality_critique") { state.aiCritique = d; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "chat_sentiment") { state.aiChatSentiment = d; if (state.page === "studio") renderStudioInspector(); }
      else if (t === "publish_package" && d.ok) {
        const pub = state.pub;
        pub.aiEngine = d.ai_engine || "";
        pub.titleVariants = d.titles || [];
        pub.title = pub.titleVariants[0] || pub.title;
        pub.description = d.description || pub.description;
        pub.hashtags = d.hashtags || pub.hashtags;
        pub.thumbHeadline = d.thumb_headline || pub.thumbHeadline;
        pub.thumbSubtitle = d.thumb_subtitle || pub.thumbSubtitle;
        const needCover = !!pub._autoGenCoverNext;
        pub._autoGenCoverNext = false;
        if (state.page === "publish") renderPublishPage();
        showToast("AI Пакет готов", `Движок: ${pub.aiEngine}`, "success");
        if (needCover && pub.videoPath) {
          callBackend("createThumbnail", JSON.stringify({
            video_path: pub.videoPath,
            timestamp_sec: parseSec(pub.thumbTs) ?? 15,
            headline: pub.thumbHeadline,
            subtitle: pub.thumbSubtitle,
            accent_color: pub.thumbAccent,
          })).then((raw) => {
            try {
              const res = JSON.parse(raw || "{}");
              if (res.ok && res.output_path) { pub.thumbPath = res.output_path; if (state.page === "publish") renderPublishPage(); }
            } catch {}
          });
        }
      }
    } else if (type === "local_ai_status") {
      state.localAi = data || null;
      if (state.page === "publish") renderPublishPage();
    } else if (type === "publish_result") {
      state.pub.results = data?.results || [];
      if (state.page === "publish") renderPublishPage();
    } else if (type === "scheduled_posts") {
      state.scheduledPosts = data || [];
      if (state.page === "publish") renderPublishPage();
    } else if (type === "obs_result") {
      if (data?.action === "status" && data.ok) {
        state.obsScenes = (data.data?.scenes || []).map((s) => s.sceneName || s);
        if (state.page === "settings") renderSettingsPage();
      }
    } else if (type === "catalog") {
      state.catalogLoading = false;
      state.catalogError = data.error || "";
      if (data.kind === "vods") state.catalogVods = data.items || [];
      else state.catalogClips = data.items || [];
      if (state.page === "vod") renderVodPage();
    } else if (type === "highlights") {
      state.highlights = data;
      if (state.page === "studio") renderStudioPage();
    } else if (type === "snapshot") {
      if (data.req_id === bs.pendingSnapReq) {
        if (data.ok && data.path) {
          setBsMedia("image", data.path);
          $("#bsSubtitle").textContent = data.message || "Кадр загружен";
        } else {
          $("#bsSubtitle").textContent = data.message || "Канал оффлайн — можно выделить область на сетке 16:9";
        }
      }
    } else if (type === "resources") {
      updateTelemetry(data);
    } else if (type === "diagnostics") {
      state.diagnostics = data || {};
      applyDiagnosticsUi();
      if (state.page === "settings") renderSettingsPage();
    } else if (type === "update") {
      state.update = data || {};
      renderUpdateBanner();
      const updBox = $("#settingsUpdateBox");
      if (updBox && state.page === "settings") {
        updBox.innerHTML = renderSettingsUpdateBoxHtml();
        bindSettingsUpdateEvents();
      }
    } else if (type === "config") {
      state.config = data;
      state.settingsDraft = null;
      applyTheme(data?.theme);
      if (state.page === "live") renderLivePage();
    } else if (type === "log") {
      state.logs.push(data.line);
      if (state.logs.length > 600) state.logs.shift();
      const box = $("#logConsole");
      if (box) { box.textContent += `\n${data.line}`; box.scrollTop = box.scrollHeight; }
    } else if (type === "toast") {
      showToast(data.title, data.message, data.level);
    }
  }

  // ==================== STARTUP ====================
  function initApp() {
    hydrateIcons(document);

    // Prevent accidental WebEngine navigation when dragging files onto the window
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());

    document.querySelectorAll(".nav-item").forEach((b) => { b.onclick = () => switchPage(b.dataset.page); });
    $("#btnOpenFolder").onclick = () => callBackend("openInExplorer", "");
    $("#btnOpenCmd").onclick = openCmdPalette;
    $("#btnOpenChangelog")?.addEventListener("click", openChangelogModal);
    $("#changelogClose")?.addEventListener("click", () => $("#changelogModal").classList.add("hidden"));
    $("#changelogModal")?.addEventListener("click", (e) => { if (e.target.id === "changelogModal") $("#changelogModal").classList.add("hidden"); });
    $("#cmdInput").oninput = (e) => renderCmdResults(e.target.value);
    $("#cmdModal").onclick = (e) => { if (e.target.id === "cmdModal") $("#cmdModal").classList.add("hidden"); };
    const closeLightboxPlayer = () => {
      const v = $("#lightboxVideo");
      v.onplaying = null;
      v.oncanplay = null;
      v.onerror = null;
      v.pause();
      v.removeAttribute("src");
      v.load();
      $("#playerOverlay")?.classList.add("hidden");
      $("#playerModal").classList.add("hidden");
    };
    $("#playerClose").onclick = closeLightboxPlayer;
    $("#playerModal").onclick = (e) => { if (e.target.id === "playerModal") closeLightboxPlayer(); };

    window.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdPalette(); return; }
      if (e.key === "Escape") {
        $("#cmdModal").classList.add("hidden");
        $("#chDrawer").classList.add("hidden");
        $("#changelogModal")?.classList.add("hidden");
        if (!$("#playerModal").classList.contains("hidden")) closeLightboxPlayer();
        return;
      }
      const inInput = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
      if (inInput) return;

      if ((e.key === "Delete" || e.key === "Backspace") && !$("#blurModal").classList.contains("hidden")) {
        if (bs.selected >= 0) { bs.regions.splice(bs.selected, 1); bs.selected = Math.min(bs.selected, bs.regions.length - 1); renderBsZones(); }
        return;
      }

      // #50 Pro Video Player Keyboard Shortcuts (Space, J/K/L, Left/Right, I/O)
      const activeVid = !$("#playerModal").classList.contains("hidden")
        ? $("#lightboxVideo")
        : (state.page === "studio" ? $("#studioMonitor") : null);
      if (!activeVid || !activeVid.src) return;

      const k = e.key.toLowerCase();
      const doSeek = (delta) => {
        if (activeVid.id === "studioMonitor") seekStudioMonitor(getStudioTime() + delta, !activeVid.paused);
        else if (activeVid.id === "lightboxVideo" && _lbPath) seekLightbox(_lbOffset + (activeVid.currentTime || 0) + delta);
      };
      if (e.code === "Space" || k === "k") {
        e.preventDefault();
        if (activeVid.paused) activeVid.play().catch(() => {}); else activeVid.pause();
      } else if (k === "j") {
        e.preventDefault();
        doSeek(-10);
      } else if (k === "l") {
        e.preventDefault();
        doSeek(10);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        doSeek(-5);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        doSeek(5);
      } else if (k === "i" && activeVid.id === "studioMonitor") {
        e.preventDefault();
        $("#tlBtnIn")?.click();
      } else if (k === "o" && activeVid.id === "studioMonitor") {
        e.preventDefault();
        $("#tlBtnOut")?.click();
      }
    });

    initBlurStudioEvents();
    initDrawerEvents();
    window.__trDispatch = handleBackendEvent;

    // Connect QWebChannel to Python backend
    if (typeof QWebChannel !== "undefined" && window.qt && window.qt.webChannelTransport) {
      new QWebChannel(window.qt.webChannelTransport, async (channel) => {
        state.backend = channel.objects.backend;
        state.backend.event.connect(handleBackendEvent);
        const raw = await callBackend("getBootstrap");
        const boot = JSON.parse(raw || "{}");
        state.config = boot.config || {};
        state.channels = boot.channels || [];
        state.logs = boot.logs || [];
        state.diagnostics = boot.diagnostics || {};
        state.exportPresets = boot.export_presets || {};
        state.update = boot.update || state.update;
        (boot.jobs || []).forEach((j) => { state.jobs[j.id] = j; });
        applyTheme(state.config.theme);
        applyDiagnosticsUi();
        renderUpdateBanner();
        renderCurrentPage();
        const refreshWorkspaces = async () => {
          try {
            const wsData = JSON.parse((await callBackend("getWorkspaces")) || "{}");
            const sel = $("#wsSelect");
            if (sel && Array.isArray(wsData.workspaces)) {
              sel.innerHTML = wsData.workspaces.map((w) => `<option value="${esc(w)}" ${w === wsData.active ? "selected" : ""}>${esc(w)}</option>`).join("");
            }
          } catch {}
        };
        const applyWorkspaceSwitch = async (name) => {
          if (!name) return;
          const ok = await callBackend("switchWorkspace", name);
          if (ok) {
            const b2 = JSON.parse((await callBackend("getBootstrap")) || "{}");
            state.config = b2.config || {};
            state.channels = b2.channels || [];
            state.settingsDraft = null;
            applyTheme(state.config.theme);
            await refreshWorkspaces();
            renderCurrentPage();
            if (state.channels.length > 0) callBackend("checkChannels");
          }
        };
        $("#wsSelect")?.addEventListener("change", (e) => applyWorkspaceSwitch(e.target.value));
        $("#btnAddWs")?.addEventListener("click", () => {
          const name = window.prompt("Название нового профиля проекта (например: Клиент1, Игры, Основной):", "");
          if (name && name.trim()) applyWorkspaceSwitch(name.trim());
        });
        await refreshWorkspaces();
        if (state.channels.length > 0) {
          callBackend("checkChannels");
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", initApp);
})();
