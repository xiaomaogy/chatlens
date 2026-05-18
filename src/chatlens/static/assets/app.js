import { api, escapeHtml, icon, emoji, badge, renderMd, PALETTES,
         shell, topbar, loadStatus, invalidateStatus, toast,
         fmtAbsTime, fmtRelTime } from './ui.js';

/* ---------- dashboard state (persists across renders) ---------- */
const State = {
  pinnedOnly: localStorage.getItem('cl.pinnedOnly') === '1',
  // Show groups the user has minimized out of the default view. Defaults off
  // — clicking the toggle reveals them (rendered faded) so they can be
  // unhidden without permanently re-cluttering the dashboard.
  showHidden: localStorage.getItem('cl.showHidden') === '1',
  selectMode: false,
  selection: new Set(),
  lastGroupClicked: null,
  // 精华 list multi-select state
  assetSelectMode: false,
  assetSelection: new Set(),
  lastAssetClicked: null,
  bulkDelete: null,  // { done, total, errors: [] }
  // cached list of group ids in display order, used for shift-click ranges
  visibleGroupIds: [],
  visibleAssetIds: [],
  // Per-group asset list sort direction. Persisted so the choice carries
  // between sessions; defaults to newest-first.
  groupAssetSortDir: localStorage.getItem('cl.groupAssetSortDir') === 'asc' ? 'asc' : 'desc',
};

function rangeSelect(ids, anchor, target, set) {
  const a = ids.indexOf(anchor);
  const b = ids.indexOf(target);
  if (a < 0 || b < 0) { set.add(target); return; }
  const [from, to] = [Math.min(a, b), Math.max(a, b)];
  for (let i = from; i <= to; i++) set.add(ids[i]);
}

function persistPinnedOnly() { localStorage.setItem('cl.pinnedOnly', State.pinnedOnly ? '1' : '0'); }
function persistShowHidden() { localStorage.setItem('cl.showHidden', State.showHidden ? '1' : '0'); }
function clearSelection() { State.selection.clear(); State.selectMode = false; }

/* ---------- dashboard ---------- */
async function pageDashboard() {
  const status = await loadStatus();
  // First-launch gate: if wechat-cli isn't ready for a *setup* reason (never
  // initialised, WeChat closed, keys empty/stale, bundled binary broken), the
  // dashboard is useless — hand off to the state-aware wizard. Transient
  // states (`db_locked`, `sessions_failed`) stay on the dashboard with a
  // banner; the user usually just needs to wait a few seconds.
  const WIZARD_STATES = new Set([
    'binary_unavailable', 'needs_init', 'keys_empty', 'keys_stale', 'needs_app',
  ]);
  if (WIZARD_STATES.has(status.setup_state)) { return renderSetupWizard(status); }
  document.getElementById('app').innerHTML = shell('dashboard', `
    ${topbar(['Dashboard'])}
    <div class="p-8 text-slate-500">正在同步 wechat-cli…</div>`, status);

  let groups = [];
  try {
    await api('/api/groups/sync', { method:'POST', body:'{}' });
    const qs = new URLSearchParams();
    if (State.pinnedOnly) qs.set('pinned_only', 'true');
    if (State.showHidden) qs.set('include_hidden', 'true');
    const tail = qs.toString();
    groups = await api(`/api/groups${tail ? '?' + tail : ''}`);
  } catch (e) {
    toast('同步失败：' + e.message, 'error');
  }
  const hiddenCount = groups.filter(g => g.hidden).length;
  State.visibleGroupIds = groups.map(g => g.id);
  const latest = await api('/api/assets?limit=10').catch(() => []);

  const stats = [
    { l:'微信群', v: String(groups.length), s: State.pinnedOnly ? '已显示精选' : '已同步' },
    { l:'未读消息', v: String(groups.reduce((a,g)=>a+(g.unread||0),0)), s:'累计跨群' },
    { l:'生成内容', v: String(latest.length), s:'近 10 条' },
  ];
  const statsHtml = stats.map(s => `
    <div class="bg-white rounded-xl border border-line p-5">
      <div class="text-xs text-slate-500">${s.l}</div>
      <div class="text-3xl font-bold mt-1">${s.v}</div>
      <div class="text-xs text-slate-400 mt-1">${s.s}</div>
    </div>`).join('');

  const groupsHtml = groups.map((g,i) => groupCard(g, i)).join('') ||
    `<div class="col-span-3 bg-white border border-dashed border-line rounded-xl p-12 text-center text-slate-500">
      ${State.pinnedOnly ? '还没有精选群。点击其它群的 ☆ 来精选。' : '没有同步到任何群组。请确认 wechat-cli 可用。'}
    </div>`;

  const sumHtml = latest.length ? latest.map(s => `
    <a href="#asset/${s.id}" class="block py-3 border-b border-line last:border-0 hover:bg-slate-50 -mx-2 px-2 rounded">
      <div class="flex items-center gap-2 text-xs">${badge(s.kind)}<span class="text-slate-500">${escapeHtml(s.group_name)} · ${escapeHtml(s.for_date||'')}</span></div>
      <div class="mt-0.5 font-medium text-ink">${escapeHtml(s.title)}</div>
      <div class="text-sm text-slate-600 mt-0.5 line-clamp-1">${escapeHtml(s.description)}</div>
    </a>`).join('') : `<div class="text-sm text-slate-500 py-3">还没有生成的内容。点击群卡片的「生成精华」开始。</div>`;

  const pinnedCount = groups.filter(g => g.pinned).length;
  const pinToggle = `
    <label class="flex items-center gap-2 text-sm cursor-pointer select-none">
      <input id="pinnedOnlyToggle" type="checkbox" class="rounded border-line text-accent focus:ring-accent" ${State.pinnedOnly?'checked':''}>
      <span>仅显示精选</span>
    </label>`;
  // Always show the toggle — the "(N)" count is only meaningful when the
  // toggle is on (because then the response actually includes hidden groups).
  // When off, the dashboard hides folded-in-WeChat groups silently, and the
  // toggle is how the user discovers them.
  const showHiddenToggle = `
    <label class="flex items-center gap-2 text-sm cursor-pointer select-none" title="显示在微信里折叠/隐藏的群">
      <input id="showHiddenToggle" type="checkbox" class="rounded border-line text-accent focus:ring-accent" ${State.showHidden?'checked':''}>
      <span>显示隐藏${State.showHidden && hiddenCount > 0 ? ` (${hiddenCount})` : ''}</span>
    </label>`;
  const summarizePinnedBtn = pinnedCount > 0
    ? `<div class="relative inline-flex" id="summarizePinnedWrap">
         <button id="summarizePinnedBtn" class="px-3 py-1.5 text-sm bg-accent text-white rounded-l-lg flex items-center gap-1 hover:bg-accent2" title="为精选群生成今天的精华">${icon('sparkle')}<span>精选群全量生成 (${pinnedCount})</span></button>
         <button id="summarizePinnedChevron" class="px-2 py-1.5 text-sm bg-accent text-white rounded-r-lg hover:bg-accent2 border-l border-white/25" title="选择回填范围（按天）" aria-label="选择回填天数">▾</button>
         <div id="summarizePinnedMenu" class="summarizeMenu hidden absolute right-0 top-full mt-1 bg-white rounded-lg shadow-lg border border-line py-1 text-xs whitespace-nowrap z-20 min-w-[140px] text-ink">
           <div class="px-3 py-1.5 text-[10px] uppercase tracking-wider text-slate-400">回填过去</div>
           <button class="summarizePinnedDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="1">1 天 (今天)</button>
           <button class="summarizePinnedDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-yesterday="1">1 天 (昨天)</button>
           <button class="summarizePinnedDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="3">3 天</button>
           <button class="summarizePinnedDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="7">7 天</button>
         </div>
       </div>`
    : '';
  const rebuildDigestBtn = `<button id="rebuildDigestBtn" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 hover:bg-slate-50" title="把今天所有单群精华合并成一份跨群「每日精华」。不会重新跑各群,只在已有精华上做汇总。">${icon('sparkle')}<span>整合今日单群精华</span></button>`;
  const selectBtn = State.selectMode
    ? `<button id="cancelSelect" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 hover:bg-slate-50">${icon('x')}<span>取消选择</span></button>`
    : `<button id="enterSelect" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 hover:bg-slate-50">${icon('check')}<span>多选</span></button>`;
  const refreshBtn = `<button id="refreshBtn" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 hover:bg-slate-50">${icon('refresh')}<span>刷新</span></button>`;

  const syncedAt = status?.wechat?.last_message_at || null;
  const syncedHtml = renderSyncFreshness(syncedAt);

  document.getElementById('app').innerHTML = shell('dashboard', `
    ${topbar(['Dashboard'], `${pinToggle}${showHiddenToggle}${summarizePinnedBtn}${rebuildDigestBtn}${selectBtn}${refreshBtn}`)}
    <div class="p-8 space-y-6 pb-24">
      ${syncedHtml}
      <div class="grid grid-cols-3 gap-4">${statsHtml}</div>
      <section>
        <div class="flex items-baseline justify-between mb-3">
          <h2 class="text-base font-semibold">微信群 (${groups.length})</h2>
          <span class="text-xs text-slate-500">${State.selectMode ? '点击卡片选择 · 选中后点底部按钮批量生成' : '按精选 / 最近活跃排序 · 点 ☆ 精选'}</span>
        </div>
        <div class="grid grid-cols-3 gap-3">${groupsHtml}</div>
      </section>
      <section>
        <div class="flex items-baseline justify-between mb-3">
          <h2 class="text-base font-semibold">最新生成</h2>
          <a href="#summaries" class="text-sm text-accent">查看全部 →</a>
        </div>
        <div class="bg-white rounded-xl border border-line p-5">${sumHtml}</div>
      </section>
    </div>
    ${selectionBar()}
  `, status);

  bindDashboardEvents();
}

/* ---------- summarize-button + days-picker dropdown ----------
 *
 * Renders the "生成精华" button as a split control:
 *   [生成精华] [▾]
 * Plain button = summarize today (the common case, one click).
 * Chevron = open a menu of backfill ranges (3 / 7 / 14 / 30 days).
 *
 * Each option triggers a job that generates one daily_summary per date in the
 * range. (group, date) is enforced as a single summary by the backend, so
 * re-runs replace whatever was there before for that date.
 */
function summarizeButtonHtml(g, opts = {}) {
  const large = !!opts.large;
  const mainCls = large
    ? 'summarizeBtn px-4 py-2 text-sm bg-accent text-white rounded-l-lg hover:bg-accent2 flex items-center gap-1.5'
    : 'summarizeBtn px-2.5 py-1 text-xs bg-accent text-white rounded-l-md hover:bg-accent2 flex items-center gap-1';
  const chevCls = large
    ? 'summarizeChevron px-2 py-2 text-sm bg-accent text-white rounded-r-lg hover:bg-accent2 border-l border-white/25'
    : 'summarizeChevron px-1.5 py-1 text-xs bg-accent text-white rounded-r-md hover:bg-accent2 border-l border-white/25';
  return `
    <div class="relative inline-flex summarizeWrap" data-id="${g.id}" data-name="${escapeHtml(g.name)}">
      <button type="button" class="${mainCls}" title="生成今天的精华">${icon('sparkle')}<span>生成精华</span></button>
      <button type="button" class="${chevCls}" title="选择回填范围（按天）" aria-label="选择回填天数">▾</button>
      <div class="summarizeMenu hidden absolute right-0 top-full mt-1 bg-white rounded-lg shadow-lg border border-line py-1 text-xs whitespace-nowrap z-20 min-w-[140px] text-ink">
        <div class="px-3 py-1.5 text-[10px] uppercase tracking-wider text-slate-400">回填过去</div>
        <button type="button" class="summarizeDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="3">3 天</button>
        <button type="button" class="summarizeDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="7">7 天</button>
        <button type="button" class="summarizeDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="14">14 天</button>
        <button type="button" class="summarizeDaysItem block w-full text-left px-3 py-1.5 hover:bg-slate-50" data-days="30">30 天</button>
      </div>
    </div>`;
}

