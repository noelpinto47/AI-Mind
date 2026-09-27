"use strict";

/* =====================================================================
   AI Mind — chat client
   ---------------------------------------------------------------------
   Sections
     1  Configuration and feature flags
     2  Helpers (DOM, storage, dates, files, toasts, menus, dialogs)
     3  State, settings and theme
     4  Markdown rendering
     5  Messages
     6  Sending, streaming, stopping
     7  Conversations and sidebar
     8  Composer (attachments, slash prompts, dictation)
     9  Side panel, command palette, settings, shortcuts
    10  Boot

   Backend contract used today
     GET    /api/conversations?search=      -> {conversations: [{id, title, updated_at}]}
     GET    /api/conversation/<id>          -> {title, messages: [{role, content}]}
     PATCH  /api/conversations/<id>         {title} -> {conversation: {title}}
     DELETE /api/conversations/<id>
     POST   /api/chat                       {message, conversation_id} -> {response, conversation_id}

   Optional reply fields. The UI renders each one as soon as it appears,
   so no frontend change is needed when the backend starts sending them:
     title, reasoning, reasoning_ms, sources: [{title, url, snippet}],
     follow_ups: [string], usage: {input_tokens, output_tokens}

   Everything listed in FEATURES below is wired up in the UI but switched
   off. Each entry says what the backend has to provide. Set `on: true`
   once it does.
   ===================================================================== */

/* ---------------------------------------------------------------------
   1. Configuration
   --------------------------------------------------------------------- */

const API = {
  chat: "/api/chat",
  chatStream: "/api/chat/stream",
  conversations: "/api/conversations",
  conversation: id => `/api/conversation/${encodeURIComponent(id)}`,
  conversationItem: id => `/api/conversations/${encodeURIComponent(id)}`,
  feedback: "/api/feedback",
  share: id => `/api/conversations/${encodeURIComponent(id)}/share`,
  archive: id => `/api/conversations/${encodeURIComponent(id)}/archive`,
  aiRouterStatus: "/api/ai-router/status",
  projects: "/api/projects",
  project: id => `/api/projects/${encodeURIComponent(id)}`
};

const FEATURES = {
  streaming: {
    on: false,
    label: "Streaming replies",
    hint: "Implement POST /api/chat/stream (server-sent events). Events are JSON: " +
      '{type:"delta",content}, {type:"reasoning",content}, {type:"sources",sources}, ' +
      '{type:"done",conversation_id,title,follow_ups,usage}. See streamReply().'
  },
  regenerate: {
    on: false,
    label: "Regenerate",
    hint: "POST /api/chat with {regenerate:true}. Drop the last assistant message and answer again without saving the user message twice."
  },
  editMessage: {
    on: false,
    label: "Editing messages",
    hint: "POST /api/chat with {edit_index}. Delete messages from that index onward, then answer the edited prompt."
  },
  feedbackSync: {
    on: false,
    label: "Saving feedback",
    hint: 'POST /api/feedback with {conversation_id, index, value: "up" | "down" | null}. Thumbs already work locally.'
  },
  webSearch: {
    on: false,
    label: "Web search",
    hint: "Accept options.web_search and return sources[] in the reply. Inline [1], [2] markers become citation chips."
  },
  deepThink: {
    on: false,
    label: "Deeper thinking",
    hint: "Accept options.reasoning and return reasoning (and reasoning_ms) in the reply."
  },
  deepResearch: {
    on: false,
    label: "Deep research",
    hint: 'Accept options.mode = "deepResearch". Long-running; ideally streams progress.'
  },
  imageGeneration: {
    on: false,
    label: "Image creation",
    hint: 'Accept options.mode = "imageGeneration" and return Markdown with an image data URL.'
  },
  canvas: {
    on: false,
    label: "Canvas",
    hint: 'Accept options.mode = "canvas". Preview for HTML and SVG code blocks already works.'
  },
  fileUpload: {
    on: false,
    label: "Image and PDF uploads",
    hint: "Accept attachments[] ({name, type, size, data}) where data is a data URL. Text and code files already work."
  },
  modelSettings: {
    on: true,
    label: "Model selection",
    hint: "options.model is sent as the router's preferred_provider; options.temperature is not yet read server-side."
  },
  customInstructions: {
    on: false,
    label: "Custom instructions",
    hint: "Accept options.instructions = {about, style} and add them to the system prompt."
  },
  memoryToggle: {
    on: false,
    label: "Memory switch",
    hint: "Accept options.use_memory (boolean)."
  },
  share: {
    on: false,
    label: "Sharing",
    hint: "POST /api/conversations/<id>/share -> {url}."
  },
  archive: {
    on: false,
    label: "Archive",
    hint: "POST /api/conversations/<id>/archive and hide archived chats from the list."
  },
  projects: {
    on: true,
    label: "Projects",
    hint: "GET/POST /api/projects, GET/PATCH/DELETE /api/projects/<id>. Instructions are injected into the system prompt server-side."
  },
  promptLibrary: {
    on: false,
    label: "Prompt library",
    hint: "Saved prompts. Listen for the aimind:feature event. Slash templates already work."
  }
};

const MODELS = [
  {id: "auto", name: "Auto", desc: "Automatically uses the best available provider", ico: "sparkle", live: true}
];

// Friendly display names for the router's provider ids (ai_router.py state.name).
const PROVIDER_LABELS = {
  groq: "Groq",
  gemini: "Gemini",
  cloudflare: "Cloudflare",
  mistral: "Mistral",
  huggingface: "Hugging Face",
  openrouter: "OpenRouter"
};

function providerLabel(name) {
  return PROVIDER_LABELS[name] || (name ? name[0].toUpperCase() + name.slice(1) : name);
}

// The live model list: "Auto" plus one entry per provider actually
// configured in ai_router.py (fetched from /api/ai-router/status).
// Falls back to just "Auto" if that fetch hasn't happened yet or failed.
function availableModels() {
  if (!state.providers.length) return MODELS;
  return [
    MODELS[0],
    ...state.providers.map(p => ({
      id: p.provider,
      name: providerLabel(p.provider),
      desc: p.available ? p.model : `${p.model} · resting after a recent failure`,
      ico: "bulb",
      live: true
    }))
  ];
}

const MODES = {
  deepResearch: {label: "Deep research", ico: "compass"},
  imageGeneration: {label: "Create image", ico: "image"},
  canvas: {label: "Canvas", ico: "layout"}
};

const PROMPTS = [
  {id: "summarize", title: "Summarize", text: "Summarize the following in five bullet points:\n\n"},
  {id: "explain", title: "Explain step by step", text: "Explain this step by step, assuming I'm new to it:\n\n"},
  {id: "review", title: "Review code", text: "Review this code for bugs, readability and performance. Suggest concrete fixes:\n\n"},
  {id: "debug", title: "Debug an error", text: "Help me debug this error. Here is the message and the code around it:\n\n"},
  {id: "rewrite", title: "Rewrite clearly", text: "Rewrite this to be clearer and more concise, keeping my tone:\n\n"},
  {id: "translate", title: "Translate", text: "Translate the following into English and note anything ambiguous:\n\n"},
  {id: "plan", title: "Make a plan", text: "Turn this into a step-by-step plan with milestones and risks:\n\n"},
  {id: "compare", title: "Compare options", text: "Compare these options in a table and recommend one:\n\n"}
];

const SHORTCUTS = [
  {keys: "mod+k", label: "Search chats and actions"},
  {keys: "mod+shift+o", label: "New chat"},
  {keys: "mod+b", label: "Show or hide the sidebar"},
  {keys: "mod+,", label: "Open settings"},
  {keys: "mod+/", label: "Show keyboard shortcuts"},
  {keys: "mod+shift+;", label: "Copy the last reply"},
  {keys: "enter", label: "Send message"},
  {keys: "shift+enter", label: "New line"},
  {keys: "/", label: "Insert a prompt template (at the start of a message)"},
  {keys: "esc", label: "Stop a reply, or close a panel"}
];

const TEXT_EXTENSIONS = new Set(
  ("txt md markdown csv tsv json jsonl xml yml yaml toml ini log js jsx ts tsx mjs cjs py rb php java kt kts " +
   "go rs c h cpp hpp cs swift dart html htm css scss sass less sql sh bash zsh ps1 bat gradle lua r pl vue svelte tex").split(" ")
);
const MAX_FILES = 5;
const MAX_TEXT_BYTES = 200 * 1024;
const MAX_BINARY_BYTES = 10 * 1024 * 1024;

const CODE_EXTENSIONS = {
  javascript: "js", js: "js", jsx: "jsx", typescript: "ts", ts: "ts", tsx: "tsx", python: "py", py: "py",
  html: "html", css: "css", json: "json", bash: "sh", sh: "sh", shell: "sh", zsh: "sh", java: "java", go: "go",
  rust: "rs", dart: "dart", php: "php", sql: "sql", yaml: "yml", yml: "yml", markdown: "md", md: "md",
  xml: "xml", svg: "svg", c: "c", cpp: "cpp", csharp: "cs", cs: "cs", swift: "swift", kotlin: "kt", ruby: "rb"
};

const DEFAULT_SETTINGS = {
  fontSize: "medium",
  width: "comfortable",
  reveal: true,
  enterToSend: true,
  details: false,
  name: "",
  about: "",
  style: "",
  model: "auto",
  temperature: 0.7,
  memory: true
};

/* ---------------------------------------------------------------------
   2. Helpers
   --------------------------------------------------------------------- */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const store = {
  get(key, fallback = null) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch (error) { /* storage unavailable */ }
  },
  remove(key) {
    try { localStorage.removeItem(key); } catch (error) { /* storage unavailable */ }
  },
  getJSON(key, fallback) {
    try {
      const value = JSON.parse(localStorage.getItem(key));
      return value === null || value === undefined ? fallback : value;
    } catch (error) {
      return fallback;
    }
  },
  setJSON(key, value) {
    store.set(key, JSON.stringify(value));
  }
};

