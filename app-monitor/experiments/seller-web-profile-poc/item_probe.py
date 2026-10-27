"""Observe one normal anonymous item page navigation, stop on its first detail response."""
import argparse,json,time,re
from urllib.parse import urlsplit,parse_qs,urlencode
from playwright.sync_api import sync_playwright
parser=argparse.ArgumentParser(description="Observe the official public item detail response in anonymous Chrome")
parser.add_argument('item_url')
args=parser.parse_args()
url=urlsplit(args.item_url)
query=parse_qs(url.query)
if url.scheme!='https' or url.hostname!='www.goofish.com' or url.path!='/item' or not re.fullmatch(r'[1-9][0-9]{7,19}',query.get('id',[''])[0]):
 parser.error('Provide a public https://www.goofish.com/item?id=... URL')
# Keep public routing/analytics fields only; do not accept credential query parameters.
item_url='https://www.goofish.com/item?'+urlencode({k:query[k][0] for k in ('id','categoryId','spm') if k in query})
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=False)
 c=b.new_context(viewport={'width':1440,'height':1000});page=c.new_page();seen=[]
 def response(r):
  path=urlsplit(r.url).path
  if path!='/h5/mtop.taobao.idle.pc.detail/1.0/':return
  out={'http':r.status,'endpoint':path}
  try:
   data=r.json();out['ret']=data.get('ret');payload=data.get('data',{});item=payload.get('itemDO',{})
   if any(str(v).startswith('SUCCESS') for v in out.get('ret') or []):
    out['item']={k:item.get(k) for k in ('itemId','title','desc','soldPrice','itemStatus')}
    out['parameters']=item.get('cpvLabels',[])
    out['image_count']=len(item.get('imageInfos') or [])
  except Exception as e:out['error']=type(e).__name__
  seen.append(out)
 page.on('response',response)
 try:
  page.goto(item_url,wait_until='domcontentloaded',timeout=25000)
  deadline=time.monotonic()+12
  while time.monotonic()<deadline and not seen:
   page.wait_for_timeout(200)
  print(json.dumps({'anonymous':True,'natural_item_page':True,'title':page.title(),'detail_requests':seen},ensure_ascii=False),flush=True)
 except Exception as e:print(json.dumps({'error':type(e).__name__,'detail_requests':seen}),flush=True)
 finally:b.close()