function bindSummarizeWraps(root = document) {
  for (const wrap of root.querySelectorAll('.summarizeWrap')) {
    const main = wrap.querySelector('.summarizeBtn');
    const chev = wrap.querySelector('.summarizeChevron');
    const menu = wrap.querySelector('.summarizeMenu');
    main?.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); summarizeGroup(wrap, 1); });
    chev?.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      // Close every other open menu first.
      for (const m of document.querySelectorAll('.summarizeMenu')) {
        if (m !== menu) m.classList.add('hidden');
      }
      menu?.classList.toggle('hidden');
    });
    for (const item of wrap.querySelectorAll('.summarizeDaysItem')) {
      item.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        const days = parseInt(item.dataset.days, 10) || 1;
        menu?.classList.add('hidden');
        summarizeGroup(wrap, days);
      });
    }
  }
}

// Global click-outside listener (idempotent): closes any open summarize menu
// when the user clicks elsewhere.
if (!window._summarizeMenuListenerInstalled) {
  window._summarizeMenuListenerInstalled = true;
  document.addEventListener('click', () => {
    for (const m of document.querySelectorAll('.summarizeMenu')) m.classList.add('hidden');
  });
}

function renderSyncFreshness(epochSec) {
  if (!epochSec) {
    return `
      <div class="flex items-center gap-3 bg-slate-100 border border-line rounded-xl px-4 py-2.5 text-sm text-slate-600">
        <span class="inline-block w-2 h-2 rounded-full bg-slate-400"></span>
        <span>未能读取 wechat-cli 数据时间 — 请确认微信桌面端已打开。</span>
      </div>`;
  }
  const diffMin = Math.max(0, (Date.now()/1000 - epochSec) / 60);
  // Fresh: under 30 min · Stale: 30 min – 6 h · Cold: > 6 h
  let tone, dot, hint;
  if (diffMin < 30) {
    tone = 'bg-emerald-50 border-emerald-200 text-emerald-900';
    dot  = 'bg-emerald-500 animate-pulse';
    hint = '数据是新鲜的';
  } else if (diffMin < 360) {
    tone = 'bg-amber-50 border-amber-200 text-amber-900';
    dot  = 'bg-amber-500';
    hint = '已过一段时间 — 在微信里收一条新消息再回来刷新';
  } else {
    tone = 'bg-rose-50 border-rose-200 text-rose-900';
    dot  = 'bg-rose-500';
    hint = '数据可能过期 — 请确认微信桌面端已打开并接收新消息';
  }
  return `
    <div class="flex items-center gap-3 ${tone} border rounded-xl px-4 py-2.5 text-sm">
      <span class="inline-block w-2 h-2 rounded-full ${dot}"></span>
      <span class="font-medium">最新消息 · ${escapeHtml(fmtAbsTime(epochSec))}</span>
      <span class="opacity-70">(${escapeHtml(fmtRelTime(epochSec))})</span>
      <span class="ml-auto text-xs opacity-70">${escapeHtml(hint)}</span>
    </div>`;
}

function groupCard(g, i) {
  const selected = State.selection.has(g.id);
  const ringCls = selected
    ? 'border-accent ring-2 ring-accent/30'
    : 'border-line hover:border-accent2';
  // Hidden groups still render (when the dashboard toggle is on) but at
  // reduced opacity, so they read as "tucked away" without being invisible.
  const fadeCls = g.hidden ? 'opacity-60' : '';
  const hideBtn = `
    <button class="absolute top-3 right-9 ${g.hidden?'text-slate-500 hover:text-ink':'text-slate-300 hover:text-slate-600'} hideBtn" data-id="${g.id}" data-hidden="${g.hidden?1:0}" title="${g.hidden?'取消隐藏':'隐藏这个群'}">
      ${icon(g.hidden ? 'eye' : 'eyeOff')}
    </button>`;
  return `
    <div class="bg-white rounded-xl border ${ringCls} ${fadeCls} p-4 transition relative groupCard" data-id="${g.id}" data-name="${escapeHtml(g.name)}">
      ${hideBtn}
      <button class="absolute top-3 right-3 ${g.pinned?'text-amber-500':'text-slate-300 hover:text-amber-500'} pinBtn" data-id="${g.id}" data-pinned="${g.pinned?1:0}" title="${g.pinned?'取消精选':'加入精选'}">
        ${icon('star')}
      </button>
      <a href="#group/${g.id}" class="block pr-7">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-lg bg-gradient-to-br ${g.accent_color || PALETTES[i%6]} grid place-items-center text-white font-bold text-sm shrink-0">${escapeHtml(emoji(g.name))}</div>
          <div class="min-w-0 flex-1">
            <div class="font-medium text-ink truncate">${escapeHtml(g.name)}</div>
            <div class="text-xs text-slate-500 truncate">${escapeHtml(g.last_sender||'')}: ${escapeHtml(g.last_message||'')}</div>
          </div>
        </div>
      </a>
      <div class="mt-3 flex items-center justify-between text-xs">
        <div class="flex gap-3 text-slate-500">
          <span>未读 <span class="font-semibold ${g.unread?'text-ink':''}">${g.unread||0}</span></span>
          <span>精华 <span class="font-semibold ${g.asset_count?'text-indigo-600':''}">${g.asset_count||0}</span></span>
        </div>
        ${State.selectMode
          ? `<label class="flex items-center gap-1.5 cursor-pointer">
               <input type="checkbox" class="rounded border-line text-accent focus:ring-accent selCheckbox" data-id="${g.id}" ${selected?'checked':''}>
               <span class="text-slate-500">选中</span>
             </label>`
          : summarizeButtonHtml(g)}
      </div>
    </div>`;
}

function selectionBar() {
  if (!State.selectMode || State.selection.size === 0) return '';
  return `
    <div class="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 flex items-center gap-3 bg-ink text-white px-5 py-3 rounded-xl shadow-2xl">
      <div class="text-sm">已选 <span class="font-semibold">${State.selection.size}</span> 个群</div>
      <div class="w-px h-5 bg-white/20"></div>
      <button id="bulkRunBtn" class="px-3 py-1.5 bg-accent rounded-lg text-sm flex items-center gap-1 hover:bg-accent2">
        ${icon('sparkle')}<span>批量生成精华</span>
      </button>
      <button id="bulkClearBtn" class="px-3 py-1.5 text-sm text-white/70 hover:text-white">清空</button>
    </div>`;
}

/* Bulk progress is driven by the backend job API. The bar lives in its own
   fixed DOM node outside #app so dashboard re-renders don't disturb it. */
function renderBulkBar(job) {
  let el = document.getElementById('bulkBar');
  if (!job) { if (el) el.remove(); return; }
  const { id, done, total, current_name, errors, results = [], current_started_at, status, cancel_requested } = job;
  const running = status === 'running';
  const cancelling = running && cancel_requested;
  const nowSec = Date.now() / 1000;
  const elapsed = running && current_started_at ? Math.floor(nowSec - current_started_at) : 0;
  const elapsedTxt = running && elapsed > 2 ? ` · ${elapsed}s` : '';
  // Count skipped vs successful out of job.results — skipped items are
  // legitimate (empty day / day closed / low activity / no new messages)
  // and shouldn't be counted toward the red "failures" bucket.
  const skipCount = results.filter(r => r && r.skipped).length;
  const successCount = Math.max(0, results.length - skipCount);
  const errorCount = errors.length;
  // Default errors panel to expanded the first time a finished job has any —
  // makes the failure detail one-glance discoverable. User can collapse via
  // the same toggle and the choice sticks for that bar.
  if (el && !el.dataset.errorsExpandedTouched && !running && errorCount > 0) {
    el.dataset.errorsExpanded = '1';
    el.dataset.errorsExpandedTouched = '1';
  }
  const errorsExpanded = el?.dataset.errorsExpanded === '1';
  // Surface the "查看 N 个失败" toggle the moment any failure shows up —
  // including while the job is still running — so the user can drill in
  // without waiting for completion.
  const hasErrorsToShow = errorCount > 0;
  const errorRows = hasErrorsToShow && errorsExpanded ? `
    <div class="mt-3 max-h-48 overflow-y-auto bg-white/5 rounded-md p-2 text-xs space-y-1.5">
      ${errors.map(e => {
        const fullMsg = (e.msg || '').replaceAll('"', '&quot;');
        return `
        <div class="flex gap-2 group/err" title="${escapeHtml(fullMsg)}">
          <span class="text-rose-300 shrink-0">✗</span>
          <div class="min-w-0">
            <div class="font-medium text-white">${escapeHtml(e.name || e.group_id || '?')}${e.for_date ? ` · ${escapeHtml(e.for_date)}` : ''}</div>
            <div class="text-white/60 break-words whitespace-pre-wrap">${escapeHtml(e.msg || '')}</div>
          </div>
        </div>`;
      }).join('')}
    </div>` : '';
  const errorToggle = hasErrorsToShow
    ? `<button id="bulkErrorsToggle" class="text-rose-300 hover:text-rose-200 text-xs">${errorsExpanded ? '收起详情' : `查看 ${errorCount} 个失败`}</button>`
    : '';
  const dotColor = cancelling ? 'bg-amber-400' : running ? 'bg-emerald-400' : 'bg-slate-400';
  const headLabel = cancelling
    ? '正在取消…'
    : (running ? '批量生成中…' : (status === 'cancelled' ? '已取消' : '完成'));
  // Tally line is always computed; rendered on its own line below the body
  // so it shows during the run (live counts) without crowding the
  // "正在处理：xxx" status into a truncate-ellipsis. Each part appears only
  // when non-zero.
  const tallyParts = [];
  if (successCount > 0) tallyParts.push(`${successCount} 个成功`);
  if (skipCount > 0) tallyParts.push(`<span class="text-slate-300">${skipCount} 个跳过</span>`);
  if (errorCount > 0) tallyParts.push(`<span class="text-rose-300">${errorCount} 个失败</span>`);
  const tallyLine = tallyParts.length
    ? `<div class="mt-1 text-xs text-white/70">${tallyParts.join(' · ')}</div>`
    : '';
  const bodyLabel = cancelling
    ? '收到取消请求,正在终止当前 Claude 调用…'
    : (running
       ? '正在处理：' + escapeHtml(current_name||'') + elapsedTxt + ' · 每组约 60-120 秒'
       : (status === 'cancelled' ? '已取消' : '完成。'));
  const html = `
    <div class="flex items-center justify-between text-sm">
      <div class="flex items-center gap-2">
        <span class="inline-block w-2 h-2 rounded-full ${dotColor} ${running ? 'animate-pulse' : ''}"></span>
        <span>${headLabel} <span class="font-semibold">${done}/${total}</span></span>
      </div>
      <div class="flex items-center gap-3 text-xs">
        ${errorToggle}
        ${running
          ? `<button id="bulkCancel" class="text-white/70 hover:text-white ${cancelling ? 'opacity-40 cursor-not-allowed' : ''}" ${cancelling ? 'disabled' : ''}>${cancelling ? '取消中…' : '取消'}</button>`
          : '<button id="bulkDismiss" class="text-white/70 hover:text-white">关闭</button>'}
      </div>
    </div>
    <div class="mt-2 h-2 bg-white/10 rounded overflow-hidden">
      <div class="h-full ${cancelling ? 'bg-amber-400' : errorCount > 0 ? 'bg-amber-400' : 'bg-accent'} transition-all duration-300" style="width:${(done/total*100).toFixed(1)}%"></div>
    </div>
    <div class="mt-2 text-xs text-white/70 truncate">${bodyLabel}</div>
    ${tallyLine}
    ${errorRows}`;
  if (!el) {
    el = document.createElement('div');
    el.id = 'bulkBar';
    el.className = 'fixed bottom-6 left-1/2 -translate-x-1/2 z-40 w-[520px] bg-ink text-white p-4 rounded-xl shadow-2xl';
    document.body.appendChild(el);
  }
  el.innerHTML = html;
  el.querySelector('#bulkDismiss')?.addEventListener('click', () => {
    localStorage.removeItem('cl.bulkJobId'); renderBulkBar(null);
  });
  el.querySelector('#bulkCancel')?.addEventListener('click', async () => {
    if (!confirm('取消批量生成？已经完成的精华会保留。')) return;
    try { await api(`/api/jobs/${id}/cancel`, { method:'POST', body:'{}' }); }
    catch (e) { toast(e.message, 'error'); }
  });
  el.querySelector('#bulkErrorsToggle')?.addEventListener('click', () => {
    el.dataset.errorsExpanded = errorsExpanded ? '0' : '1';
    renderBulkBar(job);
  });
}