/** Tiny element builder: h("div", {class: "x", onclick}, child, child...) */
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** SVG icon from the sprite in index.html */
function icon(name, extraClass = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `ico ${extraClass}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

const sameId = (a, b) => a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const uid = () => Math.random().toString(36).slice(2, 10);
const truncate = (text, max) => (text.length > max ? `${text.slice(0, max).trimEnd()}…` : text);
const isCoarsePointer = () => window.matchMedia("(pointer: coarse)").matches;

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value ?? "";
  return element.innerHTML;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(ms) {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
}

/** Accepts SQLite-style "YYYY-MM-DD HH:MM:SS" (UTC), ISO strings, epoch seconds/ms and Dates. */
function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return value;
  if (typeof value === "number") return new Date(value < 1e12 ? value * 1000 : value);
  const text = String(value).trim();
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(text);
  const looksSql = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(text);
  const date = new Date(looksSql && !hasZone ? `${text.replace(" ", "T")}Z` : text);
  return Number.isNaN(date.getTime()) ? null : date;
}

function groupLabel(date) {
  if (!date) return "Earlier";
  const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 8) return "Previous 7 days";
  if (days < 31) return "Previous 30 days";
  return date.toLocaleDateString([], {month: "long", year: "numeric"});
}

function timeOf(value) {
  const date = parseDate(value);
  return date ? date.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) : "";
}

function fileSlug(text) {
  return (text || "chat").replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-").slice(0, 60) || "chat";
}

function extensionFor(language) {
  return CODE_EXTENSIONS[(language || "").toLowerCase()] || "txt";
}

function downloadFile(name, text, mime = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], {type: `${mime};charset=utf-8`}));
  const link = h("a", {href: url, download: name});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function copyText(value) {
  const text = value || "";
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const area = h("textarea", {readonly: true, style: "position:fixed;left:-9999px;top:0"});
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (fallbackError) { ok = false; }
    area.remove();
    return ok;
  }
}

/** Swaps a copy icon for a check mark (and "Copy" for "Copied") briefly. */
function flashCopied(button) {
  const label = $(".code-action-label", button);
  const original = label ? label.textContent : "";
  button.classList.add("copied");
  if (label) label.textContent = "Copied";
  setTimeout(() => {
    button.classList.remove("copied");
    if (label) label.textContent = original;
  }, 1500);
}

const IS_MAC = /mac|iphone|ipad|ipod/i.test((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "");

function keyNames(combo) {
  const names = IS_MAC
    ? {mod: "⌘", shift: "⇧", alt: "⌥", enter: "↵", esc: "Esc"}
    : {mod: "Ctrl", shift: "Shift", alt: "Alt", enter: "Enter", esc: "Esc"};
  return combo.split("+").map(part => names[part] ?? part.toUpperCase());
}

function formatKeys(combo) {
  return keyNames(combo).join(IS_MAC ? "" : "+");
}

function keycaps(combo) {
  return h("span", {class: "keycaps"}, keyNames(combo).map(name => h("kbd", null, name)));
}

/* ---- DOM references ------------------------------------------------- */

const dom = {
  shell: $("#app-shell"),
  sidebar: $("#sidebar"),
  sidebarCollapse: $("#sidebar-collapse"),
  backdrop: $("#history-backdrop"),
  newChat: $("#new-chat"),
  historySearch: $("#history-search"),
  historyCount: $("#history-count"),
  conversationList: $("#conversation-list"),
  profileButton: $("#profile-button"),
  profileAvatar: $("#profile-avatar"),
  profileName: $("#profile-name"),
  connectionDot: $("#connection-dot"),
  connectionText: $("#connection-text"),
  openSettings: $("#open-settings"),
  sidebarToggle: $("#mobile-history"),
  chatTitle: $("#chat-title"),
  projectPill: $("#project-pill"),
  projectPillLabel: $("#project-pill-label"),
  shareButton: $("#share-button"),
  chatMenuButton: $("#chat-menu-button"),
  themeToggle: $("#theme-toggle"),
  chat: $("#chat"),
  welcome: $("#welcome"),
  welcomeTitle: $("#welcome-title"),
  messages: $("#messages"),
  scrollBottom: $("#scroll-bottom"),
  form: $("#chat-form"),
  input: $("#message-input"),
  attachmentTray: $("#attachment-tray"),
  toolsButton: $("#tools-button"),
  toggleSearch: $("#toggle-search"),
  toggleThink: $("#toggle-think"),
  modeChip: $("#mode-chip"),
  modelButton: $("#model-button"),
  modelLabel: $("#model-label"),
  micButton: $("#mic-button"),
  sendButton: $("#send-button"),
  tokenHint: $("#token-hint"),
  keyHint: $("#composer-key-hint"),
  slashMenu: $("#slash-menu"),
  fileInput: $("#file-input"),
  dropOverlay: $("#drop-overlay"),
  toasts: $("#toasts"),
  srStatus: $("#sr-status"),
  panel: $("#panel"),
  panelTitle: $("#panel-title"),
  panelTabs: $("#panel-tabs"),
  panelTabPreview: $("#panel-tab-preview"),
  panelTabCode: $("#panel-tab-code"),
  panelCopy: $("#panel-copy"),
  panelDownload: $("#panel-download"),
  panelClose: $("#panel-close"),
  panelFrame: $("#panel-frame"),
  panelCode: $("#panel-code"),
  settings: $("#settings-dialog"),
  shortcuts: $("#shortcuts-dialog"),
  confirm: $("#confirm-dialog"),
  palette: $("#palette-dialog"),
  projects: $("#projects-dialog"),
  paletteInput: $("#palette-input"),
  paletteList: $("#palette-list")
};

/* ---------------------------------------------------------------------
   3. State, settings and theme
   --------------------------------------------------------------------- */

function loadSettings() {
  const saved = store.getJSON("ai-mind-settings", {});
  const settings = {...DEFAULT_SETTINGS, ...(saved && typeof saved === "object" ? saved : {})};
  const theme = store.get("ai-mind-theme", "system");
  settings.theme = ["light", "dark", "system"].includes(theme) ? theme : "system";
  return settings;
}

function saveSettings() {
  const {theme, ...rest} = state.settings;
  store.setJSON("ai-mind-settings", rest);
  store.set("ai-mind-theme", theme);
}

const state = {
  conversationId: store.get("conversation_id"),
  conversations: [],
  messages: [],
  title: "New chat",
  isSending: false,
  abort: null,
  epoch: 0,       // bumped on every navigation so late responses can be ignored
  loadToken: 0,
  listToken: 0,
  settings: loadSettings(),
  pinned: new Set([].concat(store.getJSON("ai-mind-pinned", [])).map(String)),
  feedback: store.getJSON("ai-mind-feedback", {}) || {},
  attachments: [],
  tools: {webSearch: false, deepThink: false},
  mode: null,
  stick: true,
  online: true,
  speaking: null,
  recognition: null,
  panel: null,
  paletteChats: [],
  providers: [],          // cached /api/ai-router/status list, feeds availableModels()
  projects: [],           // cached /api/projects list
  newChatProjectId: null  // set by "+ New chat" inside a project; consumed by buildPayload()
};

/** Elements for messages, kept out of the message objects so they stay serialisable. */
const nodeOf = new WeakMap();

const mobileQuery = window.matchMedia("(max-width: 760px)");
const systemLight = window.matchMedia("(prefers-color-scheme: light)");

function applyTheme() {
  const preference = state.settings.theme;
  const light = preference === "light" || (preference === "system" && systemLight.matches);
  // Both hooks: data-theme drives the tokens on this page, body.light keeps the
  // memories page (which toggles the class itself) in step.
  document.documentElement.dataset.theme = light ? "light" : "dark";
  document.body.classList.toggle("light", light);
  const meta = $('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", light ? "#f8f8f9" : "#0a0c0e");

  const label = light ? "Switch to dark mode" : "Switch to light mode";
  dom.themeToggle.replaceChildren(icon(light ? "moon" : "sun"));
  dom.themeToggle.title = label;
  dom.themeToggle.setAttribute("aria-label", label);
}

function greeting() {
  if (!state.settings.name) return "How can I help?";
  const hour = new Date().getHours();
  const part = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  return `${part}, ${state.settings.name}`;
}

function applySettings() {
  const root = document.documentElement;
  root.dataset.font = state.settings.fontSize;
  root.dataset.width = state.settings.width;
  document.body.classList.toggle("show-details", !!state.settings.details);

  const name = state.settings.name.trim();
  dom.profileName.textContent = name || "You";
  dom.profileAvatar.textContent = (name || "N").charAt(0).toUpperCase();
  dom.welcomeTitle.textContent = greeting();

  const models = availableModels();
  const model = models.find(m => m.id === state.settings.model) || models[0];
  dom.modelLabel.textContent = model.name;
  dom.modelButton.title = model.id === "auto"
    ? `Auto — ${model.desc}`
    : `${model.name} — ${model.desc}`;

  const mod = IS_MAC ? "⌘" : "Ctrl";
  dom.keyHint.textContent = state.settings.enterToSend
    ? "Enter to send, Shift + Enter for a new line"
    : `${mod} + Enter to send, Enter for a new line`;
  applyTheme();
}

/* ---- Toasts ---------------------------------------------------------- */

function toast(message, {type = "info", duration = 4200, action = null} = {}) {
  while (dom.toasts.children.length >= 3) dom.toasts.firstElementChild.remove();

  const node = h("div", {class: `toast ${type}`, role: type === "error" ? "alert" : "status"},
    h("span", {class: "toast-dot"}),
    h("span", {class: "toast-text"}, message));

  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    node.classList.remove("in");
    setTimeout(() => node.remove(), 220);
  };

  if (action) {
    node.append(h("button", {
      type: "button",
      class: "toast-action",
      onclick: () => { action.onClick(); dismiss(); }
    }, action.label));
  }

  dom.toasts.append(node);
  requestAnimationFrame(() => node.classList.add("in"));
  timer = setTimeout(dismiss, duration);
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", () => { timer = setTimeout(dismiss, 1800); });
  return dismiss;
}

function announce(text) {
  dom.srStatus.textContent = "";
  setTimeout(() => { dom.srStatus.textContent = text; }, 60);
}

/** Feature gate. Returns true when the feature is live, otherwise explains and returns false. */
function requireFeature(key) {
  const feature = FEATURES[key];
  if (feature && feature.on) return true;
  toast(`${feature ? feature.label : key} is coming soon.`);
  if (feature) console.info(`[AI Mind] "${key}" is switched off. Backend needs: ${feature.hint}`);
  return false;
}

function emitFeature(key, detail = {}) {
  document.dispatchEvent(new CustomEvent("aimind:feature", {detail: {feature: key, ...detail}}));
}

/* ---- Menus ------------------------------------------------------------ */

let activeMenu = null;

function closeMenu({restoreFocus = false} = {}) {
  if (!activeMenu) return;
  const {node, anchor, cleanup} = activeMenu;
  activeMenu = null;
  cleanup();
  node.remove();
  if (anchor) {
    anchor.setAttribute("aria-expanded", "false");
    if (restoreFocus) anchor.focus();
  }
}

/**
 * items: {label, ico, desc, kbd, danger, checked, feature, onClick} | {divider:true} | {heading}
 * placement: "bottom-start" | "bottom-end" | "top-start" | "top-end"
 */
function openMenu(anchor, items, {placement = "bottom-start", minWidth = 220, className = ""} = {}) {
  const toggledOff = activeMenu && activeMenu.anchor === anchor;
  closeMenu();
  if (toggledOff) return;

  const menu = h("div", {class: `menu ${className}`.trim(), role: "menu"});
  menu.style.minWidth = `${minWidth}px`;

  items.forEach(item => {
    if (item.divider) return menu.append(h("div", {class: "menu-divider", role: "separator"}));
    if (item.heading) return menu.append(h("div", {class: "menu-heading"}, item.heading));

    const soon = item.feature && !(FEATURES[item.feature] && FEATURES[item.feature].on);
    const isChoice = item.checked !== undefined;
    const button = h("button", {
      type: "button",
      role: isChoice ? "menuitemradio" : "menuitem",
      "aria-checked": isChoice ? String(!!item.checked) : null,
      class: `menu-item${item.danger ? " danger" : ""}${soon ? " is-soon" : ""}`
    },
      item.ico ? icon(item.ico) : h("span", {class: "ico-spacer"}),
      h("span", {class: "menu-label"},
        h("span", {class: "menu-title"}, item.label),
        item.desc ? h("span", {class: "menu-desc"}, item.desc) : null),
      item.kbd ? h("span", {class: "kbd"}, formatKeys(item.kbd)) : null,
      item.checked ? icon("check", "menu-check") : null);
    button.addEventListener("click", () => {
      closeMenu();
      if (item.onClick) item.onClick();
    });
    menu.append(button);
  });

  document.body.append(menu);
  anchor.setAttribute("aria-expanded", "true");

  const rect = anchor.getBoundingClientRect();
  const size = menu.getBoundingClientRect();
  const [vertical, horizontal] = placement.split("-");
  let top = vertical === "top" ? rect.top - size.height - 8 : rect.bottom + 8;
  let left = horizontal === "end" ? rect.right - size.width : rect.left;
  if (vertical === "bottom" && top + size.height > window.innerHeight - 8) top = rect.top - size.height - 8;
  if (vertical === "top" && top < 8) top = rect.bottom + 8;
  menu.style.top = `${clamp(top, 8, Math.max(8, window.innerHeight - size.height - 8))}px`;
  menu.style.left = `${clamp(left, 8, Math.max(8, window.innerWidth - size.width - 8))}px`;

  const onPointerDown = event => {
    if (!menu.contains(event.target) && !anchor.contains(event.target)) closeMenu();
  };
  const onKeyDown = event => {
    const entries = $$(".menu-item", menu);
    const index = entries.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeMenu({restoreFocus: true});
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      entries[(index + 1) % entries.length].focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      entries[(index - 1 + entries.length) % entries.length].focus();
    } else if (event.key === "Tab") {
      closeMenu();
    }
  };
  const onScroll = event => { if (!menu.contains(event.target)) closeMenu(); };

  document.addEventListener("pointerdown", onPointerDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", closeMenu);

  activeMenu = {
    node: menu,
    anchor,
    cleanup() {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", closeMenu);
    }
  };

  const first = $(".menu-item", menu);
  if (first) first.focus({preventScroll: true});
}

/* ---- Dialogs ---------------------------------------------------------- */

function openDialog(dialog) {
  closeMenu();
  if (!dialog.open) dialog.showModal();
}

function confirmDialog({title, body, confirmLabel = "Confirm", danger = false}) {
  return new Promise(resolve => {
    const dialog = dom.confirm;
    dialog.replaceChildren(h("form", {method: "dialog", class: "dialog-form"},
      h("h2", {class: "dialog-title"}, title),
      h("p", {class: "dialog-body"}, body),
      h("div", {class: "dialog-actions"},
        h("button", {class: "btn ghost", value: "cancel", autofocus: danger}, "Cancel"),
        h("button", {class: `btn ${danger ? "danger" : "primary"}`, value: "ok", autofocus: !danger}, confirmLabel))));
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), {once: true});
    openDialog(dialog);
  });
}

/* ---- Network ---------------------------------------------------------- */

function httpError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}

function setConnection(online) {
  state.online = online;
  dom.connectionDot.classList.toggle("offline", !online);
  dom.connectionText.textContent = online ? "Connected to local server" : "Can't reach the server";
}

async function api(url, {method = "GET", body, signal} = {}) {
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    setConnection(false);
    throw httpError("Can't reach the server. Check that it's running, then try again.", 0);
  }

  setConnection(true);
  let data = null;
  try { data = await response.json(); } catch (error) { data = null; }

  if (!response.ok) {
    throw httpError((data && data.error) || `The server couldn't process the request (error ${response.status}).`, response.status);
  }
  return data || {};
}

/* ---------------------------------------------------------------------
   4. Markdown rendering
   --------------------------------------------------------------------- */

if (window.marked) window.marked.use({gfm: true, breaks: true});

// Markdown would eat the backslashes in \( \) and \[ \], so math is lifted out
// before parsing and put back afterwards for KaTeX. Single $...$ is not
// treated as math on purpose: it collides with prices.
const MATH_PATTERN = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)/g;
const HAS_MATH = /\$\$|\\\[|\\\(/;

function protectMath(text) {
  if (!HAS_MATH.test(text)) return {text, saved: []};
  const saved = [];
  const replaced = text.replace(MATH_PATTERN, match => {
    saved.push(match);
    return `@@MATH${saved.length - 1}@@`;
  });
  return {text: replaced, saved};
}

function renderMarkdown(text) {
  const source = text || "";
  if (!window.marked || !window.DOMPurify) {
    return `<p>${escapeHtml(source).replace(/\n/g, "<br>")}</p>`;
  }
  const {text: prepared, saved} = protectMath(source);
  let html = window.DOMPurify.sanitize(window.marked.parse(prepared), {
    USE_PROFILES: {html: true},
    FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select"],
    FORBID_ATTR: ["style"]
  });
  if (saved.length) {
    html = html.replace(/@@MATH(\d+)@@/g, (match, index) => escapeHtml(saved[Number(index)] || ""));
  }
  return html;
}

/**
 * Links open in a new tab. Remote images are never fetched automatically:
 * an image URL in a model reply can carry conversation data to a third-party
 * server, so they become plain links instead.
 */
function hardenContent(root) {
  $$("a[href]", root).forEach(link => {
    const href = link.getAttribute("href") || "";
    if (!/^(https?:|mailto:|#|\/(?!\/))/i.test(href)) {
      link.removeAttribute("href");
      return;
    }
    if (!href.startsWith("#")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  });

  $$("img", root).forEach(image => {
    const src = image.getAttribute("src") || "";
    if (/^data:image\/(png|jpe?g|gif|webp);/i.test(src) || /^\/(?!\/)/.test(src)) {
      image.loading = "lazy";
      return;
    }
    const safe = /^https?:\/\//i.test(src);
    image.replaceWith(h("a", {
      class: "remote-image",
      href: safe ? src : null,
      target: safe ? "_blank" : null,
      rel: safe ? "noopener noreferrer" : null,
      title: "Remote images are not loaded automatically"
    }, icon("image"), image.getAttribute("alt") || "Image"));
  });
}

function codeHead(language, raw) {
  const copy = h("button", {type: "button", class: "code-action", title: "Copy code"},
    icon("copy", "ico-copy"), icon("check", "ico-check"), h("span", {class: "code-action-label"}, "Copy"));
  copy.addEventListener("click", async () => {
    if (await copyText(raw)) flashCopied(copy);
    else toast("Couldn't copy. Select the code and copy it manually.", {type: "error"});
  });

  const download = h("button", {
    type: "button",
    class: "code-action",
    title: "Download code",
    onclick: () => downloadFile(`snippet.${extensionFor(language)}`, raw)
  }, icon("download"), h("span", {class: "code-action-label"}, "Download"));

  const canPreview = language === "html" || language === "svg";
  const preview = canPreview ? h("button", {
    type: "button",
    class: "code-action",
    title: "Preview in side panel",
    onclick: () => openPanel({title: `${language.toUpperCase()} preview`, code: raw, language, preview: true})
  }, icon("eye"), h("span", {class: "code-action-label"}, "Preview")) : null;

  return h("div", {class: "code-head"},
    h("span", {class: "code-lang"}, language || "text"),
    h("div", {class: "code-actions"}, preview, download, copy));
}

function decorateCodeBlock(pre, streaming) {
  if (pre.closest(".code-block")) return;
  const code = $("code", pre);
  const match = code && /language-([^\s]+)/.exec(code.className || "");
  const language = match ? match[1].toLowerCase() : "";
  const raw = (code || pre).textContent.replace(/\n$/, "");

  // Highlighting waits until a reply is complete so half-written code doesn't flicker.
  if (code && language && !streaming && window.hljs && window.hljs.getLanguage(language)) {
    try {
      code.innerHTML = window.hljs.highlight(raw, {language, ignoreIllegals: true}).value;
      code.classList.add("hljs");
    } catch (error) { /* leave unhighlighted */ }
  }

  const block = h("div", {class: "code-block"}, codeHead(language, raw));
  pre.replaceWith(block);
  block.append(pre);
}

function citeChip(number, source) {
  const label = source.domain ? `${source.title} (${source.domain})` : source.title;
  return h(source.url ? "a" : "span", {
    class: "cite",
    href: source.url,
    target: source.url ? "_blank" : null,
    rel: source.url ? "noopener noreferrer" : null,
    title: label,
    "aria-label": `Source ${number}: ${label}`
  }, String(number));
}

/** Turns [1], [2] in the reply text into citation chips when sources are available. */
function linkCitations(root, sources) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!/\[\d{1,2}\]/.test(node.nodeValue)) return NodeFilter.FILTER_REJECT;
      return node.parentElement && node.parentElement.closest("pre, code, a, .katex")
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    }
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  nodes.forEach(node => {
    const text = node.nodeValue;
    const fragment = document.createDocumentFragment();
    let last = 0;
    text.replace(/\[(\d{1,2})\]/g, (match, number, offset) => {
      const source = sources[Number(number) - 1];
      if (source) {
        fragment.append(text.slice(last, offset), citeChip(Number(number), source));
        last = offset + match.length;
      }
      return match;
    });
    if (last === 0) return;
    fragment.append(text.slice(last));
    node.replaceWith(fragment);
  });
}

function enhanceMarkdown(root, {streaming = false, sources = null} = {}) {
  hardenContent(root);
  $$("table", root).forEach(table => {
    const wrap = h("div", {class: "table-wrap"});
    table.replaceWith(wrap);
    wrap.append(table);
  });
  $$("pre", root).forEach(pre => decorateCodeBlock(pre, streaming));
  if (sources && sources.length) linkCitations(root, sources);

  if (!streaming && window.renderMathInElement && HAS_MATH.test(root.textContent)) {
    try {
      window.renderMathInElement(root, {
        delimiters: [
          {left: "$$", right: "$$", display: true},
          {left: "\\[", right: "\\]", display: true},
          {left: "\\(", right: "\\)", display: false}
        ],
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
        throwOnError: false
      });
    } catch (error) { /* math is optional */ }
  }
}

/* ---------------------------------------------------------------------
   5. Messages
   --------------------------------------------------------------------- */

const FILE_PATTERN = /<file name="([^"]*)">\n([\s\S]*?)\n<\/file>/g;

