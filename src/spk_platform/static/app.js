"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  csrf: "",
  config: null,
  saved: 0,
  applied: 0,
  library: { local: [], cache: [] },
  tracks: [],
  catalogs: {},
  view: "music",
  source: "search",
  capabilities: {},
  events: null,
  timer: null,
  refreshTimer: null,
  playbackVersion: "",
  connectionEpoch: 0,
  accountTimer: null,
  accountDeadline: 0,
  generation: 0,
  collectionEpoch: 0,
  musicAccount: { state: "unknown" },
  musicPermissions: null,
  accountEpoch: 0,
};
const labels = {
  music: "音乐",
  speech: "Azure 语音",
  llm: "语言模型",
  mcp: "MCP 与搜索",
  device: "设备与声音",
  diagnostics: "诊断与备份",
  appearance: "配置",
};
const statusNames = {
  queued: "排队中",
  resolving: "正在解析",
  buffering: "命令已提交",
  playing: "设备报告播放",
  stopping: "正在停止",
  cancelled: "已取消",
  failed: "准备失败",
  completed: "已结束",
  unknown: "状态未知",
};
function icons() {
  window.lucide?.createIcons();
}
function element(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}
function iconButton(name, title, action) {
  const button = element("button", undefined, "icon");
  button.type = "button";
  button.title = title;
  button.setAttribute("aria-label", title);
  const icon = element("i");
  icon.dataset.lucide = name;
  button.append(icon);
  button.addEventListener("click", () => safely(action));
  return button;
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  clearTimeout(state.timer);
  state.timer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 4500);
}
async function safely(action) {
  try {
    return await action();
  } catch (error) {
    toast(error.message || "操作未完成");
  }
}
async function api(path, body, method) {
  const options = {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: {},
  };
  if (body !== undefined) {
    options.headers["X-CSRF-Token"] = state.csrf;
    if (body instanceof File) {
      options.body = body;
      options.headers["X-Filename"] = encodeURIComponent(body.name);
    } else {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
  }
  const response = await fetch("/api" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) showLogin(true);
    throw new Error(data.detail || data.category || `HTTP ${response.status}`);
  }
  return data;
}
function get(path, root = state.config) {
  return path.split(".").reduce((value, part) => value?.[part], root);
}
function set(path, value, root = state.config) {
  const parts = path.split(".");
  const key = parts.pop();
  const target = parts.reduce((value, part) => value[part], root);
  target[key] = value;
}
function showLogin(configured) {
  state.events?.close();
  clearTimeout(state.refreshTimer);
  clearTimeout(state.accountTimer);
  state.connectionEpoch += 1;
  $("#app").hidden = true;
  $("#login-view").hidden = false;
  $("#login-title").textContent = configured ? "管理登录" : "初始化管理员";
  $("#login-submit").textContent = configured ? "登录" : "创建管理员";
  $("#login-form").dataset.configured = String(configured);
}
function field(
  path,
  labelText,
  type = "text",
  options = null,
  root = state.config,
) {
  const label = element("label", labelText);
  let input;
  if (type === "select") {
    input = element("select");
    for (const [value, text] of options) {
      const option = element("option", text);
      option.value = value;
      input.append(option);
    }
  } else if (type === "textarea") {
    input = element("textarea");
    input.rows = 3;
  } else {
    input = element("input");
    input.type = type;
    if (type === "number") input.step = "any";
  }
  const value = get(path, root);
  if (type === "checkbox") {
    input.checked = Boolean(value);
    label.className = "checkbox";
  } else input.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
  input.setAttribute("aria-label", labelText);
  input.addEventListener("change", () => {
    let value = type === "checkbox" ? input.checked : input.value;
    if (type === "number") value = Number(value);
    if (Array.isArray(get(path, root)))
      value = String(value)
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
    set(path, value, root);
    if (path.startsWith("appearance.")) {
      applyAppearance();
      connectPlayback();
    }
    state.capabilities = {};
    renderCapabilities();
    updateRevisions(true);
  });
  label.append(input);
  return label;
}
function section(target, title, fields) {
  const group = element("section", undefined, "settings-group");
  group.append(element("h2", title));
  const grid = element("div", undefined, "form-grid");
  for (const spec of fields) grid.append(field(...spec));
  group.append(grid);
  $(target).append(group);
  return group;
}
function secretField(target, reference, title, metadata = {}) {
  const label = element("label", title);
  const row = element("div", undefined, "secret-row");
  const input = element("input");
  input.type = "password";
  input.autocomplete = "new-password";
  input.placeholder = metadata.configured ? "已配置，不回显" : "尚未配置";
  input.setAttribute("aria-label", title);
  const save = element("button", "更新");
  save.type = "button";
  save.addEventListener("click", () =>
    safely(async () => {
      await api("/secrets/" + reference, { value: input.value });
      input.value = "";
      await saveDraft();
      state.capabilities = {};
      await loadConfig();
      toast("密钥已保存，应用配置后生效");
    }),
  );
  const clear = iconButton("trash-2", "清除密钥", async () => {
    if (!confirm("清除该密钥？")) return;
    await api("/secrets/" + reference, { clear: true });
    await saveDraft();
    state.capabilities = {};
    await loadConfig();
  });
  row.append(input, save, clear);
  label.append(
    row,
    element(
      "small",
      metadata.configured ? `已配置 / v${metadata.version}` : "未配置",
      "secret-meta",
    ),
  );
  $(target).append(label);
}
function updateRevisions(dirty = false) {
  $("#revisions").textContent =
    `${dirty ? "未保存 · " : ""}保存 ${state.saved} / 应用 ${state.applied}`;
}
async function loadConfig() {
  const data = await api("/config");
  state.config = data.config;
  state.saved = data.saved_revision;
  state.applied = data.applied_revision;
  state.secrets = data.secrets;
  updateRevisions();
  renderSettings();
  applyAppearance();
}