let _bulkPollTimer = null;
function startBulkPoll(jobId) {
  if (_bulkPollTimer) clearInterval(_bulkPollTimer);
  localStorage.setItem('cl.bulkJobId', jobId);
  const tick = async () => {
    let job;
    try { job = await api(`/api/jobs/${jobId}`); }
    catch (e) {
      // Job vanished (server restart, manual cleanup) — stop polling.
      clearInterval(_bulkPollTimer); _bulkPollTimer = null;
      localStorage.removeItem('cl.bulkJobId');
      renderBulkBar(null);
      return;
    }
    renderBulkBar(job);
    if (job.status !== 'running') {
      clearInterval(_bulkPollTimer); _bulkPollTimer = null;
      const ok = job.done - job.errors.length;
      toast(`批量生成完成：${ok}/${job.total} 成功`, job.errors.length ? 'error' : 'success');
      if (job.errors.length) invalidateStatus();
      // Refresh the dashboard once so updated 精华 counts show up.
      render();
    }
  };
  tick();
  _bulkPollTimer = setInterval(tick, 1500);
}

async function attachToActiveJob() {
  // On page load: pick up any in-flight jobs the server is still running, so
  // closing/reopening the tab doesn't drop the progress UI. Bulk goes to the
  // big bottom-center bar; single jobs go to the bottom-right chip stack.
  let jobsList = [];
  try { jobsList = await api('/api/jobs?include_recent=true'); } catch {}
  const stored = localStorage.getItem('cl.bulkJobId');
  const runningBulk = jobsList.find(j => j.kind === 'bulk_summarize' && j.status === 'running');
  if (runningBulk) startBulkPoll(runningBulk.id);
  else if (stored) startBulkPoll(stored);  // safety net if server lost the job mid-restart
  else {
    // No active bulk and no localStorage handle. If the latest finished bulk
    // had failures and the user hasn't dismissed it, re-surface so failures
    // can't silently disappear behind a closed window.
    const dismissed = JSON.parse(localStorage.getItem('cl.bulkDismissed') || '[]');
    const recent = jobsList
      .filter(j => j.kind === 'bulk_summarize' && j.status !== 'running')
      .sort((a, b) => (b.finished_at || 0) - (a.finished_at || 0))[0];
    if (recent && (recent.errors?.length || 0) > 0 && !dismissed.includes(recent.id)) {
      renderBulkBar(recent);
      const el = document.getElementById('bulkBar');
      el?.querySelector('#bulkDismiss')?.addEventListener('click', () => {
        const arr = JSON.parse(localStorage.getItem('cl.bulkDismissed') || '[]');
        if (!arr.includes(recent.id)) arr.push(recent.id);
        localStorage.setItem('cl.bulkDismissed', JSON.stringify(arr.slice(-50)));
      });
    }
  }
  for (const j of jobsList) {
    if (j.status === 'running' && (j.kind === 'single_summarize' || j.kind === 'digest_rebuild')) {
      startSinglePoll(j.id);
    }
  }
}

function bindDashboardEvents() {
  document.getElementById('refreshBtn')?.addEventListener('click', () => { invalidateStatus(); render(); });
  document.getElementById('summarizePinnedBtn')?.addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation();
    summarizeAllPinned({ days: 1 });
  });
  document.getElementById('summarizePinnedChevron')?.addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation();
    const menu = document.getElementById('summarizePinnedMenu');
    // Close other open menus first.
    for (const m of document.querySelectorAll('.summarizeMenu')) m.classList.add('hidden');
    menu?.classList.toggle('hidden');
  });
  for (const item of document.querySelectorAll('.summarizePinnedDaysItem')) {
    item.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      document.getElementById('summarizePinnedMenu')?.classList.add('hidden');
      if (item.dataset.yesterday) {
        // Compute yesterday in the user's local TZ — picking the operator's
        // wall-clock "yesterday" matches how they think about chat history.
        const d = new Date();
        d.setDate(d.getDate() - 1);
        const dateStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        summarizeAllPinned({ dates: [dateStr] });
      } else {
        summarizeAllPinned({ days: parseInt(item.dataset.days, 10) || 1 });
      }
    });
  }
  document.getElementById('rebuildDigestBtn')?.addEventListener('click', () => rebuildTodayDigest());
  document.getElementById('pinnedOnlyToggle')?.addEventListener('change', (e) => {
    State.pinnedOnly = e.target.checked; persistPinnedOnly(); render();
  });
  document.getElementById('showHiddenToggle')?.addEventListener('change', (e) => {
    State.showHidden = e.target.checked; persistShowHidden(); render();
  });
  document.getElementById('enterSelect')?.addEventListener('click', () => {
    State.selectMode = true; render();
  });
  document.getElementById('cancelSelect')?.addEventListener('click', () => {
    clearSelection(); render();
  });
  document.getElementById('bulkClearBtn')?.addEventListener('click', () => {
    State.selection.clear(); render();
  });
  document.getElementById('bulkRunBtn')?.addEventListener('click', () => runBulkSummarize());

  for (const b of document.querySelectorAll('.pinBtn')) {
    b.addEventListener('click', async (e) => {
      e.stopPropagation(); e.preventDefault();
      const pinned = b.dataset.pinned === '1';
      try {
        await api(`/api/groups/${b.dataset.id}/pin`, { method:'POST', body: JSON.stringify({ pinned: !pinned }) });
        render();
      } catch (err) { toast(err.message, 'error'); }
    });
  }
  for (const b of document.querySelectorAll('.hideBtn')) {
    b.addEventListener('click', async (e) => {
      e.stopPropagation(); e.preventDefault();
      const hidden = b.dataset.hidden === '1';
      try {
        await api(`/api/groups/${b.dataset.id}/hide`, { method:'POST', body: JSON.stringify({ hidden: !hidden }) });
        render();
      } catch (err) { toast(err.message, 'error'); }
    });
  }
  for (const c of document.querySelectorAll('.selCheckbox')) {
    c.addEventListener('change', () => {
      if (c.checked) State.selection.add(c.dataset.id);
      else State.selection.delete(c.dataset.id);
      render();
    });
  }
  if (State.selectMode) {
    // Suppress browser text-selection range that shift+click otherwise triggers.
    const suppressSelect = (e) => { if (e.shiftKey) e.preventDefault(); };
    for (const card of document.querySelectorAll('.groupCard')) {
      card.addEventListener('mousedown', suppressSelect);
      card.addEventListener('click', (e) => {
        if (e.target.closest('.pinBtn') || e.target.closest('.hideBtn')) return;
        e.preventDefault();
        e.stopPropagation();
        const id = card.dataset.id;
        if (e.shiftKey && State.lastGroupClicked && State.lastGroupClicked !== id) {
          rangeSelect(State.visibleGroupIds, State.lastGroupClicked, id, State.selection);
        } else if (State.selection.has(id)) {
          State.selection.delete(id);
        } else {
          State.selection.add(id);
        }
        State.lastGroupClicked = id;
        window.getSelection?.()?.removeAllRanges();
        render();
      });
    }
  }
  bindSummarizeWraps();
}

async function summarizeGroup(wrapEl, days = 1) {
  const id = wrapEl.dataset.id, name = wrapEl.dataset.name;
  const btnEl = wrapEl.querySelector('.summarizeBtn');
  const chevEl = wrapEl.querySelector('.summarizeChevron');
  const original = btnEl.innerHTML;
  btnEl.disabled = true;
  if (chevEl) chevEl.disabled = true;
  const label = days > 1 ? `回填 ${days} 天…` : '分析中…';
  btnEl.innerHTML = `<span class="inline-block w-3 h-3 rounded-full bg-white/40 animate-pulse"></span><span>${label}</span>`;
  btnEl.classList.add('opacity-80','cursor-wait');
  try {
    const path = days > 1
      ? `/api/groups/${id}/summarize?days=${days}`
      : `/api/groups/${id}/summarize`;
    const r = await api(path, { method:'POST', body:'{}' });
    const tmsg = days > 1
      ? `正在为「${name}」回填过去 ${days} 天的精华(每天约 30–90 秒)`
      : `正在为「${name}」生成精华，约 30–90 秒（关闭页面也不会中断）`;
    toast(tmsg);
    // Don't auto-navigate on completion — yanking the user out of the
    // dashboard / current view is jarring, especially when they're queueing
    // multiple groups. The success toast + refreshed counts on the current
    // page are enough; they'll click into the asset when they want to read it.
    startSinglePoll(r.job_id);
  } catch (e) {
    toast(e.message, 'error');
    btnEl.disabled = false;
    if (chevEl) chevEl.disabled = false;
    btnEl.innerHTML = original;
    btnEl.classList.remove('opacity-80','cursor-wait');
  }
}

/* Single-summarize background jobs. Backend always runs them as jobs so the
   browser can close without losing work. We track each active job locally to
   render a compact floating chip; on reload we reattach via /api/jobs. */
const _singleJobs = new Map();         // jobId -> { timerId, snapshot, autoNavigate }
const _singleAutoNavigate = new Set(); // jobIds that should jump to the new asset on done

