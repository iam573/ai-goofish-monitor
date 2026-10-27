// Seven everyday deduction items; specific part evidence keeps its original field.
export const deductionItems = [
  {key:'screen_replaced',label:'弹屏',fields:['screen_replaced','screen_original','screen_popup']},
  {key:'screen_aging',label:'屏幕老化',fields:['screen_aging']},
  {key:'battery_replaced',label:'弹电',fields:['battery_replaced','battery_cell_replaced','battery_third_party','battery_popup']},
  {key:'battery_health',label:'电池低于80%',condition:'自动读取电池健康度，数值低于80%时命中。例如78%符合，80%不符合。'},
  {key:'face_id_fault',label:'面容',fields:['face_id_fault']},
  {key:'front_camera_spots',label:'前摄彩点',fields:['front_camera_spots']},
  {key:'rear_camera_spots',label:'后摄彩点',fields:['rear_camera_spots']},
];
const normalize = word=>word.normalize('NFKC').replace(/[\u200b\u200c\u200d\u2060\ufeff]/g,'').toLowerCase().trim();
const unique = words=>[...new Map(words.map(word=>[normalize(word),word])).values()];

export function deductionRows(keywords) {
  return deductionItems.map(item=>({...item,...(item.fields?{words:unique(item.fields.flatMap(field=>keywords[field]||[]))}:{})}));
}

export function mergeDeductionKeywords(original, edits) {
  const merged=Object.fromEntries(Object.entries(original).map(([field,words])=>[field,[...words]]));
  for(const item of deductionItems) {
    if(!item.fields||!Object.hasOwn(edits,item.key))continue;
    const words=unique(edits[item.key]);
    const enabled=new Set(words.map(normalize));
    const oldWords=new Set(item.fields.flatMap(field=>original[field]||[]).map(normalize));
    for(const field of item.fields)merged[field]=(original[field]||[]).filter(word=>enabled.has(normalize(word)));
    merged[item.key].push(...words.filter(word=>!oldWords.has(normalize(word))));
  }
  return merged;
}
