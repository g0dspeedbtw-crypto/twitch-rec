/* Twitch Recorder Pro — Studio Web UI Controller */
(function () {
  "use strict";

  const QUALITIES = ["best", "1080p60", "1080p", "720p60", "720p", "480p", "360p", "worst"];
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
    shortsCam: null,
    shortsGame: null,
    libSearch: "",
    libFilter: "all",
    libStats: null,
    multiCutSegments: [],
    ytMeta: null,
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
    state.page = page;
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
    document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
    if (page === "library") callBackend("scanLibrary");
    renderCurrentPage();
  }

  function renderCurrentPage() {
    if (state.page === "live") renderLivePage();
    else if (state.page === "vod") renderVodPage();
    else if (state.page === "library") renderLibraryPage();
    else if (state.page === "blur") renderBlurPage();
    else if (state.page === "studio") renderStudioPage();
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
          <label class="switch-inline" style="background:rgba(255,255,255,0.03);border:1px solid var(--border);padding:7px 12px;border-radius:10px;">
            <input type="checkbox" id="liveAutoMon" ${state.config.auto_monitor_enabled ? "checked" : ""}/>
            <span class="switch-track"></span>
            <span>Автомониторинг</span>
          </label>
          <button class="btn btn-subtle" id="btnCheckNow">${iconSvg("refresh", 15)}<span>Обновить</span></button>
          <button class="btn btn-success" id="btnRecAll">${iconSvg("record", 13)}<span>Записать всех</span></button>
          <button class="btn btn-danger" id="btnStopAll">${iconSvg("stop", 13)}<span>Стоп всем</span></button>
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

  function renderChannelCard(ch) {
    const st = ch.status || {};
    const live = !!st.is_live;
    const rec = !!ch.is_recording;
    const tel = ch.telemetry || {};
    const zonesCnt = (ch.blur_regions || []).length;
    const presets = Object.keys(ch.blur_presets || { "Основной": [] });
    const bust = state.previewBust[ch.key] || 1;
    const thumbHtml = live && ch.has_preview
      ? `<img src="/media/live/${encodeURIComponent(ch.key)}?t=${bust}" alt="" />`
      : iconSvg("radio", 24);

    return `
      <div class="channel-card ${rec ? "is-recording" : ""}" data-key="${esc(ch.key)}">
        <div class="ch-top">
          <div class="ch-thumb">
            ${thumbHtml}
            <span class="ch-status-badge ${rec ? "rec" : (live ? "live" : "off")}">
              ${rec ? `<span class="pulse-dot"></span>ЗАПИСЬ` : (live ? `<span class="pulse-dot"></span>В ЭФИРЕ` : "ОФФЛАЙН")}
            </span>
            ${live && st.viewers ? `<span class="ch-viewers-badge">${st.viewers.toLocaleString("ru-RU")} зрит.</span>` : ""}
          </div>
          <div class="ch-info">
            <div class="ch-name-row">
              <span class="ch-name">${esc(ch.username)}</span>
              <span class="plat-tag plat-${esc(ch.platform)}">${esc(ch.platform)}</span>
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
              <button class="btn-icon act-settings" title="Расписание, фильтры, чат, переподключение">${iconSvg("gear", 15)}</button>
              <button class="btn-icon danger act-del" title="Удалить канал">${iconSvg("trash", 15)}</button>
            </div>
          </div>
        </div>

        ${rec ? `
          <div class="ch-rec-strip">
            <span class="ch-rec-stats" data-tel-key="${esc(ch.key)}">${esc(tel.state || "Запись")} · ${esc(tel.dur || "00:00:00")} · ${esc(tel.size || "0 КБ")} · ${esc(tel.speed || "1.0x")}</span>
            <button class="btn btn-soft btn-sm act-marker" title="Поставить маркер хайлайта (также F9)">${iconSvg("bookmark", 13)}<span>Маркер</span></button>
          </div>
        ` : ""}

        <div class="ch-controls">
          <select class="input-select compact act-quality" title="Качество потока">
            ${QUALITIES.map((q) => `<option value="${q}" ${ch.quality === q ? "selected" : ""}>${q}</option>`).join("")}
          </select>
          <select class="input-select compact act-blurmode" title="Режим блюра">
            <option value="postprocess" ${ch.blur_mode === "postprocess" ? "selected" : ""}>Постобработка</option>
            <option value="realtime" ${ch.blur_mode === "realtime" ? "selected" : ""}>Реалтайм (GPU)</option>
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
      el.onclick = () => { state.studioFile = el.dataset.path; switchPage("studio"); };
    });
    wrap.querySelectorAll(".act-reveal").forEach((el) => {
      el.onclick = () => callBackend("openInExplorer", el.dataset.path);
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

      <div class="card add-bar" style="margin-bottom:18px;">
        <input class="input" id="libSearch" placeholder="Поиск по каналу или имени файла..." value="${esc(state.libSearch)}" />
        <select class="input-select" id="libFilter" style="width:200px;">
          <option value="all" ${state.libFilter === "all" ? "selected" : ""}>Все видео</option>
          <option value="blurred" ${state.libFilter === "blurred" ? "selected" : ""}>Только с блюром</option>
          <option value="raw" ${state.libFilter === "raw" ? "selected" : ""}>Только исходники</option>
          <option value="chat" ${state.libFilter === "chat" ? "selected" : ""}>Есть чат / маркеры</option>
        </select>
      </div>

      <div id="libGridWrap"></div>
    `;

    $("#btnLibRefresh").onclick = () => callBackend("scanLibrary");
    $("#btnLibFolder").onclick = () => callBackend("openInExplorer", "");
    $("#libSearch").oninput = (e) => { state.libSearch = e.target.value; renderLibraryGrid(); };
    $("#libFilter").onchange = (e) => { state.libFilter = e.target.value; renderLibraryGrid(); };

    renderLibraryGrid();
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

  // ==================== PAGE 5: STUDIO & AI ====================
  function renderStudioPage() {
    const root = $("#page-studio");
    const stJobs = Object.values(state.jobs).filter((j) => j.category === "studio").reverse();
    const chPresets = state.channels.filter((c) => (c.blur_regions || []).length > 0);
    const hasVid = !!state.studioFile;

    root.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Монтаж и AI-студия</div>
          <div class="page-sub">Интерактивный триммер, Shorts 9:16, тепловая карта чата/звука, Whisper субтитры, авто-цензура и экспорт</div>
        </div>
      </div>

      <div class="subnav" id="studioSubnav">
        <button data-sub="trim" class="${state.studioSub === "trim" ? "active" : ""}">Обрезка / Склейка / AFK</button>
        <button data-sub="shorts" class="${state.studioSub === "shorts" ? "active" : ""}">Shorts / TikTok 9:16</button>
        <button data-sub="highlights" class="${state.studioSub === "highlights" ? "active" : ""}">Хайлайты и Чат</button>
        <button data-sub="ai" class="${state.studioSub === "ai" ? "active" : ""}">AI Субтитры и Цензура</button>
        <button data-sub="export" class="${state.studioSub === "export" ? "active" : ""}">Экспорт и Звук</button>
      </div>

      <div class="card" style="margin-bottom:14px;">
        <div class="input-row">
          <input class="input" id="stFileInp" value="${esc(state.studioFile)}" placeholder="Выберите видеофайл для работы в студии..." />
          <button class="btn btn-subtle" id="btnStPick">${iconSvg("folder", 15)}<span>Выбрать видео</span></button>
        </div>
      </div>

      ${state.studioSub === "trim" ? `
        <div class="card">
          ${hasVid ? `<video class="studio-video-preview" id="trimVideo" controls src="${mediaUrl(state.studioFile)}"></video>` : ""}
          <div class="input-row" style="flex-wrap:wrap;margin-bottom:10px;">
            <div class="field" style="width:125px;"><label>Начало (I)</label><input class="input mono" id="trimStart" value="00:00:00" /></div>
            <div class="field" style="width:125px;"><label>Конец (O)</label><input class="input mono" id="trimEnd" value="00:01:00" /></div>
            ${hasVid ? `
              <button class="btn btn-subtle btn-sm" id="btnMarkIn" title="Клавиша I">От (I)</button>
              <button class="btn btn-subtle btn-sm" id="btnMarkOut" title="Клавиша O">До (O)</button>
            ` : ""}
            <button class="btn btn-soft btn-sm" id="btnAddCutSeg">${iconSvg("plus", 13)}<span>В нарезку</span></button>
            <label class="switch-inline"><input type="checkbox" id="trimAcc" checked/><span class="switch-track"></span><span>Точный кадр</span></label>
            <div style="flex:1"></div>
            <button class="btn btn-soft" id="btnRemoveAfk">${iconSvg("sparkles", 15)}<span>Удалить тишину/AFK</span></button>
            <button class="btn btn-primary" id="btnDoTrim">${iconSvg("scissors", 15)}<span>Обрезать</span></button>
          </div>
          ${hasVid ? `<div style="font-size:11.5px;color:var(--text-muted);margin-bottom:8px;">Хоткеи плеера: <kbd>Space</kbd> пауза · <kbd>I</kbd> начало · <kbd>O</kbd> конец · <kbd>J</kbd>/<kbd>L</kbd> ±10с · <kbd>←</kbd>/<kbd>→</kbd> ±5с</div>` : ""}
          ${state.multiCutSegments.length > 0 ? `
            <div style="border-top:1px solid var(--border);padding-top:10px;margin-bottom:12px;">
              <div class="input-row" style="justify-content:space-between;flex-wrap:wrap;">
                <span style="font-size:12.5px;font-weight:600;">Отрезки Multi-Cut нарезки (${state.multiCutSegments.length})</span>
                <div class="input-row">
                  <button class="btn btn-danger-ghost btn-sm" id="btnClearMultiCut">Очистить</button>
                  <button class="btn btn-success btn-sm" id="btnRunMultiCut">${iconSvg("film", 13)}<span>Собрать нарезку (${state.multiCutSegments.length})</span></button>
                </div>
              </div>
              <div class="multicut-list">
                ${state.multiCutSegments.map((seg, i) => `
                  <span class="multicut-chip">#${i + 1} ${fmtDur(seg[0])}–${fmtDur(seg[1])} <button data-rm-seg="${i}" title="Удалить">×</button></span>
                `).join("")}
              </div>
            </div>
          ` : ""}
          <div style="border-top:1px solid var(--border);padding-top:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
            <span style="color:var(--text-secondary);font-size:13px;">Склейка нескольких записей или клипов в один файл</span>
            <label class="switch-inline"><input type="checkbox" id="catNorm"/><span class="switch-track"></span><span>Выровнять в 1080p60</span></label>
            <div style="flex:1"></div>
            <button class="btn btn-subtle" id="btnDoConcat">${iconSvg("plus", 15)}<span>Выбрать 2+ файлов и склеить</span></button>
          </div>
        </div>
      ` : state.studioSub === "shorts" ? `
        <div class="card">
          <div class="form-grid-2" style="margin-bottom:14px;">
            <div class="field">
              <label>Компоновка 9:16 (1080×1920)</label>
              <select class="input-select" id="shortsMode">
                <option value="split">Вебка сверху + Геймплей снизу</option>
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
          </div>
          <div class="input-row">
            <button class="btn btn-soft" id="btnPickShortsZones">${iconSvg("crop", 15)}<span>Выделить Вебку (1) и Геймплей (2) на видео</span></button>
            <div style="flex:1"></div>
            <button class="btn btn-primary" id="btnRunShorts">${iconSvg("smartphone", 15)}<span>Создать Shorts 9:16</span></button>
          </div>
        </div>
      ` : state.studioSub === "highlights" ? `
        <div class="card">
          <div class="input-row" style="margin-bottom:10px;flex-wrap:wrap;">
            <input class="input" style="flex:1;" id="hlChatInp" value="${esc(state.studioChatFile)}" placeholder="Файл чата (.chat.json — выбирается для тепловой карты чата)..." />
            <button class="btn btn-subtle" id="btnPickChat">${iconSvg("folder", 15)}<span>Чат JSON</span></button>
            <button class="btn btn-primary" id="btnScanChat">${iconSvg("flame", 15)}<span>Анализ чата</span></button>
            <button class="btn btn-soft" id="btnScanAudio">${iconSvg("volume", 15)}<span>Громкие моменты</span></button>
            <button class="btn btn-subtle" id="btnBurnChat">${iconSvg("video", 15)}<span>Вшить чат</span></button>
          </div>
          <div class="input-row" style="margin-bottom:12px;flex-wrap:wrap;">
            <input class="input" style="flex:1;" id="hlVodUrl" placeholder="Скачать чат прошлого Twitch VOD (ссылка https://twitch.tv/videos/... или ID)..." />
            <button class="btn btn-subtle btn-sm" id="btnFetchVodChat">${iconSvg("download", 14)}<span>Скачать чат VOD</span></button>
            ${state.highlights.highlights.length > 0 ? `
              <button class="btn btn-subtle btn-sm" id="btnExportEdl">${iconSvg("save", 14)}<span>Экспорт .EDL</span></button>
              <button class="btn btn-success btn-sm" id="btnBatchCutHl">${iconSvg("scissors", 14)}<span>Нарезать все (${state.highlights.highlights.length})</span></button>
            ` : ""}
          </div>
          <div class="heatmap-box" id="heatmapBox">${renderHeatmapSvg()}</div>
          <div class="hl-list" style="margin-top:12px;">
            ${state.highlights.highlights.map((h) => `
              <div class="hl-row">
                <div><span class="mono" style="color:var(--accent-light);font-weight:700;">${fmtDur(h.timestamp_sec)}</span> &nbsp; ${esc(h.reason)}</div>
                <button class="btn btn-success btn-sm act-cut-hl" data-ts="${h.timestamp_sec}">${iconSvg("scissors", 13)}<span>Вырезать ±20с</span></button>
              </div>
            `).join("")}
          </div>
        </div>
      ` : state.studioSub === "ai" ? `
        <div class="card">
          <div class="form-grid-4" style="margin-bottom:14px;">
            <div class="field">
              <label>Модель Whisper</label>
              <select class="input-select" id="aiModel">
                ${["tiny", "base", "small", "medium"].map((m) => `<option ${state.config.whisper_model === m ? "selected" : ""}>${m}</option>`).join("")}
              </select>
            </div>
            <div class="field">
              <label>Язык</label>
              <select class="input-select" id="aiLang">
                <option value="ru">Русский (ru)</option><option value="en">English (en)</option><option value="auto">Авто</option>
              </select>
            </div>
            <div class="field"><label>Караоке-субтитры</label><label class="switch-inline" style="margin-top:6px;"><input type="checkbox" id="aiBurn" checked/><span class="switch-track"></span><span>Вшить (Shorts Pop)</span></label></div>
            <div class="field"><label>Цензура</label><label class="switch-inline" style="margin-top:6px;"><input type="checkbox" id="aiBeep" checked/><span class="switch-track"></span><span>Писк 1кГц</span></label></div>
          </div>
          <div class="form-grid-2" style="margin-bottom:12px;">
            <div class="field">
              <label>Запрещённые слова для авто-цензуры (через запятую)</label>
              <input class="input" id="aiBanned" value="${esc(state.config.banned_words || "")}" />
            </div>
            <div class="field">
              <label>Словарь Whisper (ники стримеров, игровой сленг)</label>
              <input class="input" id="aiPrompt" value="${esc(state.config.whisper_prompt || "")}" placeholder="skywhywalker, клатч, эйс, мираж..." />
            </div>
          </div>
          <div class="input-row" style="flex-wrap:wrap;">
            <button class="btn btn-primary" id="btnAiSubs">${iconSvg("sparkles", 15)}<span>Караоке-субтитры Shorts</span></button>
            <button class="btn btn-danger" id="btnAiCensor">${iconSvg("volume", 15)}<span>Запикать запрещённые слова</span></button>
            <div style="flex:1"></div>
            <button class="btn btn-soft" id="btnGenYtMeta">${iconSvg("film", 15)}<span>AI Заголовки и Таймкоды YouTube</span></button>
          </div>
          ${state.ytMeta ? `
            <div class="yt-meta-box">
              <div class="input-row" style="justify-content:space-between;">
                <span style="font-weight:700;font-size:13px;">YouTube SEO (${state.ytMeta.source === "llm" ? "LLM AI" : "Авто-шаблон"})</span>
                <button class="btn btn-subtle btn-sm" id="btnCopyYtMeta">${iconSvg("copy", 13)}<span>Копировать всё</span></button>
              </div>
              <div class="field"><label>Варианты заголовков</label><textarea readonly>${esc((state.ytMeta.titles || []).join("\n"))}</textarea></div>
              <div class="field"><label>Описание и таймкоды</label><textarea readonly>${esc(state.ytMeta.description || "")}</textarea></div>
              <div class="field"><label>Теги</label><input class="input" readonly value="${esc(state.ytMeta.tags || "")}" /></div>
            </div>
          ` : ""}
        </div>
      ` : `
        <div class="card">
          <div class="form-grid-2" style="margin-bottom:12px;">
            <div class="field">
              <label>Пресет экспорта и сжатия</label>
              <select class="input-select" id="expPreset">
                ${Object.entries(state.exportPresets).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join("")}
              </select>
            </div>
            <div class="field">
              <label>PNG Вотермарка канала (опционально, правый верхний угол)</label>
              <div class="input-row">
                <input class="input" id="expWmPath" value="${esc(state.config.watermark_path || "")}" placeholder="Без вотермарки" />
                <button class="btn btn-subtle" id="btnPickWm">${iconSvg("image", 15)}</button>
              </div>
            </div>
          </div>
          <div class="input-row" style="flex-wrap:wrap;">
            <label class="switch-inline"><input type="checkbox" id="expNorm" checked/><span class="switch-track"></span><span>Нормализация EBU R128</span></label>
            <label class="switch-inline"><input type="checkbox" id="expVoice"/><span class="switch-track"></span><span>Усиление голоса (Compressor + Highpass)</span></label>
            <div style="flex:1"></div>
            <button class="btn btn-primary" id="btnDoExport">${iconSvg("download", 15)}<span>Экспортировать</span></button>
          </div>
        </div>
      `}

      <div class="section-heading"><span>Задачи студии (${stJobs.length})</span></div>
      ${stJobs.length ? `<div class="jobs-list">${stJobs.map(renderJobCard).join("")}</div>` : ""}
    `;

    root.querySelectorAll("#studioSubnav button").forEach((b) => {
      b.onclick = () => { state.studioSub = b.dataset.sub; renderStudioPage(); };
    });
    $("#stFileInp").oninput = (e) => { state.studioFile = e.target.value; };
    $("#btnStPick").onclick = async () => {
      const p = await callBackend("pickFile", "Видео (*.mp4 *.mkv *.ts *.flv *.mov)");
      if (p) { state.studioFile = p; renderStudioPage(); }
    };

    // Trim / Multi-Cut / AFK / Concat
    $("#btnMarkIn")?.addEventListener("click", () => { const v = $("#trimVideo"); if (v) $("#trimStart").value = fmtDur(v.currentTime); });
    $("#btnMarkOut")?.addEventListener("click", () => { const v = $("#trimVideo"); if (v) $("#trimEnd").value = fmtDur(v.currentTime); });
    $("#btnAddCutSeg")?.addEventListener("click", () => {
      const s = parseSec($("#trimStart").value) ?? 0, e = parseSec($("#trimEnd").value) ?? 0;
      if (e <= s) return showToast("Multi-Cut", "Конец отрезка должен быть больше начала", "error");
      state.multiCutSegments.push([s, e]);
      renderStudioPage();
    });
    $("#btnClearMultiCut")?.addEventListener("click", () => { state.multiCutSegments = []; renderStudioPage(); });
    root.querySelectorAll("[data-rm-seg]").forEach((btn) => {
      btn.onclick = () => { state.multiCutSegments.splice(Number(btn.dataset.rmSeg), 1); renderStudioPage(); };
    });
    $("#btnRunMultiCut")?.addEventListener("click", () => {
      if (!state.studioFile || !state.multiCutSegments.length) return showToast("Multi-Cut", "Выберите видео и добавьте отрезки", "error");
      const out = appendFileSuffix(state.studioFile, `_montage_${state.multiCutSegments.length}cuts`, ".mp4");
      callBackend("runStudioAction", JSON.stringify({
        action: "multi_cut", title: `Нарезка (${state.multiCutSegments.length} отрезков)`, subtitle: state.studioFile.split(/[\\/]/).pop(),
        params: { input_path: state.studioFile, output_path: out, segments: state.multiCutSegments, accurate: $("#trimAcc").checked }
      }));
    });
    $("#btnDoTrim")?.addEventListener("click", () => {
      const s = parseSec($("#trimStart").value) ?? 0, e = parseSec($("#trimEnd").value) ?? 0;
      if (!state.studioFile || e <= s) return showToast("Обрезка", "Выберите видео и корректный интервал От–До", "error");
      const out = appendFileSuffix(state.studioFile, `_cut_${Math.floor(s)}-${Math.floor(e)}`);
      callBackend("runStudioAction", JSON.stringify({
        action: "trim", title: `Обрезка ${fmtDur(s)}–${fmtDur(e)}`, subtitle: state.studioFile.split(/[\\/]/).pop(),
        params: { input_path: state.studioFile, start_sec: s, end_sec: e, output_path: out, accurate: $("#trimAcc").checked }
      }));
    });
    $("#btnRemoveAfk")?.addEventListener("click", () => {
      if (!state.studioFile) return showToast("Выберите видео", "", "error");
      const out = appendFileSuffix(state.studioFile, "_no_afk");
      callBackend("runStudioAction", JSON.stringify({
        action: "remove_silence_afk", title: "Удаление тишины и AFK", subtitle: state.studioFile.split(/[\\/]/).pop(),
        params: { input_path: state.studioFile, output_path: out }
      }));
    });
    $("#btnDoConcat")?.addEventListener("click", async () => {
      const raw = await callBackend("pickVideoFiles");
      const files = JSON.parse(raw || "[]");
      if (files.length < 2) return;
      const out = appendFileSuffix(files[0], "_merged", ".mp4");
      callBackend("runStudioAction", JSON.stringify({
        action: "concat", title: `Склейка (${files.length} файлов)`, subtitle: out.split(/[\\/]/).pop(),
        params: { input_paths: files, output_path: out, normalize: $("#catNorm").checked }
      }));
    });

    // Shorts 9:16
    $("#btnPickShortsZones")?.addEventListener("click", () => {
      const init = [
        state.shortsCam || { x: 0.02, y: 0.55, w: 0.28, h: 0.35, mode: "blur", strength: 10, label: "1. Вебка (верх)" },
        state.shortsGame || { x: 0.22, y: 0.05, w: 0.56, h: 0.90, mode: "blur", strength: 10, label: "2. Геймплей (низ)" },
      ];
      openBlurStudio({ mode: "shorts", videoPath: state.studioFile, regions: init });
    });
    $("#btnRunShorts")?.addEventListener("click", () => {
      if (!state.studioFile) return showToast("Выберите видео", "", "error");
      const mode = $("#shortsMode").value;
      const out = appendFileSuffix(state.studioFile, "_shorts_9x16", ".mp4");
      callBackend("runStudioAction", JSON.stringify({
        action: "shorts", title: "Рендер Shorts 9:16", subtitle: "1080×1920",
        params: {
          input_path: state.studioFile, output_path: out,
          cam_region: mode === "split" ? (state.shortsCam || { x: 0.02, y: 0.55, w: 0.28, h: 0.35 }) : null,
          game_region: mode === "split" ? (state.shortsGame || { x: 0.22, y: 0.05, w: 0.56, h: 0.90 }) : null,
          blur_channel_key: $("#shortsBlurCh").value,
        }
      }));
    });

    // Highlights & Chat (#19, #25, #33)
    $("#btnPickChat")?.addEventListener("click", async () => {
      const p = await callBackend("pickFile", "Чат JSON (*.json)");
      if (p) { state.studioChatFile = p; renderStudioPage(); }
    });
    $("#btnScanChat")?.addEventListener("click", () => {
      const path = $("#hlChatInp").value.trim() || appendFileSuffix(state.studioFile, "", ".chat.json");
      state.studioChatFile = path;
      callBackend("analyzeHighlights", "chat", path);
      showToast("Анализ чата", "Строим тепловую карту активности...", "info");
    });
    $("#btnScanAudio")?.addEventListener("click", () => {
      if (!state.studioFile) return showToast("Выберите видео", "", "error");
      callBackend("analyzeHighlights", "audio", state.studioFile);
      showToast("Анализ звука", "Ищем громкие моменты...", "info");
    });
    $("#btnFetchVodChat")?.addEventListener("click", () => {
      const url = $("#hlVodUrl").value.trim();
      if (!url) return showToast("Чат VOD", "Вставьте ссылку на Twitch VOD", "error");
      callBackend("downloadVodChat", url, state.studioFile || "");
    });
    $("#btnExportEdl")?.addEventListener("click", () => {
      callBackend("exportHighlightsEdl", state.studioFile, JSON.stringify(state.highlights.highlights || []));
    });
    $("#btnBatchCutHl")?.addEventListener("click", () => {
      if (!state.studioFile) return showToast("Выберите видео", "", "error");
      const tsList = (state.highlights.highlights || []).map((h) => h.timestamp_sec);
      if (!tsList.length) return;
      callBackend("runStudioAction", JSON.stringify({
        action: "batch_highlights", title: `Пакет хайлайтов (${tsList.length} шт.)`, subtitle: state.studioFile.split(/[\\/]/).pop(),
        params: { input_path: state.studioFile, timestamps: tsList, pad_sec: 20 }
      }));
    });
    $("#btnBurnChat")?.addEventListener("click", () => {
      if (!state.studioFile) return showToast("Выберите видео", "", "error");
      const baseChat = ($("#hlChatInp")?.value.trim() || state.studioChatFile || state.studioFile);
      const assPath = baseChat.endsWith(".chat.json")
        ? baseChat.replace(/\.chat\.json$/, ".chat.ass")
        : appendFileSuffix(baseChat, "", ".chat.ass");
      const out = appendFileSuffix(state.studioFile, "_with_chat");
      callBackend("runStudioAction", JSON.stringify({
        action: "burn_chat", title: "Наложение чата", subtitle: state.studioFile.split(/[\\/]/).pop(),
        params: { input_path: state.studioFile, ass_path: assPath, output_path: out }
      }));
    });
    root.querySelectorAll(".act-cut-hl").forEach((b) => {
      b.onclick = () => {
        if (!state.studioFile) return showToast("Выберите видео", "", "error");
        const t = Number(b.dataset.ts);
        const s = Math.max(0, t - 20), e = t + 20;
        const out = appendFileSuffix(state.studioFile, `_hl_${Math.floor(t)}s`);
        callBackend("runStudioAction", JSON.stringify({
          action: "trim", title: `Хайлайт ${fmtDur(t)}`, subtitle: "±20 сек",
          params: { input_path: state.studioFile, start_sec: s, end_sec: e, output_path: out, accurate: false }
        }));
      };
    });

    // AI Whisper + YouTube SEO (#20, #26, #30)
    $("#btnAiSubs")?.addEventListener("click", () => {
      callBackend("runStudioAction", JSON.stringify({
        action: "subtitles", title: "Whisper Караоке-субтитры", subtitle: `Модель ${$("#aiModel").value}`,
        input_path: state.studioFile, model: $("#aiModel").value, language: $("#aiLang").value,
        burn_subtitles: $("#aiBurn").checked, shorts_style: true, whisper_prompt: $("#aiPrompt").value.trim(),
      }));
    });
    $("#btnAiCensor")?.addEventListener("click", () => {
      callBackend("runStudioAction", JSON.stringify({
        action: "censor", title: "AI Аудио-цензура", subtitle: `Модель ${$("#aiModel").value}`,
        input_path: state.studioFile, model: $("#aiModel").value, language: $("#aiLang").value,
        banned_words: $("#aiBanned").value, censor_beep: $("#aiBeep").checked, whisper_prompt: $("#aiPrompt").value.trim(),
      }));
    });
    $("#btnGenYtMeta")?.addEventListener("click", () => {
      callBackend("generateYouTubeMeta", state.studioFile);
    });
    $("#btnCopyYtMeta")?.addEventListener("click", () => {
      if (!state.ytMeta) return;
      const txt = `${(state.ytMeta.titles || []).join("\n")}\n\n${state.ytMeta.description || ""}\n\nТеги: ${state.ytMeta.tags || ""}`;
      navigator.clipboard?.writeText(txt);
      showToast("Скопировано", "Заголовки, описание и теги в буфере обмена", "success");
    });

    // Export (#23, #24)
    $("#btnPickWm")?.addEventListener("click", async () => {
      const p = await callBackend("pickFile", "Картинки PNG/WebP (*.png *.webp *.jpg)");
      if (p && $("#expWmPath")) $("#expWmPath").value = p;
    });
    $("#btnDoExport")?.addEventListener("click", () => {
      if (!state.studioFile) return showToast("Выберите видео", "", "error");
      const pid = $("#expPreset").value;
      const out = appendFileSuffix(state.studioFile, `_${pid}`, ".mp4");
      callBackend("runStudioAction", JSON.stringify({
        action: "export_preset", title: `Экспорт (${state.exportPresets[pid]?.label || pid})`, subtitle: state.studioFile.split(/[\\/]/).pop(),
        params: {
          input_path: state.studioFile, output_path: out, preset_id: pid,
          normalize_audio: $("#expNorm").checked,
          voice_enhance: $("#expVoice").checked,
          watermark_path: $("#expWmPath").value.trim(),
        }
      }));
    });

    bindJobEvents(root);
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
          <div class="field"><label>Проверка эфиров (сек)</label><input type="number" class="input" id="cfgInterval" value="${c.check_interval_sec || 30}" /></div>
          <div class="field"><label>Потоков VOD</label><input type="number" class="input" id="cfgVodFrag" value="${c.vod_concurrent_fragments || 4}" /></div>
          <div class="field"><label>Предупр. диска (&lt; ГБ)</label><input type="number" class="input" id="cfgMinGb" value="${c.min_free_gb || 15}" /></div>
        </div>
        <div class="form-grid-3" style="margin-bottom:14px;">
          <div class="field">
            <label>Видеокодек (GPU / CPU)</label>
            <select class="input-select" id="cfgEnc">
              <option value="auto" ${c.encoder === "auto" ? "selected" : ""}>Авто (${esc(d.active_encoder || "libx264")})</option>
              ${encs.map((e) => `<option value="${esc(e)}" ${c.encoder === e ? "selected" : ""}>${esc(e)}</option>`).join("")}
            </select>
          </div>
          <div class="field"><label>Качество CQ/CRF (23 оптимум)</label><input type="number" class="input" id="cfgCq" value="${c.video_quality || 23}" /></div>
          <div class="field"><label>Лимит одноврем. рендеров GPU</label><input type="number" class="input" id="cfgMaxBlurs" value="${c.max_concurrent_blurs || 1}" min="1" max="6" /></div>
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
          <div class="form-grid-3">
            <div class="field"><label>API Base URL</label><input class="input" id="cfgLlmUrl" value="${esc(c.llm_api_url || "")}" placeholder="https://api.openai.com/v1" /></div>
            <div class="field"><label>API Key</label><input type="password" class="input" id="cfgLlmKey" value="${esc(c.llm_api_key || "")}" placeholder="sk-..." /></div>
            <div class="field"><label>Модель</label><input class="input" id="cfgLlmModel" value="${esc(c.llm_model || "gpt-4o-mini")}" /></div>
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
          <div><div class="setting-row-title">Глобальная клавиша маркера хайлайта</div><div class="setting-row-desc">${isMac ? "Доступно только в Windows (в macOS используйте кнопку «Маркер» на карточке)" : "Работает даже поверх полноэкранных игр"}</div></div>
          <select class="input-select compact" id="cfgHotkey" ${isMac ? "disabled" : ""}>
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
      </div>

      <div class="card ${state.settingsSub === "system" ? "" : "hidden"}">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;gap:10px;flex-wrap:wrap;">
          <div>
            <div class="setting-row-title">Облачные обновления (GitHub Releases)</div>
            <div class="setting-row-desc">Автоматическая загрузка с проверкой SHA-256 и установка на Windows и macOS</div>
          </div>
          <span class="diag-pill">Текущая версия: v${esc(d.app_version || "2.2.0")}</span>
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
      check_interval_sec: Math.max(10, Number($("#cfgInterval").value) || 30),
      vod_concurrent_fragments: Math.max(1, Number($("#cfgVodFrag").value) || 4),
      min_free_gb: Math.max(1, Number($("#cfgMinGb").value) || 15),
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
      telegram_bot_token: $("#cfgTgTok").value.trim(),
      telegram_chat_id: $("#cfgTgChat").value.trim(),
      telegram_bot_enabled: $("#cfgTgBot").checked,
      discord_webhook_url: $("#cfgDcWh").value.trim(),
      notify_on_live: $("#cfgNLive").checked,
      notify_on_finish: $("#cfgNFin").checked,
      notify_on_blur: $("#cfgNBlur").checked,
      theme: $("#cfgTheme").value,
      compact_cards: $("#cfgCompact").checked,
      minimize_to_tray: $("#cfgTray").checked,
      start_with_windows: $("#cfgAutostart").checked,
      marker_hotkey: $("#cfgHotkey").value,
      github_repo: $("#cfgGhRepo").value.trim(),
      auto_check_updates: $("#cfgAutoUpd").checked,
    });

    $("#cfgTheme").onchange = (e) => { applyTheme(e.target.value); };
    $("#btnExportProf").onclick = () => callBackend("exportProfileZip");
    $("#btnImportProf").onclick = () => callBackend("importProfileZip");

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
      vid.src = mediaUrl(path);
      vid.classList.remove("hidden");
      bar.classList.remove("hidden");
      empty.classList.add("hidden");
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
        fx.innerHTML = `<img src="${mediaUrl(r.image_path)}" draggable="false"/>`;
      }
      box.appendChild(fx);

      const tag = document.createElement("div");
      tag.className = "blur-box-tag";
      tag.textContent = `#${idx + 1} ${r.label || "Баннер"}${r.auto_detect ? " [AI]" : ""}`;
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
    $("#bsStrengthField").classList.toggle("hidden", r.mode === "solid" || r.mode === "image");
    $("#bsFeatherField").classList.toggle("hidden", r.mode === "solid" || r.mode === "image");
    $("#bsStrength").value = r.strength ?? 25;
    $("#bsStrengthVal").textContent = r.strength ?? 25;
    $("#bsFeather").value = r.feather ?? 0;
    $("#bsFeatherVal").textContent = `${r.feather ?? 0} px`;
    $("#bsStartSec").value = r.start_sec != null ? r.start_sec : "";
    $("#bsEndSec").value = r.end_sec != null ? r.end_sec : "";
    $("#bsAutoDetect").checked = !!r.auto_detect;
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
      if (!vid.duration) return;
      $("#bsScrubber").value = (vid.currentTime / vid.duration) * 100;
      $("#bsTimeLbl").textContent = `${fmtDur(vid.currentTime)} / ${fmtDur(vid.duration)}`;
    };
    $("#bsScrubber").oninput = (e) => {
      if (vid.duration) vid.currentTime = (Number(e.target.value) / 100) * vid.duration;
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
      const p = await callBackend("pickFile", "Картинки (*.png *.jpg *.jpeg *.webp)");
      if (p && bs.regions[bs.selected]) { bs.regions[bs.selected].image_path = p; renderBsZones(); }
    };
    $("#bsStrength").oninput = (e) => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].strength = Number(e.target.value); renderBsZones(); } };
    $("#bsFeather").oninput = (e) => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].feather = Number(e.target.value); renderBsZones(); } };
    const syncStartSec = (e) => { if (bs.regions[bs.selected]) bs.regions[bs.selected].start_sec = parseSec(e.target.value); };
    const syncEndSec = (e) => { if (bs.regions[bs.selected]) bs.regions[bs.selected].end_sec = parseSec(e.target.value); };
    $("#bsStartSec").oninput = syncStartSec;
    $("#bsStartSec").onchange = syncStartSec;
    $("#bsEndSec").oninput = syncEndSec;
    $("#bsEndSec").onchange = syncEndSec;
    $("#bsAutoDetect").onchange = (e) => { if (bs.regions[bs.selected]) { bs.regions[bs.selected].auto_detect = e.target.checked; renderBsZones(); } };
    $("#bsSaveTpl").onclick = async () => {
      const r = bs.regions[bs.selected];
      if (!r || !bs.mediaPath) return showToast("Нужен кадр", "Сначала загрузите кадр со стрима или видео", "error");
      const currentTs = bs.mediaType === "video" ? (Number(vid.currentTime) || 0) : 0;
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

    const closeModal = () => { vid.pause(); $("#blurModal").classList.add("hidden"); };
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
    body.innerHTML = `
      <div>
        <div class="setting-row"><div><div class="setting-row-title">Авто-переподключение при обрыве</div><div class="setting-row-desc">Мгновенно возобновит запись при падении стрима</div></div><label class="switch-inline"><input type="checkbox" id="cdReconn" ${ch.auto_reconnect ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Авто-склейка частей после обрыва</div><div class="setting-row-desc">Склеит все куски стрима в один целый файл</div></div><label class="switch-inline"><input type="checkbox" id="cdConcat" ${ch.auto_concat_parts ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Параллельная запись чата Twitch</div><div class="setting-row-desc">Создаёт .chat.json и .chat.ass для хайлайтов</div></div><label class="switch-inline"><input type="checkbox" id="cdChat" ${ch.record_chat ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Пропуск рекламы Twitch</div><div class="setting-row-desc">Флаг --twitch-disable-ads</div></div><label class="switch-inline"><input type="checkbox" id="cdAds" ${ch.disable_ads ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Сохранять исходник после блюра</div><div class="setting-row-desc">Не удалять _raw файл после постобработки</div></div><label class="switch-inline"><input type="checkbox" id="cdKeep" ${ch.keep_original ? "checked" : ""}/><span class="switch-track"></span></label></div>
        <div class="setting-row"><div><div class="setting-row-title">Разбивка по частям (минут)</div><div class="setting-row-desc">0 = одним непрерывным файлом</div></div><input type="number" class="input" style="width:95px;" id="cdSeg" value="${ch.segment_minutes || 0}" min="0" max="720"/></div>
      </div>
      <div class="field"><label>Фильтр категорий / игр (через запятую, пусто = все)</label><input class="input" id="cdCat" value="${esc(ch.category_filter || "")}" placeholder="Just Chatting, Counter-Strike 2"/></div>
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
    `;
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
        segment_minutes: Number($("#cdSeg").value) || 0,
        category_filter: $("#cdCat").value.trim(),
        title_filter: $("#cdTitle").value.trim(),
        schedule_enabled: $("#cdSched").checked,
        schedule_start: $("#cdSStart").value.trim() || "00:00",
        schedule_end: $("#cdSEnd").value.trim() || "23:59",
        schedule_days: days,
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

  // ==================== LIGHTBOX PLAYER & COMMAND PALETTE ====================
  function openLightboxPlayer(path, name, markers = []) {
    $("#playerTitle").textContent = name || path;
    const v = $("#lightboxVideo");
    v.src = mediaUrl(path);
    const row = $("#playerMarkersRow");
    if (row) {
      const mkHtml = (markers || []).map((m) =>
        `<button class="btn btn-soft btn-sm act-seek-mk" data-t="${Number(m.t) || 0}">${fmtDur(m.t)} · ${esc(m.note || "Маркер")}</button>`
      ).join("");
      row.innerHTML = (mkHtml ? `<span style="font-weight:600;color:var(--text-primary);">Маркеры:</span> ${mkHtml}` : "")
        + `<span style="margin-left:auto;">Хоткеи: <kbd>Space</kbd> пауза · <kbd>J</kbd>/<kbd>L</kbd> ±10с · <kbd>←</kbd>/<kbd>→</kbd> ±5с</span>`;
      row.querySelectorAll(".act-seek-mk").forEach((b) => {
        b.onclick = () => { v.currentTime = Number(b.dataset.t) || 0; v.play(); };
      });
    }
    $("#playerModal").classList.remove("hidden");
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
      { label: "Открыть: VOD и Клипы", sub: "Страница", run: () => switchPage("vod") },
      { label: "Открыть: Библиотека записей", sub: "Страница", run: () => switchPage("library") },
      { label: "Открыть: Блюр и Очередь GPU", sub: "Страница", run: () => switchPage("blur") },
      { label: "Открыть: Монтаж и AI-студия", sub: "Страница", run: () => switchPage("studio") },
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
    diskEl.textContent = state.activeGame
      ? `🎮 Игра: ${state.activeGame} · ${(snap.disk_free_gb || 0).toFixed(0)} ГБ`
      : `Свободно: ${(snap.disk_free_gb || 0).toFixed(1)} ГБ`;
    const low = (snap.disk_free_gb || 0) < (state.config.min_free_gb || 15);
    diskEl.parentElement.classList.toggle("low", low);
    $("#telDiskBar").style.width = `${Math.min(100, Math.max(6, ((snap.disk_free_gb || 0) / 500) * 100))}%`;
  }

  function applyDiagnosticsUi() {
    const enc = state.diagnostics.active_encoder || "libx264";
    const encLabels = {
      h264_nvenc: "RTX NVENC готов",
      h264_videotoolbox: "Apple VideoToolbox",
      h264_qsv: "Intel QSV готов",
      h264_amf: "AMD AMF готов",
    };
    const gpuLbl = $("#telGpuLabel");
    if (gpuLbl) gpuLbl.textContent = encLabels[enc] || `Кодек: ${enc}`;
    const kbd = $("#cmdShortcutKbd");
    if (kbd) kbd.textContent = state.diagnostics.platform === "darwin" ? "⌘ K" : "Ctrl K";
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
    } else if (type === "files_dropped") {
      // #49 Native OS Drag & Drop from Finder/Explorer
      const paths = data || [];
      if (!paths.length) return;
      const first = paths[0];
      if (!$("#blurModal").classList.contains("hidden")) {
        loadBsLocalMedia(first);
        showToast("Файл загружен в Студию блюра", first.split(/[\\/]/).pop(), "success");
      } else if (state.page === "studio") {
        state.studioFile = first;
        renderStudioPage();
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
    $("#cmdInput").oninput = (e) => renderCmdResults(e.target.value);
    $("#cmdModal").onclick = (e) => { if (e.target.id === "cmdModal") $("#cmdModal").classList.add("hidden"); };
    $("#playerClose").onclick = () => { $("#lightboxVideo").pause(); $("#playerModal").classList.add("hidden"); };
    $("#playerModal").onclick = (e) => { if (e.target.id === "playerModal") { $("#lightboxVideo").pause(); $("#playerModal").classList.add("hidden"); } };

    window.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdPalette(); return; }
      if (e.key === "Escape") {
        $("#cmdModal").classList.add("hidden");
        $("#chDrawer").classList.add("hidden");
        if (!$("#playerModal").classList.contains("hidden")) { $("#lightboxVideo").pause(); $("#playerModal").classList.add("hidden"); }
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
        : (state.page === "studio" && state.studioSub === "trim" ? $("#trimVideo") : null);
      if (!activeVid || !activeVid.src) return;

      const k = e.key.toLowerCase();
      if (e.code === "Space" || k === "k") {
        e.preventDefault();
        if (activeVid.paused) activeVid.play(); else activeVid.pause();
      } else if (k === "j") {
        e.preventDefault();
        activeVid.currentTime = Math.max(0, activeVid.currentTime - 10);
      } else if (k === "l") {
        e.preventDefault();
        activeVid.currentTime = Math.min(activeVid.duration || 999999, activeVid.currentTime + 10);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        activeVid.currentTime = Math.max(0, activeVid.currentTime - 5);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        activeVid.currentTime = Math.min(activeVid.duration || 999999, activeVid.currentTime + 5);
      } else if (k === "i" && activeVid.id === "trimVideo") {
        e.preventDefault();
        if ($("#trimStart")) $("#trimStart").value = fmtDur(activeVid.currentTime);
      } else if (k === "o" && activeVid.id === "trimVideo") {
        e.preventDefault();
        if ($("#trimEnd")) $("#trimEnd").value = fmtDur(activeVid.currentTime);
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
        if (state.channels.length > 0) {
          callBackend("checkChannels");
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", initApp);
})();