function startSinglePoll(jobId, opts = {}) {
  if (opts.autoNavigate) _singleAutoNavigate.add(jobId);
  if (_singleJobs.has(jobId)) return;
  const entry = { timerId: null, snapshot: null };
  _singleJobs.set(jobId, entry);
  const tick = async () => {
    let job;
    try { job = await api(`/api/jobs/${jobId}`); }
    catch {
      _stopSinglePoll(jobId);
      renderSingleChips();
      return;
    }
    entry.snapshot = job;
    renderSingleChips();
    if (job.status !== 'running') {
      _stopSinglePoll(jobId);
      _finishSingle(job);
    }
  };
  tick();
  entry.timerId = setInterval(tick, 1500);
}

function _stopSinglePoll(jobId) {
  const entry = _singleJobs.get(jobId);
  if (entry?.timerId) clearInterval(entry.timerId);
  _singleJobs.delete(jobId);
}

function _finishSingle(job) {
  const autoNav = _singleAutoNavigate.has(job.id);
  _singleAutoNavigate.delete(job.id);
  if (job.errors.length) {
    toast(`「${job.current_name||''}」生成失败：${job.errors[0].msg}`, 'error');
    invalidateStatus();
  } else {
    const r = job.results[0] || {};
    // For single-group summaries we get summary_id; for digest rebuilds we get
    // daily_digest_id. Either way the navigation target is an asset page.
    const targetId = r.summary_id || r.daily_digest_id;
    if (job.kind === 'digest_rebuild') {
      toast(`「${job.current_name||''}」已生成`, 'success');
    } else if (r.skipped) {
      // Skip is a successful no-op (empty day / day closed / low activity /
      // no new messages). Surface it as info rather than an "已生成" lie.
      const label = {
        no_messages: '当天没有聊天记录',
        low_activity: '消息量太少',
        day_closed: '已有精华且这一天已过',
        no_new_messages: '没有新消息',
        delta_too_small: '新消息太少',
      }[r.reason] || '已跳过';
      toast(`「${job.current_name||''}」已跳过 · ${label}`, 'info');
    } else {
      const extra = r.share_count ? `，含 ${r.share_count} 篇嘉宾分享` : '';
      toast(`「${job.current_name||''}」精华已生成（${r.message_count||0} 条消息${extra}）`, 'success');
    }
    if (autoNav && targetId) {
      location.hash = '#asset/' + targetId;
      renderSingleChips();
      return;
    }
  }
  // Refresh the visible page so updated counts / new asset rows appear.
  const h = location.hash || '#dashboard';
  if (h.startsWith('#dashboard') || h.startsWith('#group/')) render();
  renderSingleChips();
}

function renderSingleChips() {
  let host = document.getElementById('singleChips');
  const entries = [..._singleJobs.values()].filter(e => e.snapshot);
  if (!entries.length) { if (host) host.remove(); return; }
  if (!host) {
    host = document.createElement('div');
    host.id = 'singleChips';
    host.className = 'fixed bottom-6 right-6 z-40 flex flex-col gap-2 items-end';
    document.body.appendChild(host);
    // One-time CSS injection for the indeterminate progress bar animation.
    if (!document.getElementById('cl-indeterminate-style')) {
      const s = document.createElement('style');
      s.id = 'cl-indeterminate-style';
      s.textContent = `@keyframes cl-indet { 0% { left:-40%; } 100% { left:100%; } }
        .cl-indet { position:absolute; top:0; bottom:0; width:40%; background:linear-gradient(90deg, transparent, #6366F1, transparent); animation: cl-indet 1.5s linear infinite; }`;
      document.head.appendChild(s);
    }
  }
  const nowSec = Date.now() / 1000;
  host.innerHTML = entries.map(({ snapshot: j }) => {
    const elapsed = j.current_started_at ? Math.max(0, Math.floor(nowSec - j.current_started_at)) : 0;
    const running = j.status === 'running';
    const cancelling = running && j.cancel_requested;
    const dotCls = cancelling ? 'bg-amber-400' : running ? 'bg-emerald-400' : 'bg-slate-400';
    const dot = `<span class="inline-block w-2 h-2 rounded-full ${dotCls} ${running ? 'animate-pulse' : ''}"></span>`;
    // Determinate progress when total > 1 (multi-day backfill); otherwise an
    // indeterminate bar that scrolls to prove the UI is alive.
    const showDeterminate = running && j.total > 1;
    const determinateBar = showDeterminate
      ? `<div class="mt-1.5 h-1 bg-white/10 rounded overflow-hidden">
          <div class="h-full ${cancelling ? 'bg-amber-400' : 'bg-accent'} transition-all duration-300" style="width:${(j.done/j.total*100).toFixed(1)}%"></div>
        </div>`
      : '';
    const indeterminateBar = running && !showDeterminate
      ? `<div class="mt-1.5 relative h-1 bg-white/10 rounded overflow-hidden"><div class="cl-indet"></div></div>`
      : '';
    const headline = cancelling
      ? `正在取消「${escapeHtml(j.current_name||'')}」…`
      : running
        ? `生成中:「${escapeHtml(j.current_name||'')}」`
        : (j.errors.length ? `「${escapeHtml(j.current_name||'')}」失败` : `「${escapeHtml(j.current_name||'')}」已完成`);
    const phaseLine = running && j.current_phase
      ? `<div class="text-white/70 text-[11px] truncate">${escapeHtml(j.current_phase)} · ${elapsed}s${j.total > 1 ? ` · ${j.done}/${j.total}` : ''}</div>`
      : (running ? `<div class="text-white/70 text-[11px]">${elapsed}s${j.total > 1 ? ` · ${j.done}/${j.total}` : ''}</div>` : '');
    const cancelBtn = running && !cancelling
      ? `<button class="singleCancelBtn ml-2 text-white/60 hover:text-white text-[11px]" data-job="${escapeHtml(j.id)}" title="取消">${icon('x')}</button>`
      : (cancelling ? `<span class="ml-2 text-amber-300 text-[11px]">取消中</span>` : '');
    return `<div class="bg-ink text-white px-3 py-2 rounded-lg shadow-2xl text-xs min-w-[260px] max-w-[360px]">
      <div class="flex items-center gap-2">
        ${dot}<span class="truncate flex-1">${headline}</span>${cancelBtn}
      </div>
      ${phaseLine}
      ${determinateBar}${indeterminateBar}
    </div>`;
  }).join('');
  for (const b of host.querySelectorAll('.singleCancelBtn')) {
    b.addEventListener('click', async (e) => {
      e.preventDefault();
      const jobId = b.dataset.job;
      if (!confirm('取消生成?已完成的部分会保留。')) return;
      try { await api(`/api/jobs/${jobId}/cancel`, { method:'POST', body:'{}' }); }
      catch (err) { toast(err.message, 'error'); }
    });
  }
}

async function rebuildTodayDigest() {
  try {
    const r = await api('/api/digest/rebuild', { method:'POST', body:'{}' });
    toast(`正在用 ${r.source_count} 份单群精华生成 ${r.date} 跨群汇总… 关闭页面也不会中断`);
    startSinglePoll(r.job_id, { autoNavigate: true });
  } catch (e) { toast(e.message, 'error'); }
}

async function summarizeAllPinned(opts = {}) {
  // opts: { days: int } | { dates: ['YYYY-MM-DD', ...] }. days takes the
  // today-anchored shorthand path; dates is explicit (used by yesterday-only).
  const pinned = await api('/api/groups?pinned_only=true');
  if (!pinned.length) { toast('还没有精选群。先点 ☆ 加几个再来。', 'error'); return; }
  const dates = opts.dates;
  const days = opts.days || (dates ? dates.length : 1);
  const total = pinned.length * (dates ? dates.length : days);
  let msg;
  if (dates && dates.length === 1) {
    msg = `为 ${pinned.length} 个精选群生成 ${dates[0]} 的精华？\n大约 ${total * 1}–${total * 2} 分钟。`;
  } else if (days > 1) {
    msg = `为 ${pinned.length} 个精选群批量回填过去 ${days} 天的精华？\n共 ${total} 次生成，大约 ${total * 1}–${total * 2} 分钟。`;
  } else {
    msg = `为 ${pinned.length} 个精选群批量生成今天的精华？\n大约 ${pinned.length * 1}–${pinned.length * 2} 分钟。`;
  }
  if (!confirm(msg)) return;
  await launchBulkSummarize(pinned.map(g => g.id), opts);
}

async function runBulkSummarize() {
  // Triggered by the in-list "批量生成精华" button after multi-select.
  const ids = [...State.selection];
  if (!ids.length) return;
  State.selectMode = false; State.selection.clear();
  document.querySelector('.fixed.bottom-6.left-1\\/2.-translate-x-1\\/2.flex')?.remove();
  await launchBulkSummarize(ids);
}