function applyAppearance() {
  document.documentElement.dataset.theme =
    state.config?.appearance?.theme || "light";
}

function connectPlayback() {
  state.events?.close();
  clearTimeout(state.refreshTimer);
  const epoch = ++state.connectionEpoch;
  if (state.config.appearance.theme === "eink") {
    const poll = async () => {
      try {
        const snapshot = await api("/playback");
        if (epoch !== state.connectionEpoch) return;
        showPlayback(snapshot);
        $("#connection").textContent = "低频刷新";
      } catch (error) {
        if (epoch !== state.connectionEpoch) return;
        $("#connection").textContent = "连接中断";
      }
      state.refreshTimer = setTimeout(
        poll,
        state.config.appearance.eink_refresh_seconds * 1000,
      );
    };
    poll();
    return;
  }
  state.events = new EventSource("/api/events");
  state.events.addEventListener("session-expired", () => {
    showLogin(true);
    safely(async () => {
      const session = await api("/session");
      state.csrf = session.csrf;
      showLogin(session.configured);
    });
  });
  state.events.addEventListener("playback", (event) => {
    $("#connection").textContent = "已连接";
    $("#connection").classList.add("online");
    showPlayback(JSON.parse(event.data));
  });
  state.events.onerror = () => {
    $("#connection").textContent = "重连中";
    $("#connection").classList.remove("online");
  };
}
async function saveDraft() {
  const result = await api("/config", {
    expected_revision: state.saved,
    config: state.config,
  });
  state.saved = result.revision;
  updateRevisions();
}
function renderSettings() {
  for (const id of [
    "speech-fields",
    "llm-fields",
    "device-fields",
    "music-fields",
    "search-fields",
    "appearance-fields",
  ])
    $("#" + id).replaceChildren();
  section("#speech-fields", "Azure Speech", [
    ["speech.region", "Region"],
    ["speech.language", "识别语言"],
    ["speech.asr_endpoint", "ASR Endpoint"],
    ["speech.tts_endpoint", "TTS Endpoint"],
    ["speech.voice", "音色"],
    ["speech.rate", "语速", "number"],
    ["speech.gain_db", "TTS 增益 / dB", "number"],
    ["speech.streaming", "启用流式 TTS", "checkbox"],
    ["speech.session_seconds", "每轮聆听上限 / 秒", "number"],
    ["speech.resume_seconds", "唤醒后无人说话续播 / 秒（0 关闭）", "number"],
  ]);
  secretField(
    "#speech-fields",
    state.config.speech.asr_key_ref,
    "ASR Key",
    state.secrets[state.config.speech.asr_key_ref],
  );
  secretField(
    "#speech-fields",
    state.config.speech.tts_key_ref,
    "TTS Key",
    state.secrets[state.config.speech.tts_key_ref],
  );
  section("#llm-fields", "模型连接", [
    ["llm.endpoint", "API Base"],
    [
      "llm.protocol",
      "协议",
      "select",
      [
        ["chat", "Chat Completions"],
        ["responses", "Responses"],
      ],
    ],
    [
      "llm.auth",
      "鉴权",
      "select",
      [
        ["bearer", "Bearer"],
        ["azure-api-key", "Azure OpenAI v1 api-key"],
      ],
    ],
    ["llm.model", "模型 / 部署名称"],
    ["llm.timeout", "超时 / 秒", "number"],
    ["llm.max_tokens", "输出 Token 上限", "number"],
    [
      "llm.chat_token_parameter",
      "Chat Token 参数",
      "select",
      [
        ["max_completion_tokens", "max_completion_tokens"],
        ["max_tokens", "max_tokens"],
      ],
    ],
    ["llm.max_rounds", "工具轮数上限", "number"],
    ["llm.mcp_servers", "允许的 MCP 服务 ID"],
  ]);
  secretField(
    "#llm-fields",
    state.config.llm.key_ref,
    "LLM Key",
    state.secrets[state.config.llm.key_ref],
  );
  section("#device-fields", "设备连接", [
    ["device.ip", "音箱 IP"],
    ["device.advertised_media_url", "音箱可访问的媒体 URL"],
    ["device.allowed_cidrs", "允许网段 / CIDR"],
    ["approved_private_hosts", "批准的内网 Endpoint 主机"],
    ["device.command_ttl", "命令 TTL / 秒", "number"],
    ["device.state_ttl", "设备状态有效期 / 秒", "number"],
    ["speech.mic_gain", "输入增益", "number"],
    ["speech.vad_mode", "VAD 等级 / 0-3", "number"],
    ["speech.vad_threshold", "VAD 能量门限", "number"],
  ]);
  section("#music-fields", "播放策略", [
    ["music.night_enabled", "启用夜间静音", "checkbox"],
    ["music.timezone", "时区"],
    ["music.night_start", "夜间开始 / 小时", "number"],
    ["music.night_end", "夜间结束 / 小时", "number"],
    ["music.advance_margin", "曲间切换余量 / 秒", "number"],
    ["music.cache_enabled", "启用在线缓存", "checkbox"],
    ["music.cache_mb", "缓存上限 / MB", "number"],
    ["music.one_file_mb", "单文件上限 / MB", "number"],
    ["music.disk_floor_mb", "磁盘余量 / MB", "number"],
  ]);
  section("#search-fields", "搜索策略", [
    [
      "search_mode",
      "联网模式",
      "select",
      [
        ["off", "关闭"],
        ["auto", "自动"],
        ["explicit", "仅明确要求联网"],
      ],
    ],
  ]);
  section("#appearance-fields", "主题", [
    [
      "appearance.theme",
      "主题",
      "select",
      [
        ["light", "明亮"],
        ["dark", "暗色"],
        ["eink", "墨水屏"],
      ],
    ],
    ["appearance.eink_refresh_seconds", "墨水屏刷新间隔 / 秒", "number"],
    [
      "appearance.eink_layout",
      "墨水屏布局",
      "select",
      [
        ["auto", "自动"],
        ["portrait", "竖屏"],
        ["landscape", "横屏"],
      ],
    ],
  ]);
  $("#shuffle").checked = state.config.music.shuffle;
  $("#repeat").value = state.config.music.repeat;
  renderMCP();
  renderCapabilities();
  icons();
}
function renderMCP() {
  const container = $("#mcp-servers");
  container.replaceChildren();
  for (const server of state.config.mcp_servers) {
    const group = element("section", undefined, "mcp-server");
    const heading = element("div", undefined, "section-heading");
    heading.append(
      element("h2", server.name || server.server_id),
      iconButton("trash-2", "删除服务", async () => {
        if (!confirm("删除该 MCP 服务？")) return;
        state.config.mcp_servers = state.config.mcp_servers.filter(
          (item) => item !== server,
        );
        renderMCP();
        updateRevisions(true);
      }),
    );
    group.append(heading);
    const grid = element("div", undefined, "form-grid");
    const specs = [
      ["server_id", "服务 ID"],
      ["name", "名称"],
      ["enabled", "启用", "checkbox"],
      [
        "transport",
        "传输",
        "select",
        [
          ["streamable-http", "Streamable HTTP"],
          ["stdio", "受控 stdio"],
        ],
      ],
      ["endpoint", "Endpoint"],
      ["template", "本地模板", "select", [["brave-search", "Brave Search"]]],
      [
        "auth",
        "鉴权",
        "select",
        [
          ["none", "无鉴权"],
          ["bearer", "Bearer"],
          ["api-key", "API Key header"],
          ["oauth", "OAuth（未支持）"],
        ],
      ],
      ["header_name", "API Key header"],
      ["key_ref", "密钥引用"],
      ["search_tool", "默认搜索工具"],
      ["query_parameter", "查询参数名"],
      ["timeout", "超时 / 秒", "number"],
      ["max_results", "最大结果数", "number"],
      ["max_bytes", "最大结果字节数", "number"],
      ["cache_ttl", "非敏感查询缓存 / 秒", "number"],
    ];
    for (const spec of specs)
      grid.append(
        field(spec[0], spec[1], spec[2] || "text", spec[3] || null, server),
      );
    group.append(grid);
    const buttons = element("div", undefined, "button-row");
    const discover = element("button", "发现工具");
    discover.addEventListener("click", () =>
      safely(() => probe("mcp-discover", server.server_id)),
    );
    const query = element("input");
    query.placeholder = "测试查询";
    query.setAttribute("aria-label", "MCP 测试查询");
    const search = element("button", "搜索测试");
    search.addEventListener("click", () =>
      safely(() =>
        probe("mcp-search", server.server_id, { query: query.value }),
      ),
    );
    const loop = element("button", "LLM 搜索闭环");
    loop.addEventListener("click", () =>
      safely(() => probe("mcp-loop", server.server_id, { query: query.value })),
    );
    buttons.append(discover, query, search, loop);
    group.append(buttons);
    const tools = element("div", undefined, "tool-list");
    for (const entry of state.catalogs[server.server_id] || []) {
      const label = element("label", undefined, "tool-entry");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.checked = server.approved_tools[entry.name] === entry.hash;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) server.approved_tools[entry.name] = entry.hash;
        else delete server.approved_tools[entry.name];
        updateRevisions(true);
      });
      const description = element("div", entry.name);
      description.append(element("small", entry.description));
      label.append(checkbox, description);
      tools.append(label);
    }
    group.append(tools);
    container.append(group);
    if (server.key_ref) {
      const target = element("div");
      target.id = "mcp-key-" + server.server_id;
      group.append(target);
      secretField(
        "#" + target.id,
        server.key_ref,
        "MCP / 搜索 Key",
        state.secrets[server.key_ref],
      );
    }
  }
  icons();
}
async function probe(stage, serverId, extra = {}) {
  if (
    !["models", "voices", "mcp-discover"].includes(stage) &&
    !confirm("此测试可能产生服务商费用，继续？")
  )
    return;
  await saveDraft();
  let payload = {
    revision: state.saved,
    confirmed: true,
    server_id: serverId,
    ...extra,
  };
  if (stage === "asr") {
    const file = $("#asr-sample").files[0];
    if (!file) throw new Error("请选择已获授权的 WAV 样本");
    if (file.size > 400000) throw new Error("样本过大");
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (const value of bytes) binary += String.fromCharCode(value);
    payload.audio = btoa(binary);
  }
  const target = stage.startsWith("mcp")
    ? "#mcp-result"
    : ["voices", "tts", "asr"].includes(stage)
      ? "#speech-result"
      : "#llm-result";
  $(target).textContent = "检测中…";
  const result = await api("/probe/" + stage, payload);
  $(target).textContent = JSON.stringify(result, null, 2);
  state.capabilities[stage] = result;
  renderCapabilities();
  if (result.status === "passed" && stage === "models") {
    $("#model-list").replaceChildren();
    for (const model of result.result) {
      const button = element("button", model);
      button.addEventListener("click", () => {
        state.config.llm.model = model;
        renderSettings();
        updateRevisions(true);
      });
      $("#model-list").append(button);
    }
  }
  if (result.status === "passed" && stage === "voices") {
    $("#voices").replaceChildren();
    for (const voice of result.result.filter(
      (item) => item.locale === state.config.speech.language,
    )) {
      const button = element("button", voice.name);
      button.addEventListener("click", () => {
        state.config.speech.voice = voice.name;
        renderSettings();
        updateRevisions(true);
      });
      $("#voices").append(button);
    }
  }
  if (result.status === "passed" && stage === "mcp-discover") {
    state.catalogs[serverId] = result.result;
    renderMCP();
  }
}
function renderCapabilities() {
  $("#capabilities").replaceChildren();
  for (const [stage, name] of [
    ["models", "目录"],
    ["text", "文本"],
    ["tools", "工具"],
    ["stream", "流式"],
  ]) {
    const result = state.capabilities[stage];
    const item = element(
      "div",
      name,
      "capability " + (result?.status || "unknown"),
    );
    item.append(
      element(
        "strong",
        result
          ? result.status === "passed"
            ? "已通过"
            : result.category || "失败"
          : "未检测",
      ),
    );
    $("#capabilities").append(item);
  }
}
function showPlayback(snapshot) {
  state.generation = snapshot.generation;
  const version = JSON.stringify(snapshot);
  if (version === state.playbackVersion) return;
  state.playbackVersion = version;
  const current = snapshot.current;
  $("#track-title").textContent =
    current?.intent?.title ||
    (current?.kind === "tts" ? "语音播报" : "等待播放");
  $("#track-artist").textContent =
    current?.intent?.artist || current?.intent?.provider || "暂无曲目";
  $("#playback-state").textContent = current
    ? statusNames[current.status] || current.status
    : "空闲";
  $("#playback-state").classList.toggle(
    "warning",
    ["unknown", "failed"].includes(current?.status),
  );
  $("#device-evidence").textContent =
    {
      unknown: "设备状态未知",
      accepted: "设备已接收命令",
      playing: "播放器报告播放",
      stopped: "播放器已停止",
      completed: "播放器已结束",
    }[snapshot.device_state] || snapshot.device_state;
  $("#queue-count").textContent = snapshot.queue.length;
  $("#queue").replaceChildren();
  for (const item of snapshot.queue) {
    const row = element("div", undefined, "queue-item");
    const text = element("div", item.intent?.title || "待播放曲目");
    text.append(element("small", item.intent?.artist || item.source));
    row.append(
      text,
      iconButton("x", "移出队列", () =>
        api("/playback/remove", { playback_id: item.playback_id }),
      ),
    );
    $("#queue").append(row);
  }
  if (!snapshot.queue.length)
    $("#queue").append(element("div", "队列为空", "empty"));
  icons();
}
function renderTracks() {
  const container = $("#tracks");
  container.replaceChildren();
  state.tracks.forEach((track, index) => {
    const row = element("div", undefined, "track-row");
    row.append(
      element("span", String(index + 1).padStart(2, "0"), "track-number"),
    );
    const description = element("div");
    description.append(
      element("h3", track.title),
      element(
        "p",
        [
          track.artist,
          track.playable === false
            ? "无播放权限"
            : track.provider === "cache"
              ? "缓存"
              : track.provider === "local"
                ? "本地"
                : "网易云",
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    const actions = element("div", undefined, "track-actions");
    const play = iconButton(
      track.kind === "playlist" ? "list-music" : "play",
      track.kind === "playlist" ? "打开歌单" : "播放",
      () => playTrack(track),
    );
    play.disabled = track.playable === false;
    const enqueue = iconButton("list-plus", "加入队列", () =>
      playTrack(track, true),
    );
    enqueue.disabled = track.playable === false || track.kind === "playlist";
    actions.append(play, enqueue);
    row.append(description, actions);
    container.append(row);
  });
  if (!state.tracks.length)
    container.append(element("div", "尚无曲目", "empty"));
  icons();
}
function trackIntent(track) {
  const result = {};
  for (const key of ["provider", "id", "title", "artist", "original_id"])
    if (track[key] !== undefined) result[key] = String(track[key]);
  return result;
}
async function playTrack(track, enqueue = false) {
  if (track.kind === "playlist") {
    const epoch = ++state.collectionEpoch;
    const tracks = await api("/music/playlist", { id: track.id });
    if (epoch !== state.collectionEpoch) return;
    state.tracks = tracks;
    renderTracks();
    return;
  }
  await api("/playback", {
    request_id: [...crypto.getRandomValues(new Uint8Array(16))]
      .map((value) => value.toString(16).padStart(2, "0"))
      .join(""),
    intent: { track: trackIntent(track), enqueue },
  });
  toast(enqueue ? "已加入队列" : "正在准备");
}
async function library() {
  try {
    state.library = await api("/music/library");
    if (["local", "cache"].includes(state.source)) {
      state.tracks = state.library[state.source];
      renderTracks();
    }
  } catch (error) {
    if (["local", "cache"].includes(state.source)) {
      $("#tracks").replaceChildren(element("div", "媒体服务未就绪", "empty"));
    }
  }
}
async function loadCollection(kind) {
  const access = window.SpkMusicAccess.collectionAccess(
    state.musicAccount,
    state.musicPermissions,
  )[kind];
  if (!access?.enabled) throw new Error(access?.reason || "音乐集合未授权");
  const epoch = ++state.collectionEpoch;
  $("#tracks").replaceChildren(element("div", "正在读取…", "empty"));
  try {
    const result = await api("/music/collections", { kind });
    if (epoch !== state.collectionEpoch) return;
    state.tracks = result.tracks || result.playlists || [];
    $("#collection-status").textContent = state.tracks.length
      ? ""
      : "该列表暂无曲目";
    renderTracks();
  } catch (error) {
    if (epoch !== state.collectionEpoch) return;
    state.tracks = [];
    $("#tracks").replaceChildren(element("div", error.message, "empty"));
  }
}

async function playList(enqueue = false) {
  const tracks = state.tracks.filter(
    (track) => track.kind !== "playlist" && track.playable !== false,
  );
  if (!tracks.length)
    throw new Error("没有可播放的曲目，请先选择一个歌单或列表");
  const batch = [...crypto.getRandomValues(new Uint8Array(16))]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  showPlayback(await api("/playback"));
  const result = await api("/playback/list", {
    request_id: batch,
    tracks: tracks.slice(0, 50).map(trackIntent),
    enqueue,
    expected_generation: state.generation,
  });
  state.generation = result.generation;
  toast(enqueue ? "列表已加入队列" : "正在准备列表");
}

function updateMusicAccess() {
  const access = window.SpkMusicAccess.collectionAccess(
    state.musicAccount,
    state.musicPermissions,
  );
  for (const [kind, rule] of Object.entries(access)) {
    const button = $(`[data-library="${kind}"]`);
    button.disabled = !rule.enabled;
    button.setAttribute("aria-disabled", String(!rule.enabled));
    button.title = rule.reason;
  }
  $("#refresh-collections").disabled =
    state.musicAccount.state !== "authorized";
  if (access[state.source] && !access[state.source].enabled) {
    state.collectionEpoch += 1;
    state.tracks = [];
    state.source = "search";
    for (const tab of $$("[data-library]"))
      tab.classList.toggle("selected", tab.dataset.library === "search");
    $("#search-form").hidden = false;
    $("#tracks").replaceChildren(element("div", "个人歌单已关闭", "empty"));
  }
}

async function refreshMusicCapabilities() {
  if (state.musicAccount.state !== "authorized") return updateMusicAccess();
  const epoch = state.accountEpoch;
  state.musicPermissions = null;
  updateMusicAccess();
  try {
    const permissions = await api("/music/capabilities", {});
    if (
      epoch !== state.accountEpoch ||
      state.musicAccount.state !== "authorized"
    )
      return;
    state.musicPermissions = permissions;
    updateMusicAccess();
    const names = { daily: "每日推荐", liked: "红心", favorites: "收藏歌单" };
    $("#collection-status").textContent = Object.entries(permissions)
      .map(
        ([kind, rule]) =>
          names[kind] + ": " + (rule.available ? "可用" : "当前账号未提供"),
      )
      .join(" / ");
  } catch (error) {
    if (epoch !== state.accountEpoch) return;
    state.musicPermissions = {};
    updateMusicAccess();
    $("#collection-status").textContent = error.message;
  }
}

function showAccount(data) {
  const changed = data.state !== state.musicAccount.state;
  if (changed) {
    state.accountEpoch += 1;
    state.musicPermissions = null;
  }
  state.musicAccount = data;
  const names = {
    authorized: "已绑定",
    pending: "等待扫码",
    unauthorized: "未绑定",
    unconfigured: "未配置应用凭据",
    expired: "二维码已过期",
    failed: "绑定检查失败",
    unknown: "状态未确认",
  };
  $("#account-state").textContent =
    data.available === false ? "CLI 未安装" : names[data.state] || "状态未确认";
  $("#account-state").classList.toggle("warning", data.state !== "authorized");
  $("#account-detail").textContent = data.checked_at
    ? "最近检查 " + new Date(data.checked_at * 1000).toLocaleTimeString()
    : "";
  const visible = data.state === "pending" && data.qr_image && data.qr_url;
  $("#login-qr").hidden = !visible;
  if (visible) {
    $("#login-qr-image").src = data.qr_image;
    $("#login-qr-link").href = data.qr_url;
  } else {
    $("#login-qr-image").removeAttribute("src");
    $("#login-qr-link").removeAttribute("href");
  }
  state.accountDeadline = data.expires_at || 0;
  updateMusicAccess();
  if (data.state === "authorized" && (changed || !state.musicPermissions))
    refreshMusicCapabilities();
}

function accountUnavailable(error) {
  showAccount({ state: "unknown", checked_at: 0 });
  $("#account-detail").textContent = error.message;
}

function pollAccount() {
  clearTimeout(state.accountTimer);
  if (state.accountDeadline * 1000 <= Date.now()) return;
  state.accountTimer = setTimeout(async () => {
    try {
      const data = await api("/music/account/poll", {});
      showAccount(data);
      if (data.state === "pending") pollAccount();
    } catch (error) {
      accountUnavailable(error);
    }
  }, 4000);
}

async function alarms() {
  const rows = await api("/alarms");
  $("#alarms").replaceChildren();
  for (const alarm of rows) {
    const row = element("div", undefined, "alarm-row");
    const text = element("div");
    text.append(
      element(
        "strong",
        `${String(alarm.hour).padStart(2, "0")}:${String(alarm.minute).padStart(2, "0")}`,
      ),
      element("span", alarm.track.title),
    );
    row.append(
      text,
      iconButton("trash-2", "删除闹钟", async () => {
        await api("/alarms/" + alarm.id, {}, "DELETE");
        await alarms();
      }),
    );
    $("#alarms").append(row);
  }
  if (!rows.length) $("#alarms").append(element("div", "暂无闹钟", "empty"));
}
async function diagnostics() {
  const data = await api("/diagnostics");
  state.capabilities = data.probes;
  renderCapabilities();
  const container = $("#diagnostics");
  container.replaceChildren();
  for (const [title, value] of [
    ["应用版本", data.applied_revision],
    ["保存版本", data.saved_revision],
    ["可用磁盘", (data.disk_free / 1073741824).toFixed(1) + " GB"],
    [
      "最近 UDP",
      data.voice.last_packet
        ? new Date(data.voice.last_packet * 1000).toLocaleTimeString()
        : "未接收",
    ],
    ["唤醒", data.voice.wake || "未连接"],
    ["语音错误", data.voice.errors ?? "未知"],
  ]) {
    const item = element("div");
    item.append(element("dt", title), element("dd", String(value)));
    container.append(item);
  }
  $("#events").replaceChildren();
  for (const event of data.events.slice().reverse()) {
    const row = element("div", undefined, "event-row");
    row.append(
      element("time", new Date(event.created * 1000).toLocaleTimeString()),
      element("span", event.type + " / " + event.payload),
    );
    $("#events").append(row);
  }
}
async function enter() {
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  await loadConfig();
  updateMusicAccess();
  try {
    let account = await api("/music/account");
    if (account.configured && account.state === "unknown")
      account = await api("/music/account/check", {});
    showAccount(account);
  } catch (error) {
    accountUnavailable(error);
  }
  await library();
  await alarms();
  showPlayback(await api("/playback"));
  connectPlayback();
  navigate(location.hash.slice(1) || "music");
}
function navigate(view) {
  state.view = labels[view] ? view : "music";
  for (const item of $$(".view"))
    item.hidden = item.id !== "view-" + state.view;
  for (const button of $$("nav button"))
    button.classList.toggle("selected", button.dataset.view === state.view);
  $("#page-title").textContent = labels[state.view];
  location.hash = state.view;
  if (state.view === "diagnostics") safely(diagnostics);
  window.scrollTo({ top: 0, behavior: "auto" });
}
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  let result;
  try {
    // The old session may have expired or been revoked by a password reset.
    state.csrf = (await api("/session")).csrf;
    result = await api(
      $("#login-form").dataset.configured === "true" ? "/login" : "/setup",
      { password: $("#password").value, bootstrap: $("#bootstrap").value },
    );
  } catch (error) {
    $("#login-error").textContent = error.message || "登录未完成";
    return;
  }
  state.csrf = result.csrf;
  $("#password").value = "";
  $("#bootstrap").value = "";
  await safely(enter);
});
for (const button of $$("nav button"))
  button.addEventListener("click", () => navigate(button.dataset.view));
$("#logout").addEventListener("click", () =>
  safely(async () => {
    await api("/logout", {});
    location.reload();
  }),
);
$("#save-settings").addEventListener("click", () =>
  safely(async () => {
    await saveDraft();
    toast("草稿已保存");
  }),
);
$("#apply-settings").addEventListener("click", () =>
  safely(async () => {
    await saveDraft();
    await api("/config/apply", { revision: state.saved });
    state.applied = state.saved;
    state.capabilities = {};
    updateRevisions();
    renderCapabilities();
    toast("配置已应用");
  }),
);
for (const action of ["stop", "next", "replay", "previous"])
  $("#" + action).addEventListener("click", () =>
    safely(async () => {
      await api("/playback/" + action, {});
      toast("请求已提交");
    }),
  );
for (const button of $$("[data-probe]"))
  button.addEventListener("click", () =>
    safely(() => probe(button.dataset.probe)),
  );
for (const button of $$("[data-library]"))
  button.addEventListener("click", () => {
    state.source = button.dataset.library;
    state.collectionEpoch += 1;
    for (const tab of $$("[data-library]"))
      tab.classList.toggle("selected", tab === button);
    $("#search-form").hidden = [
      "local",
      "cache",
      "daily",
      "liked",
      "favorites",
    ].includes(state.source);
    $("#search-query").placeholder =
      state.source === "playlist" ? "歌单 ID" : "歌曲、歌手";
    if (["local", "cache"].includes(state.source)) {
      state.tracks = state.library[state.source];
      renderTracks();
    }
    if (["daily", "liked", "favorites"].includes(state.source))
      safely(() => loadCollection(state.source));
  });
$("#search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  safely(async () => {
    const epoch = ++state.collectionEpoch;
    $("#tracks").replaceChildren(element("div", "搜索中…", "empty"));
    const tracks = await api(
      state.source === "playlist" ? "/music/playlist" : "/music/search",
      state.source === "playlist"
        ? { id: $("#search-query").value }
        : { query: $("#search-query").value },
    );
    if (epoch !== state.collectionEpoch) return;
    state.tracks = tracks;
    renderTracks();
  });
});
$("#refresh-library").addEventListener("click", () => safely(library));
$("#upload-music").addEventListener("change", (event) =>
  safely(async () => {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 40 * 1048576) throw new Error("文件超过 40 MB");
    toast("上传与转码中");
    await api("/music/upload", file);
    await library();
    toast("音乐已导入");
    event.target.value = "";
  }),
);
$("#shuffle").addEventListener("change", () => {
  state.config.music.shuffle = $("#shuffle").checked;
  updateRevisions(true);
});
$("#repeat").addEventListener("change", () => {
  state.config.music.repeat = $("#repeat").value;
  updateRevisions(true);
});
$("#clear-cache").addEventListener("click", () =>
  safely(async () => {
    if (!confirm("清理未在播放的音乐缓存？")) return;
    await api("/music/clear", {});
    await library();
    toast("缓存已清理");
  }),
);
$("#account-refresh").addEventListener("click", () =>
  safely(async () => {
    try {
      showAccount(await api("/music/account/check", {}));
    } catch (error) {
      accountUnavailable(error);
    }
  }),
);
for (const action of ["login", "poll", "logout"])
  $("#account-" + action).addEventListener("click", () =>
    safely(async () => {
      let data;
      try {
        data = await api("/music/account/" + action, {});
      } catch (error) {
        accountUnavailable(error);
        return;
      }
      clearTimeout(state.accountTimer);
      showAccount(data);
      if (data.state === "pending") pollAccount();
    }),
  );
