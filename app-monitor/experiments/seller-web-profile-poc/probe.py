"""Use the website's own SDK in a clean anonymous browser; never export credentials."""
import argparse
import json
import re
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser(description="Read public seller profile through the official web SDK")
parser.add_argument('user_id')
parser.add_argument('--headless',action='store_true',help='May be rejected by the website; visible Chrome is the verified mode')
args=parser.parse_args()
if not re.fullmatch(r'[1-9][0-9]{6,19}',args.user_id):
 parser.error('user_id must be a numeric seller ID')

with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=args.headless)
 context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page()
 try:
  page.goto(f'https://www.goofish.com/personal?userId={args.user_id}',wait_until='domcontentloaded',timeout=25000)
  page.wait_for_function("window.lib?.mtop?.request && !document.body.innerText.includes('非法访问')",timeout=15000)
  result=page.evaluate('''async userId=>{
    const call=async(api,data)=>{
      try {return await window.lib.mtop.request({api,v:'1.0',type:'POST',appKey:'34839810',accountSite:'xianyu',dataType:'json',timeout:7000,needLogin:false,sessionOption:'AutoLoginOnly',ecode:0,data});}
      catch(e) {return {ret:e.ret||['REQUEST_FAILED'],data:{}};}
    };
    const out={};
    const head=await call('mtop.idle.web.user.page.head',{self:false,userId});
    out.head={ret:head.ret,nickname:head.data?.module?.base?.displayName,credit:(head.data?.module?.base?.ylzTags||[]).map(t=>({text:t.text,role:t.attributes?.role,level:t.attributes?.level}))};
    const items=await call('mtop.idle.web.xyh.item.list',{needGroupInfo:true,pageNumber:1,pageSize:20,userId});
    const cards=items.data?.cardList||[];
    out.items={ret:items.ret,count:cards.length,nextPage:items.data?.nextPage,totalCount:items.data?.totalCount,
      groups:(items.data?.itemGroupList||[]).map(g=>({name:g.groupName,count:g.itemNumber})),
      samples:cards.slice(0,3).map(c=>({id:c.cardData?.id,title:c.cardData?.title,status:c.cardData?.itemStatus,price:c.cardData?.priceInfo?.price}))};
    const ratedUid=head.data?.baseInfo?.kcUserId;
    if(ratedUid){
      const rates=await call('mtop.idle.web.trade.rate.list',{ratedUid,rateType:0,raterType:0,rowsPerPage:20,pageNumber:1,foldFlag:0,fishAdCode:'330110',extraTag:''});
      out.rates={ret:rates.ret,count:rates.data?.cardList?.length,nextPage:rates.data?.nextPage};
    }
    return out;
  }''',args.user_id)
  print(json.dumps({'anonymous':True,'sdk_direct_calls':result},ensure_ascii=False),flush=True)
 except Exception as e:print(json.dumps({'sdk_probe_error':type(e).__name__}),flush=True)
 finally:browser.close()