async function launchBulkSummarize(ids, opts = {}) {
  // opts: { days?: int, dates?: ['YYYY-MM-DD', ...] }. When `dates` is set
  // it wins server-side; otherwise the today-anchored `days` window is used.
  try {
    const body = { group_ids: ids };
    if (Array.isArray(opts.dates) && opts.dates.length) body.dates = opts.dates;
    else body.days = opts.days || 1;
    const r = await api('/api/groups/bulk-summarize', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    startBulkPoll(r.job_id);
  } catch (e) {
    toast('批量启动失败：' + e.message, 'error');
  }
}

/* ---------- group detail ---------- */
function sortGroupAssets(assets, dir) {
  // Primary key: the date the chat happened (for_date), falling back to the
  // generation date. Tiebreak by created_at so two assets for the same day
  // still have a stable, meaningful order.
  const factor = dir === 'desc' ? -1 : 1;
  return [...assets].sort((a, b) => {
    const da = a.for_date || (a.created_at || '').slice(0, 10);
    const dbk = b.for_date || (b.created_at || '').slice(0, 10);
    if (da !== dbk) return factor * da.localeCompare(dbk);
    return factor * (a.created_at || '').localeCompare(b.created_at || '');
  });
}

async function pageGroup(id) {
  const status = await loadStatus();
  const g = await api(`/api/groups/${id}`);
  const sortDir = State.groupAssetSortDir;
  const sortArrow = sortDir === 'desc' ? '▼' : '▲';
  const rows = sortGroupAssets(g.assets, sortDir).map(a => `
    <tr class="border-b border-line hover:bg-slate-50 group">
      <td class="py-3 px-4 w-28">${badge(a.kind)}</td>
      <td class="py-3 px-4"><a href="#asset/${a.id}" class="text-accent hover:underline font-medium">${escapeHtml(a.title)}</a></td>
      <td class="py-3 px-4 text-sm text-slate-600 max-w-md truncate">${escapeHtml(a.description)}</td>
      <td class="py-3 px-4 text-sm text-slate-500 tabular-nums whitespace-nowrap">${escapeHtml(a.for_date||a.created_at.slice(0,10))}</td>
      <td class="py-3 px-4 w-12 text-right">
        <button class="text-slate-400 hover:text-rose-600 delBtn" data-id="${a.id}" title="删除">${icon('trash')}</button>
      </td>
    </tr>`).join('') || `<tr><td colspan="5" class="py-12 text-center text-slate-500">这个群还没有精华。点击右上角「生成精华」开始。</td></tr>`;
  document.getElementById('app').innerHTML = shell('dashboard', `
    ${topbar([{label:'Dashboard',href:'dashboard'}, g.name])}
    <div class="p-8">
      <div class="flex items-center gap-4 mb-6">
        <a href="#dashboard" class="text-slate-400 hover:text-ink">${icon('back')}</a>
        <div class="w-14 h-14 rounded-xl bg-gradient-to-br ${g.accent_color||PALETTES[0]} grid place-items-center text-white font-bold text-2xl">${escapeHtml(emoji(g.name))}</div>
        <div>
          <h1 class="text-xl font-bold flex items-center gap-2">
            ${escapeHtml(g.name)}
            ${g.pinned ? `<span class="text-amber-500" title="精选">${icon('star')}</span>` : ''}
          </h1>
          <div class="text-sm text-slate-500 mt-0.5">${g.assets.length} 条内容</div>
        </div>
        <div class="ml-auto">${summarizeButtonHtml(g, { large: true })}</div>
      </div>
      <div class="bg-white rounded-xl border border-line overflow-hidden">
        <table class="w-full text-sm">
          <thead class="bg-slate-50 text-xs text-slate-500 text-left">
            <tr><th class="py-2.5 px-4">类型</th><th class="py-2.5 px-4">标题</th><th class="py-2.5 px-4">摘要</th><th id="sortByDate" class="py-2.5 px-4 w-28 cursor-pointer select-none hover:text-ink" title="点击切换排序方向">日期 <span class="text-slate-400">${sortArrow}</span></th><th></th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`, status);
  bindSummarizeWraps();
  for (const b of document.querySelectorAll('.delBtn')) {
    b.addEventListener('click', async () => {
      if (!confirm('删除这条内容？')) return;
      try { await api(`/api/assets/${b.dataset.id}`, { method:'DELETE' }); render(); }
      catch (e) { toast(e.message, 'error'); }
    });
  }
  document.getElementById('sortByDate')?.addEventListener('click', () => {
    State.groupAssetSortDir = State.groupAssetSortDir === 'desc' ? 'asc' : 'desc';
    localStorage.setItem('cl.groupAssetSortDir', State.groupAssetSortDir);
    render();
  });
}

/* ---------- asset view ---------- */
async function pageAsset(id) {
  const status = await loadStatus();
  const a = await api(`/api/assets/${id}`);
  // Wrap `[Tn]` topic markers in the rendered body with anchor links to the
  // topic detail page. Done as a string substitution post-render so it doesn't
  // require touching the markdown parser.
  let articleHtml = renderMd(a.body_md);
  if (a.kind === 'daily_summary' && a.topics && a.topics.length) {
    articleHtml = articleHtml.replace(/<li>(\[T(\d+)\][^<]*)<\/li>/g, (m, body, n) => {
      const topicId = 'T' + n;
      return `<li><a href="#topic/${a.id}/${topicId}" class="text-accent hover:underline">${body}</a></li>`;
    });
  }
  // For guest shares: pull the original-chat thread out of meta into a dedicated
  // section the operator can scroll back through, with inline images.
  let originalChatHtml = '';
  if (a.kind === 'guest_share' && a.topics && a.topics[0]?.messages?.length) {
    const msgs = a.topics[0].messages;
    originalChatHtml = `
      <section class="mt-6 bg-white rounded-xl border border-line overflow-hidden">
        <div class="px-5 py-3 border-b border-line bg-slate-50 flex items-center justify-between">
          <h3 class="text-sm font-semibold">📜 原始对话 · ${msgs.length} 条</h3>
          <span class="text-xs text-slate-500">${a.speaker ? '主讲：' + escapeHtml(a.speaker) : ''}</span>
        </div>
        <div class="divide-y divide-line">
          ${msgs.map(m => `
            <div class="px-5 py-3 flex gap-4">
              <div class="text-xs text-slate-400 tabular-nums whitespace-nowrap shrink-0 w-36">${escapeHtml(m.ts||'')}</div>
              <div class="min-w-0 flex-1">
                <div class="text-xs text-slate-500 font-medium">${escapeHtml(m.sender||'')}</div>
                <div class="mt-1 text-ink whitespace-pre-wrap break-words">${renderMessageText(m.text||'', a.id)}</div>
              </div>
            </div>`).join('')}
        </div>
      </section>`;
  }
  const speakerBadge = a.kind === 'guest_share' && a.speaker
    ? `<span class="px-2 py-0.5 text-xs rounded font-medium bg-amber-100 text-amber-800">主讲：${escapeHtml(a.speaker)}</span>`
    : '';
  const deleteBtn = `<button id="assetDeleteBtn" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 text-slate-500 hover:text-rose-600 hover:border-rose-300 hover:bg-rose-50">${icon('trash')}<span>删除</span></button>`;
  // Daily-digest assets aren't tied to a single group — use a different
  // breadcrumb path and a different left-nav highlight.
  const isDigest = a.kind === 'daily_digest';
  const crumbs = isDigest
    ? [{label:'每日精华',href:'digests'}, a.title]
    : [{label:'Dashboard',href:'dashboard'}, {label:a.group_name,href:'group/'+a.group_id}, a.title];
  const activeNav = isDigest ? 'digests' : 'summaries';
  const metaLine = isDigest
    ? `<span class="text-slate-500">${escapeHtml(a.for_date || a.created_at.slice(0,10))}</span>`
    : `<span class="text-slate-500">${escapeHtml(a.group_name)} · ${escapeHtml(a.for_date || a.created_at.slice(0,10))}</span>`;
  // Show "更新于 ..." when the asset has been overwritten in place (via the
  // incremental path) — `updated_at` is set then. Fresh assets keep
  // updated_at = null and display "生成于 created_at".
  const _stampIso = a.updated_at || a.created_at;
  const _stampLabel = a.updated_at ? '更新于' : '生成于';
  const _stampSec = _stampIso ? Date.parse(_stampIso) / 1000 : 0;
  const createdLine = _stampSec
    ? `<span class="text-slate-500">· ${_stampLabel} ${escapeHtml(fmtAbsTime(_stampSec))} <span class="text-slate-400">(${escapeHtml(fmtRelTime(_stampSec))})</span></span>`
    : '';
  document.getElementById('app').innerHTML = shell(activeNav, `
    ${topbar(crumbs, deleteBtn)}
    <div class="p-8 max-w-4xl">
      <div class="flex items-center gap-2 text-xs mb-3 flex-wrap">
        ${badge(a.kind)}
        ${speakerBadge}
        ${metaLine}
        ${createdLine}
      </div>
      <article class="prose-zh bg-white rounded-xl border border-line p-8">${articleHtml}</article>
      ${originalChatHtml}
    </div>`, status);
  document.getElementById('assetDeleteBtn').addEventListener('click', async () => {
    if (!confirm(`删除「${a.title}」？此操作不可撤销。`)) return;
    try {
      await api(`/api/assets/${a.id}`, { method:'DELETE' });
      toast('已删除', 'success');
      location.hash = '#group/' + a.group_id;
    } catch (e) { toast(e.message, 'error'); }
  });
}

/* ---------- topic detail (one topic, its messages with inline images) ---------- */
function renderMessageText(text, assetId) {
  // Convert `[图片 xxx.jpg]` into <img> served from /api/media/<assetId>/<file>.
  // Also auto-link bare URLs and escape the rest.
  const SENT = '';
  const stash = [];
  const park = (html) => { stash.push(html); return SENT + (stash.length - 1) + SENT; };
  let s = escapeHtml(text);
  s = s.replace(/\[图片\s+([^\]]+)\]/g, (_, fname) =>
    park(`<img src="/api/media/${assetId}/${encodeURIComponent(fname.trim())}" class="my-2 rounded-lg border border-line max-w-[260px] max-h-64 object-contain cursor-zoom-in" alt="" onclick="window.open(this.src,'_blank')">`));
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>"'】)]+)/g, (_, pre, url) =>
    pre + park(`<a href="${url}" target="_blank" rel="noopener" class="text-accent hover:underline">${url}</a>`));
  s = s.replace(new RegExp(SENT + '(\\d+)' + SENT, 'g'), (_, i) => stash[+i]);
  return s;
}

async function pageTopic(arg) {
  const status = await loadStatus();
  const [assetId, topicId] = (arg || '').split('/');
  if (!assetId || !topicId) {
    document.getElementById('app').innerHTML = shell('summaries', topbar(['出错']) +
      '<div class="p-8 text-slate-500">URL 不完整</div>', status);
    return;
  }
  const a = await api(`/api/assets/${assetId}`);
  const topic = (a.topics || []).find(t => t.id === topicId) || (a.topics || [])[Number(topicId.replace('T','')) - 1];
  if (!topic) {
    document.getElementById('app').innerHTML = shell('summaries', topbar(['未找到话题']) +
      `<div class="p-8 text-slate-500">没有找到话题 ${escapeHtml(topicId)}</div>`, status);
    return;
  }
  const msgs = topic.messages || [];
  const rows = msgs.map(m => `
    <div class="px-5 py-3 flex gap-4 border-b border-line last:border-0">
      <div class="text-xs text-slate-400 tabular-nums whitespace-nowrap shrink-0 w-36">${escapeHtml(m.ts||'')}</div>
      <div class="min-w-0 flex-1">
        <div class="text-xs text-slate-500 font-medium">${escapeHtml(m.sender||'')}</div>
        <div class="mt-1 text-ink whitespace-pre-wrap break-words">${renderMessageText(m.text||'', assetId)}</div>
      </div>
    </div>`).join('') || '<div class="p-8 text-center text-slate-400">没有匹配到原始消息</div>';
  document.getElementById('app').innerHTML = shell('summaries', `
    ${topbar([
      {label:'Dashboard',href:'dashboard'},
      {label:a.group_name,href:'group/'+a.group_id},
      {label:a.title,href:'asset/'+a.id},
      topic.title || topicId,
    ])}
    <div class="p-8 max-w-4xl">
      <div class="flex items-center gap-3 mb-4">
        <a href="#asset/${a.id}" class="text-slate-400 hover:text-ink">${icon('back')}</a>
        <span class="text-xs px-2 py-0.5 rounded bg-indigo-100 text-indigo-700 font-medium">${escapeHtml(topic.id || topicId)}</span>
        <h1 class="text-xl font-bold">${escapeHtml(topic.title || '')}</h1>
        <span class="ml-auto text-sm text-slate-500">${msgs.length} 条相关消息</span>
      </div>
      <div class="bg-white rounded-xl border border-line">${rows}</div>
    </div>`, status);
}

/* ---------- daily digests list ---------- */
async function pageDigests() {
  const status = await loadStatus();
  const list = await api('/api/assets?kind=daily_digest&limit=200');
  const rows = list.map(s => `
    <a href="#asset/${s.id}" class="block py-4 border-b border-line last:border-0 hover:bg-slate-50 -mx-2 px-2 rounded">
      <div class="flex items-center gap-2 text-xs">${badge(s.kind)}<span class="text-slate-500">${escapeHtml(s.for_date||'')}</span></div>
      <div class="mt-1 font-medium text-lg">${escapeHtml(s.title)}</div>
      <div class="text-sm text-slate-600 mt-1 line-clamp-3">${escapeHtml(s.description)}</div>
    </a>`).join('') || `<div class="text-slate-500 py-12 text-center">还没有「每日精华」。点击 Dashboard 上的「精选群全量生成」会自动产出。</div>`;
  document.getElementById('app').innerHTML = shell('digests', `
    ${topbar(['每日精华'])}
    <div class="p-8 max-w-4xl">
      <p class="text-sm text-slate-500 mb-4">每天「精选群全量生成」结束后会自动汇总成一篇跨群的「每日精华」总览。</p>
      <div class="bg-white rounded-xl border border-line p-5">${rows}</div>
    </div>`, status);
}