$("#music-credentials").addEventListener("submit", (event) => {
  event.preventDefault();
  safely(async () => {
    const data = Object.fromEntries(new FormData(event.target));
    const account = await api("/music/account/credentials", data);
    showAccount(account);
    event.target.reset();
    toast("音乐应用凭据已更新");
  });
});
$("#play-list").addEventListener("click", () => safely(() => playList(false)));
$("#enqueue-list").addEventListener("click", () =>
  safely(() => playList(true)),
);
$("#refresh-collections").addEventListener("click", () =>
  safely(refreshMusicCapabilities),
);

$("#ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  safely(async () => {
    $("#answer").textContent = "请求中…";
    const result = await api("/ask", {
      text: $("#ask-text").value,
      allow_search: $("#ask-search").checked,
      speak: $("#ask-speak").checked,
    });
    $("#answer").textContent = result.cancelled ? "已取消" : result.text;
    $("#sources").replaceChildren();
    for (const source of result.sources || []) {
      try {
        const url = new URL(source.url);
        if (!["http:", "https:"].includes(url.protocol)) continue;
        const link = element("a", source.title || url.hostname);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        $("#sources").append(link);
      } catch {}
    }
  });
});
$("#add-mcp").addEventListener("click", () => {
  // Never reuse an ID or a stored key reference left behind by a deleted server.
  const used = new Set([
    ...Object.keys(state.secrets || {}),
    ...state.config.mcp_servers.flatMap((server) => [
      server.server_id,
      server.key_ref,
    ]),
  ]);
  let number = 1;
  while (used.has("search_" + number)) number += 1;
  const id = "search_" + number;
  state.config.mcp_servers.push({
    server_id: id,
    name: "搜索服务",
    enabled: false,
    transport: "streamable-http",
    endpoint: "",
    template: "brave-search",
    auth: "none",
    header_name: "X-API-Key",
    key_ref: id,
    approved_tools: {},
    search_tool: "",
    query_parameter: "query",
    timeout: 10,
    max_results: 5,
    max_bytes: 32768,
    cache_ttl: 0,
  });
  renderMCP();
  updateRevisions(true);
});
$("#refresh-diagnostics").addEventListener("click", () => safely(diagnostics));
$("#rollback").addEventListener("click", () =>
  safely(async () => {
    const revision = Number($("#rollback-revision").value);
    if (!revision || !confirm(`回滚到版本 ${revision}？`)) return;
    await api("/config/apply", { revision });
    await loadConfig();
    toast("已回滚运行配置");
  }),
);
$("#restore-settings").addEventListener("change", (event) =>
  safely(async () => {
    const file = event.target.files[0];
    if (!file || file.size > 120000) throw new Error("备份大小无效");
    const data = JSON.parse(await file.text());
    const result = await api("/restore", {
      ...data,
      expected_revision: state.saved,
    });
    await loadConfig();
    toast("设置已导入草稿，尚未应用");
    event.target.value = "";
  }),
);
$("#new-alarm").addEventListener("click", () => {
  $("#alarm-track").replaceChildren();
  state.tracks.forEach((track, index) => {
    const option = element("option", track.title);
    option.value = index;
    $("#alarm-track").append(option);
  });
  if (!state.tracks.length) return toast("请先选择曲库来源或搜索曲目");
  $("#alarm-dialog").showModal();
});
$("#cancel-alarm").addEventListener("click", () => $("#alarm-dialog").close());
$("#alarm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  safely(async () => {
    const [hour, minute] = $("#alarm-time").value.split(":").map(Number);
    const track = state.tracks[Number($("#alarm-track").value)];
    await api("/alarms", { hour, minute, track: trackIntent(track) });
    $("#alarm-dialog").close();
    await alarms();
  });
});
window.addEventListener("hashchange", () => {
  if (state.config) navigate(location.hash.slice(1));
});
safely(async () => {
  const session = await api("/session");
  state.csrf = session.csrf;
  if (session.authenticated) await enter();
  else {
    showLogin(session.configured);
    $("#bootstrap").hidden = session.configured || session.local_setup;
    $("#bootstrap-label").hidden = session.configured || session.local_setup;
  }
  icons();
});
