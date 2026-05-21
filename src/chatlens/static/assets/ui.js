// Small shared helpers for the ChatLens SPA.

export const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
));

export async function api(path, opts={}) {
  const res = await fetch(path, {
    headers: {'Content-Type':'application/json'},
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

export function icon(name) {
  const paths = {
    grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
    book:'M4 19.5A2.5 2.5 0 016.5 17H20M4 4.5A2.5 2.5 0 016.5 2H20v15H6.5A2.5 2.5 0 004 19.5V4.5z',
    refresh:'M4 4v6h6M20 20v-6h-6M4 14a8 8 0 0014.9 4M20 10A8 8 0 015.1 6',
    sparkle:'M12 3v3M12 18v3M5 12H2M22 12h-3M5.6 5.6L7.7 7.7M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1',
    star:'M12 2l3.1 6.3 7 1-5 4.9 1.2 6.9L12 18l-6.3 3.3L7 14.2 2 9.3l7-1z',
    trash:'M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6',
    back:'M19 12H5M12 5l-7 7 7 7',
    chevronDown:'M6 9l6 6 6-6',
    check:'M5 13l4 4L19 7',
    x:'M6 6l12 12M18 6L6 18',
    share:'M4 12v7a2 2 0 002 2h12a2 2 0 002-2v-7M16 6l-4-4-4 4M12 2v13',
    eyeOff:'M17.94 17.94A10.94 10.94 0 0112 20c-7 0-11-8-11-8a19.79 19.79 0 015.06-5.94M9.9 4.24A10.94 10.94 0 0112 4c7 0 11 8 11 8a19.86 19.86 0 01-3.17 4.19M1 1l22 22M14.12 14.12a3 3 0 11-4.24-4.24',
    eye:'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z M12 9a3 3 0 100 6 3 3 0 000-6z',
    download:'M12 3v12M7 10l5 5 5-5M5 21h14',
  };
  return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${paths[name]||''}"/></svg>`;
}

export function emoji(name) { return (name || '').slice(0, 2) || '💬'; }

export function badge(kind) {
  if (kind === 'guest_share') return `<span class="px-2 py-0.5 text-xs rounded font-medium bg-indigo-100 text-indigo-700">嘉宾分享</span>`;
  if (kind === 'daily_digest') return `<span class="px-2 py-0.5 text-xs rounded font-medium bg-amber-100 text-amber-800">每日精华</span>`;
  return `<span class="px-2 py-0.5 text-xs rounded font-medium bg-emerald-100 text-emerald-700">群日报</span>`;
}

/* Tiny markdown renderer that supports headings, blockquotes, lists,
   inline bold, links, and images. Output is sanitised: text content is
   always escaped; only the URL passes through inside href/src attrs. */
export function renderMd(md) {
  const lines = (md || '').split('\n');
  const out = [];
  let inList = null;
  const flush = () => { if (inList) { out.push(`</${inList}>`); inList = null; } };
  const inline = (raw) => {
    let s = escapeHtml(raw);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // images first so the URL doesn't get re-matched by the link regex
    const SENT = '\u0001';
    const placeholders = [];
    const stash = (html) => { placeholders.push(html); return SENT + (placeholders.length - 1) + SENT; };
    s = s.replace(/!\[([^\]]*)\]\(([^\s)]+)\)/g, (_, alt, url) =>
      stash(`<img src="${escapeHtml(url)}" alt="${alt}" class="my-3 rounded-lg border border-line max-w-sm max-h-80 object-contain cursor-zoom-in cl-zoomable">`));
    s = s.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g, (_, text, url) => {
      // Hash-only URLs are in-app SPA navigation (e.g. #asset/xxx, #topic/aaa/T1)
      // and must NOT open in a new tab; otherwise the browser drops out of the
      // current route into a fresh SPA instance.
      if (url.startsWith('#')) {
        return stash(`<a href="${escapeHtml(url)}" class="text-accent hover:underline">${text}</a>`);
      }
      const wxOnly = /xiaohongshu\.com|xhslink\.com|mp\.weixin\.qq\.com|weixin:\/\/|wxapp:\/\//i.test(url);
      const badge = wxOnly ? '<span class="ml-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 align-middle whitespace-nowrap">需在微信打开</span>' : '';
      return stash(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="text-accent hover:underline">${text}</a>${badge}`);
    });
    // Auto-link bare URLs the LLM forgot to wrap in [text](url) syntax.
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>"'【】）)]+)/g,
      (_, pre, url) => pre + stash(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="text-accent hover:underline">${escapeHtml(url)}</a>`));
    // Put placeholders back
    s = s.replace(new RegExp(SENT + '(\\d+)' + SENT, 'g'), (_, i) => placeholders[+i]);
    return s;
  };
  for (const raw of lines) {
    if (/^# /.test(raw))     { flush(); out.push(`<h1>${inline(raw.slice(2))}</h1>`); continue; }
    if (/^## /.test(raw))    { flush(); out.push(`<h2>${inline(raw.slice(3))}</h2>`); continue; }
    if (/^### /.test(raw))   { flush(); out.push(`<h3>${inline(raw.slice(4))}</h3>`); continue; }
    if (/^> /.test(raw))     { flush(); out.push(`<blockquote>${inline(raw.slice(2))}</blockquote>`); continue; }
    if (/^- /.test(raw))     { if (inList!=='ul'){flush(); out.push('<ul>'); inList='ul';} out.push(`<li>${inline(raw.slice(2))}</li>`); continue; }
    if (/^\d+\.\s/.test(raw)){ if (inList!=='ol'){flush(); out.push('<ol>'); inList='ol';} out.push(`<li>${inline(raw.replace(/^\d+\.\s/,''))}</li>`); continue; }
    if (raw.trim()==='')     { flush(); continue; }
    flush(); out.push(`<p>${inline(raw)}</p>`);
  }
  flush();
  return out.join('\n');
}

export const PALETTES = [
  'from-indigo-500 to-violet-500','from-sky-500 to-cyan-500','from-emerald-500 to-teal-500',
  'from-fuchsia-500 to-pink-500','from-amber-500 to-orange-500','from-rose-500 to-red-500',
];

/* status pill + banner --------------------------------------------- */
let _statusCache = null;
let _statusCacheAt = 0;
const STATUS_TTL_MS = 10_000;

function _relTime(epochSec) {
  if (!epochSec) return '';
  const diff = Date.now()/1000 - epochSec;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff/60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff/3600)} 小时前`;
  if (diff < 86400*7) return `${Math.floor(diff/86400)} 天前`;
  const d = new Date(epochSec * 1000);
  return `${d.getMonth()+1}/${d.getDate()}`;
}

// Format an epoch-second timestamp in the machine's local timezone.
// We deliberately omit the `timeZone` option in Intl.DateTimeFormat so it
// defaults to the runtime's local zone (read from the OS at render time).
// That way the same timestamp shows as 13:48 in Beijing and 00:48 in NY,
// without any explicit handling — the WebView inherits the system tz.
const _absFmt = new Intl.DateTimeFormat(undefined, {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit',
  hour12: false,
});

export function fmtAbsTime(epochSec) {
  if (!epochSec) return '';
  // Intl outputs "2026/05/14, 13:48" in zh-CN and "05/14/2026, 13:48" in en-US.
  // Normalise the date portion to YYYY-MM-DD for a single readable format.
  const parts = _absFmt.formatToParts(new Date(epochSec * 1000));
  const get = (t) => parts.find(p => p.type === t)?.value || '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`;
}

export const fmtRelTime = _relTime;

function _wechatState(w) {
  if (!w || w.available === false) {
    return { severity: 'error',
             label: 'wechat-cli 不可用',
             detail: w?.error || 'ChatLens 内置的 wechat-cli 没有跑起来。',
             subtext: '' };
  }
  if (w.needs_init) {
    return { severity: 'warn',
             label: '需要初始化',
             detail: '首次启动需要从 WeChat 提取一次密钥 — 点击下方按钮进入设置。',
             subtext: '' };
  }
  if (w.app_running === false) {
    return { severity: 'error',
             label: 'WeChat 未启动',
             detail: '请打开 WeChat / 微信 桌面应用，再回来刷新页面。',
             subtext: '' };
  }
  if (w.connected === false) {
    return { severity: 'warn',
             label: 'wechat-cli 未连接',
             detail: w.error || 'WeChat 已运行，但 wechat-cli 还读不到数据 — 试着重新跑 init。',
             subtext: '' };
  }
  const v = (w.version || '').split('version ').pop();
  const sub = w.last_message_at ? `最近消息 · ${_relTime(w.last_message_at)}` : '';
  return { severity: 'ok', label: `wechat-cli ${v}`.trim(), detail: '', subtext: sub,
           last_message_at: w.last_message_at || null };
}

function _claudeState(c) {
  if (!c || c.configured === false) {
    return { severity: 'error',
             label: 'claude CLI 不可用',
             detail: c?.error || '找不到 claude CLI。安装 Claude Code 并登录后再试。' };
  }
  if (c.rate_limited) {
    return { severity: 'error',
             label: 'Claude 额度已用尽',
             detail: c.last_error?.msg || 'Claude Max 当前周期的额度已用完,等额度恢复后再试。' };
  }
  if (c.auth_error) {
    return { severity: 'error',
             label: 'Claude 登录已失效',
             detail: c.last_error?.msg || '运行 `claude` 重新登录。' };
  }
  if (c.last_error) {
    return { severity: 'warn',
             label: 'Claude 最近一次失败',
             detail: c.last_error.msg || '查看终端日志了解详情。' };
  }
  return { severity: 'ok', label: 'claude CLI', detail: '' };
}

export async function loadStatus(force=false) {
  if (!force && _statusCache && Date.now() - _statusCacheAt < STATUS_TTL_MS) return _statusCache;
  try {
    // Health and preferences are independent; fetching in parallel keeps the
    // sidebar render unblocked even if one is slow.
    const [h, prefs] = await Promise.all([
      api('/api/health'),
      api('/api/preferences').catch(() => null),
    ]);
    const wechat = _wechatState(h.wechat_cli);
    const claude = _claudeState(h.claude_cli);
    const problems = [wechat, claude].filter(s => s.severity !== 'ok');
    _statusCache = {
      wechat, claude,
      version: h.version || '',
      preferences: prefs || { claude_model: 'sonnet', allowed_models: ['sonnet', 'opus'] },
      // Top-level convenience for route gating — the setup wizard wants to
      // replace the dashboard wholesale rather than just show a banner.
      needs_init: !!h.wechat_cli?.needs_init,
      // Raw payload (with `setup_state` enum) so the wizard can switch on the
      // precise corner case instead of guessing from boolean flags.
      wechat_raw: h.wechat_cli || {},
      setup_state: h.wechat_cli?.setup_state || (h.wechat_cli?.needs_init ? 'needs_init' : null),
      banner: problems.length ? {
        severity: problems.some(s => s.severity === 'error') ? 'error' : 'warn',
        items: problems,
      } : null,
    };
  } catch {
    _statusCache = { wechat: { severity:'ok', label:'', detail:'' },
                     claude: { severity:'ok', label:'', detail:'' },
                     preferences: { claude_model: 'sonnet', allowed_models: ['sonnet', 'opus'] },
                     banner: null };
  }
  _statusCacheAt = Date.now();
  return _statusCache;
}

export function invalidateStatus() { _statusCache = null; _statusCacheAt = 0; }

function _pill(s) {
  if (!s?.label) return '';
  const tone = s.severity === 'error' ? 'text-rose-300'
             : s.severity === 'warn'  ? 'text-amber-300'
             : 'text-slate-500';
  const mark = s.severity === 'ok' ? '✓' : s.severity === 'warn' ? '⚠' : '✗';
  const sub = s.subtext ? `<div class="text-slate-500 pl-4">${escapeHtml(s.subtext)}</div>` : '';
  return `<div class="${tone}">${mark} ${escapeHtml(s.label)}</div>${sub}`;
}

function _banner(b) {
  if (!b) return '';
  const tone = b.severity === 'error'
    ? 'bg-rose-50 border-rose-200 text-rose-900'
    : 'bg-amber-50 border-amber-200 text-amber-900';
  const rows = b.items.map(i => `
    <div class="flex gap-2 items-start">
      <span class="font-semibold">${escapeHtml(i.label)}</span>
      ${i.detail ? `<span class="opacity-80">— ${escapeHtml(i.detail)}</span>` : ''}
    </div>`).join('');
  return `
    <div class="border-b ${tone} px-8 py-2.5 text-sm space-y-1">${rows}</div>`;
}

/* shell + topbar ---------------------------------------------------- */
const NAV = [
  { key:'dashboard', label:'Dashboard', icon:'grid' },
  { key:'summaries', label:'精华',     icon:'book' },
  { key:'digests',   label:'每日精华',  icon:'star' },
];

export function shell(active, body, status) {
  const items = NAV.map(n => `
    <a href="#${n.key}" class="flex items-center gap-3 px-4 py-2 rounded-lg text-sm
       ${active===n.key ? 'bg-white/10 text-white font-medium' : 'text-slate-300 hover:bg-white/5 hover:text-white'}">
      <span class="opacity-80">${icon(n.icon)}</span><span>${n.label}</span>
    </a>`).join('');
  // Model selector lives in the sidebar header so it's visible from any page
  // and changes take effect on the very next summarize call.
  const prefs = status?.preferences || { claude_model: 'sonnet', allowed_models: ['sonnet', 'opus'] };
  const allowed = prefs.allowed_models || ['sonnet', 'opus'];
  const modelOpts = allowed.map(m => `
    <option value="${escapeHtml(m)}" ${m === prefs.claude_model ? 'selected' : ''}>${escapeHtml(m)}</option>`
  ).join('');
  return `
    <div class="min-h-screen flex">
      <aside class="w-56 bg-ink text-slate-300 flex flex-col shrink-0">
        <div class="px-5 py-4 flex items-center gap-2.5 border-b border-white/5">
          <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-500 grid place-items-center text-white font-bold shrink-0">C</div>
          <div class="min-w-0 flex-1">
            <div class="text-white font-semibold tracking-wide leading-tight">ChatLens</div>
            ${status?.version ? `<div class="font-mono text-[10px] text-slate-400 leading-tight">v${escapeHtml(status.version)}</div>` : ''}
          </div>
        </div>
        <div class="px-3 py-3 border-b border-white/5 text-xs leading-5 space-y-1">
          ${_pill(status?.wechat)}
          ${status?.wechat?.severity === 'ok' ? `
          <a href="#setup" class="block pl-4 text-[11px] text-slate-500 hover:text-slate-300">🔧 重新提取密钥</a>` : ''}
          ${_pill(status?.claude)}
        </div>
        <div class="px-3 py-3 border-b border-white/5">
          <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1.5" for="modelSelect">Claude 模型</label>
          <select id="modelSelect" class="w-full bg-white/5 border border-white/10 text-slate-100 text-xs rounded px-2 py-1.5 outline-none focus:border-indigo-400 cursor-pointer">${modelOpts}</select>
        </div>
        <nav class="flex-1 px-2 py-3 space-y-1">${items}</nav>
        <div class="p-3 border-t border-white/5 text-xs text-slate-400 leading-5">本机模式</div>
      </aside>
      <main class="flex-1 min-w-0 relative">
        ${_banner(status?.banner)}
        ${body}
      </main>
    </div>`;
}

export function topbar(crumbs, right='') {
  const parts = crumbs.map((c,i) => i===crumbs.length-1
    ? `<span class="text-ink font-medium">${escapeHtml(typeof c==='string'?c:c.label)}</span>`
    : `<a href="#${c.href||''}" class="text-slate-400 hover:text-ink">${escapeHtml(c.label||c)}</a><span class="text-slate-300">/</span>`
  ).join(' ');
  return `
    <div class="h-14 px-8 flex items-center justify-between border-b border-line bg-white">
      <div class="text-sm flex items-center gap-2">${parts}</div>
      <div class="flex items-center gap-3">${right}</div>
    </div>`;
}

/* toast ------------------------------------------------------------- */
let _toastTimer = null;
export function toast(msg, kind='info') {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'fixed bottom-6 right-6 z-50';
    document.body.appendChild(el);
  }
  const tones = {info:'bg-ink text-white', error:'bg-rose-600 text-white', success:'bg-emerald-600 text-white'};
  el.innerHTML = `<div class="${tones[kind]||tones.info} px-4 py-2.5 rounded-lg shadow-lg text-sm">${escapeHtml(msg)}</div>`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.innerHTML = ''; }, 4500);
}