/* ---------- summaries list ---------- */
async function pageSummaries() {
  const status = await loadStatus();
  const list = await api('/api/assets?limit=500');
  State.visibleAssetIds = list.map(s => s.id);
  const selMode = State.assetSelectMode;
  const rows = list.map(s => {
    const checked = State.assetSelection.has(s.id);
    const ringCls = checked ? 'bg-accentSoft -mx-2 px-2 rounded' : '';
    const left = selMode
      ? `<label class="flex items-center pt-1 cursor-pointer">
           <input type="checkbox" class="rounded border-line text-accent focus:ring-accent assetSel" data-id="${s.id}" data-title="${escapeHtml(s.title)}" ${checked?'checked':''}>
         </label>`
      : '';
    const right = selMode
      ? ''
      : `<button class="text-slate-300 hover:text-rose-600 p-1 mt-1 delBtn" data-id="${s.id}" data-title="${escapeHtml(s.title)}" title="删除">${icon('trash')}</button>`;
    const innerLink = selMode
      ? `<div class="assetRow flex-1 min-w-0 cursor-pointer" data-id="${s.id}" data-title="${escapeHtml(s.title)}">
           <div class="flex items-center gap-2 text-xs">${badge(s.kind)}<span class="text-slate-500">${escapeHtml(s.group_name)} · ${escapeHtml(s.for_date||'')}</span></div>
           <div class="mt-1 font-medium">${escapeHtml(s.title)}</div>
           <div class="text-sm text-slate-600 mt-1 line-clamp-2">${escapeHtml(s.description)}</div>
         </div>`
      : `<a href="#asset/${s.id}" class="flex-1 min-w-0">
           <div class="flex items-center gap-2 text-xs">${badge(s.kind)}<span class="text-slate-500">${escapeHtml(s.group_name)} · ${escapeHtml(s.for_date||'')}</span></div>
           <div class="mt-1 font-medium">${escapeHtml(s.title)}</div>
           <div class="text-sm text-slate-600 mt-1 line-clamp-2">${escapeHtml(s.description)}</div>
         </a>`;
    return `<div class="flex items-start gap-3 py-4 border-b border-line last:border-0 ${selMode?'':'hover:bg-slate-50'} ${ringCls}">
      ${left}${innerLink}${right}
    </div>`;
  }).join('') || `<div class="text-slate-500 py-12 text-center">还没有任何内容。</div>`;

  const selectedAll = list.length > 0 && State.assetSelection.size === list.length;
  const topbarRight = selMode
    ? `<label class="flex items-center gap-2 text-sm cursor-pointer select-none">
         <input id="selectAll" type="checkbox" class="rounded border-line text-accent focus:ring-accent" ${selectedAll?'checked':''}>
         <span>全选</span>
       </label>
       <button id="cancelAssetSelect" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 hover:bg-slate-50">${icon('x')}<span>取消选择</span></button>`
    : `<button id="enterAssetSelect" class="px-3 py-1.5 text-sm border border-line rounded-lg flex items-center gap-1 hover:bg-slate-50">${icon('check')}<span>多选</span></button>`;

  document.getElementById('app').innerHTML = shell('summaries', `
    ${topbar(['精华'], topbarRight)}
    <div class="p-8 max-w-4xl pb-24">
      <div class="bg-white rounded-xl border border-line p-5">${rows}</div>
    </div>
    ${bulkDeleteBar()}
    ${bulkDeleteProgress()}
  `, status);

  document.getElementById('enterAssetSelect')?.addEventListener('click', () => {
    State.assetSelectMode = true; render();
  });
  document.getElementById('cancelAssetSelect')?.addEventListener('click', () => {
    State.assetSelectMode = false; State.assetSelection.clear(); render();
  });
  document.getElementById('selectAll')?.addEventListener('change', (e) => {
    State.assetSelection = e.target.checked ? new Set(list.map(s=>s.id)) : new Set();
    render();
  });
  document.getElementById('bulkDeleteRun')?.addEventListener('click', () => runBulkDelete(list));
  document.getElementById('bulkDeleteClear')?.addEventListener('click', () => {
    State.assetSelection.clear(); render();
  });
  document.getElementById('bulkDeleteDismiss')?.addEventListener('click', () => {
    State.bulkDelete = null; render();
  });
  for (const c of document.querySelectorAll('.assetSel')) {
    c.addEventListener('click', (e) => {
      const id = c.dataset.id;
      if (e.shiftKey && State.lastAssetClicked && State.lastAssetClicked !== id) {
        e.preventDefault();
        rangeSelect(State.visibleAssetIds, State.lastAssetClicked, id, State.assetSelection);
        State.lastAssetClicked = id;
        render();
        return;
      }
      // For plain click we let the checkbox toggle naturally, then sync state.
      setTimeout(() => {
        if (c.checked) State.assetSelection.add(id);
        else State.assetSelection.delete(id);
        State.lastAssetClicked = id;
        render();
      }, 0);
    });
  }
  for (const r of document.querySelectorAll('.assetRow')) {
    r.addEventListener('mousedown', (e) => { if (e.shiftKey) e.preventDefault(); });
    r.addEventListener('click', (e) => {
      const id = r.dataset.id;
      if (e.shiftKey && State.lastAssetClicked && State.lastAssetClicked !== id) {
        rangeSelect(State.visibleAssetIds, State.lastAssetClicked, id, State.assetSelection);
      } else if (State.assetSelection.has(id)) {
        State.assetSelection.delete(id);
      } else {
        State.assetSelection.add(id);
      }
      State.lastAssetClicked = id;
      window.getSelection?.()?.removeAllRanges();
      render();
    });
  }
  for (const b of document.querySelectorAll('.delBtn')) {
    b.addEventListener('click', async (e) => {
      e.preventDefault();
      if (!confirm(`删除「${b.dataset.title}」？此操作不可撤销。`)) return;
      try { await api(`/api/assets/${b.dataset.id}`, { method:'DELETE' }); render(); }
      catch (err) { toast(err.message, 'error'); }
    });
  }
}

function bulkDeleteBar() {
  if (!State.assetSelectMode || State.assetSelection.size === 0) return '';
  return `
    <div class="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 flex items-center gap-3 bg-ink text-white px-5 py-3 rounded-xl shadow-2xl">
      <div class="text-sm">已选 <span class="font-semibold">${State.assetSelection.size}</span> 条</div>
      <div class="w-px h-5 bg-white/20"></div>
      <button id="bulkDeleteRun" class="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 rounded-lg text-sm flex items-center gap-1">
        ${icon('trash')}<span>删除选中</span>
      </button>
      <button id="bulkDeleteClear" class="px-3 py-1.5 text-sm text-white/70 hover:text-white">清空</button>
    </div>`;
}

function bulkDeleteProgress() {
  if (!State.bulkDelete) return '';
  const { done, total, errors } = State.bulkDelete;
  return `
    <div class="fixed bottom-6 left-1/2 -translate-x-1/2 z-40 w-[420px] bg-ink text-white p-4 rounded-xl shadow-2xl">
      <div class="flex items-center justify-between text-sm">
        <div>批量删除中… <span class="font-semibold">${done}/${total}</span></div>
        ${done===total ? '<button id="bulkDeleteDismiss" class="text-white/70 hover:text-white text-xs">关闭</button>' : ''}
      </div>
      <div class="mt-2 h-2 bg-white/10 rounded overflow-hidden">
        <div class="h-full bg-rose-500 transition-all" style="width:${(done/total*100).toFixed(1)}%"></div>
      </div>
      <div class="mt-2 text-xs text-white/70">
        ${done<total ? '正在删除…' : '完成。'} ${errors.length ? `· ${errors.length} 个失败` : ''}
      </div>
    </div>`;
}

async function runBulkDelete(list) {
  const ids = [...State.assetSelection];
  if (!ids.length) return;
  const titles = Object.fromEntries(list.map(s => [s.id, s.title]));
  if (!confirm(`确定删除选中的 ${ids.length} 条内容？此操作不可撤销。`)) return;
  State.bulkDelete = { done: 0, total: ids.length, errors: [] };
  State.assetSelectMode = false;
  State.assetSelection.clear();
  render();
  for (const id of ids) {
    try { await api(`/api/assets/${id}`, { method:'DELETE' }); }
    catch (e) { State.bulkDelete.errors.push({ id, title: titles[id], msg: e.message }); }
    State.bulkDelete.done += 1;
    render();
  }
  toast(`批量删除完成：${State.bulkDelete.done - State.bulkDelete.errors.length}/${State.bulkDelete.total} 成功`,
        State.bulkDelete.errors.length ? 'error' : 'success');
}

/* ---------- router ---------- */
const ROUTES = {
  dashboard: () => pageDashboard(),
  group:     (id) => pageGroup(id),
  asset:     (id) => pageAsset(id),
  topic:     (rest) => pageTopic(rest),
  digests:   () => pageDigests(),
  summaries: () => pageSummaries(),
  // #setup: manual re-entry into the wizard even when setup_state is `ready`.
  // Used by the sidebar's "重新提取密钥" link when the user noticed a group
  // failing to load (missing key) and wants to backfill.
  setup:     () => pageSetup(),
};

// Force-enter the wizard with a synthetic `keys_partial` state. status() never
// returns this state on its own — it's a manual recovery flow. The wizard's
// keys_partial render emphasises "click into more chats first" and ships a
// --force flag to the init invocation (otherwise wechat-cli skips because
// all_keys.json already exists).
async function pageSetup() {
  const status = await loadStatus(true);
  status.setup_state = 'keys_partial';
  status.wechat_raw = { ...(status.wechat_raw || {}), setup_state: 'keys_partial' };
  return renderSetupWizard(status);
}

async function render() {
  const hash = location.hash.slice(1) || 'dashboard';
  const sep = hash.indexOf('/');
  const route = sep < 0 ? hash : hash.slice(0, sep);
  const arg = sep < 0 ? '' : hash.slice(sep + 1);
  const fn = ROUTES[route] || ROUTES.dashboard;
  try { await fn(arg); }
  catch (e) {
    document.getElementById('app').innerHTML = `
      <div class="min-h-screen grid place-items-center text-slate-500">
        <div class="text-center"><div class="text-rose-600 font-medium mb-1">出错了</div><div class="text-sm">${escapeHtml(e.message)}</div></div>
      </div>`;
  }
}

window.addEventListener('hashchange', () => {
  // Leaving a page exits its select mode so it doesn't surprise on return.
  if (!location.hash.startsWith('#dashboard')) { State.selectMode = false; State.selection.clear(); }
  if (!location.hash.startsWith('#summaries')) { State.assetSelectMode = false; State.assetSelection.clear(); }
  render();
});

/* ---------- First-launch setup wizard (state-aware) ----------
 * The wizard replaces the dashboard wholesale whenever wechat-cli isn't fully
 * connected. It reads `status.setup_state` (set by wechat.py:status()) and
 * shows state-specific guidance + the right primary action for each distinct
 * failure mode. While the wizard is mounted it polls /api/health every 3s, so
 * transitions (e.g. user opens WeChat → state flips from `needs_app` to
 * `needs_init`) flow without a manual refresh.
 */
