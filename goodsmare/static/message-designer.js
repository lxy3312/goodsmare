/* 消息积木编辑器。草稿与定时刷新分开，预览只调用本机的只读接口。 */
"use strict";
var MessageDesigner = (() => {
  const drafts = new Map();
  const clone = value => JSON.parse(JSON.stringify(value));
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  const glyphs = { image:"▧", title:"T", status:"◉", item_id:"#", price:"¥", cny:"元", drop:"↓", source:"↗", watch:"☆", keyword:"⌕", extra:"≡", published:"◷", found:"◴", buy_button:"门", original_button:"↗", copy_original:"▣", copy_buy:"▣", image_button:"▧", search_button:"⌕", page_button:"▤", manage_button:"⚙", note:"Aa", note2:"Aa", divider:"—" };
  function frameHTML(content) {
    const script = `document.addEventListener('click',function(e){
      var c=e.target.closest('copy'),a=e.target.closest('a');
      if(c){e.preventDefault();parent.postMessage({type:'goodsmare-preview-copy',text:c.getAttribute('data-clipboard-text')},'*')}
      else if(a){e.preventDefault();parent.postMessage({type:'goodsmare-preview-link'},'*')}
    });document.querySelectorAll('img').forEach(function(img){
      var p=document.createElement('div');p.textContent='正在加载商品配图…';p.style.cssText='padding:55px 16px;background:#f5f3f4;border:1px dashed #ddd;border-radius:10px;color:#888;text-align:center;font-size:12px';img.before(p);
      function failed(){p.textContent='图片暂时无法加载 · 手机端仍可尝试查看';img.remove()}
      img.addEventListener('load',function(){p.remove()},{once:true});img.addEventListener('error',failed,{once:true});
      if(img.complete){if(img.naturalWidth)p.remove();else failed()}
    });`;
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src http: https:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
      <style>body{margin:0;padding:10px 14px;font:14px/1.6 'Microsoft YaHei UI','Microsoft YaHei',system-ui,sans-serif;color:#222;background:#fff}img{max-width:100%}a,copy{cursor:pointer}a{color:#285887}</style>
      </head><body>${content}<script>${script}</script></body></html>`;
  }
  let state, deps, root, scope = "", previewKind = "wxpusher", sample = "latest", previewTab = "visual";
  let timer, sequence = 0, previewKey = "", lastField = "mdTitleTemplate", dragging = "";
  const q = selector => root.querySelector(selector);
  const set = (selector, html) => { const el = q(selector); if (el._mdHTML !== html) { el._mdHTML = html; el.innerHTML = html; } };
  const channel = () => state.config.channels.find(c => c.id === scope);
  const saved = () => scope ? channel()?.message ?? null : state.config.message ?? null;
  const fingerprint = () => JSON.stringify({ saved: saved(), global: state.config.message });
  function draft() {
    let d = drafts.get(scope);
    if (!d || (!d.dirty && d.saved !== fingerprint())) {
      const value = saved();
      d = { layout: clone(value || state.config.message || state.message_layout.defaults),
        own: !scope || value !== null, dirty:false, revision:0, saved:fingerprint() };
      if (!value && !state.config.message) d.layout.enabled = false;
      drafts.set(scope, d);
    }
    return d;
  }
  function changed(mutate, redraw = true) {
    const d = draft();
    if (!d.own) d.layout = clone(state.config.message || state.message_layout.defaults);
    d.own = true; d.layout.enabled = true;
    mutate(d.layout); d.dirty = true; d.revision++;
    if (redraw) draw(); else drawStatus();
    schedule();
  }
  function move(key, destination) {
    changed(layout => {
      const at = layout.blocks.indexOf(key);
      if (at < 0) return;
      layout.blocks.splice(at, 1);
      layout.blocks.splice(Math.max(0, Math.min(layout.blocks.length, destination)), 0, key);
    });
  }
  function tile(label, hint, attr, on, glyph) {
    return `<button type="button" class="md-tile" ${attr} aria-pressed="${on}"><span class="md-mark" aria-hidden="true">✓</span>${glyph ? `<span class="md-glyph" aria-hidden="true">${esc(glyph)}</span>` : ""}<b>${esc(label)}</b><small>${esc(hint)}</small></button>`;
  }
  function choices(key, items, css = "") {
    return `<div class="md-choices ${css}">${items.map(([value,label,hint]) => tile(label, hint || "", `data-option="${key}" data-value="${value}"`, draft().layout[key] === value)).join("")}</div>`;
  }
  function drawStatus() {
    const d = draft();
    q("#mdEnabled").checked = !!d.layout.enabled;
    q("#mdInherit").hidden = !scope;
    q("#mdInherit").setAttribute("aria-pressed", String(!d.own));
    q("#mdInherit").textContent = d.own ? "改为跟随全局" : "✓ 正在跟随全局";
    q("#mdModeHint").textContent = scope && !d.own ? "正在预览全局布局；修改任意方块会为这个渠道建立独立布局。"
      : d.layout.enabled ? "自定义已开启。勾选模块，按下方顺序排列；没有对应信息的模块自动隐藏。"
      : "当前使用渠道原有格式。选择预设或任意模块即可开始自定义。";
    q("#mdSaveState").textContent = d.dirty ? "有未保存修改 · 预览已跟随草稿" : scope && !d.own ? "已保存 · 跟随全局" : "已保存 · 后续推送生效";
    q("#mdSaveState").className = "md-save-state" + (d.dirty ? " dirty" : "");
    q("#mdSave").disabled = !d.dirty || !!d.saving;
  }
  function draw() {
    const d = draft(), layout = d.layout, schema = state.message_layout;
    const active = document.activeElement;
    const focus = active && root.contains(active) && active.id ? { id:active.id, start:active.selectionStart, end:active.selectionEnd } : null;
    set("#mdScope", `<option value="">全部渠道 · 全局布局${drafts.get("")?.dirty ? " *" : ""}</option>` + state.config.channels.map(c =>
      `<option value="${esc(c.id)}">${esc(c.name || state.channel_types[c.type]?.label || c.type)}${drafts.get(c.id)?.dirty ? " *" : ""}</option>`).join(""));
    q("#mdScope").value = scope;
    set("#mdPresets", Object.entries(schema.presets).map(([key,p]) => tile(p.label, p.hint, `data-preset="${key}"`, layout.enabled && JSON.stringify(layout) === JSON.stringify(p.settings), {shopping:"▧",quick:"ϟ",complete:"▦",text:"Aa"}[key])).join(""));
    const groups = [...new Set(schema.blocks.map(b => b.group))];
    set("#mdBlocks", groups.map(group => `<div class="md-group"><h5>${esc(group)}</h5><div class="md-tiles">${schema.blocks.filter(b => b.group === group).map(b => tile(b.label, b.hint, `data-block="${b.key}"`, layout.blocks.includes(b.key), glyphs[b.key])).join("")}</div></div>`).join(""));
    q("#mdSelectedCount").textContent = `${layout.blocks.length} / ${schema.blocks.length}`;
    const labels = Object.fromEntries(schema.blocks.map(b => [b.key,b.label]));
    set("#mdOrder", layout.blocks.map((key,i) => `<div class="md-order-item" draggable="true" data-order="${key}"><span>${i + 1}</span>${esc(labels[key])}<button type="button" data-move="${key}" data-step="-1" aria-label="${esc(labels[key])}向前移" ${i ? "" : "disabled"}>←</button><button type="button" data-move="${key}" data-step="1" aria-label="${esc(labels[key])}向后移" ${i < layout.blocks.length - 1 ? "" : "disabled"}>→</button><button type="button" data-remove="${key}" aria-label="移除${esc(labels[key])}">×</button></div>`).join("") || '<span class="hint">选择上方的方块，添加消息模块。</span>');
    set("#mdImageSize", choices("image_size", [["small","小图","96 px"],["medium","中图","220 px"],["large","大图","320 px"]]));
    set("#mdDensity", choices("density", [["card","舒展卡片","留出阅读间距"],["compact","紧凑排列","更少空白"]], "two"));
    set("#mdClickTarget", choices("click_target", [["inherit","跟随购买设置","使用下方点开商品设置"],["doorzo","直接去任意门","不支持的来源打开原页"],["original","原商品页","打开卖家原链接"],["page","这批推送页","需要配置手机访问"]], "four"));
    for (const [id,key] of [["mdTitleLength","title_length"],["mdMaxItems","max_items"],["mdTitleTemplate","title_template"],["mdNote","note"],["mdNote2","note2"]]) {
      if (document.activeElement !== q("#" + id)) q("#" + id).value = layout[key];
    }
    q("#mdNoteField").hidden = !layout.blocks.includes("note");
    q("#mdNote2Field").hidden = !layout.blocks.includes("note2");
    set("#mdVariables", schema.variables.map(v => `<button type="button" data-variable="${esc(v)}">{${esc(v)}}</button>`).join(""));
    set("#mdPreviewKind", Object.entries(state.channel_types).map(([key,type]) => `<option value="${key}">${esc(type.label)}</option>`).join(""));
    q("#mdPreviewKind").value = channel()?.type || previewKind;
    q("#mdPreviewKind").disabled = !!scope;
    drawStatus();
    if (focus) {
      const el = document.getElementById(focus.id);
      if (el && document.activeElement !== el) { el.focus({preventScroll:true}); if (typeof focus.start === "number" && el.type !== "number") el.setSelectionRange(focus.start,focus.end); }
    }
  }
  function schedule() {
    const d = draft();
    const key = JSON.stringify([scope,d.layout,d.own,previewKind,sample,state.config.buy,state.rate,state.access?.base,state.config.web?.push_page]);
    if (key === previewKey) return;
    previewKey = key;
    const ticket = ++sequence;
    clearTimeout(timer);
    q("#mdPreviewStatus").textContent = "更新中…";
    timer = setTimeout(() => preview(ticket), 220);
  }
  async function preview(ticket) {
    const d = draft(), requestedScope = scope;
    try {
      const result = await deps.api("/api/message/preview", { channel_id:scope, type:previewKind, sample, message:clone(d.layout), inherit:!!scope && !d.own });
      if (ticket !== sequence || requestedScope !== scope) return;
      q("#mdPreviewTitle").textContent = result.title;
      q("#mdTextPreview").textContent = result.title + "\n\n" + result.text;
      q("#mdTarget").textContent = "点开通知：" + (result.target || "无链接");
      set("#mdWarnings", result.warnings.map(w => `<p>${esc(w)}</p>`).join(""));
      q("#mdPreviewStatus").textContent = result.sample_label;
      q("#mdFrame").srcdoc = frameHTML(result.html);
    } catch (error) {
      if (ticket !== sequence) return;
      q("#mdPreviewStatus").textContent = "预览失败";
      set("#mdWarnings", `<p class="md-error">${esc(error.message)}</p>`);
    }
  }
  async function save() {
    const d = draft(), requestedScope = scope, revision = d.revision;
    if (!d.dirty || d.saving) return;
    if (d.layout.enabled && d.own && !d.layout.blocks.length) { deps.toast("至少选一个消息模块", true); return; }
    d.saving = true; drawStatus();
    try {
      await deps.api("/api/message/settings", { channel_id:requestedScope, message:clone(d.layout), inherit:!!requestedScope && !d.own });
      await deps.refresh();
      if (d.revision === revision) { d.dirty = false; d.saved = ""; }
      deps.toast(requestedScope ? "这个渠道的消息布局已保存" : "全局消息布局已保存");
    } catch (error) { deps.toast(error.message, true); }
    finally { d.saving = false; draw(); schedule(); }
  }
  function mount() {
    root.innerHTML = `<div class="md-heading"><div><h3>消息积木<span class="md-badge">24 个模块</span></h3><p>想收到什么，点亮哪些方块。拖动排序，组合你的商品通知。</p></div><select id="mdScope" aria-label="选择要编辑的消息布局"></select></div>
      <div class="md-layout"><div class="md-editor">
        <div class="panel md-section md-mode"><label class="row-switch"><span>使用自定义布局</span><span class="switch"><input id="mdEnabled" type="checkbox"><span></span></span></label><button id="mdInherit" type="button" class="sm">跟随全局</button><p id="mdModeHint" class="hint"></p></div>
        <div class="panel md-section"><h4>从一个预设开始</h4><p class="hint">选好后，继续添加或移除方块。</p><div id="mdPresets" class="md-tiles"></div></div>
        <div class="panel md-section"><h4>消息里放什么 <span id="mdSelectedCount" class="md-badge"></span></h4><p class="hint">再次点击取消。降价、上架时间、任意门等模块只在有对应信息时出现。</p><div id="mdBlocks"></div></div>
        <div class="panel md-section"><h4>消息顺序</h4><p class="hint">拖动积木调整顺序；手机可以用左右箭头。渠道固定的通知标题、附件位置以客户端为准。</p><div id="mdOrder" class="md-order"></div></div>
        <div class="panel md-section md-options"><h4>显示与点击</h4>
          <div><span class="md-field-title">配图大小</span><div id="mdImageSize"></div></div>
          <div><span class="md-field-title">内容间距</span><div id="mdDensity"></div></div>
          <div class="row2"><label class="field"><span>商品标题最长字数</span><input id="mdTitleLength" type="number" min="20" max="500" step="1"><small>20～500，超出显示省略号。</small></label><label class="field"><span>每条汇总最多几件</span><input id="mdMaxItems" type="number" min="0" max="30" step="1"><small>0 表示按渠道容量自动分批，不会丢弃商品。</small></label></div>
          <div><span class="md-field-title">点开整条通知去哪</span><div id="mdClickTarget"></div><p class="hint" style="margin-top:8px">有通知点击入口的渠道使用此设置；正文里的操作模块分别打开各自链接。WxPusher 需在手机端将「通知点击设置」设为「原文链接」。</p></div>
        </div>
        <div class="panel md-section md-options"><h4>标题与自定义文字</h4><label class="field"><span>通知标题模板</span><input id="mdTitleTemplate" type="text" maxlength="200" autocomplete="off"><small>通知标题是渠道必需项；留空使用 goodsmare 默认标题。</small></label>
          <label class="field" id="mdNoteField"><span>自定义文字 ①</span><textarea id="mdNote" maxlength="500" placeholder="例如：{关注} 有新商品，{价格}，记得核对运费。"></textarea></label>
          <label class="field" id="mdNote2Field"><span>自定义文字 ②</span><textarea id="mdNote2" maxlength="500" placeholder="写一个结尾提醒，也可以插入变量。"></textarea></label>
          <div><span class="hint">点击变量，插入最近编辑的文本框。无数据的变量留空。</span><div class="md-variables" id="mdVariables"></div></div>
        </div>
        <div class="panel md-savebar"><span id="mdSaveState" class="md-save-state" aria-live="polite"></span><button type="button" id="mdReset">恢复默认积木</button><button type="button" class="primary" id="mdSave">保存布局</button></div>
      </div><aside class="panel md-preview"><div class="md-preview-head"><b>实时预览</b><span class="md-preview-status" id="mdPreviewStatus" aria-live="polite">准备中…</span></div><div class="md-controls"><select id="mdPreviewKind" aria-label="预览推送渠道"></select><select id="mdSample" aria-label="预览商品"><option value="latest">最近发现的商品</option><option value="example">日站示例 · 有图有价格</option><option value="drop">降价示例 · 查看降价模块</option><option value="xianyu">闲鱼示例 · 人民币商品</option></select></div>
        <div class="md-preview-tabs"><button type="button" data-preview-tab="visual" aria-pressed="true">消息内容</button><button type="button" data-preview-tab="text" aria-pressed="false">内容文本</button></div>
        <div class="md-phone" id="mdVisual"><div class="md-phone-top">消息详情 · 外观以接收客户端为准</div><div id="mdPreviewTitle" class="md-phone-title"></div><iframe id="mdFrame" title="推送消息内容预览" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe></div><pre id="mdTextPreview" hidden></pre><p class="md-target" id="mdTarget"></p><div id="mdWarnings" class="md-warnings"></div>
      </aside></div>`;
    root.addEventListener("click", event => {
      const button = event.target.closest("button"); if (!button) return;
      const data = button.dataset;
      if (data.block || data.remove) {
        const key = data.block || data.remove;
        changed(layout => { layout.blocks = layout.blocks.includes(key) ? layout.blocks.filter(k => k !== key) : [...layout.blocks,key]; });
      } else if (data.preset) changed(layout => Object.assign(layout, clone(state.message_layout.presets[data.preset].settings)));
      else if (data.option) changed(layout => { layout[data.option] = data.value; });
      else if (data.move) move(data.move, draft().layout.blocks.indexOf(data.move) + Number(data.step));
      else if (data.variable) {
        const field = q("#" + lastField), value = `{${data.variable}}`, key = {mdTitleTemplate:"title_template",mdNote:"note",mdNote2:"note2"}[lastField];
        if (!key || field.closest("[hidden]")) return;
        const start = field.selectionStart ?? field.value.length, end = field.selectionEnd ?? start;
        field.value = (field.value.slice(0,start) + value + field.value.slice(end)).slice(0,field.maxLength);
        changed(layout => { layout[key] = field.value; }, false);
        field.focus(); field.setSelectionRange(Math.min(start + value.length,field.value.length),Math.min(start + value.length,field.value.length));
      } else if (data.previewTab) {
        previewTab = data.previewTab;
        q("#mdVisual").hidden = previewTab !== "visual"; q("#mdTextPreview").hidden = previewTab !== "text";
        root.querySelectorAll("[data-preview-tab]").forEach(b => b.setAttribute("aria-pressed",String(b.dataset.previewTab === previewTab)));
      } else if (button.id === "mdInherit") {
        const d = draft(); d.own = false; d.layout = clone(state.config.message || state.message_layout.defaults);
        if (!state.config.message) d.layout.enabled = false;
        d.dirty = true; d.revision++; draw(); schedule();
      } else if (button.id === "mdReset") changed(layout => Object.assign(layout, clone(state.message_layout.defaults)));
      else if (button.id === "mdSave") save();
    });
    q("#mdEnabled").onchange = event => { const enabled = event.target.checked; changed(layout => { layout.enabled = enabled; }); };
    q("#mdScope").onchange = event => { scope = event.target.value; draw(); schedule(); };
    q("#mdPreviewKind").onchange = event => { previewKind = event.target.value; schedule(); };
    q("#mdSample").onchange = event => { sample = event.target.value; schedule(); };
    for (const [id,key] of [["mdTitleTemplate","title_template"],["mdNote","note"],["mdNote2","note2"],["mdTitleLength","title_length"],["mdMaxItems","max_items"]]) {
      const field = q("#" + id);
      field.addEventListener("focus", () => { if (field.type !== "number") lastField = id; });
      field.addEventListener("input", () => {
        if (field.type === "number" && (!field.value || !field.validity.valid)) return;
        changed(layout => { layout[key] = field.type === "number" ? Number(field.value) : field.value; }, false);
      });
      if (field.type === "number") field.addEventListener("change", () => { if (!field.validity.valid || !field.value) { field.value = draft().layout[key]; deps.toast("数值超出范围，已恢复上一次有效值", true); } });
    }
    q("#mdOrder").addEventListener("dragstart", event => { const item = event.target.closest("[data-order]"); if (!item) return; dragging = item.dataset.order; event.dataTransfer.setData("text/plain",dragging); event.dataTransfer.effectAllowed = "move"; item.classList.add("dragging"); });
    q("#mdOrder").addEventListener("dragover", event => { if (dragging) event.preventDefault(); });
    q("#mdOrder").addEventListener("drop", event => { event.preventDefault(); const item = event.target.closest("[data-order]"); if (item && dragging && item.dataset.order !== dragging) move(dragging,draft().layout.blocks.indexOf(item.dataset.order)); dragging = ""; });
    q("#mdOrder").addEventListener("dragend", () => { dragging = ""; root.querySelectorAll(".dragging").forEach(el => el.classList.remove("dragging")); });
    window.addEventListener("message", async event => {
      if (event.source !== q("#mdFrame").contentWindow) return;
      if (event.data?.type === "goodsmare-preview-copy" && /^https?:\/\//.test(event.data.text || "")) deps.toast(await deps.copyText(event.data.text) ? "链接已复制" : "复制失败，请长按链接复制", false);
      else if (event.data?.type === "goodsmare-preview-link") deps.toast("这是内容预览；真实推送里点击即可打开对应链接");
    });
    window.addEventListener("beforeunload", event => { if ([...drafts.values()].some(d => d.dirty)) { event.preventDefault(); event.returnValue = ""; } });
  }
  function render(nextState, callbacks) {
    const el = document.getElementById("messageDesigner");
    if (!el || !nextState.message_layout) return;
    state = nextState; deps = callbacks;
    if (!root) { root = el; mount(); }
    root.hidden = false;
    if (scope && !channel()) scope = "";
    draw(); schedule();
  }
  return { render };
})();