/** Files are sent as <file> blocks inside the prompt, so they survive a reload and work with any backend. */
function composePrompt(text, attachments = []) {
  const files = attachments
    .filter(item => item.kind === "text")
    .map(item => `<file name="${item.name.replace(/["\n]/g, "'")}">\n${item.content.replace(/<\/file>/gi, "<\\/file>")}\n</file>`);
  return [...files, (text || "").trim()].filter(Boolean).join("\n\n");
}

function splitAttachments(content) {
  const files = [];
  const text = (content || "").replace(FILE_PATTERN, (match, name, body) => {
    files.push({kind: "text", name, content: body.replace(/<\\\/file>/gi, "</file>")});
    return "";
  }).trim();
  return {files, text};
}

function normalizeSources(list) {
  if (!Array.isArray(list)) return null;
  const items = list.map(entry => {
    const source = typeof entry === "string" ? {url: entry} : (entry || {});
    let url = null;
    let domain = "";
    if (source.url) {
      try {
        const parsed = new URL(source.url, window.location.href);
        if (/^https?:$/.test(parsed.protocol)) {
          url = parsed.href;
          domain = parsed.hostname.replace(/^www\./, "");
        }
      } catch (error) { /* invalid URL: keep the title only */ }
    }
    return {title: source.title || domain || "Source", url, domain, snippet: source.snippet || ""};
  });
  return items.length ? items : null;
}

function normalizeFollowUps(list) {
  if (!Array.isArray(list)) return null;
  const items = list
    .map(entry => (typeof entry === "string" ? entry : (entry && (entry.text || entry.question)) || ""))
    .map(text => text.trim())
    .filter(Boolean)
    .slice(0, 4);
  return items.length ? items : null;
}

function normalizeMessage(raw) {
  const usage = raw.usage && typeof raw.usage === "object" ? raw.usage : null;
  // History loaded from the server (GET /api/conversation/<id>) carries
  // router info nested under `metadata` (see database.py get_messages),
  // not at the top level the way a fresh /api/chat response does. Without
  // this, the provider/model badge would render right after sending a
  // message but vanish the moment the conversation reloads from history
  // (e.g. after switching tabs and coming back to a backgrounded page).
  const metadata = raw.metadata && typeof raw.metadata === "object" ? raw.metadata : null;
  return {
    id: raw.id ?? null,
    role: raw.role === "user" ? "user" : "assistant",
    content: typeof raw.content === "string" ? raw.content : String(raw.content ?? ""),
    created_at: raw.created_at ?? raw.timestamp ?? null,
    reasoning: raw.reasoning || null,
    reasoningMs: raw.reasoning_ms || (usage && usage.reasoning_ms) || null,
    sources: normalizeSources(raw.sources),
    followUps: normalizeFollowUps(raw.follow_ups ?? raw.followUps),
    usage,
    provider: raw.provider || (metadata && metadata.provider) || (usage && usage.provider) || null,
    model: raw.model || (metadata && metadata.model) || (usage && usage.model) || null,
    latencyMs: raw.latency_ms || (metadata && metadata.latency_ms) || (usage && usage.latency_ms) || null,
    fallback_used: raw.fallback_used ?? (metadata && metadata.fallback_used) ?? (usage && usage.fallback_used) ?? null,
    attempts: raw.attempts || (metadata && metadata.attempts) || (usage && usage.attempts) || null
  };
}

function setWelcomeVisible(visible) {
  dom.welcome.hidden = !visible;
  dom.chat.classList.toggle("is-empty", visible);
}

function clearMessages() {
  state.messages.length = 0;
  dom.messages.replaceChildren();
  setWelcomeVisible(true);
  updateScrollButton();
}

function truncateFrom(index) {
  if (index < 0) return;
  state.messages.splice(index).forEach(message => {
    const node = nodeOf.get(message);
    if (node) node.remove();
  });
  $$(".message.transient", dom.messages).forEach(node => node.remove());
}

function setTitle(title) {
  state.title = title || "New chat";
  dom.chatTitle.textContent = state.title;
  document.title = state.title === "New chat" ? "AI Mind" : `${state.title} – AI Mind`;
}

/* ---- Scrolling ------------------------------------------------------ */

function scrollToBottom(force = false, smooth = false) {
  if (!force && !state.stick) return;
  if (force) state.stick = true;
  requestAnimationFrame(() => {
    if (smooth && dom.chat.scrollTo) dom.chat.scrollTo({top: dom.chat.scrollHeight, behavior: "smooth"});
    else dom.chat.scrollTop = dom.chat.scrollHeight;
  });
}

function updateScrollButton() {
  const gap = dom.chat.scrollHeight - dom.chat.scrollTop - dom.chat.clientHeight;
  state.stick = gap < 120;
  dom.scrollBottom.hidden = gap < 260 || state.messages.length === 0;
}

/* ---- Feedback, speech, copy --------------------------------------------- */

function feedbackKey(message) {
  return `${state.conversationId}:${state.messages.indexOf(message)}`;
}

function feedbackFor(message) {
  return state.feedback[feedbackKey(message)] || null;
}

async function setFeedback(message, value, button) {
  const key = feedbackKey(message);
  const next = state.feedback[key] === value ? null : value;
  if (next) state.feedback[key] = next;
  else delete state.feedback[key];
  store.setJSON("ai-mind-feedback", state.feedback);

  const node = button.closest(".message");
  $$(".act-up, .act-down", node).forEach(el => {
    el.setAttribute("aria-pressed", String(el.classList.contains(`act-${next}`)));
  });

  if (FEATURES.feedbackSync.on) {
    try {
      await api(API.feedback, {
        method: "POST",
        body: {conversation_id: state.conversationId, index: state.messages.indexOf(message), value: next}
      });
    } catch (error) {
      toast("Feedback is saved on this device only. The server didn't accept it.", {type: "error"});
    }
  }
}

function plainText(markdown) {
  const box = h("div", {html: renderMarkdown(markdown)});
  $$("pre", box).forEach(pre => pre.replaceWith(" Code block omitted. "));
  return box.textContent.replace(/\s+/g, " ").trim();
}

function stopSpeech() {
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  state.speaking = null;
  $$(".act-speak").forEach(button => button.setAttribute("aria-pressed", "false"));
}

function toggleSpeech(message, button) {
  if (!("speechSynthesis" in window)) {
    toast("Read aloud isn't supported in this browser.", {type: "error"});
    return;
  }
  if (state.speaking === message) {
    stopSpeech();
    return;
  }
  stopSpeech();
  const utterance = new SpeechSynthesisUtterance(plainText(message.content));
  utterance.lang = document.documentElement.lang || "en";
  utterance.onend = utterance.onerror = () => {
    if (state.speaking === message) stopSpeech();
  };
  state.speaking = message;
  button.setAttribute("aria-pressed", "true");
  window.speechSynthesis.speak(utterance);
}

async function copyLastReply() {
  const last = [...state.messages].reverse().find(message => message.role === "assistant");
  if (!last) {
    toast("There's no reply to copy yet.");
    return;
  }
  toast((await copyText(last.content)) ? "Copied the last reply." : "Couldn't copy the reply.");
}

/* ---- Message building blocks ----------------------------------------------- */

function actionButton({ico, label, cls = "", feature = null, onClick}) {
  const soon = feature && !FEATURES[feature].on;
  const button = h("button", {
    type: "button",
    class: `action ${cls}${soon ? " is-soon" : ""}`.trim(),
    title: soon ? `${label} (coming soon)` : label,
    "aria-label": label
  }, icon(ico));
  button.addEventListener("click", event => {
    event.stopPropagation();
    onClick(button);
  });
  return button;
}

function copyAction(getText) {
  const button = h("button", {type: "button", class: "action act-copy", title: "Copy", "aria-label": "Copy message"},
    icon("copy", "ico-copy"), icon("check", "ico-check"));
  button.addEventListener("click", async event => {
    event.stopPropagation();
    if (await copyText(getText())) flashCopied(button);
    else toast("Couldn't copy. Select the text and copy it manually.", {type: "error"});
  });
  return button;
}

function feedbackAction(message, value) {
  const button = actionButton({
    ico: "thumb",
    label: value === "up" ? "Good response" : "Bad response",
    cls: `act-${value}`,
    onClick: btn => setFeedback(message, value, btn)
  });
  button.setAttribute("aria-pressed", String(feedbackFor(message) === value));
  return button;
}

function fileChip(file) {
  const bytes = new Blob([file.content]).size;
  return h("button", {
    type: "button",
    class: "file-chip",
    title: `Open ${file.name}`,
    onclick: () => openPanel({title: file.name, code: file.content, language: file.name.split(".").pop().toLowerCase(), preview: false})
  }, icon("file"), h("span", {class: "file-chip-name"}, file.name), h("span", {class: "file-chip-size"}, formatBytes(bytes)));
}

function imageChip(attachment) {
  return h("div", {class: "file-chip image"},
    attachment.dataUrl ? h("img", {src: attachment.dataUrl, alt: ""}) : icon("image"),
    h("span", {class: "file-chip-name"}, attachment.name));
}

function thinkingIndicator() {
  return h("div", {class: "thinking", role: "status"},
    h("span", {class: "thinking-dots", "aria-hidden": "true"}, h("span"), h("span"), h("span")),
    h("span", {class: "thinking-label"}, "Thinking"));
}

/** Updates the wait message so a slow reply never looks frozen. */
function startStatusTicker(node) {
  const label = $(".thinking-label", node);
  if (!label) return () => {};
  const steps = [[10000, "Still working on it"], [30000, "This is taking longer than usual"]];
  const timers = steps.map(([delay, text]) => setTimeout(() => {
    if (label.isConnected) label.textContent = text;
  }, delay));
  return () => timers.forEach(clearTimeout);
}

function makeCollapsible(body, box) {
  if (body.scrollHeight <= 360) return;
  body.classList.add("collapsed");
  const toggle = h("button", {type: "button", class: "expand-toggle"}, "Show more");
  toggle.addEventListener("click", () => {
    const collapsed = body.classList.toggle("collapsed");
    toggle.textContent = collapsed ? "Show more" : "Show less";
  });
  box.append(toggle);
}

/* ---- User messages ---------------------------------------------------------- */

function renderUserMessage(message, animate) {
  const {files, text} = splitAttachments(message.content);
  const wrap = h("div", {class: "message-content-wrap"});

  const chips = [...files.map(fileChip), ...(message.attachments || []).map(imageChip)];
  if (chips.length) wrap.append(h("div", {class: "message-files"}, chips));

  if (text) {
    const body = h("div", {class: "user-text"}, text);
    const box = h("div", {class: "message-content"}, body);
    wrap.append(box);
    requestAnimationFrame(() => makeCollapsible(body, box));
  }

  wrap.append(h("div", {class: "message-actions", role: "group", "aria-label": "Message actions"},
    h("span", {class: "message-details"}, message.created_at ? h("span", null, timeOf(message.created_at)) : null),
    copyAction(() => text || message.content),
    actionButton({ico: "pencil", label: "Edit message", cls: "act-edit", feature: "editMessage", onClick: () => startEdit(message)})));

  return h("article", {class: `message user${animate ? "" : " no-anim"}`}, wrap);
}

function startEdit(message) {
  if (!requireFeature("editMessage") || state.isSending) return;
  const node = nodeOf.get(message);
  const box = node && $(".message-content", node);
  if (!box) return;

  const {files, text} = splitAttachments(message.content);
  const area = h("textarea", {class: "edit-area", rows: "3", "aria-label": "Edit your message"});
  area.value = text;
  const fit = () => {
    area.style.height = "auto";
    area.style.height = `${Math.min(area.scrollHeight, 320)}px`;
  };
  area.addEventListener("input", fit);

  const save = h("button", {type: "button", class: "btn primary sm"}, "Save and resend");
  const cancel = h("button", {type: "button", class: "btn ghost sm"}, "Cancel");
  const editor = h("div", {class: "edit-box"}, area, h("div", {class: "edit-actions"}, cancel, save));

  cancel.addEventListener("click", () => editor.replaceWith(box));
  save.addEventListener("click", () => {
    const next = composePrompt(area.value, files);
    if (!next) return;
    sendMessage(next, {editIndex: state.messages.indexOf(message)});
  });

  box.replaceWith(editor);
  fit();
  area.focus();
}

/* ---- Assistant messages ------------------------------------------------------ */

function renderSources(sources) {
  return h("div", {class: "sources"},
    h("div", {class: "sources-head"}, icon("globe"), h("span", null, sources.length === 1 ? "1 source" : `${sources.length} sources`)),
    h("div", {class: "sources-list"}, sources.map((source, index) => h(source.url ? "a" : "div", {
      class: "source-card",
      href: source.url,
      target: source.url ? "_blank" : null,
      rel: source.url ? "noopener noreferrer" : null,
      title: source.snippet || source.title
    },
      h("span", {class: "source-index"}, String(index + 1)),
      h("span", {class: "source-text"},
        h("span", {class: "source-title"}, source.title),
        source.domain ? h("span", {class: "source-domain"}, source.domain) : null)))));
}

function syncReasoning(wrap, content, message, streaming) {
  let details = $(".reasoning", wrap);
  if (!message.reasoning) {
    if (details) details.remove();
    return;
  }
  if (!details) {
    const summary = h("summary", null,
      icon("bulb"), h("span", {class: "reasoning-label"}), icon("chevron-right", "reasoning-chevron"));
    summary.addEventListener("click", () => { details.dataset.userToggled = "1"; });
    details = h("details", {class: "reasoning"}, summary, h("div", {class: "reasoning-body markdown"}));
    wrap.insertBefore(details, $(".sources", wrap) || content);
  }

  const thinking = streaming && !message.content;
  $(".reasoning-label", details).textContent = thinking
    ? "Thinking"
    : message.reasoningMs ? `Thought for ${formatDuration(message.reasoningMs)}` : "Reasoning";
  details.classList.toggle("is-thinking", thinking);
  if (!details.dataset.userToggled) details.open = thinking;

  const body = $(".reasoning-body", details);
  body.innerHTML = renderMarkdown(message.reasoning);
  hardenContent(body);
}

