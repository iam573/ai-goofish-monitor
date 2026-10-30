import {deductionRows,mergeDeductionKeywords} from './deduction-keywords.js';
import {initKeywordEditor,initTagEditor} from './keyword-editor.js';

export function initSmart({api,escapeHtml,toast,state,gradeOrder,loadOverview,loadDeals,updateRuleHints,onDirty}) {
  const root=document.querySelector('#valuation-controls');
  const input=name=>root.querySelector(`[name="valuation_${name}"]`);
  const e=value=>escapeHtml(value).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const labels={model:'机型',storage:'容量',whole_phone:'整机',full_price:'整机实价',battery_health:'电池健康度',screen_watermark:'屏幕水印',screen_broken:'碎屏',function_fault:'功能故障',locked:'锁机/监管/卡贴',repaired:'拆修',screen_replaced:'换屏',battery_replaced:'换电池',water_damage:'进水',back_glass_broken:'后盖破损',warranty:'保修',color:'颜色',variant:'版本'};
  const statuses={matched:'符合条件',pending:'待确认',rejected:'不符合',analysis_failed:'判价或复核失败'};
  labels.battery_cell_replaced='更换电芯';
  labels.part_replaced='换过电池以外的配件或维修（原装件除外）';
  Object.assign(labels,{screen_original:'原装更换屏',screen_popup:'屏幕弹窗',battery_popup:'电池弹窗',battery_third_party:'第三方电池',battery_swollen:'电池鼓包',overseas:'海外版本',face_id_fault:'面容故障',rear_camera_replaced:'后摄更换',camera_watermark:'镜头水印',screen_aging:'屏幕老化',front_camera_spots:'前摄彩点',rear_camera_spots:'后摄彩点',front_camera_flare:'前镜头蓝光',rear_camera_flare:'后镜头蓝光',true_tone_missing:'缺少原彩',full_set_missing:'缺少全套配件'});
  let config=null,record=null;
  const gradeEditor=initKeywordEditor(document.querySelector('#valuation-keywords'),{escapeHtml,onChange:onDirty});
  const deductionEditor=initKeywordEditor(document.querySelector('#valuation-deduction-keywords'),{escapeHtml,onChange:onDirty});
  const sellerEditor=initTagEditor(document.querySelector('#seller-keyword-editor'),{escapeHtml,onChange:onDirty});
  const booleanFields=['enabled','shadow','ai_enabled','notify_pending','exclude_overseas','exclude_trade_sellers'];
  const scopeFields=['exclude_overseas','blocked_variants','exclude_trade_sellers','seller_exclude_keywords'];
  function updateScopeControls(){
    const enabled=input('enabled').checked;
    document.querySelector('#fixed-grade-choice').hidden=enabled;
    document.querySelector('#valuation-auto-settings').disabled=!enabled;
    document.querySelector('#valuation-summary').textContent=enabled?'按商品描述判断成色和扣费，金额自动取对应机型报价。':'按固定参考成色报价判断，不自动判成色或扣费。';
    for(const el of document.querySelectorAll('#valuation-variants input'))el.disabled=!input('exclude_overseas').checked;
    sellerEditor.setDisabled(!input('exclude_trade_sellers').checked);
  }
  const read=()=>{
    if(!config)return null;
    const payload={};for(const key of booleanFields)payload[key]=input(key).checked;
    payload.blocked_variants=[...document.querySelectorAll('#valuation-variants input:checked')].map(el=>el.value);
    payload.seller_exclude_keywords=sellerEditor.read();
    for(const key of ['base_url','model'])payload[key]=input(key).value.trim();
    payload.price_cap_grade=input('price_cap_grade').value;
    payload.accepted_grades=[...root.querySelectorAll('[data-accepted-grade]:checked')].map(el=>el.value);
    payload.blocked_conditions=[...document.querySelectorAll('#valuation-blocked input:checked')].map(el=>el.value);
    payload.grade_keywords=gradeEditor.read();
    payload.condition_keywords=mergeDeductionKeywords(config.condition_keywords,deductionEditor.read());
    return {...config,...payload};
  };
  const payload=()=>{const value=read();for(const key of ['key_configured','reviewed_samples','fact_fields','version'])delete value[key];if(input('api_key').value.trim())value.api_key=input('api_key').value.trim();return value;};
  const comparable=(key,value)=>JSON.stringify(['grade_keywords','condition_keywords'].includes(key)?Object.entries(value).sort(([a],[b])=>a.localeCompare(b)):value);
  function isDirty(){if(!config)return false;const value=read();return Boolean(input('api_key').value.trim())||['enabled','shadow','ai_enabled','notify_pending','base_url','model','price_cap_grade','accepted_grades','blocked_conditions','grade_keywords','condition_keywords',...scopeFields].some(key=>comparable(key,value[key])!==comparable(key,config[key]));}
  async function loadConfig(){
    config=await api('/api/smart/config');state.smart=config;
    for(const key of booleanFields)input(key).checked=config[key]??true;
    for(const key of ['base_url','model'])input(key).value=config[key];
    input('price_cap_grade').value=config.price_cap_grade??'';
    input('api_key').value='';input('api_key').placeholder=config.key_configured?'已配置，留空保留':'未接入模型时留空';
    gradeEditor.load(gradeOrder.map(grade=>({key:grade,label:grade,words:config.grade_keywords[grade],accepted:config.accepted_grades.includes(grade)})));
    deductionEditor.load(deductionRows(config.condition_keywords));
    document.querySelector('#valuation-blocked').innerHTML=['screen_watermark','screen_broken','function_fault','locked','part_replaced','repaired'].map(key=>`<label class="check"><input type="checkbox" value="${key}" ${config.blocked_conditions.includes(key)?'checked':''}>${labels[key]}</label>`).join('');
    const variants={'美版':'美版','日版':'日版','韩版':'韩版','欧版':'欧版','港澳版':'港澳版','台版':'台版','加版':'加版','海外版':'其他外版'};
    config.blocked_variants??=Object.keys(variants);
    config.seller_exclude_keywords??=['通讯','通信','手机专卖','手机店','二手机','二手手机','手机回收','高价回收','上门回收','卖手机','实体数码','严选数码','数码店','数码科技'];
    for(const key of ['exclude_overseas','exclude_trade_sellers'])config[key]??=true;
    document.querySelector('#valuation-variants').innerHTML=Object.entries(variants).map(([key,label])=>`<label class="check"><input type="checkbox" value="${key}" ${config.blocked_variants.includes(key)?'checked':''}>${label}</label>`).join('');
    sellerEditor.load(config.seller_exclude_keywords);
    updateScopeControls();
    if(state.rules)updateRuleHints();onDirty();
  }
  async function saveConfig(){if(!isDirty())return;await api('/api/smart/config',{method:'PUT',body:JSON.stringify(payload())});await loadConfig();}
  root.addEventListener('input',()=>onDirty());root.addEventListener('change',()=>{updateScopeControls();onDirty();});
  document.querySelector('#rules-reset').addEventListener('click',()=>loadConfig().catch(err=>toast(err.message,true)));
  document.querySelector('#valuation-test').addEventListener('click',async event=>{event.target.disabled=true;try{await api('/api/smart/test',{method:'POST'});toast('模型连接和证据校验通过');}catch(err){toast(err.message,true);}finally{event.target.disabled=false;}});
  const options=(values,selected)=>values.map(([value,label])=>`<option value="${value}" ${value===selected?'selected':''}>${label}</option>`).join('');
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
