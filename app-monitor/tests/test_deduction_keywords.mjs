import assert from 'node:assert/strict';
import test from 'node:test';
import {deductionRows,mergeDeductionKeywords} from '../web/deduction-keywords.js';

const original={screen_replaced:['国产屏','换屏'],screen_original:['换原装屏'],screen_popup:['弹屏'],
  screen_aging:['屏幕老化'],battery_replaced:['换电池','第三方电池'],battery_cell_replaced:['换电芯'],
  battery_third_party:['第三方电池'],battery_popup:['弹电'],face_id_fault:['无面容'],
  front_camera_spots:['前摄彩点'],rear_camera_spots:['后摄彩点'],water_damage:['进水']};

test('只展示七项，电池健康度用数值条件，部件词合并显示且去重',()=>{
  const rows=deductionRows(original);
  assert.deepEqual(rows.map(row=>row.label),['弹屏','屏幕老化','弹电','电池低于80%','面容','前摄彩点','后摄彩点']);
  assert.deepEqual(rows.find(row=>row.key==='battery_replaced').words,['换电池','第三方电池','换电芯','弹电']);
  const health=rows.find(row=>row.key==='battery_health');
  assert.equal(health.words,undefined);
  assert.match(health.condition,/80%不符合/);
});

test('打开并保存保持所有词及具体部件归属，隐藏配置不丢失',()=>{
  const edits=Object.fromEntries(deductionRows(original).filter(row=>row.words).map(row=>[row.key,row.words]));
  assert.deepEqual(mergeDeductionKeywords(original,edits),original);
});

test('删除合并词会移除所有所属字段，新词进入通用项目，不伪造具体部件',()=>{
  const merged=mergeDeductionKeywords(original,{battery_replaced:['换电池','弹电','新品牌电池']});
  assert.deepEqual(merged.battery_replaced,['换电池','新品牌电池']);
  assert.deepEqual(merged.battery_third_party,[]);
  assert.deepEqual(merged.battery_cell_replaced,[]);
  assert.deepEqual(merged.battery_popup,['弹电']);
  assert.deepEqual(merged.water_damage,['进水']);
  assert.deepEqual(original.battery_cell_replaced,['换电芯']);
});

test('清空一项可移除其具体字段，未编辑项目和零宽/大小写匹配稳定',()=>{
  const merged=mergeDeductionKeywords(original,{screen_replaced:[],face_id_fault:['无\u200b面容']});
  assert.deepEqual(merged.screen_replaced,[]);
  assert.deepEqual(merged.screen_original,[]);
  assert.deepEqual(merged.screen_popup,[]);
  assert.deepEqual(merged.face_id_fault,['无面容']);
  assert.deepEqual(merged.battery_replaced,original.battery_replaced);
});