function syncSources(wrap, content, message) {
  const existing = $(".sources", wrap);
  if (!message.sources || !message.sources.length) {
    if (existing) existing.remove();
    return;
  }
  const signature = message.sources.map(source => source.url || source.title).join("|");
  if (existing && existing.dataset.signature === signature) return;
  const fresh = renderSources(message.sources);
  fresh.dataset.signature = signature;
  if (existing) existing.replaceWith(fresh);
  else wrap.insertBefore(fresh, content);
}

function syncFollowUps(wrap, message, streaming) {
  const slot = $(".followups-slot", wrap);
  if (!slot) return;
  if (streaming || message.pending || !message.followUps) {
    slot.replaceChildren();
    return;
  }
  if (slot.childElementCount) return;
  slot.append(h("div", {class: "followups", role: "group", "aria-label": "Related questions"},
    h("div", {class: "followups-head"}, icon("list"), h("span", null, "Related")),
    message.followUps.map(question => h("button", {
      type: "button",
      class: "followup",
      onclick: () => sendMessage(question)
    }, h("span", null, question), icon("plus")))));
}

function syncDetails(node, message) {
  // Update provider & model badge next to action icons
  const badge = $(".ai-badge", node);
  if (badge) {
    if (message.provider || message.model) {
      const label = [message.provider, message.model].filter(Boolean).join(" · ");
      badge.textContent = label;
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  const details = $(".message-details", node);
  if (!details) return;
  details.replaceChildren();
  const parts = [];
  if (message.created_at) parts.push(timeOf(message.created_at));
  if (message.latencyMs) parts.push(formatDuration(message.latencyMs));
  const tokens = message.usage && (message.usage.output_tokens ?? message.usage.completion_tokens);
  if (tokens) parts.push(`${tokens} tokens`);
  parts.forEach(part => details.append(h("span", null, part)));
}

function updateAssistantNode(node, message, {streaming = false} = {}) {
  const wrap = $(".message-content-wrap", node);
  const content = $(".message-content", wrap);
  const markdown = $(".markdown", content);

  syncSources(wrap, content, message);
  syncReasoning(wrap, content, message, streaming);

  markdown.innerHTML = renderMarkdown(message.content);
  enhanceMarkdown(markdown, {streaming, sources: message.sources});

  if (message.content || message.reasoning) {
    const waiting = $(".thinking", content);
    if (waiting) waiting.remove();
  }

  node.classList.toggle("is-streaming", streaming);
  node.classList.toggle("is-stopped", !!message.stopped);
  syncFollowUps(wrap, message, streaming);
  syncDetails(node, message);
}

function renderAssistantMessage(message, animate) {
  const avatar = h("div", {class: "message-avatar", "aria-hidden": "true"}, icon("sparkle"));
  const content = h("div", {class: "message-content"}, h("div", {class: "markdown"}));
  const actions = h("div", {class: "message-actions", role: "group", "aria-label": "Message actions"},
    copyAction(() => message.content),
    feedbackAction(message, "up"),
    feedbackAction(message, "down"),
    actionButton({ico: "volume", label: "Read aloud", cls: "act-speak", onClick: button => toggleSpeech(message, button)}),
    actionButton({ico: "refresh", label: "Regenerate", cls: "act-regenerate", feature: "regenerate", onClick: () => regenerate(message)}),
    h("span", {class: "ai-badge"}),
    h("span", {class: "message-details"}));
  const wrap = h("div", {class: "message-content-wrap"},
    content, actions, h("div", {class: "followups-slot"}), h("div", {class: "stopped-note"}, "Response stopped"));

  const node = h("article", {
    class: `message assistant${animate ? "" : " no-anim"}${message.pending ? " is-pending" : ""}`
  }, avatar, wrap);

  if (message.pending) content.prepend(thinkingIndicator());
  updateAssistantNode(node, message);
  return node;
}

function renderMessage(message, {animate = true} = {}) {
  return message.role === "user" ? renderUserMessage(message, animate) : renderAssistantMessage(message, animate);
}

function appendMessage(message, {animate = true} = {}) {
  state.messages.push(message);
  const node = renderMessage(message, {animate});
  nodeOf.set(message, node);
  dom.messages.append(node);
  setWelcomeVisible(false);
  return node;
}

function renderErrorMessage(error, onRetry) {
  return h("article", {class: "message assistant error transient", role: "alert"},
    h("div", {class: "message-avatar", "aria-hidden": "true"}, icon("alert")),
    h("div", {class: "message-content-wrap"},
      h("div", {class: "message-content"},
        h("p", {class: "error-title"}, "Couldn't get a reply"),
        h("p", {class: "error-detail"}, (error && error.message) || "Something went wrong on the server."),
        h("div", {class: "error-actions"},
          h("button", {type: "button", class: "btn sm", onclick: onRetry}, icon("refresh"), "Try again")))));
}

/* ---------------------------------------------------------------------
   6. Sending, streaming, stopping
   --------------------------------------------------------------------- */

const renderQueue = new Map();

/** Coalesces streaming updates to one render per animation frame. */
function scheduleRender(node, message) {
  if (renderQueue.has(node)) return;
  renderQueue.set(node, requestAnimationFrame(() => {
    renderQueue.delete(node);
    updateAssistantNode(node, message, {streaming: true});
    scrollToBottom();
  }));
}

function cancelRender(node) {
  if (!renderQueue.has(node)) return;
  cancelAnimationFrame(renderQueue.get(node));
  renderQueue.delete(node);
}

function normalizeReply(data) {
  const usage = data.usage && typeof data.usage === "object" ? data.usage : null;
  return {
    content: typeof data.response === "string" ? data.response : "",
    conversationId: data.conversation_id ?? null,
    title: data.title || null,
    reasoning: data.reasoning || null,
    reasoningMs: data.reasoning_ms || (usage && usage.reasoning_ms) || null,
    sources: normalizeSources(data.sources),
    followUps: normalizeFollowUps(data.follow_ups),
    usage,
    id: data.message_id ?? null,
    provider: data.provider || null,
    model: data.model || null,
    projectId: data.project_id ?? null,
    projectName: data.project_name || null
  };
}

function adoptConversation(reply) {
  if (reply.conversationId === null || reply.conversationId === undefined || reply.conversationId === "") return;
  state.conversationId = reply.conversationId;
  store.set("conversation_id", String(reply.conversationId));
  state.newChatProjectId = null;
  renderProjectPill(reply.projectId ? {id: reply.projectId, name: reply.projectName} : null);
}

function buildPayload(content, {regenerate, editIndex, files}) {
  const payload = {message: content, conversation_id: state.conversationId};
  const options = {};

  // Only meaningful when starting a brand-new conversation - the backend
  // only honors project_id at creation time (see /api/chat).
  if (!state.conversationId && state.newChatProjectId) {
    payload.project_id = state.newChatProjectId;
  }

  if (FEATURES.modelSettings.on) {
    options.model = state.settings.model;
    options.temperature = state.settings.temperature;
  }
  if (FEATURES.webSearch.on && state.tools.webSearch) options.web_search = true;
  if (FEATURES.deepThink.on && state.tools.deepThink) options.reasoning = true;
  if (state.mode) options.mode = state.mode;
  if (FEATURES.customInstructions.on && (state.settings.about || state.settings.style)) {
    options.instructions = {about: state.settings.about, style: state.settings.style};
  }
  if (FEATURES.memoryToggle.on) options.use_memory = state.settings.memory;
  if (Object.keys(options).length) payload.options = options;

  if (regenerate && FEATURES.regenerate.on) payload.regenerate = true;
  if (editIndex !== null && FEATURES.editMessage.on) payload.edit_index = editIndex;

  const binary = files.filter(file => file.kind !== "text");
  if (binary.length && FEATURES.fileUpload.on) {
    payload.attachments = binary.map(file => ({name: file.name, type: file.type, size: file.size, data: file.dataUrl}));
  }
  return payload;
}

async function fetchReply(payload, signal, onPartial) {
  if (!FEATURES.streaming.on) {
    const data = await api(API.chat, {method: "POST", body: payload, signal});
    if (!data.response) throw new Error("The AI returned an empty response.");
    return normalizeReply(data);
  }
  return streamReply(payload, signal, onPartial);
}

/** Reads server-sent events from /api/chat/stream. See FEATURES.streaming for the event shapes. */
async function streamReply(payload, signal, onPartial) {
  let response;
  try {
    response = await fetch(API.chatStream, {
      method: "POST",
      headers: {"Content-Type": "application/json", Accept: "text/event-stream"},
      body: JSON.stringify(payload),
      signal
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    setConnection(false);
    throw httpError("Can't reach the server. Check that it's running, then try again.", 0);
  }

  if (!response.ok || !response.body) {
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    throw httpError((data && data.error) || `The server couldn't process the request (error ${response.status}).`, response.status);
  }
  setConnection(true);

  const reply = {
    content: "", reasoning: null, reasoningMs: null, sources: null, followUps: null,
    usage: null, title: null, conversationId: null, id: null
  };

  const apply = event => {
    switch (event.type) {
      case "reasoning":
        reply.reasoning = (reply.reasoning || "") + (event.content ?? "");
        break;
      case "sources":
        reply.sources = normalizeSources(event.sources);
        break;
      case "meta":
      case "done":
        reply.conversationId = event.conversation_id ?? reply.conversationId;
        reply.title = event.title || reply.title;
        reply.followUps = normalizeFollowUps(event.follow_ups) || reply.followUps;
        reply.usage = event.usage || reply.usage;
        reply.reasoningMs = event.reasoning_ms || reply.reasoningMs;
        reply.id = event.message_id ?? reply.id;
        break;
      case "error":
        throw httpError(event.error || "The reply was interrupted.", 500);
      default:
        reply.content += event.content ?? event.delta ?? event.text ?? "";
    }
    onPartial(reply);
  };

  const consume = chunk => {
    const data = chunk.split(/\r?\n/)
      .filter(line => line.startsWith("data:"))
      .map(line => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data || data === "[DONE]") return;
    let event;
    try { event = JSON.parse(data); } catch (error) { event = {type: "delta", content: data}; }
    if (typeof event === "string") event = {type: "delta", content: event};
    apply(event);
  };

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    const parts = buffer.split(/\r?\n\r?\n/);
    buffer = parts.pop();
    parts.forEach(consume);
  }
  if (buffer.trim()) consume(buffer);

  if (!reply.content) throw new Error("The AI returned an empty response.");
  return reply;
}

/**
 * The backend answers in one piece, so the reply is revealed gradually on the
 * client. This is cosmetic and can be turned off in Settings. It stops
 * immediately when the person presses Stop.
 */
function revealText(node, message, full, signal) {
  return new Promise(resolve => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!state.settings.reveal || reduceMotion || full.length < 60) {
      message.content = full;
      updateAssistantNode(node, message);
      resolve("done");
      return;
    }

    const duration = clamp(full.length * 2.2, 700, 6000);
    const start = performance.now();
    let frame = 0;

    const onAbort = () => {
      cancelAnimationFrame(frame);
      resolve("aborted");
    };
    signal.addEventListener("abort", onAbort, {once: true});

    const tick = now => {
      const progress = Math.min(1, (now - start) / duration);
      let cut = Math.floor(full.length * progress);
      if (cut < full.length && cut > 0) {
        const code = full.charCodeAt(cut - 1);
        if (code >= 0xd800 && code <= 0xdbff) cut -= 1; // don't split a surrogate pair
      }
      message.content = progress >= 1 ? full : full.slice(0, cut);
      updateAssistantNode(node, message, {streaming: progress < 1});
      scrollToBottom();
      if (progress < 1) {
        frame = requestAnimationFrame(tick);
      } else {
        signal.removeEventListener("abort", onAbort);
        resolve("done");
      }
    };
    frame = requestAnimationFrame(tick);
  });
}

function titleFromContent(content) {
  const {files, text} = splitAttachments(content);
  const source = (text || (files[0] && files[0].name) || "New chat").replace(/\s+/g, " ");
  return truncate(source, 42);
}

async function refreshAfterReply(epoch) {
  await loadConversations();
  if (epoch !== state.epoch) return;
  const entry = state.conversations.find(conversation => sameId(conversation.id, state.conversationId));
  if (entry && entry.title) setTitle(entry.title);
}

function setSending(value) {
  state.isSending = value;
  document.body.classList.toggle("is-generating", value);
  const button = dom.sendButton;
  button.replaceChildren(icon(value ? "stop" : "arrow-up"));
  button.classList.toggle("is-stop", value);
  button.setAttribute("aria-label", value ? "Stop generating" : "Send message");
  button.title = value ? "Stop generating (Esc)" : "Send message";
  updateComposerState();
}

function stopGenerating() {
  if (state.abort) state.abort.abort();
}

async function sendMessage(content, {regenerate = false, editIndex = null, files = []} = {}) {
  content = (content || "").trim();
  if (state.isSending || (!content && !files.length)) return;

  const epoch = state.epoch;
  closeMenu();
  stopSpeech();

  let userMessage = null;
  if (regenerate) {
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === "assistant") {
      state.messages.pop();
      const old = nodeOf.get(last);
      if (old) old.remove();
    }
  } else {
    if (editIndex !== null) truncateFrom(editIndex);
    userMessage = {
      role: "user",
      content,
      created_at: new Date().toISOString(),
      attachments: files.filter(file => file.kind !== "text")
    };
    appendMessage(userMessage);
    resetComposer();
  }

  const firstTurn = !regenerate && state.messages.length === 1;
  $$(".followups-slot", dom.messages).forEach(slot => slot.replaceChildren());
  $$(".message.transient", dom.messages).forEach(node => node.remove());

  const message = {role: "assistant", content: "", pending: true, created_at: new Date().toISOString()};
  const node = renderMessage(message);
  dom.messages.append(node);
  scrollToBottom(true);

  const stopTicker = startStatusTicker(node);
  const controller = new AbortController();
  state.abort = controller;
  setSending(true);

  const started = performance.now();
  let outcome = "done";
  let failure = null;
  let result = null;
  let streamed = false;

  try {
    result = await fetchReply(buildPayload(content, {regenerate, editIndex, files}), controller.signal, partial => {
      streamed = true;
      message.content = partial.content;
      message.reasoning = partial.reasoning;
      message.sources = partial.sources;
      scheduleRender(node, message);
    });
    if (epoch !== state.epoch) return;

    adoptConversation(result);
    message.reasoning = result.reasoning;
    message.reasoningMs = result.reasoningMs;
    message.sources = result.sources;
    message.followUps = result.followUps;
    message.usage = result.usage;
    message.id = result.id;
    message.provider = result.provider;
    message.model = result.model;

    if (streamed) {
      cancelRender(node);
      message.content = result.content;
    } else {
      outcome = await revealText(node, message, result.content, controller.signal);
    }
    if (epoch !== state.epoch) return;
  } catch (error) {
    if (epoch !== state.epoch) return;
    if (error && error.name === "AbortError") {
      outcome = "aborted";
    } else {
      outcome = "error";
      failure = error;
    }
  } finally {
    stopTicker();
    cancelRender(node);
    if (state.abort === controller) state.abort = null;
    if (!state.abort) setSending(false);
  }

  if (outcome === "error") {
    node.remove();
    const errorNode = renderErrorMessage(failure, () => {
      errorNode.remove();
      if (regenerate) {
        sendMessage(content, {regenerate: true, files});
        return;
      }
      const index = state.messages.indexOf(userMessage);
      truncateFrom(index);
      sendMessage(content, {files, editIndex: editIndex !== null ? index : null});
    });
    dom.messages.append(errorNode);
    scrollToBottom(true);
    announce("The reply failed. You can try again.");
    return;
  }

  if (outcome === "aborted" && !message.content) {
    node.remove();
    return;
  }

  message.pending = false;
  message.stopped = outcome === "aborted";
  message.latencyMs = performance.now() - started;
  state.messages.push(message);
  nodeOf.set(message, node);
  node.classList.remove("is-pending");
  updateAssistantNode(node, message);
  scrollToBottom();
  announce(message.stopped ? "Response stopped." : "AI Mind replied.");
  if (!isCoarsePointer()) dom.input.focus();

  if (result) {
    if (firstTurn) setTitle(result.title || titleFromContent(content));
    else if (result.title) setTitle(result.title);
    await refreshAfterReply(epoch);
  }
}