let _wizardPollTimer = null;
function _stopWizardPoll() { if (_wizardPollTimer) { clearTimeout(_wizardPollTimer); _wizardPollTimer = null; } }
function _startWizardPoll() {
  _stopWizardPoll();
  const tick = async () => {
    invalidateStatus();
    const fresh = await loadStatus(true);
    if (fresh.setup_state === 'ready') {
      _stopWizardPoll();
      render();
      return;
    }
    // Same state → just refresh the inner card so live signals (app_running,
    // key counts) reflect reality. Different state → re-render the whole
    // wizard with the new state's copy.
    const currentEl = document.getElementById('wizardRoot');
    if (currentEl && currentEl.dataset.state !== fresh.setup_state) {
      renderSetupWizard(fresh);
      return;
    }
    _wizardPollTimer = setTimeout(tick, 3000);
  };
  _wizardPollTimer = setTimeout(tick, 3000);
}

async function renderSetupWizard(status) {
  _stopWizardPoll();
  const w = status.wechat_raw || {};
  const state = status.setup_state || w.setup_state || 'needs_init';
  // keys_partial means "all_keys.json exists but missing some entries" —
  // re-init must use --force to overwrite, otherwise wechat-cli no-ops.
  const force = state === 'keys_partial';
  let cmd = '';
  try {
    const r = await api(`/api/setup/init-command${force ? '?force=true' : ''}`);
    cmd = r.command || '';
  } catch (e) {
    cmd = '（无法获取命令 —— 请确认 ChatLens 后台已启动）';
  }

  const body = _wizardBodyFor(state, w, cmd);
  document.getElementById('app').innerHTML = shell('dashboard', `
    <div class="min-h-screen flex items-center justify-center p-8">
      <div id="wizardRoot" data-state="${state}" class="max-w-xl w-full bg-white border border-line rounded-2xl p-8 shadow-sm">
        ${body}
        <div id="setupNote" class="mt-4 text-xs text-slate-500"></div>
      </div>
    </div>`, status);

  _wireWizardActions(state, cmd);
  _startWizardPoll();
}

/* ---- Per-state copy + primary actions ---- */
function _wizardBodyFor(state, w, cmd) {
  switch (state) {
    case 'binary_unavailable':
      return _renderBinaryUnavailable(w);
    case 'needs_app':
      return _renderNeedsApp();
    case 'keys_empty':
      return _renderKeysEmpty(cmd, w);
    case 'keys_stale':
      return _renderKeysStale(cmd, w);
    case 'keys_partial':
      return _renderKeysPartial(cmd, w);
    case 'db_locked':
      return _renderDbLocked(w);
    case 'sessions_failed':
      return _renderGenericFailure(w);
    case 'needs_init':
    default:
      return _renderFirstTime(cmd, w);
  }
}

function _wizardHeader(title, sub, severity='warn') {
  const dot = severity === 'error' ? 'bg-rose-500' : severity === 'ok' ? 'bg-emerald-500' : 'bg-amber-500';
  return `
    <div class="flex items-start gap-3 mb-5">
      <span class="mt-1.5 w-2.5 h-2.5 rounded-full ${dot} flex-shrink-0"></span>
      <div>
        <div class="text-2xl font-semibold leading-tight">${escapeHtml(title)}</div>
        <div class="text-sm text-slate-600 mt-1">${sub}</div>
      </div>
    </div>`;
}

function _initCmdCard(cmd) {
  return `
    <div class="bg-slate-50 border border-line rounded-lg p-3 mb-5">
      <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1.5">init 命令(如果按钮没用，可手动复制到 Terminal 运行)</div>
      <pre class="text-xs font-mono text-slate-800 whitespace-pre-wrap break-all" id="initCmd">${escapeHtml(cmd)}</pre>
    </div>`;
}

/* ----- Pre-flight checklist for first-init ------------------------------
 * Gates the "Run init" button behind four conditions. Two are auto-detected
 * (WeChat process running, WeChat has a SQLCipher DB open via lsof); two are
 * self-attested with macOS Settings deeplinks (App Management for Terminal,
 * Full Disk Access for Terminal). The self-attest checkboxes persist in
 * localStorage so re-renders during the 3s poll don't wipe them.
 *
 * Why these four: without all of them, sudo wechat-cli init either
 *   (a) hits codesign Operation-not-permitted (App Management missing),
 *   (b) can read keys but later can't read WeChat's DBs (FDA missing),
 *   (c) extracts zero keys silently (WeChat not loaded enough — DB not
 *       loaded means user hasn't clicked into any chat yet).
 * Catching all four up front means init succeeds on first try.
 */

const _SELF_ATTEST_KEY_APP_MGMT = 'chatlens_preflight_app_mgmt';
const _SELF_ATTEST_KEY_FDA = 'chatlens_preflight_fda';

function _getSelfAttest(key) {
  try { return localStorage.getItem(key) === '1'; }
  catch { return false; }
}
function _setSelfAttest(key, val) {
  try { localStorage.setItem(key, val ? '1' : '0'); }
  catch {}
}

// macOS deeplink URL schemes that drop the user directly on the right
// privacy panel. Tested on macOS 13–15; if Apple rearranges the schemes in
// a future version these still degrade gracefully (System Settings just
// opens at the top level).
const _DEEPLINK_APP_MGMT = 'x-apple.systempreferences:com.apple.preference.security?Privacy_AppBundles';
const _DEEPLINK_FDA = 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles';

function _preflightItems(w) {
  // Live-detected items pulled from /api/health.
  return [
    {
      id: 'wechat_running',
      kind: 'auto',
      label: '微信正在运行',
      ok: w.app_running === true,
      hint: '打开 WeChat / Weixin 桌面应用。',
    },
    {
      id: 'wechat_db_loaded',
      kind: 'auto',
      label: '微信里点过一条聊天（让密钥进内存）',
      ok: w.db_loaded === true,
      hint: '在微信里随便点一个聊天 —— 它会解密那个对话的数据库，密钥才会真正 load 到内存里。',
    },
    {
      id: 'app_mgmt',
      kind: 'attest',
      storageKey: _SELF_ATTEST_KEY_APP_MGMT,
      label: 'Terminal 拿到 App Management 权限',
      hint: 'macOS 13+ 需要这个才能让 codesign 给微信重签 get-task-allow entitlement。没它 init 会卡在 Operation not permitted。',
      deeplink: _DEEPLINK_APP_MGMT,
      deeplinkLabel: '打开 App Management',
    },
    {
      id: 'fda',
      kind: 'attest',
      storageKey: _SELF_ATTEST_KEY_FDA,
      label: 'Terminal 拿到 Full Disk Access',
      hint: '让 wechat-cli 能读 ~/Library/Containers/com.tencent.xinWeChat/ 下的数据库。',
      deeplink: _DEEPLINK_FDA,
      deeplinkLabel: '打开 Full Disk Access',
    },
  ];
}

function _renderPreflight(w) {
  const items = _preflightItems(w);
  const rows = items.map(it => {
    const isOk = it.kind === 'auto' ? it.ok : _getSelfAttest(it.storageKey);
    const dot = isOk
      ? '<span class="w-5 h-5 rounded-full bg-emerald-500 text-white flex items-center justify-center text-xs flex-shrink-0">✓</span>'
      : '<span class="w-5 h-5 rounded-full border-2 border-slate-300 flex-shrink-0"></span>';
    const auto = it.kind === 'auto'
      ? '<span class="text-[10px] uppercase tracking-wider text-slate-400 ml-2">自动检测</span>'
      : '';
    const action = it.kind === 'attest'
      ? `<div class="flex items-center gap-2 mt-2">
           <a href="${it.deeplink}" class="px-3 py-1 text-xs border border-line rounded-lg hover:bg-slate-50 inline-flex items-center gap-1">⚙️ ${escapeHtml(it.deeplinkLabel)}</a>
           <label class="flex items-center gap-1.5 text-xs text-slate-700 cursor-pointer">
             <input type="checkbox" class="preflightChk" data-key="${it.storageKey}" ${isOk ? 'checked' : ''}>
             <span>已加入并打开开关</span>
           </label>
         </div>`
      : '';
    return `
      <div class="flex items-start gap-3 py-2.5 ${isOk ? 'opacity-70' : ''}">
        ${dot}
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium text-slate-800">${escapeHtml(it.label)}${auto}</div>
          <div class="text-xs text-slate-500 mt-0.5">${it.hint}</div>
          ${action}
        </div>
      </div>`;
  }).join('');
  return `
    <div class="bg-slate-50 border border-line rounded-lg p-4 mb-4">
      <div class="text-[11px] uppercase tracking-wider text-slate-500 mb-2 font-semibold">跑 init 前请完成这 4 项</div>
      ${rows}
    </div>`;
}

function _preflightAllOk(w) {
  return _preflightItems(w).every(it =>
    it.kind === 'auto' ? it.ok : _getSelfAttest(it.storageKey)
  );
}

function _initButtons({primary='在 Terminal 中运行 init', showDone=true, w=null}={}) {
  const ready = w ? _preflightAllOk(w) : true;
  const disabledAttrs = ready ? '' : 'disabled aria-disabled="true"';
  const disabledClass = ready
    ? 'bg-accent text-white hover:bg-accent2'
    : 'bg-slate-200 text-slate-400 cursor-not-allowed';
  return `
    <div class="flex items-center gap-2 flex-wrap">
      <button id="runInitBtn" ${disabledAttrs} class="px-4 py-2 text-sm rounded-lg flex items-center gap-1.5 ${disabledClass}">${icon('sparkle')}<span>${escapeHtml(primary)}</span></button>
      <button id="copyInitBtn" class="px-3 py-2 text-sm border border-line rounded-lg hover:bg-slate-50">复制命令</button>
      <div class="ml-auto"></div>
      ${showDone ? '<button id="doneInitBtn" class="px-3 py-2 text-sm border border-line rounded-lg hover:bg-slate-50">我已完成 →</button>' : ''}
    </div>
    ${ready ? '' : '<div class="mt-2 text-xs text-slate-500">完成上面 4 项后这个按钮就会激活。</div>'}`;
}

function _renderFirstTime(cmd, w) {
  return `
    ${_wizardHeader('欢迎使用 ChatLens', '第一次启动需要从微信本地数据库提取一次密钥（只需要做一次）。')}
    ${_renderPreflight(w)}
    <div class="text-xs text-slate-500 mb-3">点 Run init 后 ChatLens 会启<strong>新 Terminal 实例</strong>跑 <code>sudo wechat-cli init</code>。如果第一次提示「task_for_pid 失败正在重签微信」是正常的 —— 按它的指示 ⌘Q 微信、重开、登录、再回 Terminal 按上箭头 Enter 重跑一次 init 就行。</div>
    ${_initCmdCard(cmd)}
    ${_initButtons({w})}`;
}

function _renderNeedsApp() {
  return `
    ${_wizardHeader('请先打开 WeChat 并登录', '密钥需要从 WeChat 进程内存里提取,所以 WeChat 必须正在运行且已登录。', 'warn')}
    <ol class="list-decimal pl-5 text-sm text-slate-700 space-y-2 mb-5">
      <li>打开 <strong>WeChat / 微信</strong> 桌面应用。</li>
      <li>登录,等到能在 WeChat 里<strong>看到自己的聊天列表</strong>(数据库密钥这时才进内存)。</li>
      <li>本页面会自动检测 —— 一旦 WeChat 启动并就绪,会自动跳到下一步。</li>
    </ol>
    <div class="text-xs text-slate-500">未检测到 WeChat 进程 · 每 3 秒自动重新检测</div>`;
}

