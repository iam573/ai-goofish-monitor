// One keyword editor for both condition and grade vocabularies.
export const splitWords = value => [...new Set(String(value || '').split(/[,，、\n]+/).map(v=>v.trim()).filter(Boolean))];

export function initKeywordEditor(root, {escapeHtml, onChange}) {
  const e = value => escapeHtml(String(value ?? '')).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const values = new Map();
  const cardFor = node => node.closest('[data-keyword-card]');
  const normalized = word => word.normalize('NFKC').replace(/[\u200b\u200c\u200d\u2060\ufeff]/g,'').toLowerCase();
  const unique = words => [...new Map(words.map(word=>[normalized(word),word])).values()];
  function renderTags(card) {
    card.querySelector('[data-keyword-tags]').innerHTML=(values.get(card.dataset.keywordCard)||[]).map(word=>
      `<span class="kw">${e(word)}<button type="button" data-remove-keyword="${e(word)}" aria-label="删除关键词 ${e(word)}">×</button></span>`).join('');
  }
  function commit(input) {
    const card=cardFor(input);if(!card)return;
    const words=splitWords(input.value);input.value='';
    if(words.length) {values.set(card.dataset.keywordCard,unique([...values.get(card.dataset.keywordCard),...words]));renderTags(card);onChange();}
  }
  root.addEventListener('keydown',event=>{
    const input=event.target.closest('[data-add-keyword]');if(!input||event.isComposing)return;
    if(['Enter',',','，','、'].includes(event.key)) {event.preventDefault();commit(input);}
    else if(event.key==='Backspace'&&!input.value) {
      const card=cardFor(input);values.set(card.dataset.keywordCard,values.get(card.dataset.keywordCard).slice(0,-1));renderTags(card);onChange();
    }
  });
  root.addEventListener('input',event=>{
    const input=event.target.closest('[data-add-keyword]');if(!input||event.isComposing)return;
    if(/[,，、\n]/.test(input.value))commit(input);
    onChange();
  });
  root.addEventListener('compositionend',event=>{if(event.target.matches('[data-add-keyword]'))onChange();});
  root.addEventListener('focusout',event=>{if(event.target.matches('[data-add-keyword]'))commit(event.target);});
  root.addEventListener('click',event=>{
    const button=event.target.closest('[data-remove-keyword]');
    if(button) {
      const card=cardFor(button);values.set(card.dataset.keywordCard,values.get(card.dataset.keywordCard).filter(word=>word!==button.dataset.removeKeyword));renderTags(card);onChange();return;
    }
    if(event.target.closest('.tag-editor'))cardFor(event.target).querySelector('[data-add-keyword]').focus();
  });
  return {
    load(rows) {
      values.clear();for(const row of rows)if(row.words)values.set(row.key,[...row.words]);
      root.innerHTML=rows.map(row=>`<article class="keyword-card" data-keyword-card="${e(row.key)}" data-group="${e(row.group||'')}" data-label="${e(row.label)}">
        <div class="keyword-card-head"><b>${e(row.label)}</b>${row.accepted===undefined?'':`<label class="check"><input type="checkbox" data-accepted-grade value="${e(row.key)}" ${row.accepted?'checked':''}>收货</label>`}</div>
        ${row.condition?`<p class="keyword-condition">${e(row.condition)}</p>`:`<div class="tag-editor"><div class="tags" data-keyword-tags></div><input data-add-keyword placeholder="添加命中词…" aria-label="${e(row.label)}添加命中词" autocomplete="off"></div>`}
        ${row.help?`<p class="hint">${e(row.help)}</p>`:''}</article>`).join('');
      for(const card of root.querySelectorAll('[data-keyword-card]'))if(values.has(card.dataset.keywordCard))renderTags(card);
    },
    read() {
      return Object.fromEntries([...values].map(([key,words])=>{
        const card=[...root.querySelectorAll('[data-keyword-card]')].find(el=>el.dataset.keywordCard===key);
        return [key,unique([...words,...splitWords(card?.querySelector('[data-add-keyword]').value)])];
      }));
    },
    filter(query='',group='') {
      query=query.trim().toLowerCase();let count=0;
      for(const card of root.querySelectorAll('[data-keyword-card]')) {
        card.hidden=Boolean(group&&card.dataset.group!==group)||!`${card.dataset.label} ${(values.get(card.dataset.keywordCard)||[]).join(' ')}`.toLowerCase().includes(query);
        if(!card.hidden)count++;
      }
      return count;
    },
  };
}

// Compact tag field using the same markup and styles as the exclusion words.
export function initTagEditor(root, {escapeHtml, onChange}) {
  const input = root.querySelector('input');
  const tags = root.querySelector('.tags');
  const e = word => escapeHtml(word).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  let words = [], disabled = false;
  function render() {
    tags.innerHTML = words.map(word => `<span class="kw">${e(word)}<button type="button" data-remove-tag="${e(word)}" aria-label="删除 ${e(word)}" ${disabled ? 'disabled' : ''}>×</button></span>`).join('');
  }
  function commit() {
    if (disabled) return;
    const added = splitWords(input.value);
    input.value = '';
    if (!added.length) return;
    words = [...new Set([...words, ...added])];
    render(); onChange();
  }
  input.addEventListener('keydown', event => {
    if (disabled || event.isComposing) return;
    if (['Enter', ',', '，', '、'].includes(event.key)) { event.preventDefault(); commit(); }
    else if (event.key === 'Backspace' && !input.value) { words = words.slice(0, -1); render(); onChange(); }
  });
  input.addEventListener('input', event => {
    if (!event.isComposing && /[,，、\n]/.test(input.value)) commit();
    onChange();
  });
  input.addEventListener('compositionend', () => {
    if (/[,，、\n]/.test(input.value)) commit();
    onChange();
  });
  input.addEventListener('blur', commit);
  root.addEventListener('click', event => {
    if (disabled) return;
    const button = event.target.closest('[data-remove-tag]');
    if (button) { words = words.filter(word => word !== button.dataset.removeTag); render(); onChange(); }
    else input.focus();
  });
  return {
    load(value) { words = [...value]; input.value = ''; render(); },
    read() { return [...new Set([...words, ...splitWords(input.value)])]; },
    setDisabled(value) { disabled = value; input.disabled = value; root.setAttribute('aria-disabled', String(value)); render(); },
  };
}