function regenerate(message) {
  if (!requireFeature("regenerate") || state.isSending) return;
  const index = state.messages.indexOf(message);
  const previous = state.messages[index - 1];
  if (index !== state.messages.length - 1 || !previous || previous.role !== "user") return;
  sendMessage(previous.content, {regenerate: true});
}

/* ---------------------------------------------------------------------
   7. Conversations and sidebar
   --------------------------------------------------------------------- */

function setSidebarOpen(open) {
  dom.shell.classList.toggle("history-open", open);
  if (mobileQuery.matches) dom.sidebarToggle.setAttribute("aria-expanded", String(open));
}

function setSidebarCollapsed(collapsed) {
  dom.shell.classList.toggle("sidebar-collapsed", collapsed);
  store.set("ai-mind-sidebar", collapsed ? "collapsed" : "open");
  if (!mobileQuery.matches) dom.sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
}

function toggleSidebar() {
  if (mobileQuery.matches) setSidebarOpen(!dom.shell.classList.contains("history-open"));
  else setSidebarCollapsed(!dom.shell.classList.contains("sidebar-collapsed"));
}

function renderListError(error) {
  dom.conversationList.replaceChildren(h("div", {class: "conversation-empty"},
    h("p", null, error.message || "Couldn't load your chats."),
    h("button", {type: "button", class: "btn sm", onclick: () => loadConversations()}, "Try again")));
}

function conversationItem(conversation) {
  const active = sameId(conversation.id, state.conversationId);
  const pinned = state.pinned.has(String(conversation.id));
  const title = conversation.title || "New chat";

  const more = h("button", {
    type: "button",
    class: "conversation-action",
    title: "Chat options",
    "aria-label": `Options for ${title}`,
    "aria-haspopup": "menu",
    "aria-expanded": "false"
  }, icon("more"));
  more.addEventListener("click", event => {
    event.stopPropagation();
    openMenu(more, conversationMenuItems(conversation, {anchor: more}), {placement: "bottom-end", minWidth: 200});
  });

  let projectMark = null;
  if (conversation.project_id) {
    projectMark = icon("folder", "project-mark");
    projectMark.setAttribute("title", conversation.project_name || "In a project");
  }

  const item = h("div", {
    class: `conversation-item${active ? " active" : ""}`,
    role: "button",
    tabindex: "0",
    title,
    "aria-current": active ? "true" : null,
    dataset: {id: String(conversation.id)}
  },
    pinned ? icon("pin", "pin-mark") : null,
    projectMark,
    h("span", {class: "conversation-title"}, title),
    h("span", {class: "conversation-actions"}, more));

  item.addEventListener("click", () => selectConversation(conversation.id));
  item.addEventListener("keydown", event => {
    if (event.target === item && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      selectConversation(conversation.id);
    }
  });
  return item;
}

function renderConversations() {
  const list = dom.conversationList;
  list.replaceChildren();
  const query = dom.historySearch.value.trim();
  dom.historyCount.textContent = state.conversations.length ? String(state.conversations.length) : "";

  if (!state.conversations.length) {
    list.append(h("div", {class: "conversation-empty"}, query
      ? "No chats match your search."
      : "No chats yet. Start a conversation and it will appear here."));
    return;
  }

  const byRecent = [...state.conversations].sort((a, b) => {
    const left = parseDate(a.updated_at);
    const right = parseDate(b.updated_at);
    return (right ? right.getTime() : 0) - (left ? left.getTime() : 0);
  });

  const group = (label, items) => h("section", {class: "conversation-group"},
    h("h3", {class: "conversation-group-title"}, label),
    items.map(conversationItem));

  if (query) {
    list.append(group("Results", byRecent));
    return;
  }

  const pinned = byRecent.filter(conversation => state.pinned.has(String(conversation.id)));
  const rest = byRecent.filter(conversation => !state.pinned.has(String(conversation.id)));
  if (pinned.length) list.append(group("Pinned", pinned));

  const groups = new Map();
  rest.forEach(conversation => {
    const label = groupLabel(parseDate(conversation.updated_at));
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(conversation);
  });
  groups.forEach((items, label) => list.append(group(label, items)));
}

async function loadConversations() {
  const token = ++state.listToken;
  const params = new URLSearchParams({search: dom.historySearch.value.trim()});
  try {
    const data = await api(`${API.conversations}?${params}`);
    if (token !== state.listToken) return state.conversations;
    state.conversations = data.conversations || [];
    renderConversations();
    return state.conversations;
  } catch (error) {
    if (token === state.listToken && error.name !== "AbortError") renderListError(error);
    return [];
  }
}

function resetToNewChat() {
  state.epoch += 1;
  state.conversationId = null;
  state.newChatProjectId = null;
  store.remove("conversation_id");
  clearMessages();
  setTitle("New chat");
  renderProjectPill(null);
  renderConversations();
}

function newChat() {
  stopGenerating();
  stopSpeech();
  saveDraft();
  resetToNewChat();
  state.attachments = [];
  renderAttachments();
  dom.input.value = loadDraft();
  autoResize();
  updateComposerState();
  setSidebarOpen(false);
  closePanel();
  if (!isCoarsePointer()) dom.input.focus();
}

async function selectConversation(id) {
  if (sameId(id, state.conversationId) && state.messages.length) {
    setSidebarOpen(false);
    return;
  }

  stopGenerating();
  stopSpeech();
  saveDraft();
  const token = ++state.loadToken;

  try {
    const data = await api(API.conversation(id));
    if (token !== state.loadToken) return;

    state.epoch += 1;
    state.conversationId = id;
    store.set("conversation_id", String(id));

    state.messages.length = 0;
    dom.messages.replaceChildren();
    (data.messages || [])
      .filter(raw => raw && raw.role !== "system")
      .forEach(raw => appendMessage(normalizeMessage(raw), {animate: false}));

    setWelcomeVisible(state.messages.length === 0);
    setTitle(data.title || "New chat");
    state.newChatProjectId = null;
    renderProjectPill(data.project_id ? {id: data.project_id, name: data.project_name} : null);
    state.attachments = [];
    renderAttachments();
    dom.input.value = loadDraft();
    autoResize();
    updateComposerState();
    renderConversations();
    setSidebarOpen(false);
    closePanel();
    scrollToBottom(true);
  } catch (error) {
    if (token !== state.loadToken) return;
    if (error.status === 404) {
      resetToNewChat();
      toast("That chat no longer exists.", {type: "error"});
    } else {
      toast(error.message, {type: "error"});
    }
  }
}

/* ---- Rename, pin, delete, export ------------------------------------------- */