function _renderKeysEmpty(cmd, w) {
  return `
    ${_wizardHeader('上次 init 没有真的提取到密钥', '<code>~/.wechat-cli/all_keys.json</code> 是空的 —— 几乎一定是 WeChat 当时还没完全登录、聊天列表还没滚出来。', 'warn')}
    ${_renderPreflight(w)}
    <div class="text-xs text-slate-500 mb-3">这次重跑前确认上面 4 项全过 —— 尤其是 <strong>"点过一条聊天"</strong> 这一项必须显示绿勾。</div>
    ${_initCmdCard(cmd)}
    ${_initButtons({primary: '重新运行 init', w})}`;
}

function _renderKeysStale(cmd, w) {
  return `
    ${_wizardHeader('密钥失效，可能是 WeChat 更新过', '<code>wechat-cli</code> 能读到密钥文件，但用这些密钥已经解不开 WeChat 的数据库 —— 通常是 WeChat 自动更新后换了加密方案。', 'error')}
    ${_renderPreflight(w)}
    <div class="text-xs text-slate-500 mb-3">重新提取一次密钥即可。上面 4 项过完后按下面按钮。</div>
    ${_initCmdCard(cmd)}
    ${_initButtons({primary: '重新运行 init', w})}`;
}

// keys_partial = manual recovery entered via `#setup`. Triggered when the user
// noticed a specific group failing to load (key for that DB's shard wasn't in
// WeChat's memory at last init). Fix: open the failing chat + a few others in
// WeChat so WeChat decrypts more shards, then re-init with --force.
function _renderKeysPartial(cmd, w) {
  return `
    ${_wizardHeader('补一次缺失的密钥', '<code>~/.wechat-cli/all_keys.json</code> 里有 key 了，但可能少了某个聊天分片的 key —— 通常表现为「某个群读不出来」。在微信里多点几个聊天让 WeChat 解密更多分片，然后再跑一次 init。', 'warn')}
    <div class="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4 text-sm text-amber-900">
      <div class="font-semibold mb-1">这次重跑的关键：</div>
      <ol class="list-decimal pl-5 space-y-1 text-[13px]">
        <li>在微信里点开 <strong>那个读不出来的群</strong>（如果还能定位到）</li>
        <li>额外点几个 <strong>不常用的群 / 旧聊天</strong> —— 越分散越好，覆盖更多 DB 分片</li>
        <li>每个聊天滚一下让消息真的 load 出来</li>
        <li>然后按下面按钮 —— 会用 <code>--force</code> 重新跑 init</li>
      </ol>
    </div>
    ${_renderPreflight(w)}
    <div class="text-xs text-slate-500 mb-3">上面 4 项过完后按下面按钮。Run init 会带 <code>--force</code> flag 覆盖现有 all_keys.json。</div>
    ${_initCmdCard(cmd)}
    ${_initButtons({primary: '重新跑 init (--force)', w})}`;
}

function _renderDbLocked(w) {
  return `
    ${_wizardHeader('数据库被 WeChat 锁住了', 'wechat-cli 能读到密钥但拿不到数据库 —— 通常是 WeChat 正在写入(同步消息中)。', 'warn')}
    <div class="text-sm text-slate-700 mb-5">稍等 10–30 秒,等 WeChat 同步完毕再重试。本页面每 3 秒自动重新检测。</div>
    ${w.error ? `<div class="text-xs text-slate-500 font-mono break-all">${escapeHtml(String(w.error))}</div>` : ''}
    <div class="mt-4"><button id="recheckBtn" class="px-3 py-2 text-sm border border-line rounded-lg hover:bg-slate-50">立即重试</button></div>`;
}

function _renderGenericFailure(w) {
  return `
    ${_wizardHeader('wechat-cli 暂时读不到数据', '可能是配置漂移或 WeChat 内部结构变化。', 'warn')}
    <div class="bg-slate-50 border border-line rounded-lg p-3 mb-4 text-xs font-mono text-slate-800 whitespace-pre-wrap break-all">${escapeHtml(String(w.error || '(无错误信息)'))}</div>
    <div class="text-sm text-slate-700 mb-3">可以先重启 WeChat 试试;如果还是不行,试着重新跑 init。</div>
    <div class="flex items-center gap-2 flex-wrap">
      <button id="recheckBtn" class="px-3 py-2 text-sm border border-line rounded-lg hover:bg-slate-50">重新检测</button>
      <button id="runInitBtn" class="px-3 py-2 text-sm border border-line rounded-lg hover:bg-slate-50">重新跑 init</button>
    </div>`;
}

function _renderBinaryUnavailable(w) {
  return `
    ${_wizardHeader('ChatLens 内置 wechat-cli 启动失败', '通常是 .app 没复制完整或者 macOS 安全策略阻止了。', 'error')}
    <div class="bg-rose-50 border border-rose-200 rounded-lg p-3 mb-4 text-xs font-mono text-rose-900 whitespace-pre-wrap break-all">${escapeHtml(String(w.error || '(无错误信息)'))}</div>
    <ol class="list-decimal pl-5 text-sm text-slate-700 space-y-1 mb-4">
      <li>检查 <code>/Applications/ChatLens.app</code>(或你放置的位置)是不是完整的 — 重新从 .dmg 拖一遍最稳。</li>
      <li>第一次运行请用 <strong>右键 → 打开</strong> 而不是双击。</li>
      <li>仍不行的话,在 Terminal 里跑 <code>xattr -dr com.apple.quarantine /Applications/ChatLens.app</code>。</li>
    </ol>
    <button id="recheckBtn" class="px-3 py-2 text-sm border border-line rounded-lg hover:bg-slate-50">重新检测</button>`;
}

function _wireWizardActions(state, cmd) {
  const note = document.getElementById('setupNote');
  const setNote = (s) => { if (note) note.textContent = s; };

  // Pre-flight self-attest checkboxes (App Mgmt, FDA). When the user toggles
  // one, persist + re-render so the Run init button can flip enabled/disabled
  // without waiting for the next 3s poll.
  document.querySelectorAll('.preflightChk').forEach(el => {
    el.addEventListener('change', () => {
      _setSelfAttest(el.dataset.key, el.checked);
      // Re-render the wizard with the current cached status. Avoid hitting
      // /api/health again — the auto-detected fields haven't changed.
      loadStatus(false).then(s => renderSetupWizard(s));
    });
  });

  document.getElementById('runInitBtn')?.addEventListener('click', async (e) => {
    if (e.currentTarget.disabled) return;
    setNote('正在打开新 Terminal 实例…');
    try {
      // keys_partial → force=true so wechat-cli re-extracts even though
      // all_keys.json already exists. Other states leave force off (default).
      const qs = state === 'keys_partial' ? '?force=true' : '';
      await api(`/api/setup/run-init${qs}`, { method:'POST', body:'{}' });
      setNote('新 Terminal 已打开。输 sudo 密码后,本页每 3 秒自动检测密钥是否成功提取。');
    } catch (err) {
      setNote(`打开失败：${err.message}。请手动复制上方命令到 Terminal 运行。`);
    }
  });

  document.getElementById('copyInitBtn')?.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(cmd);
      setNote('命令已复制到剪贴板,粘贴到 Terminal 后回车即可。');
    } catch (e) {
      setNote('复制失败,请手动选中上方命令复制。');
    }
  });

  document.getElementById('doneInitBtn')?.addEventListener('click', () => {
    setNote('正在验证密钥是否可用…');
    invalidateStatus();
    // keys_partial entered via #setup — done means "go back to dashboard"
    // rather than re-rendering the wizard from the same hash route.
    if (state === 'keys_partial') {
      location.hash = '#dashboard';
    } else {
      render();
    }
  });

  document.getElementById('recheckBtn')?.addEventListener('click', () => {
    setNote('重新检测中…');
    invalidateStatus();
    render();
  });
}

/* ---------- Model selector (sidebar) ----------
 * Single global handler delegated to the document — the sidebar is re-rendered
 * on every page transition, but the listener stays alive and matches the
 * fresh <select id="modelSelect">. Persists the choice via PUT and refreshes
 * status so the dropdown stays in sync if multiple windows ever open.
 */
document.addEventListener('change', async (e) => {
  if (e.target?.id !== 'modelSelect') return;
  const model = e.target.value;
  try {
    await api('/api/preferences/model', {
      method: 'PUT',
      body: JSON.stringify({ model }),
    });
    invalidateStatus();
    toast(`已切换到 ${model}，下次生成精华生效`, 'success');
  } catch (err) {
    toast('切换失败：' + err.message, 'error');
  }
});

/* ---------- Cmd+F find-in-page ----------
 * pywebview's WKWebView doesn't bind the macOS-native find-in-page on its own,
 * so Cmd+F otherwise does nothing inside the bundled .app. Inject a tiny
 * floating search bar that uses WebKit's `window.find()` to highlight and
 * jump to matches. Captures Cmd+F (and Ctrl+F for parity) at the window
 * level; Esc / × closes. Enter = next match, Shift+Enter = previous.
 */
function setupFindBar() {
  let bar = null;
  let input = null;
  let lastQuery = '';

  const open = () => {
    if (bar) { input.focus(); input.select(); return; }
    bar = document.createElement('div');
    bar.className = 'fixed top-3 right-3 z-[9999] bg-white border border-line rounded-lg shadow-lg flex items-center gap-1 px-2 py-1.5';
    bar.innerHTML = `
      <input id="cl-find-input" type="text" placeholder="搜索页面内容…" class="text-sm px-2 py-1 w-56 outline-none">
      <button id="cl-find-prev" class="px-2 py-0.5 text-slate-500 hover:text-ink" title="上一个 (Shift+Enter)">↑</button>
      <button id="cl-find-next" class="px-2 py-0.5 text-slate-500 hover:text-ink" title="下一个 (Enter)">↓</button>
      <button id="cl-find-close" class="px-2 py-0.5 text-slate-500 hover:text-ink" title="关闭 (Esc)">✕</button>`;
    document.body.appendChild(bar);
    input = bar.querySelector('#cl-find-input');
    input.value = lastQuery;
    input.focus();
    input.select();

    const findNext = (backwards) => {
      const q = input.value;
      if (!q) return;
      lastQuery = q;
      // window.find(text, caseSensitive, backwards, wrapAround, wholeWord, searchInFrames, showDialog)
      window.find(q, false, !!backwards, true, false, false, false);
    };

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key === 'Enter') { e.preventDefault(); findNext(e.shiftKey); }
    });
    bar.querySelector('#cl-find-next').addEventListener('click', (e) => { e.preventDefault(); findNext(false); input.focus(); });
    bar.querySelector('#cl-find-prev').addEventListener('click', (e) => { e.preventDefault(); findNext(true); input.focus(); });
    bar.querySelector('#cl-find-close').addEventListener('click', (e) => { e.preventDefault(); close(); });
  };

  const close = () => {
    if (!bar) return;
    bar.remove();
    bar = null;
    input = null;
    window.getSelection?.()?.removeAllRanges();
  };

  // Capture phase so a focused input or contenteditable doesn't swallow it.
  window.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'f' || e.key === 'F')) {
      e.preventDefault();
      open();
    } else if (e.key === 'Escape' && bar) {
      e.preventDefault();
      close();
    }
  }, true);
}
setupFindBar();

render();
attachToActiveJob();
