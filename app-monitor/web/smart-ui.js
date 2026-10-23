export function initSmart({api,escapeHtml,toast,state,gradeOrder,loadOverview,loadDeals,updateRuleHints,onDirty}) {
  const root=document.querySelector('#valuation-controls');
  const input=name=>root.querySelector(`[name="valuation_${name}"]`);
  const e=value=>escapeHtml(value).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const labels={model:'机型',storage:'容量',whole_phone:'整机',full_price:'整机实价',battery_health:'电池健康度',screen_watermark:'屏幕水印',screen_broken:'碎屏',function_fault:'功能故障',locked:'锁机/监管/卡贴',repaired:'拆修',screen_replaced:'换屏',battery_replaced:'换电池',water_damage:'进水',back_glass_broken:'后盖破损',warranty:'保修',color:'颜色',variant:'版本'};
  const statuses={matched:'符合条件',pending:'待确认',rejected:'不符合',analysis_failed:'判价或复核失败'};
  Object.assign(labels,{screen_original:'原装更换屏',screen_popup:'屏幕弹窗',battery_popup:'电池弹窗',battery_third_party:'第三方电池',battery_swollen:'电池鼓包',overseas:'海外版本',face_id_fault:'面容故障',rear_camera_replaced:'后摄更换',camera_watermark:'镜头水印',screen_aging:'屏幕老化',front_camera_spots:'前摄彩点',rear_camera_spots:'后摄彩点',front_camera_flare:'前镜头蓝光',rear_camera_flare:'后镜头蓝光',true_tone_missing:'缺少原彩',full_set_missing:'缺少全套配件'});
  let config=null,rows=[],record=null;
  const read=()=>{
    if(!config)return null;
    const payload={};for(const key of ['enabled','shadow','ai_enabled','notify_pending'])payload[key]=input(key).checked;
    for(const key of ['base_url','model'])payload[key]=input(key).value.trim();
    for(const [key,id]of [['accepted_grades','valuation-grades'],['blocked_conditions','valuation-blocked']])payload[key]=[...document.querySelectorAll(`#${id} input:checked`)].map(el=>el.value);
    payload.grade_keywords=Object.fromEntries([...root.querySelectorAll('[data-keyword-grade]')].map(el=>[el.dataset.keywordGrade,el.value.split(/[,，\n]+/).map(v=>v.trim()).filter(Boolean)]));
    return {...config,...payload};
  };
  const payload=()=>{const value=read();for(const key of ['key_configured','reviewed_samples','fact_fields','version'])delete value[key];if(input('api_key').value.trim())value.api_key=input('api_key').value.trim();return value;};
  function isDirty(){if(!config)return false;const value=read();return Boolean(input('api_key').value.trim())||['enabled','shadow','ai_enabled','notify_pending','base_url','model','accepted_grades','blocked_conditions','grade_keywords'].some(key=>JSON.stringify(value[key])!==JSON.stringify(config[key]));}
  async function loadConfig(){
    config=await api('/api/smart/config');state.smart=config;
    for(const key of ['enabled','shadow','ai_enabled','notify_pending'])input(key).checked=config[key];
    for(const key of ['base_url','model'])input(key).value=config[key];
    input('api_key').value='';input('api_key').placeholder=config.key_configured?'已配置，留空保留':'未接入模型时留空';
    document.querySelector('#valuation-summary').textContent=config.enabled?`本地关键词判成色 → 报价扣费${config.ai_enabled?' → AI复核':''} → ${config.shadow?'仅记录':'推送'}；未说明的电池/拆修等按正常处理。`:'暂时使用固定成色报价。';
    document.querySelector('#valuation-keywords').innerHTML=gradeOrder.map(grade=>`<label>${grade}关键词<textarea rows="2" data-keyword-grade="${grade}">${e(config.grade_keywords[grade].join('，'))}</textarea></label>`).join('');
    document.querySelector('#valuation-grades').innerHTML=gradeOrder.map(grade=>`<label class="check"><input type="checkbox" value="${grade}" ${config.accepted_grades.includes(grade)?'checked':''}>${grade}</label>`).join('');
    document.querySelector('#valuation-blocked').innerHTML=['screen_watermark','screen_broken','function_fault','locked','repaired'].map(key=>`<label class="check"><input type="checkbox" value="${key}" ${config.blocked_conditions.includes(key)?'checked':''}>${labels[key]}</label>`).join('');
    if(state.rules)updateRuleHints();onDirty();
  }
  async function saveConfig(){if(!isDirty())return;await api('/api/smart/config',{method:'PUT',body:JSON.stringify(payload())});await loadConfig();}
  root.addEventListener('input',()=>onDirty());root.addEventListener('change',()=>onDirty());
  document.querySelector('#rules-reset').addEventListener('click',()=>loadConfig().catch(err=>toast(err.message,true)));
  document.querySelector('#valuation-test').addEventListener('click',async event=>{event.target.disabled=true;try{await api('/api/smart/test',{method:'POST'});toast('模型连接和证据校验通过');}catch(err){toast(err.message,true);}finally{event.target.disabled=false;}});
  const options=(values,selected)=>values.map(([value,label])=>`<option value="${value}" ${value===selected?'selected':''}>${label}</option>`).join('');
  async function loadPolicies(){
    const model=document.querySelector('#valuation-policy-model').value.trim();if(!model)throw new Error('请输入报价表中的机型');
    rows=(await api(`/api/smart/policies?model=${encodeURIComponent(model)}`)).items;
    document.querySelector('#valuation-policy-rows').innerHTML=rows.map((row,index)=>{const p=row.policy;return `<div class="smart-policy" data-policy-index="${index}"><b>${e(row.text)}</b>
      <label>处理<select data-key="action">${options([['deduct','按报价扣费'],['grade','改成色档位'],['ignore','不参与扣费']],p.action)}</select></label>
      ${(p.grades||[]).length?`<p class="hint">适用成色：${e(p.grades.join('、'))}</p>`:''}${(p.requires||[]).length?`<p class="hint">同时满足：${e(p.requires.map(c=>`${labels[c.field]||c.field} ${c.operator} ${c.value}`).join('；'))}</p>`:''}${(p.alternatives||[]).length?`<p class="hint">也可由：${e(p.alternatives.map(c=>`${labels[c.field]||c.field} ${c.value}`).join('；'))}</p>`:''}
      <div class="field-row"><label>触发字段<select data-key="field">${options(Object.entries(labels),p.field)}</select></label><label>比较<select data-key="operator">${options([['eq','等于'],['contains','包含'],['in','属于任一选项（|分隔）'],['lt','小于'],['le','小于等于'],['gt','大于'],['ge','大于等于']],p.operator)}</select></label></div>
      <label>触发值<input data-key="value" value="${e(p.value)}" placeholder="有/无填 yes/no；健康度填数字"></label>
      <div class="field-row"><label>互斥组<input data-key="group" value="${e(p.group)}"></label><label>目标成色<select data-key="grade">${options(gradeOrder.map(grade=>[grade,grade]),p.grade)}</select></label></div>
      <label class="check"><input data-key="tiered" type="checkbox" ${p.tiered?'checked':''}>电池分档，取更严格阈值，不叠扣</label>
      <label class="check"><input data-key="reviewed" type="checkbox" ${p.reviewed?'checked':''}>使用这条规则（未识别的规则请核对后启用）</label></div>`;}).join('')||'<p class="hint">暂无识别到的报价备注，可在每日报价查看原图。</p>';
  }
  document.querySelector('#valuation-policy-load').addEventListener('click',()=>loadPolicies().catch(err=>toast(err.message,true)));
  document.querySelector('#valuation-policy-save').addEventListener('click',async()=>{try{
    const updates=rows.map((row,index)=>{const policy={...row.policy};for(const el of root.querySelector(`[data-policy-index="${index}"]`).querySelectorAll('[data-key]'))policy[el.dataset.key]=el.type==='checkbox'?el.checked:el.value;if(Object.keys(policy).some(key=>key!=='automatic'&&JSON.stringify(policy[key])!==JSON.stringify(row.policy[key])))policy.automatic=false;return{rule_key:row.rule_key,policy};});
    await api(`/api/smart/policies?model=${encodeURIComponent(document.querySelector('#valuation-policy-model').value.trim())}`,{method:'PUT',body:JSON.stringify(updates)});await loadPolicies();toast('扣费规则已保存');
  }catch(err){toast(err.message,true);}});
  async function openRecord(id){
    record=await api(`/api/smart/assessments/${id}`);const v=record.result,item=record.snapshot;
    document.querySelector('#valuation-detail-title').textContent=`${statuses[record.status]} · ${item.title}`;
    const range=v.value_low!=null?`¥${v.value_low}${v.value_low!==v.value_high?`～${v.value_high}`:''}`:v.value_high!=null?`上限 ¥${v.value_high}（待确认）`:'无法估价';
    document.querySelector('#valuation-detail-body').innerHTML=`<p>卖家：${e(item.seller_name||'未识别')}${item.is_fish_shop?'（鱼小铺）':''}</p><p>挂牌 ¥${item.price} · ${v.grade||'成色待确认'}${v.reference_grade?` · 按${e(v.reference_grade)}报价参考`:''} · 调整后回收价 ${range}</p><p>${v.ai_reviewed?'本地规则 + AI复核':'本地关键词规则'} · ${v.shadow?'仅记录':e(record.notification_status)}</p>
      <h4>判断依据</h4><p>${e(record.facts.grade_evidence||'没有匹配到成色关键词')}</p><p>${e(v.reason)}</p>
      <table><thead><tr><th>适用扣费</th><th>金额</th><th>报价原文</th></tr></thead><tbody>${(v.deductions||[]).map(row=>`<tr><td>${e(row.name)}</td><td>¥${row.min}${row.max!==row.min?`～${row.max}`:''}${row.open_ended?'起':''}</td><td>${e(row.source)}</td></tr>`).join('')||'<tr><td colspan="3">无已匹配扣费</td></tr>'}</tbody></table><p class="hint">${e((v.defaults||[]).join('；'))}</p>
      <h4>商品原文</h4><pre>${e(item.description||'未读到正文')}</pre><details><summary>更多商品信息与依据</summary><table>${Object.entries(labels).map(([key,label])=>{const f=record.facts.findings?.[key]||{};return `<tr><td>${label}</td><td>${e(f.value==='yes'?'是':f.value==='no'?'否':f.value||'未说明')}</td><td>${e(f.evidence||'')}</td></tr>`;}).join('')}</table></details>`;
    document.querySelector('#valuation-correction').innerHTML=`<label>成色<select id="valuation-correct-grade"><option value="">无法确定</option>${options(gradeOrder.map(grade=>[grade,grade]),record.facts.grade)}</select></label><label>成色依据<input id="valuation-correct-evidence" value="${e(record.facts.grade_evidence||'')}"></label><label>待确认项（每行一项）<textarea id="valuation-correct-unknowns">${e((record.facts.uncertainties||[]).join('\n'))}</textarea></label>`;
    document.querySelector('#valuation-detail').showModal();
  }
  document.querySelector('#valuation-detail-close').addEventListener('click',()=>document.querySelector('#valuation-detail').close());
  for(const list of document.querySelectorAll('#run-detail, #deal-list'))list.addEventListener('click',event=>{const button=event.target.closest('[data-assessment-id]');if(button)openRecord(Number(button.dataset.assessmentId)).catch(err=>toast(err.message,true));});
  const action=async(path,options={})=>{const updated=await api(`/api/smart/assessments/${record.id}/${path}`,{method:'POST',...options});document.querySelector('#valuation-detail').close();await Promise.all([openRecord(updated.id),loadDeals(),loadOverview()]);toast('已重新判价');};
  document.querySelector('#valuation-reanalyze').addEventListener('click',()=>action('reanalyze').catch(err=>toast(err.message,true)));
  document.querySelector('#valuation-correct-save').addEventListener('click',()=>{const facts={...record.facts,grade:document.querySelector('#valuation-correct-grade').value||null,grade_evidence:document.querySelector('#valuation-correct-evidence').value,uncertainties:document.querySelector('#valuation-correct-unknowns').value.split('\n').filter(Boolean)};action('correction',{method:'PUT',body:JSON.stringify(facts)}).catch(err=>toast(err.message,true));});
  return{loadConfig,saveConfig,isDirty};
}