function inlineEdit(target, {value, maxLength = 120, onCommit}) {
  const input = h("input", {
    class: "inline-input",
    value,
    maxlength: String(maxLength),
    "aria-label": "Chat title"
  });
  target.replaceWith(input);
  input.focus();
  input.select();

  let finished = false;
  const finish = commit => {
    if (finished) return;
    finished = true;
    if (input.isConnected) input.replaceWith(target);
    const next = input.value.trim();
    if (commit && next && next !== value) onCommit(next);
  };
  input.addEventListener("keydown", event => {
    event.stopPropagation();
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("click", event => event.stopPropagation());
  input.addEventListener("blur", () => finish(true));
}

async function renameConversation(id, title) {
  try {
    const data = await api(API.conversationItem(id), {method: "PATCH", body: {title}});
    const saved = (data.conversation && data.conversation.title) || title;
    if (sameId(id, state.conversationId)) setTitle(saved);
    await loadConversations();
  } catch (error) {
    toast(error.message, {type: "error"});
    await loadConversations();
  }
}

function startRename(conversation) {
  const row = $$(".conversation-item").find(item => item.dataset.id === String(conversation.id));
  const target = row && $(".conversation-title", row);
  if (!target) return;
  inlineEdit(target, {value: conversation.title || "", onCommit: title => renameConversation(conversation.id, title)});
}

function togglePin(id) {
  const key = String(id);
  if (state.pinned.has(key)) state.pinned.delete(key);
  else state.pinned.add(key);
  store.setJSON("ai-mind-pinned", [...state.pinned]);
  renderConversations();
}

async function removeConversation(conversation) {
  const title = conversation.title || "this chat";
  const confirmed = await confirmDialog({
    title: "Delete chat?",
    body: `"${truncate(title, 60)}" will be permanently deleted. This can't be undone.`,
    confirmLabel: "Delete",
    danger: true
  });
  if (!confirmed) return;

  try {
    await api(API.conversationItem(conversation.id), {method: "DELETE"});
  } catch (error) {
    toast(error.message, {type: "error"});
    return;
  }

  state.pinned.delete(String(conversation.id));
  store.setJSON("ai-mind-pinned", [...state.pinned]);
  clearDraft(String(conversation.id));
  if (sameId(conversation.id, state.conversationId)) {
    stopGenerating();
    resetToNewChat();
  }
  await loadConversations();
  toast("Chat deleted.");
}

async function shareConversation(id) {
  if (!requireFeature("share")) return;
  try {
    const data = await api(API.share(id), {method: "POST"});
    if (data.url && await copyText(data.url)) toast("Share link copied.");
    else toast("The chat was shared, but the server didn't return a link.");
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

async function archiveConversation(id) {
  if (!requireFeature("archive")) return;
  try {
    await api(API.archive(id), {method: "POST"});
    if (sameId(id, state.conversationId)) resetToNewChat();
    await loadConversations();
    toast("Chat archived.");
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

function conversationToMarkdown(title, messages) {
  const lines = [`# ${title || "Chat"}`, ""];
  messages.forEach(message => {
    lines.push(message.role === "user" ? "## You" : "## AI Mind", "", message.content, "");
  });
  return lines.join("\n");
}

async function exportConversation(id, format) {
  try {
    let title = state.title;
    let messages = state.messages;
    if (!sameId(id, state.conversationId) || !messages.length) {
      const data = await api(API.conversation(id));
      title = data.title || "Chat";
      messages = (data.messages || []).map(normalizeMessage);
    }
    const base = fileSlug(title);
    if (format === "json") {
      const payload = {title, exported_at: new Date().toISOString(), messages: messages.map(m => ({role: m.role, content: m.content, created_at: m.created_at}))};
      downloadFile(`${base}.json`, JSON.stringify(payload, null, 2), "application/json");
    } else {
      downloadFile(`${base}.md`, conversationToMarkdown(title, messages), "text/markdown");
    }
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

function exportCurrent(format) {
  if (state.conversationId === null || state.conversationId === undefined || !state.messages.length) {
    toast("Send a message first, then you can export the chat.");
    return;
  }
  exportConversation(state.conversationId, format);
}

function conversationMenuItems(conversation, {includePrint = false, anchor} = {}) {
  const pinned = state.pinned.has(String(conversation.id));
  return [
    {label: "Rename", ico: "pencil", onClick: () => startRename(conversation)},
    {label: pinned ? "Unpin" : "Pin", ico: "pin", onClick: () => togglePin(conversation.id)},
    {label: "Move to project", ico: "folder", onClick: () => openMoveToProjectMenu(anchor, conversation)},
    {label: "Share", ico: "share", feature: "share", onClick: () => shareConversation(conversation.id)},
    {divider: true},
    {label: "Export as Markdown", ico: "download", onClick: () => exportConversation(conversation.id, "md")},
    {label: "Export as JSON", ico: "code", onClick: () => exportConversation(conversation.id, "json")},
    includePrint ? {label: "Print or save as PDF", ico: "print", onClick: () => window.print()} : null,
    {label: "Archive", ico: "archive", feature: "archive", onClick: () => archiveConversation(conversation.id)},
    {divider: true},
    {label: "Delete", ico: "trash", danger: true, onClick: () => removeConversation(conversation)}
  ].filter(Boolean);
}

function openMoveToProjectMenu(anchor, conversation) {
  if (!anchor) return;
  const items = [{heading: "Move to project"}];

  if (!state.projects.length) {
    items.push({label: "No projects yet — create one", ico: "plus", onClick: () => openProjects()});
  } else {
    state.projects.forEach(project => {
      items.push({
        label: project.name,
        ico: "folder",
        checked: sameId(conversation.project_id, project.id),
        onClick: () => moveConversationToProject(conversation, project.id)
      });
    });
  }

  if (conversation.project_id) {
    items.push({divider: true});
    items.push({label: "Remove from project", ico: "x", onClick: () => moveConversationToProject(conversation, null)});
  }

  openMenu(anchor, items, {placement: "bottom-end", minWidth: 220});
}

async function moveConversationToProject(conversation, projectId) {
  try {
    await api(API.conversationItem(conversation.id), {method: "PATCH", body: {project_id: projectId}});
    await loadConversations();
    if (sameId(conversation.id, state.conversationId)) {
      const project = projectId ? state.projects.find(item => sameId(item.id, projectId)) : null;
      renderProjectPill(project ? {id: project.id, name: project.name} : null);
    }
    toast(projectId ? "Moved to project." : "Removed from project.");
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

function currentConversation() {
  if (state.conversationId === null || state.conversationId === undefined) return null;
  return state.conversations.find(conversation => sameId(conversation.id, state.conversationId))
    || {id: state.conversationId, title: state.title};
}

function openChatMenu() {
  const conversation = currentConversation();
  if (!conversation) {
    toast("Send a message first to see chat options.");
    return;
  }
  openMenu(dom.chatMenuButton, conversationMenuItems(conversation, {includePrint: true, anchor: dom.chatMenuButton}), {placement: "bottom-end", minWidth: 220});
}

function startTitleRename() {
  const conversation = currentConversation();
  if (!conversation) return;
  inlineEdit(dom.chatTitle, {value: state.title, onCommit: title => renameConversation(conversation.id, title)});
}

/* ---------------------------------------------------------------------
   7b. Projects
   --------------------------------------------------------------------- */

async function loadProviders() {
  try {
    const data = await api(API.aiRouterStatus);
    state.providers = Array.isArray(data.providers) ? data.providers : [];
  } catch (error) {
    console.error("Failed to load provider status", error);
  }
  applySettings();
}

async function loadProjects() {
  try {
    const data = await api(API.projects);
    state.projects = Array.isArray(data.projects) ? data.projects : [];
  } catch (error) {
    console.error("Failed to load projects", error);
  }
  return state.projects;
}

function renderProjectPill(project) {
  if (!dom.projectPill) return;
  if (project && project.id) {
    dom.projectPillLabel.textContent = project.name;
    dom.projectPill.hidden = false;
  } else {
    dom.projectPill.hidden = true;
  }
}

function projectFieldset({name = "", description = "", instructions = ""} = {}) {
  const nameInput = h("input", {
    class: "field", type: "text", maxlength: "80",
    placeholder: "Project name", "aria-label": "Project name"
  });
  nameInput.value = name;

  const descInput = h("textarea", {
    class: "field textarea", rows: "2",
    placeholder: "What is this project for? (optional)",
    "aria-label": "Project description"
  });
  descInput.value = description;

  const instructionsInput = h("textarea", {
    class: "field textarea", rows: "5",
    placeholder: "Instructions the assistant should follow in every chat in this project (optional)",
    "aria-label": "Project instructions"
  });
  instructionsInput.value = instructions;

  return {nameInput, descInput, instructionsInput};
}

function renderProjects(view = "list", payload = null) {
  if (view === "list") {
    const cards = state.projects.map(project => {
      const openButton = h("button", {
        type: "button", class: "project-card-open",
        onclick: () => openProjectDetail(project.id)
      },
        icon("folder"),
        h("div", {class: "project-card-text"},
          h("div", {class: "project-card-name"}, project.name),
          h("div", {class: "project-card-desc"}, project.description || "No description yet"),
          h("div", {class: "project-card-count"},
            `${project.conversation_count} chat${project.conversation_count === 1 ? "" : "s"}`)));

      const moreButton = h("button", {
        type: "button", class: "icon-button", "aria-label": `More for ${project.name}`
      }, icon("more"));
      moreButton.addEventListener("click", event => {
        event.stopPropagation();
        openMenu(moreButton, [
          {label: "Open", ico: "folder", onClick: () => openProjectDetail(project.id)},
          {label: "Edit", ico: "pencil", onClick: () => renderProjects("edit", project)},
          {divider: true},
          {label: "Delete", ico: "trash", danger: true, onClick: () => deleteProjectFlow(project)}
        ], {placement: "bottom-end"});
      });

      return h("div", {class: "project-card"}, openButton, moreButton);
    });

    const body = state.projects.length
      ? h("div", {class: "project-grid"}, cards)
      : h("div", {class: "project-empty"},
          icon("folder"),
          h("p", null, "No projects yet."),
          h("p", {class: "project-empty-text"}, "Group related chats under shared instructions, the way you would in Claude or ChatGPT."));

    dom.projects.replaceChildren(h("div", {class: "dialog-form projects-shell"},
      h("div", {class: "dialog-head"},
        h("h2", {class: "dialog-title"}, "Projects"),
        h("div", {class: "dialog-head-actions"},
          h("button", {type: "button", class: "btn primary sm", onclick: () => renderProjects("edit")},
            icon("plus"), "New project"),
          h("button", {
            type: "button", class: "icon-button", "aria-label": "Close",
            onclick: () => dom.projects.close()
          }, icon("x")))),
      body));
    return;
  }

  if (view === "detail") {
    const project = payload;
    const conversations = project.conversations || [];

    const chatRows = conversations.length
      ? conversations.map(conversation => h("button", {
          type: "button", class: "project-chat-row",
          onclick: () => { dom.projects.close(); selectConversation(conversation.id); }
        }, icon("chat"), h("span", null, conversation.title)))
      : [h("p", {class: "project-empty-text"}, "No chats in this project yet.")];

    dom.projects.replaceChildren(h("div", {class: "dialog-form projects-shell"},
      h("div", {class: "dialog-head"},
        h("button", {
          type: "button", class: "icon-button", "aria-label": "Back to projects",
          onclick: () => openProjects()
        }, icon("chevron-left")),
        h("h2", {class: "dialog-title"}, project.name),
        h("div", {class: "dialog-head-actions"},
          h("button", {
            type: "button", class: "icon-button", "aria-label": "Edit project",
            onclick: () => renderProjects("edit", project)
          }, icon("pencil")),
          h("button", {
            type: "button", class: "icon-button", "aria-label": "Close",
            onclick: () => dom.projects.close()
          }, icon("x")))),
      project.description ? h("p", {class: "project-detail-desc"}, project.description) : null,
      project.instructions ? h("details", {class: "project-instructions"},
        h("summary", null, "Project instructions"),
        h("p", null, project.instructions)) : null,
      h("button", {
        type: "button", class: "btn primary",
        onclick: () => startChatInProject(project.id, project.name)
      }, icon("plus"), "New chat in this project"),
      h("div", {class: "project-chat-list"}, chatRows)));
    return;
  }

  if (view === "edit") {
    const project = payload; // null when creating a brand-new project
    const {nameInput, descInput, instructionsInput} = projectFieldset(project || {});

    const form = h("form", {class: "dialog-form"},
      h("div", {class: "dialog-head"},
        h("h2", {class: "dialog-title"}, project ? "Edit project" : "New project"),
        h("button", {
          type: "button", class: "icon-button", "aria-label": "Close",
          onclick: () => dom.projects.close()
        }, icon("x"))),
      h("label", {class: "field-label"}, "Name", nameInput),
      h("label", {class: "field-label"}, "Description", descInput),
      h("label", {class: "field-label"}, "Instructions", instructionsInput),
      h("div", {class: "dialog-actions"},
        h("button", {
          type: "button", class: "btn ghost",
          onclick: () => project ? openProjectDetail(project.id) : renderProjects("list")
        }, "Cancel"),
        project ? h("button", {
          type: "button", class: "btn danger",
          onclick: () => deleteProjectFlow(project)
        }, "Delete project") : null,
        h("button", {type: "submit", class: "btn primary"}, project ? "Save changes" : "Create project")));

    form.addEventListener("submit", async event => {
      event.preventDefault();
      const name = nameInput.value.trim();
      if (!name) { nameInput.focus(); return; }

      const payloadBody = {
        name,
        description: descInput.value.trim(),
        instructions: instructionsInput.value.trim()
      };

      try {
        const data = project
          ? await api(API.project(project.id), {method: "PATCH", body: payloadBody})
          : await api(API.projects, {method: "POST", body: payloadBody});
        await loadProjects();
        openProjectDetail(data.project.id);
      } catch (error) {
        toast(error.message, {type: "error"});
      }
    });

    dom.projects.replaceChildren(form);
    nameInput.focus();
    return;
  }
}

async function openProjectDetail(projectId) {
  try {
    const data = await api(API.project(projectId));
    renderProjects("detail", {...data.project, conversations: data.conversations});
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

async function deleteProjectFlow(project) {
  const ok = await confirmDialog({
    title: `Delete "${project.name}"?`,
    body: "Chats in this project are kept — they'll just become unfiled. This can't be undone.",
    confirmLabel: "Delete project",
    danger: true
  });
  if (!ok) return;

  try {
    await api(API.project(project.id), {method: "DELETE"});
    await loadProjects();
    renderProjects("list");
    if (currentConversation() && sameId(currentConversation().project_id, project.id)) {
      renderProjectPill(null);
    }
    toast("Project deleted.");
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

function openProjects() {
  renderProjects("list");
  openDialog(dom.projects);
  loadProjects().then(() => { if (dom.projects.open) renderProjects("list"); });
}

function startChatInProject(projectId, projectName) {
  dom.projects.close();
  newChat();
  state.newChatProjectId = projectId;
  renderProjectPill({id: projectId, name: projectName});
}

/* ---------------------------------------------------------------------
   8. Composer
   --------------------------------------------------------------------- */

function autoResize() {
  dom.input.style.height = "auto";
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 220)}px`;
}

function updateComposerState() {
  const length = dom.input.value.length;
  const hasContent = dom.input.value.trim().length > 0 || state.attachments.length > 0;
  dom.sendButton.disabled = !hasContent && !state.isSending;

  // Rough estimate only (about four characters per token in English).
  dom.tokenHint.hidden = length < 600;
  if (length >= 600) dom.tokenHint.textContent = `About ${Math.ceil(length / 4).toLocaleString()} tokens`;
}

/* ---- Drafts ------------------------------------------------------------------- */

function draftKey() {
  return state.conversationId === null || state.conversationId === undefined ? "new" : String(state.conversationId);
}

function saveDraft() {
  const drafts = store.getJSON("ai-mind-drafts", {});
  if (dom.input.value.trim()) drafts[draftKey()] = dom.input.value;
  else delete drafts[draftKey()];
  store.setJSON("ai-mind-drafts", drafts);
}

const saveDraftSoon = debounce(saveDraft, 300);

function loadDraft() {
  return store.getJSON("ai-mind-drafts", {})[draftKey()] || "";
}

function clearDraft(key = draftKey()) {
  const drafts = store.getJSON("ai-mind-drafts", {});
  delete drafts[key];
  store.setJSON("ai-mind-drafts", drafts);
}

function resetComposer() {
  clearDraft();
  dom.input.value = "";
  state.attachments = [];
  renderAttachments();
  closeSlash();
  autoResize();
  updateComposerState();
}

function fillPrompt(text) {
  dom.input.value = text;
  autoResize();
  updateComposerState();
  saveDraftSoon();
  dom.input.focus();
  dom.input.setSelectionRange(text.length, text.length);
}

/* ---- Slash prompt templates ------------------------------------------------------ */

const slash = {open: false, index: 0, items: []};

function closeSlash() {
  slash.open = false;
  dom.slashMenu.hidden = true;
  dom.input.removeAttribute("aria-activedescendant");
}

function renderSlash() {
  dom.slashMenu.replaceChildren(...slash.items.map((prompt, index) => h("button", {
    type: "button",
    role: "option",
    id: `slash-option-${index}`,
    class: `slash-item${index === slash.index ? " active" : ""}`,
    "aria-selected": String(index === slash.index),
    onmousedown: event => event.preventDefault(),
    onclick: () => applySlash(prompt)
  }, h("span", {class: "slash-cmd"}, `/${prompt.id}`), h("span", {class: "slash-desc"}, prompt.title))));
  dom.slashMenu.hidden = false;
  dom.input.setAttribute("aria-activedescendant", `slash-option-${slash.index}`);
}

function updateSlash() {
  const match = /^\/([\w-]*)$/.exec(dom.input.value);
  if (!match) {
    closeSlash();
    return;
  }
  const query = match[1].toLowerCase();
  slash.items = PROMPTS.filter(prompt => prompt.id.startsWith(query) || prompt.title.toLowerCase().includes(query));
  if (!slash.items.length) {
    closeSlash();
    return;
  }
  slash.open = true;
  slash.index = Math.min(slash.index, slash.items.length - 1);
  renderSlash();
}

function applySlash(prompt) {
  closeSlash();
  fillPrompt(prompt.text);
}

/** Returns true when the key was consumed by the slash menu. */
function handleSlashKey(event) {
  if (!slash.open) return false;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    slash.index = (slash.index + step + slash.items.length) % slash.items.length;
    renderSlash();
    return true;
  }
  if (event.key === "Enter" || event.key === "Tab") {
    event.preventDefault();
    applySlash(slash.items[slash.index]);
    return true;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeSlash();
    return true;
  }
  return false;
}

/* ---- Attachments --------------------------------------------------------------------- */

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function renderAttachments() {
  dom.attachmentTray.hidden = state.attachments.length === 0;
  dom.attachmentTray.replaceChildren(...state.attachments.map(attachment => h("div", {class: "attachment"},
    attachment.kind === "image" && attachment.dataUrl ? h("img", {src: attachment.dataUrl, alt: ""}) : icon("file"),
    h("span", {class: "attachment-name"}, attachment.name),
    h("span", {class: "attachment-size"}, formatBytes(attachment.size)),
    h("button", {
      type: "button",
      class: "attachment-remove",
      "aria-label": `Remove ${attachment.name}`,
      onclick: () => {
        state.attachments = state.attachments.filter(item => item.id !== attachment.id);
        renderAttachments();
        updateComposerState();
      }
    }, icon("x")))));
}

async function addFiles(fileList) {
  let warned = false;
  for (const file of Array.from(fileList || [])) {
    if (state.attachments.length >= MAX_FILES) {
      toast(`You can attach up to ${MAX_FILES} files to one message.`, {type: "error"});
      break;
    }
    const extension = (file.name.split(".").pop() || "").toLowerCase();
    const isText = (file.type || "").startsWith("text/") || TEXT_EXTENSIONS.has(extension) || file.type === "application/json";

    try {
      if (isText) {
        if (file.size > MAX_TEXT_BYTES) {
          toast(`${file.name} is over ${formatBytes(MAX_TEXT_BYTES)}. Attach a smaller file or paste the part you need.`, {type: "error"});
          continue;
        }
        state.attachments.push({id: uid(), kind: "text", name: file.name, size: file.size, content: await file.text()});
      } else {
        if (!FEATURES.fileUpload.on) {
          if (!warned) requireFeature("fileUpload");
          warned = true;
          continue;
        }
        if (file.size > MAX_BINARY_BYTES) {
          toast(`${file.name} is over ${formatBytes(MAX_BINARY_BYTES)}.`, {type: "error"});
          continue;
        }
        state.attachments.push({
          id: uid(),
          kind: (file.type || "").startsWith("image/") ? "image" : "file",
          name: file.name,
          type: file.type,
          size: file.size,
          dataUrl: await readAsDataURL(file)
        });
      }
    } catch (error) {
      toast(`Couldn't read ${file.name}.`, {type: "error"});
    }
  }
  renderAttachments();
  updateComposerState();
}

/* ---- Tools, modes and model ---------------------------------------------------------- */

function renderTools() {
  dom.toggleSearch.setAttribute("aria-pressed", String(state.tools.webSearch));
  dom.toggleThink.setAttribute("aria-pressed", String(state.tools.deepThink));
}

function toggleTool(name) {
  if (!requireFeature(name)) return;
  state.tools[name] = !state.tools[name];
  renderTools();
}

function renderMode() {
  const mode = state.mode && MODES[state.mode];
  dom.modeChip.hidden = !mode;
  dom.modeChip.replaceChildren();
  if (!mode) return;
  dom.modeChip.append(
    icon(mode.ico),
    h("span", null, mode.label),
    h("button", {
      type: "button",
      class: "chip-clear",
      "aria-label": `Turn off ${mode.label}`,
      onclick: () => { state.mode = null; renderMode(); }
    }, icon("x")));
}

function setMode(name) {
  if (!requireFeature(name)) return;
  state.mode = state.mode === name ? null : name;
  renderMode();
}

function openToolsMenu() {
  openMenu(dom.toolsButton, [
    {label: "Add files", desc: "Text and code files up to 200 KB", ico: "paperclip", onClick: () => dom.fileInput.click()},
    {divider: true},
    {label: "Search the web", ico: "globe", feature: "webSearch", checked: state.tools.webSearch, onClick: () => toggleTool("webSearch")},
    {label: "Think deeper", ico: "bulb", feature: "deepThink", checked: state.tools.deepThink, onClick: () => toggleTool("deepThink")},
    {divider: true},
    {label: "Deep research", ico: "compass", feature: "deepResearch", onClick: () => setMode("deepResearch")},
    {label: "Create image", ico: "image", feature: "imageGeneration", onClick: () => setMode("imageGeneration")},
    {label: "Canvas", ico: "layout", feature: "canvas", onClick: () => setMode("canvas")}
  ], {placement: "top-start", minWidth: 270});
}

function openModelMenu() {
  openMenu(dom.modelButton, [
    {heading: "Model"},
    ...availableModels().map(model => ({
      label: model.name,
      desc: model.desc,
      ico: model.ico,
      checked: state.settings.model === model.id,
      onClick: () => {
        state.settings.model = model.id;
        saveSettings();
        applySettings();
      }
    }))
  ], {placement: "top-end", minWidth: 280});
}

/* ---- Dictation (browser speech recognition) ------------------------------------------ */

function stopDictation() {
  if (state.recognition) state.recognition.stop();
}

function toggleDictation() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    toast("Voice input isn't supported in this browser. Try Chrome, Edge or Safari.", {type: "error"});
    return;
  }
  if (state.recognition) {
    stopDictation();
    return;
  }

  const recognition = new Recognition();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = true;

  const base = dom.input.value ? `${dom.input.value.replace(/\s+$/, "")} ` : "";
  let finalText = "";

  recognition.onresult = event => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalText += transcript;
      else interim += transcript;
    }
    dom.input.value = base + finalText + interim;
    autoResize();
    updateComposerState();
    saveDraftSoon();
  };
  recognition.onerror = event => {
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      toast("Microphone access is blocked. Allow it in your browser's site settings to dictate.", {type: "error"});
    } else if (event.error !== "no-speech" && event.error !== "aborted") {
      toast(`Voice input stopped (${event.error}).`, {type: "error"});
    }
  };
  recognition.onend = () => {
    state.recognition = null;
    dom.micButton.classList.remove("recording");
    dom.micButton.setAttribute("aria-pressed", "false");
    dom.micButton.title = "Dictate";
  };

  try {
    recognition.start();
  } catch (error) {
    toast("Couldn't start voice input.", {type: "error"});
    return;
  }
  state.recognition = recognition;
  dom.micButton.classList.add("recording");
  dom.micButton.setAttribute("aria-pressed", "true");
  dom.micButton.title = "Stop dictation";
}

/* ---- Drag and drop -------------------------------------------------------------------- */

let dragDepth = 0;
const hasFiles = event => Array.from((event.dataTransfer && event.dataTransfer.types) || []).includes("Files");

function bindDropEvents() {
  window.addEventListener("dragenter", event => {
    if (!hasFiles(event)) return;
    dragDepth += 1;
    dom.dropOverlay.hidden = false;
  });
  window.addEventListener("dragleave", event => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dom.dropOverlay.hidden = true;
  });
  window.addEventListener("dragover", event => {
    if (hasFiles(event)) event.preventDefault();
  });
  window.addEventListener("drop", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    dom.dropOverlay.hidden = true;
    addFiles(event.dataTransfer.files);
  });
}

/* ---------------------------------------------------------------------
   9. Side panel, command palette, settings, shortcuts
   --------------------------------------------------------------------- */

/* ---- Side panel (HTML/SVG preview and file viewer) --------------------------------------- */

function buildPreviewDoc(code, language) {
  const head = '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><base target="_blank">';
  if (language === "svg") {
    return `<!doctype html><html><head>${head}<style>html,body{margin:0;height:100%}` +
      `body{display:grid;place-items:center;background:#fff}svg{max-width:100%;max-height:100%}</style></head><body>${code}</body></html>`;
  }
  if (/<html[\s>]/i.test(code)) return code;
  return `<!doctype html><html><head>${head}</head><body>${code}</body></html>`;
}

function setPanelTab(tab) {
  const preview = tab === "preview";
  dom.panelFrame.hidden = !preview;
  dom.panelCode.hidden = preview;
  dom.panelTabPreview.setAttribute("aria-selected", String(preview));
  dom.panelTabCode.setAttribute("aria-selected", String(!preview));
}

/**
 * Previews run in a sandboxed iframe without allow-same-origin, so generated
 * pages can run scripts but can't read this app's storage or call its API.
 */
function openPanel({title, code, language = "", preview = false}) {
  state.panel = {title, code, language, preview};
  dom.panelTitle.textContent = title;
  dom.panelTabs.hidden = !preview;

  const codeElement = $("code", dom.panelCode);
  codeElement.className = "";
  codeElement.textContent = code;
  if (window.hljs && language && window.hljs.getLanguage(language)) {
    try {
      codeElement.innerHTML = window.hljs.highlight(code, {language, ignoreIllegals: true}).value;
      codeElement.className = "hljs";
    } catch (error) { /* plain text is fine */ }
  }

  if (preview) {
    dom.panelFrame.srcdoc = buildPreviewDoc(code, language);
    setPanelTab("preview");
  } else {
    dom.panelFrame.srcdoc = "";
    setPanelTab("code");
  }

  dom.panel.hidden = false;
  dom.shell.classList.add("panel-open");
}

function closePanel() {
  if (dom.panel.hidden) return;
  dom.panel.hidden = true;
  dom.shell.classList.remove("panel-open");
  dom.panelFrame.srcdoc = "";
  state.panel = null;
}

/* ---- Theme -------------------------------------------------------------------------------- */

function toggleTheme() {
  state.settings.theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  saveSettings();
  applyTheme();
}

/* ---- Command palette ------------------------------------------------------------------------ */

const palette = {index: 0, items: [], remote: [], query: ""};

function commandList() {
  return [
    {label: "New chat", ico: "plus", kbd: "mod+shift+o", run: newChat},
    {label: "Show or hide sidebar", ico: "sidebar", kbd: "mod+b", run: toggleSidebar},
    {label: "Switch light or dark theme", ico: "sun", run: toggleTheme},
    {label: "Open settings", ico: "sliders", kbd: "mod+,", run: () => openSettings()},
    {label: "Keyboard shortcuts", ico: "keyboard", kbd: "mod+/", run: openShortcuts},
    {label: "Export this chat as Markdown", ico: "download", run: () => exportCurrent("md")},
    {label: "Copy the last reply", ico: "copy", kbd: "mod+shift+;", run: copyLastReply},
    {label: "Open memories", ico: "sparkle", run: () => { window.location.href = "/memories"; }}
  ];
}

function renderPalette() {
  const query = palette.query.trim().toLowerCase();
  const actions = commandList().filter(command => !query || command.label.toLowerCase().includes(query));

  const titleMatches = state.paletteChats.filter(chat => !query || (chat.title || "").toLowerCase().includes(query));
  const merged = [...palette.remote, ...titleMatches]
    .filter((chat, index, all) => all.findIndex(other => sameId(other.id, chat.id)) === index);
  const chats = (query ? merged : state.paletteChats).slice(0, query ? 30 : 6);

  palette.items = [
    ...actions.map(command => ({kind: "action", ...command})),
    ...chats.map(chat => ({kind: "chat", chat, label: chat.title || "New chat"}))
  ];
  palette.index = clamp(palette.index, 0, Math.max(0, palette.items.length - 1));

  if (!palette.items.length) {
    dom.paletteList.replaceChildren(h("div", {class: "palette-empty"}, "Nothing matches that search."));
    dom.paletteInput.removeAttribute("aria-activedescendant");
    return;
  }

  const nodes = [];
  let previousKind = null;
  palette.items.forEach((item, index) => {
    if (item.kind !== previousKind) {
      nodes.push(h("div", {class: "palette-heading", role: "presentation"}, item.kind === "action" ? "Actions" : query ? "Chats" : "Recent chats"));
      previousKind = item.kind;
    }
    const active = index === palette.index;
    nodes.push(h("button", {
      type: "button",
      role: "option",
      id: `palette-option-${index}`,
      class: `palette-item${active ? " active" : ""}`,
      "aria-selected": String(active),
      onmousemove: () => {
        if (palette.index === index) return;
        palette.index = index;
        $$(".palette-item", dom.paletteList).forEach((el, i) => {
          el.classList.toggle("active", i === index);
          el.setAttribute("aria-selected", String(i === index));
        });
        dom.paletteInput.setAttribute("aria-activedescendant", `palette-option-${index}`);
      },
      onclick: () => runPaletteItem(item)
    },
      icon(item.kind === "action" ? item.ico : "chat"),
      h("span", {class: "palette-label"}, item.label),
      item.kbd ? keycaps(item.kbd) : null));
  });
  dom.paletteList.replaceChildren(...nodes);
  dom.paletteInput.setAttribute("aria-activedescendant", `palette-option-${palette.index}`);
  const active = $(".palette-item.active", dom.paletteList);
  if (active) active.scrollIntoView({block: "nearest"});
}

function runPaletteItem(item) {
  dom.palette.close();
  if (item.kind === "action") item.run();
  else selectConversation(item.chat.id);
}

const searchPaletteRemote = debounce(async () => {
  const query = palette.query.trim();
  if (query.length < 2) {
    palette.remote = [];
    renderPalette();
    return;
  }
  try {
    const data = await api(`${API.conversations}?${new URLSearchParams({search: query})}`);
    if (palette.query.trim() !== query) return;
    palette.remote = data.conversations || [];
    renderPalette();
  } catch (error) { /* local matches are still shown */ }
}, 250);

async function openPalette() {
  if (dom.palette.open) return;
  palette.query = "";
  palette.index = 0;
  palette.remote = [];
  state.paletteChats = state.conversations;
  dom.paletteInput.value = "";
  renderPalette();
  openDialog(dom.palette);
  dom.paletteInput.focus();

  try {
    const data = await api(`${API.conversations}?${new URLSearchParams({search: ""})}`);
    state.paletteChats = data.conversations || [];
    if (dom.palette.open) renderPalette();
  } catch (error) { /* fall back to the sidebar list */ }
}

/* ---- Settings ---------------------------------------------------------------------------------- */

const SETTINGS_TABS = [
  {id: "general", label: "General", ico: "sliders"},
  {id: "personalization", label: "Personalization", ico: "sparkle"},
  {id: "model", label: "Model", ico: "bulb"},
  {id: "data", label: "Data", ico: "archive"}
];

function updateSetting(key, value) {
  state.settings[key] = value;
  saveSettings();
  applySettings();
}

function segmented(options, value, onChange, label) {
  const group = h("div", {class: "segmented", role: "radiogroup", "aria-label": label});
  const buttons = options.map(option => {
    const button = h("button", {
      type: "button",
      role: "radio",
      class: "segment",
      "aria-checked": String(option.value === value)
    }, option.ico ? icon(option.ico) : null, option.label);
    button.addEventListener("click", () => {
      buttons.forEach(other => other.setAttribute("aria-checked", String(other === button)));
      onChange(option.value);
    });
    return button;
  });
  group.append(...buttons);
  return group;
}

function switchControl(checked, onChange, label) {
  const button = h("button", {
    type: "button",
    role: "switch",
    class: "switch",
    "aria-checked": String(checked),
    "aria-label": label
  }, h("span", {class: "switch-thumb"}));
  button.addEventListener("click", () => {
    const next = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", String(next));
    onChange(next);
  });
  return button;
}

function textField({tag = "input", value, placeholder, maxLength, label, rows, onInput}) {
  const field = h(tag, {
    class: "field",
    type: tag === "input" ? "text" : null,
    rows: rows ? String(rows) : null,
    placeholder,
    maxlength: maxLength ? String(maxLength) : null,
    "aria-label": label
  });
  field.value = value || "";
  field.addEventListener("input", () => onInput(field.value));
  return field;
}

function settingRow({title, desc, control, feature = null, stacked = false}) {
  const soon = feature && !FEATURES[feature].on;
  return h("div", {class: `setting-row${stacked ? " stacked" : ""}${soon ? " is-soon" : ""}`},
    h("div", {class: "setting-text"},
      h("div", {class: "setting-title"}, title, soon ? h("span", {class: "soon-tag"}, "Soon") : null),
      desc ? h("div", {class: "setting-desc"}, desc) : null),
    control);
}

function settingsGeneral() {
  const s = state.settings;
  const mod = IS_MAC ? "⌘" : "Ctrl";
  return [
    settingRow({
      title: "Theme",
      desc: "Follow your device or choose one.",
      control: segmented([
        {value: "system", label: "System", ico: "monitor"},
        {value: "light", label: "Light", ico: "sun"},
        {value: "dark", label: "Dark", ico: "moon"}
      ], s.theme, value => updateSetting("theme", value), "Theme")
    }),
    settingRow({
      title: "Text size",
      desc: "Applies to messages.",
      control: segmented([
        {value: "small", label: "Small"},
        {value: "medium", label: "Default"},
        {value: "large", label: "Large"}
      ], s.fontSize, value => updateSetting("fontSize", value), "Text size")
    }),
    settingRow({
      title: "Chat width",
      desc: "Wide suits code and tables.",
      control: segmented([
        {value: "comfortable", label: "Comfortable"},
        {value: "wide", label: "Wide"}
      ], s.width, value => updateSetting("width", value), "Chat width")
    }),
    settingRow({
      title: "Send with",
      desc: "The other key adds a new line.",
      control: segmented([
        {value: true, label: "Enter"},
        {value: false, label: `${mod} + Enter`}
      ], s.enterToSend, value => updateSetting("enterToSend", value), "Send shortcut")
    }),
    settingRow({
      title: "Reveal replies gradually",
      desc: "Types out a reply that arrives all at once. Stop works at any point.",
      control: switchControl(s.reveal, value => updateSetting("reveal", value), "Reveal replies gradually")
    }),
    settingRow({
      title: "Show reply details",
      desc: "Time, duration and token count under each message.",
      control: switchControl(s.details, value => updateSetting("details", value), "Show reply details")
    }),
    settingRow({
      title: "Keyboard shortcuts",
      control: h("button", {type: "button", class: "btn sm", onclick: openShortcuts}, icon("keyboard"), "View shortcuts")
    })
  ];
}

function settingsPersonalization() {
  const s = state.settings;
  return [
    settingRow({
      title: "Your name",
      desc: "Used in the greeting and your profile badge.",
      control: textField({value: s.name, placeholder: "Your name", maxLength: 40, label: "Your name", onInput: value => updateSetting("name", value)})
    }),
    settingRow({
      title: "About you",
      desc: "What should AI Mind know about you? Sent with every message once the backend supports it.",
      feature: "customInstructions",
      stacked: true,
      control: textField({
        tag: "textarea", rows: 4, value: s.about, maxLength: 1500, label: "About you",
        placeholder: "Your role, projects, tools you use",
        onInput: value => updateSetting("about", value)
      })
    }),
    settingRow({
      title: "How to respond",
      desc: "Tone, length and format you prefer.",
      feature: "customInstructions",
      stacked: true,
      control: textField({
        tag: "textarea", rows: 4, value: s.style, maxLength: 1500, label: "How to respond",
        placeholder: "Short answers first, then detail. Use code examples.",
        onInput: value => updateSetting("style", value)
      })
    }),
    settingRow({
      title: "Use saved memories",
      desc: "Let replies draw on what you've approved on the Memories page.",
      feature: "memoryToggle",
      control: switchControl(s.memory, value => updateSetting("memory", value), "Use saved memories")
    }),
    settingRow({
      title: "Manage memories",
      control: h("a", {class: "btn sm", href: "/memories"}, icon("sparkle"), "Open memories")
    })
  ];
}

function settingsModel() {
  const s = state.settings;
  const models = availableModels();
  const select = h("select", {class: "field select", "aria-label": "Default model"},
    models.map(model => h("option", {value: model.id, selected: model.id === s.model}, model.name)));
  select.addEventListener("change", () => {
    updateSetting("model", select.value);
  });

  const temperature = Number(s.temperature) || 0.7;
  const output = h("span", {class: "range-value"}, temperature.toFixed(1));
  const range = h("input", {type: "range", class: "range", min: "0", max: "1.5", step: "0.1", "aria-label": "Temperature"});
  range.value = String(temperature);
  range.addEventListener("input", () => {
    output.textContent = Number(range.value).toFixed(1);
    updateSetting("temperature", Number(range.value));
  });

  const providerRows = state.providers.length
    ? state.providers.map(p => h("div", {class: "provider-row"},
        h("span", {class: "provider-row-name"},
          h("span", {class: `provider-dot ${p.available ? "is-up" : "is-down"}`}), providerLabel(p.provider)),
        h("span", {class: "provider-row-model"}, p.model)))
    : [h("div", {class: "provider-row provider-row-empty"}, "No providers configured yet — add an API key in .env.")];

  return [
    settingRow({
      title: "Default model",
      desc: "Auto uses the router's normal priority order. Picking a provider forces it first for new messages, and it still falls back automatically if that provider is unavailable.",
      control: select
    }),
    settingRow({
      title: "Temperature",
      desc: "Lower is more focused. Higher is more varied.",
      control: h("div", {class: "range-wrap"}, range, output)
    }),
    settingRow({
      title: "Configured providers",
      desc: "From /api/ai-router/status, in the order the router tries them.",
      stacked: true,
      control: h("div", {class: "provider-list"}, providerRows)
    })
  ];
}

async function fetchAllConversations() {
  const data = await api(`${API.conversations}?${new URLSearchParams({search: ""})}`);
  const list = data.conversations || [];
  const result = [];
  for (let start = 0; start < list.length; start += 4) {
    const batch = await Promise.all(list.slice(start, start + 4).map(async conversation => {
      try {
        const full = await api(API.conversation(conversation.id));
        return {id: conversation.id, title: full.title || conversation.title, updated_at: conversation.updated_at, messages: full.messages || []};
      } catch (error) {
        return null;
      }
    }));
    result.push(...batch.filter(Boolean));
  }
  return result;
}

async function exportAll() {
  const dismiss = toast("Exporting your chats…", {duration: 120000});
  try {
    const conversations = await fetchAllConversations();
    dismiss();
    if (!conversations.length) {
      toast("There are no chats to export.");
      return;
    }
    const stamp = new Date().toISOString().slice(0, 10);
    downloadFile(`ai-mind-chats-${stamp}.json`,
      JSON.stringify({exported_at: new Date().toISOString(), conversations}, null, 2), "application/json");
    toast(`Exported ${conversations.length} ${conversations.length === 1 ? "chat" : "chats"}.`);
  } catch (error) {
    dismiss();
    toast(error.message, {type: "error"});
  }
}

async function deleteAllChats() {
  const confirmed = await confirmDialog({
    title: "Delete all chats?",
    body: "Every conversation will be permanently deleted from the server. Export them first if you want a copy.",
    confirmLabel: "Delete all",
    danger: true
  });
  if (!confirmed) return;

  try {
    const data = await api(`${API.conversations}?${new URLSearchParams({search: ""})}`);
    const list = data.conversations || [];
    let failed = 0;
    for (const conversation of list) {
      try { await api(API.conversationItem(conversation.id), {method: "DELETE"}); } catch (error) { failed += 1; }
    }
    stopGenerating();
    resetToNewChat();
    store.remove("ai-mind-drafts");
    await loadConversations();
    toast(failed ? `Deleted ${list.length - failed} chats. ${failed} couldn't be deleted.` : "All chats deleted.", {type: failed ? "error" : "info"});
  } catch (error) {
    toast(error.message, {type: "error"});
  }
}

function resetInterface() {
  ["ai-mind-settings", "ai-mind-theme", "ai-mind-pinned", "ai-mind-feedback", "ai-mind-sidebar"].forEach(key => store.remove(key));
  state.settings = loadSettings();
  state.pinned = new Set();
  state.feedback = {};
  setSidebarCollapsed(false);
  applySettings();
  renderConversations();
  renderSettings("data");
  toast("Interface settings reset.");
}

function settingsData() {
  return [
    settingRow({
      title: "Export all chats",
      desc: "Download every conversation as one JSON file.",
      control: h("button", {type: "button", class: "btn sm", onclick: exportAll}, icon("download"), "Export")
    }),
    settingRow({
      title: "Reset interface",
      desc: "Clears theme, layout, pins and feedback stored in this browser. Chats stay on the server.",
      control: h("button", {type: "button", class: "btn sm", onclick: resetInterface}, "Reset")
    }),
    settingRow({
      title: "Delete all chats",
      desc: "Permanently removes every conversation from the server.",
      control: h("button", {type: "button", class: "btn sm danger", onclick: deleteAllChats}, icon("trash"), "Delete all")
    })
  ];
}

function renderSettings(tab = "general") {
  const current = SETTINGS_TABS.find(item => item.id === tab) || SETTINGS_TABS[0];
  const builders = {general: settingsGeneral, personalization: settingsPersonalization, model: settingsModel, data: settingsData};

  const nav = h("nav", {class: "settings-nav", "aria-label": "Settings sections"},
    SETTINGS_TABS.map(item => h("button", {
      type: "button",
      class: `settings-tab${item.id === current.id ? " active" : ""}`,
      "aria-current": item.id === current.id ? "page" : null,
      onclick: () => renderSettings(item.id)
    }, icon(item.ico), item.label)));

  const pane = h("div", {class: "settings-pane"},
    h("div", {class: "settings-header"},
      h("h2", {class: "settings-heading"}, current.label),
      h("button", {type: "button", class: "icon-button", "aria-label": "Close settings", onclick: () => dom.settings.close()}, icon("x"))),
    h("div", {class: "settings-body"}, builders[current.id]()));

  dom.settings.replaceChildren(h("div", {class: "settings-shell"}, nav, pane));
  if (dom.settings.open) $(".settings-tab.active", dom.settings).focus();
}

function openSettings(tab = "general") {
  renderSettings(tab);
  openDialog(dom.settings);
}

function openShortcuts() {
  dom.shortcuts.replaceChildren(h("div", {class: "dialog-form"},
    h("div", {class: "dialog-head"},
      h("h2", {class: "dialog-title"}, "Keyboard shortcuts"),
      h("button", {type: "button", class: "icon-button", "aria-label": "Close", onclick: () => dom.shortcuts.close()}, icon("x"))),
    h("ul", {class: "shortcut-list"}, SHORTCUTS.map(item => h("li", null, h("span", null, item.label), keycaps(item.keys))))));
  openDialog(dom.shortcuts);
}

function openProfileMenu() {
  openMenu(dom.profileButton, [
    {label: "Settings", ico: "sliders", kbd: "mod+,", onClick: () => openSettings()},
    {label: "Search chats and actions", ico: "search", kbd: "mod+k", onClick: openPalette},
    {label: "Keyboard shortcuts", ico: "keyboard", kbd: "mod+/", onClick: openShortcuts},
    {divider: true},
    {label: "Memories", ico: "sparkle", onClick: () => { window.location.href = "/memories"; }}
  ], {placement: "top-start", minWidth: 250});
}

/* ---------------------------------------------------------------------
   10. Boot
   --------------------------------------------------------------------- */

/** Marks controls whose feature flag is off, so they show a "Soon" tag and explain themselves when used. */
function applyFeatureFlags() {
  $$("[data-feature]").forEach(element => {
    const feature = FEATURES[element.dataset.feature];
    if (feature && feature.on) return;
    element.classList.add("is-soon");
    const base = element.getAttribute("title") || (feature ? feature.label : "");
    if (base) element.setAttribute("title", `${base} (coming soon)`);
  });
}

function onInputKeydown(event) {
  if (handleSlashKey(event)) return;
  if (event.key !== "Enter" || event.isComposing) return;

  const withModifier = event.ctrlKey || event.metaKey;
  const shouldSend = state.settings.enterToSend ? !event.shiftKey && !event.altKey : withModifier;
  if (!shouldSend) return;

  event.preventDefault();
  if (state.isSending) {
    toast("AI Mind is still replying. Press Esc to stop it first.");
    return;
  }
  dom.form.requestSubmit();
}

function onGlobalKeydown(event) {
  const mod = IS_MAC ? event.metaKey : event.ctrlKey;
  const key = (event.key || "").toLowerCase();
  const dialogOpen = !!$("dialog[open]");

  if (mod && !event.shiftKey && !event.altKey && key === "k") {
    event.preventDefault();
    if (dom.palette.open) dom.palette.close();
    else if (!dialogOpen) openPalette();
    return;
  }

  if (mod && !dialogOpen) {
    if (event.shiftKey && key === "o") { event.preventDefault(); newChat(); return; }
    if (!event.shiftKey && key === "b") { event.preventDefault(); toggleSidebar(); return; }
    if (!event.shiftKey && key === ",") { event.preventDefault(); openSettings(); return; }
    if (!event.shiftKey && key === "/") { event.preventDefault(); openShortcuts(); return; }
    if (event.shiftKey && event.code === "Semicolon") { event.preventDefault(); copyLastReply(); return; }
  }

  if (event.key === "Escape") {
    if (dialogOpen || activeMenu) return;
    if (state.isSending) {
      event.preventDefault();
      stopGenerating();
    } else if (!dom.panel.hidden) {
      closePanel();
    } else if (state.recognition) {
      stopDictation();
    } else if (dom.shell.classList.contains("history-open")) {
      setSidebarOpen(false);
    }
  }
}

function bindEvents() {
  /* Sidebar */
  dom.newChat.addEventListener("click", newChat);
  dom.sidebarToggle.addEventListener("click", toggleSidebar);
  dom.sidebarCollapse.addEventListener("click", toggleSidebar);
  dom.backdrop.addEventListener("click", () => setSidebarOpen(false));
  dom.historySearch.addEventListener("input", debounce(loadConversations, 220));
  dom.openSettings.addEventListener("click", () => openSettings());
  dom.profileButton.addEventListener("click", openProfileMenu);
  $$(".nav-item[data-feature]").forEach(item => {
    item.addEventListener("click", () => {
      const key = item.dataset.feature;
      if (key === "projects") { openProjects(); return; }
      if (requireFeature(key)) emitFeature(key);
    });
  });
  if (dom.projectPill) dom.projectPill.addEventListener("click", () => {
    const conversation = currentConversation();
    if (conversation && conversation.project_id) openProjectDetail(conversation.project_id);
    else if (state.newChatProjectId) openProjectDetail(state.newChatProjectId);
  });

  /* Top bar */
  dom.chatTitle.addEventListener("click", startTitleRename);
  dom.chatMenuButton.addEventListener("click", openChatMenu);
  dom.themeToggle.addEventListener("click", toggleTheme);
  dom.shareButton.addEventListener("click", () => {
    if (!requireFeature("share")) return;
    const conversation = currentConversation();
    if (!conversation) toast("Send a message first, then you can share the chat.");
    else shareConversation(conversation.id);
  });

  /* Conversation view */
  dom.chat.addEventListener("scroll", updateScrollButton, {passive: true});
  dom.scrollBottom.addEventListener("click", () => scrollToBottom(true, true));
  $$(".suggestion").forEach(button => button.addEventListener("click", () => fillPrompt(button.dataset.prompt)));

  /* Composer */
  dom.form.addEventListener("submit", event => {
    event.preventDefault();
    if (state.isSending) return;
    const files = [...state.attachments];
    const content = composePrompt(dom.input.value, files);
    if (content) sendMessage(content, {files});
  });
  dom.sendButton.addEventListener("click", event => {
    if (!state.isSending) return;
    event.preventDefault();
    stopGenerating();
  });
  dom.input.addEventListener("input", () => {
    autoResize();
    updateComposerState();
    updateSlash();
    saveDraftSoon();
  });
  dom.input.addEventListener("keydown", onInputKeydown);
  dom.input.addEventListener("blur", closeSlash);
  dom.input.addEventListener("paste", event => {
    const files = event.clipboardData && event.clipboardData.files;
    if (files && files.length) {
      event.preventDefault();
      addFiles(files);
    }
  });
  dom.toolsButton.addEventListener("click", openToolsMenu);
  dom.toggleSearch.addEventListener("click", () => toggleTool("webSearch"));
  dom.toggleThink.addEventListener("click", () => toggleTool("deepThink"));
  dom.modelButton.addEventListener("click", openModelMenu);
  dom.micButton.addEventListener("click", toggleDictation);
  dom.fileInput.addEventListener("change", () => {
    addFiles(dom.fileInput.files);
    dom.fileInput.value = "";
  });
  bindDropEvents();

  /* Side panel */
  dom.panelClose.addEventListener("click", closePanel);
  dom.panelTabPreview.addEventListener("click", () => setPanelTab("preview"));
  dom.panelTabCode.addEventListener("click", () => setPanelTab("code"));
  dom.panelCopy.addEventListener("click", async () => {
    if (state.panel && await copyText(state.panel.code)) toast("Copied.");
  });
  dom.panelDownload.addEventListener("click", () => {
    if (!state.panel) return;
    const {title, code, language} = state.panel;
    const hasExtension = /\.[a-z0-9]+$/i.test(title);
    downloadFile(hasExtension ? title : `snippet.${extensionFor(language)}`, code);
  });

  /* Palette */
  dom.paletteInput.addEventListener("input", () => {
    palette.query = dom.paletteInput.value;
    palette.index = 0;
    renderPalette();
    searchPaletteRemote();
  });
  dom.paletteInput.addEventListener("keydown", event => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const count = palette.items.length;
      if (count) palette.index = (palette.index + step + count) % count;
      renderPalette();
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = palette.items[palette.index];
      if (item) runPaletteItem(item);
    }
  });

  /* Dialogs close when the backdrop is clicked */
  $$("dialog").forEach(dialog => dialog.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  }));

  /* Global */
  document.addEventListener("keydown", onGlobalKeydown);
  window.addEventListener("beforeunload", saveDraft);
  window.addEventListener("online", () => loadConversations());
  window.addEventListener("offline", () => setConnection(false));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !state.online) loadConversations();
  });
  systemLight.addEventListener("change", applyTheme);
  mobileQuery.addEventListener("change", () => setSidebarOpen(false));
  setInterval(() => { if (!state.online) loadConversations(); }, 10000);
}

async function boot() {
  applyFeatureFlags();
  applySettings();
  $$("[data-kbd]").forEach(element => { element.textContent = formatKeys(element.dataset.kbd); });

  dom.fileInput.accept = [...TEXT_EXTENSIONS].map(extension => `.${extension}`).join(",") +
    (FEATURES.fileUpload.on ? ",image/*,application/pdf" : "");

  setConnection(true);
  if (store.get("ai-mind-sidebar") === "collapsed") dom.shell.classList.add("sidebar-collapsed");
  dom.sidebarToggle.setAttribute("aria-expanded", String(!dom.shell.classList.contains("sidebar-collapsed")));

  clearMessages();
  setTitle("New chat");
  renderTools();
  renderMode();
  renderAttachments();
  bindEvents();

  dom.input.value = loadDraft();
  loadConversations();
  loadProviders();
  loadProjects();
  if (state.conversationId !== null && state.conversationId !== undefined && state.conversationId !== "") {
    await selectConversation(state.conversationId);
  }
  autoResize();
  updateComposerState();
  if (!isCoarsePointer()) dom.input.focus();
}

boot();