"""
app.py — 台股選股雷達 雲端版
支援 Railway 部署，資料存到 Supabase
手機電腦隨時都能看
"""

from flask import Flask, jsonify, render_template_string
import json, os, datetime, threading, time, ssl, urllib.request, urllib.parse

app = Flask(__name__)

# ── Supabase 設定 ──
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://eunegubxxrdxoziloyar.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImV1bmVndWJ4eHJkeG96aWxveWFyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgxNTk5MTgsImV4cCI6MjA5MzczNTkxOH0.27_yWVQAW9qD5mcx2tJ2058_6A5ZJ4cmEy6Lm7ZEQMM')

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_scanning = False
_progress = {'current': 0, 'total': 0, 'pct': 0, 'status': '待機'}

# ══ Supabase 操作 ══

def sb_request(method, path, data=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    body = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal' if method in ['POST','PATCH','DELETE'] else 'return=representation'
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            resp = r.read().decode('utf-8')
            return json.loads(resp) if resp.strip() else []
    except Exception as e:
        print(f"[Supabase] {method} {path} 失敗: {e}")
        return None

def save_to_supabase(system, data):
    today = datetime.date.today().isoformat()
    # 先刪除今天同系統舊資料
    sb_request('DELETE', f'scan_results?date=eq.{today}&system=eq.{system}')
    # 插入新資料
    result = sb_request('POST', 'scan_results', {
        'date': today,
        'system': system,
        'data': data,
        'updated_at': datetime.datetime.now().isoformat()
    })
    return result is not None

def load_from_supabase(system):
    today = datetime.date.today().isoformat()
    result = sb_request('GET', f'scan_results?date=eq.{today}&system=eq.{system}&select=data,updated_at&limit=1')
    if result and len(result) > 0:
        return result[0]
    # 找最近一筆
    result = sb_request('GET', f'scan_results?system=eq.{system}&select=data,updated_at,date&order=date.desc&limit=1')
    if result and len(result) > 0:
        return result[0]
    return None

# ══ 資料抓取 ══

def fetch_quote(code):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW?interval=1d&range=90d"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
            data = json.loads(r.read().decode('utf-8'))
        res = data['chart']['result'][0]
        meta = res['meta']
        q = res.get('indicators',{}).get('quote',[{}])[0]
        closes  = [c for c in q.get('close',[])  if c is not None]
        volumes = [v for v in q.get('volume',[]) if v is not None]
        highs   = [h for h in q.get('high',[])   if h is not None]
        lows    = [l for l in q.get('low',[])    if l is not None]
        if len(closes) < 20: return None
        price = float(meta.get('regularMarketPrice',0) or 0)
        prev  = float(meta.get('chartPreviousClose',price) or price)
        pct   = round((price-prev)/prev*100,2) if prev else 0
        def ma(n): return round(sum(closes[-n:])/n,1) if len(closes)>=n else None
        avg20 = int(sum(volumes[-20:])/min(20,len(volumes))) if volumes else 0
        todvol = volumes[-1] if volumes else 0
        def rsi(n=14):
            if len(closes)<n+1: return 50
            d=[closes[i]-closes[i-1] for i in range(1,len(closes))][-n:]
            g=[x for x in d if x>0]; l=[-x for x in d if x<0]
            ag=sum(g)/n if g else 0; al=sum(l)/n if l else 0.001
            return round(100-100/(1+ag/al),1)
        ma20v = ma(20)
        std20 = (sum((c-ma20v)**2 for c in closes[-20:])/20)**0.5 if ma20v else 0
        return {
            'price':round(price,1),'prev':round(prev,1),'pct':pct,
            'volume':int(meta.get('regularMarketVolume',0) or 0),
            'ma5':ma(5),'ma10':ma(10),'ma20':ma20v,'ma60':ma(60),
            'vol_today':int(todvol),'vol_avg20':avg20,
            'vol_ratio':round(todvol/avg20,2) if avg20 else 0,
            'rsi':rsi(),
            'bb_upper':round(ma20v+2*std20,1) if ma20v else None,
            'bb_lower':round(ma20v-2*std20,1) if ma20v else None,
            'high5':max(highs[-5:]) if len(highs)>=5 else price,
            'high20':max(highs[-20:]) if len(highs)>=20 else price,
            'low20':min(lows[-20:]) if len(lows)>=20 else price,
            'recent_closes':closes[-20:],
            'recent_volumes':volumes[-20:],
            'recent_highs':highs[-20:],
            'recent_lows':lows[-20:],
            'ok':True
        }
    except: return None

def detect_pattern(q):
    price=q['price']; ma5=q['ma5']; ma10=q['ma10']; ma20=q['ma20']
    vol_r=q['vol_ratio']; rsi=q['rsi']; pct=q['pct']
    rc=q['recent_closes']; rv=q['recent_volumes']
    rh=q['recent_highs']; rl=q['recent_lows']
    high20=q['high20']
    signals=[]

    # 出貨偵測
    dist=0
    if vol_r>=2 and abs(pct)<2: dist+=30; signals.append(f'高位爆量({vol_r}x)漲幅收斂')
    if ma20 and (price-ma20)/ma20*100>25: dist+=20; signals.append(f'乖離MA20過大')
    if ma5 and price<ma5: dist+=15; signals.append('跌破MA5')
    if len(rh)>=10 and max(rh[-5:])<max(rh[-10:-5])*0.97: dist+=20; signals.append('高點下移')
    if dist>=50: return 'dist', dist, signals

    # 突破
    brk=0
    if price>=high20*0.99 and vol_r>=1.5: brk+=35; signals.append(f'突破20日高點+放量{vol_r}x')
    if ma5 and ma10 and ma20 and price>ma5>ma10>ma20: brk+=25; signals.append('均線多頭排列')
    if pct>3 and vol_r>=2: brk+=20; signals.append(f'量價齊揚')
    if brk>=35: return 'breakout', brk, signals

    # 洗籌碼
    wash=0
    if len(rv)>=5:
        avg_r=sum(rv[-5:])/5; avg_b=sum(rv[-10:-5])/5 if len(rv)>=10 else avg_r
        if avg_r<avg_b*0.7 and price>ma20*0.97: wash+=25; signals.append('量縮價穩')
    if len(rh)>=5 and len(rl)>=5:
        rng=(max(rh[-5:])-min(rl[-5:]))/price*100
        if rng<5 and ma20 and price>ma20: wash+=20; signals.append(f'震幅收斂{rng:.1f}%')
    if ma5 and ma20 and ma5>ma20 and abs(ma5-ma20)/ma20<0.03: wash+=15; signals.append('均線收斂整理')
    if 40<=rsi<=60: wash+=10; signals.append(f'RSI中性({rsi})')
    if wash>=40: return 'wash', wash, signals

    # 糾結蓄勢
    coil=0
    if ma5 and ma10 and ma20:
        sp=(max(ma5,ma10,ma20)-min(ma5,ma10,ma20))/ma20*100
        if sp<2: coil+=35; signals.append(f'均線糾結({sp:.1f}%)')
        elif sp<4: coil+=20; signals.append(f'均線收斂({sp:.1f}%)')
    if q['vol_avg20']>0 and q['vol_today']<q['vol_avg20']*0.5: coil+=20; signals.append('量能萎縮')
    if ma20 and abs(price-ma20)/ma20<0.02: coil+=15; signals.append('股價貼近MA20')
    if coil>=40: return 'coil', coil, signals

    return 'normal', 0, signals

def score_stock(code, name, market, q):
    if not q: return None
    price=q['price']; pct=q['pct']; ma5=q['ma5']; ma20=q['ma20']
    vol_r=q['vol_ratio']; rsi=q['rsi']
    if ma20 and price<ma20*0.90: return None

    pt, ps, pat_signals = detect_pattern(q)

    if pt=='dist' and ps>=60:
        return {'code':code,'name':name,'market':market,'price':price,'pct':pct,
                'score':20,'action':'出貨警示','pattern':'⚠ 疑似出貨','pattern_type':'dist',
                'signals':pat_signals,'warnings':['高度疑似出貨'],
                'vol_ratio':vol_r,'rsi':rsi,'ma5':ma5,'ma10':q['ma10'],'ma20':ma20,
                'entry':None,'stop':round((ma20 or price)*0.97,1),'target':None,'hold':'出場觀察'}

    score=50; sigs=[]; warns=[]
    if pct>=9.5: score+=15; sigs.append('漲停板')
    elif pct>=5: score+=10; sigs.append(f'強勢+{pct}%')
    elif pct>=2: score+=5;  sigs.append(f'上漲+{pct}%')
    elif pct<-3: score-=10; warns.append(f'下跌{pct}%')

    pts={'breakout':25,'wash':20,'coil':18,'normal':0,'dist':-15}
    ptl={'breakout':'🚀 突破發動','wash':'🔄 洗籌碼整理','coil':'⚡ 均線糾結蓄勢','normal':'📊 正常','dist':'⚠ 疑似出貨'}
    score+=pts.get(pt,0); sigs+=pat_signals

    if vol_r>=3:   score+=15; sigs.append(f'爆量{vol_r}x')
    elif vol_r>=2: score+=10; sigs.append(f'放量{vol_r}x')
    elif vol_r>=1.5:score+=5; sigs.append(f'量增{vol_r}x')
    elif vol_r<0.4: score-=8; warns.append('嚴重縮量')

    if rsi:
        if 45<=rsi<=65: score+=8; sigs.append(f'RSI健康({rsi})')
        elif rsi>80: score-=12; warns.append(f'RSI過熱({rsi})')
        elif rsi<25: score+=5;  sigs.append(f'RSI超賣({rsi})')

    if ma5 and q['ma10'] and ma20 and price>ma5>q['ma10']>ma20:
        score+=12; sigs.append('均線多頭排列')
    elif ma5 and price<ma5:
        score-=5; warns.append('跌破MA5')

    score=max(0,min(100,score))
    entry=round(ma5*1.005,1) if ma5 else None
    stop=round((q['ma10'] or ma20 or price)*0.97,1)
    tp=0.15 if score>=75 else(0.10 if score>=60 else 0.07)
    target=round(price*(1+tp),1)
    if score>=78:   hold='15~30天'; action='積極布局'
    elif score>=68: hold='7~15天';  action='建議布局'
    elif score>=58: hold='3~7天';   action='建議觀察'
    else:           hold='觀望';    action='暫時觀望'

    return {'code':code,'name':name,'market':market,'price':price,'pct':pct,
            'score':score,'action':action,'pattern':ptl.get(pt,'📊 正常'),'pattern_type':pt,
            'signals':sigs,'warnings':warns,'vol_ratio':vol_r,'rsi':rsi,
            'ma5':ma5,'ma10':q['ma10'],'ma20':ma20,
            'entry':entry,'stop':stop,'target':target,'hold':hold}

def get_stock_list():
    """使用內建股票清單（避免TWSE境外封鎖）"""
    stocks = [
        ("2330","台積電","上市"),("2317","鴻海","上市"),("2454","聯發科","上市"),
        ("2308","台達電","上市"),("2303","聯電","上市"),("2379","瑞昱","上市"),
        ("2382","廣達","上市"),("2356","英業達","上市"),("2357","華碩","上市"),
        ("2353","宏碁","上市"),("3034","聯詠","上市"),("2408","南亞科","上市"),
        ("2344","華邦電","上市"),("3702","大聯大","上市"),("2395","研華","上市"),
        ("3037","欣興","上市"),("8046","南電","上市"),("4958","臻鼎-KY","上市"),
        ("8074","鉅橡","上市"),("2368","金像電","上市"),("3044","健鼎","上市"),
        ("3363","上詮","上市"),("6442","光聖","上市"),("2455","全新","上市"),
        ("8086","宏捷科","上市"),("3081","聯亞","上市"),("6515","穎崴","上市"),
        ("2345","智邦","上市"),("6223","旺矽","上市"),
        ("6669","緯穎","上市"),("3231","緯創","上市"),("5274","信驊","上市"),
        ("8027","鈦昇","上市"),("8064","東捨","上市"),("6788","華景電","上市"),
        ("2049","上銀","上市"),("2365","昆盈","上市"),
        ("2327","國巨","上市"),("2375","凱美","上市"),("6173","信昌電","上市"),
        ("2882","國泰金","上市"),("2881","富邦金","上市"),("2884","玉山金","上市"),
        ("2886","兆豐金","上市"),("2891","中信金","上市"),("2885","元大金","上市"),
        ("2892","第一金","上市"),("2880","華南金","上市"),
        ("2409","友達","上市"),("3481","群創","上市"),("2489","瑞軒","上市"),
        ("2412","中華電","上市"),("3045","台灣大","上市"),("4904","遠傳","上市"),
        ("1303","南亞","上市"),("1301","台塑","上市"),("1326","台化","上市"),
        ("2002","中鋼","上市"),("1216","統一","上市"),
        ("2324","仁寶","上市"),("2376","技嘉","上市"),("2385","群光","上市"),
        ("6176","瑞儀","上市"),("2360","致茂","上市"),("6278","台表科","上市"),
        ("4720","德淵","上市"),("6643","M31","上市"),("2441","超豐","上市"),
        ("6412","群電","上市"),("6531","愛普","上市"),("2486","一詮","上市"),
        ("6451","訊芯-KY","上市"),("3443","創意","上市"),("2426","鼎元","上市"),
        ("1514","亞力","上市"),("3669","圓展","上市"),
        ("6510","精測","上市"),("3533","嘉澤","上市"),("6770","力積電","上市"),
        ("3711","日月光投控","上市"),("3017","奇鋐","上市"),("6196","帆宣","上市"),
        ("5904","寶雅","上市"),("2912","統一超","上市"),("2207","和泰車","上市"),
        ("3008","大立光","上市"),("2474","可成","上市"),("3529","力旺","上市"),
        ("6202","盛群","上市"),("3293","鈊象","上市"),("3016","嘉晶","上市"),
        ("3714","富采","上市"),("1590","亞德客-KY","上市"),("6415","矽力-KY","上市"),
        ("2392","正崴","上市"),("5388","中磊","上市"),("3596","智易","上市"),
        ("6409","旭隼","上市"),("8114","振樺電","上市"),("4126","太醫","上市"),
        ("1795","美時","上市"),("2105","正新","上市"),("1402","遠東新","上市"),
    ]
    seen = set(); unique = []
    for s in stocks:
        if s[0] not in seen:
            seen.add(s[0]); unique.append(s)
    print(f'  內建股票清單：{len(unique)} 支')
    return unique

def run_scan_task(system='v5'):
    global _scanning, _progress
    _scanning=True
    try:
        stocks=get_stock_list()
        total=len(stocks)
        _progress={'current':0,'total':total,'pct':0,'status':'掃描中'}
        print(f"[掃描] {system} 開始，{total}支")
        results=[]
        for i,(code,name,market) in enumerate(stocks):
            q=fetch_quote(code)
            if q:
                s=score_stock(code,name,market,q)
                if s and s['score']>=55: results.append(s)
            if (i+1)%50==0:
                _progress={'current':i+1,'total':total,'pct':round((i+1)/total*100),'status':f'掃描中({len(results)}支候選)'}
                print(f"  {i+1}/{total}，候選{len(results)}支")
            time.sleep(0.2)

        results.sort(key=lambda x:x['score'],reverse=True)
        output={'updated':datetime.datetime.now().isoformat(),'date':datetime.date.today().isoformat(),
                'total_scanned':total,'total_found':len(results),'stocks':results[:100]}

        if save_to_supabase(system, output):
            print(f"[掃描完成] {len(results)}支，已存Supabase")
        else:
            print("[警告] Supabase存檔失敗")

        _progress={'current':total,'total':total,'pct':100,'status':f'完成！找到{len(results)}支'}
    except Exception as e:
        print(f"[掃描錯誤] {e}")
        _progress={'current':0,'total':0,'pct':0,'status':f'錯誤：{e}'}
    finally:
        _scanning=False

# ══ API ══

@app.route('/api/result')
def api_result():
    system=request_arg('system','v5')
    row=load_from_supabase(system)
    if row: return jsonify(row.get('data',{}))
    return jsonify({})

@app.route('/api/progress')
def api_progress():
    return jsonify(_progress)

@app.route('/api/start_scan')
def api_start_scan():
    global _scanning
    if _scanning: return jsonify({'status':'running','msg':'掃描中...'})
    t=threading.Thread(target=run_scan_task,args=('v5',),daemon=True)
    t.start()
    return jsonify({'status':'started','msg':'掃描開始'})

@app.route('/api/status')
def api_status():
    row=load_from_supabase('v5')
    data=row.get('data',{}) if row else {}
    return jsonify({
        'updated':data.get('updated','尚未掃描'),
        'date':data.get('date',''),
        'total_found':data.get('total_found',0),
        'scanning':_scanning,
        'progress':_progress
    })

def request_arg(key, default=''):
    from flask import request
    return request.args.get(key, default)

def auto_scan_on_start():
    time.sleep(3)
    row=load_from_supabase('v5')
    today=datetime.date.today().isoformat()
    if not row or row.get('date','')!=today:
        print("[自動掃描] 今天尚未掃描，自動開始...")
        run_scan_task('v5')
    else:
        data=row.get('data',{})
        print(f"[自動掃描] 今天已掃描（{data.get('total_found',0)}支），跳過")

# ══ 前端 ══
HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股選股雷達</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#07080b;--bg2:#0d1018;--bg3:#141920;--bg4:#1a2130;--bd:rgba(255,255,255,0.06);--bd2:rgba(255,255,255,0.13);--t:#dde2f0;--t2:#7a8299;--t3:#3a4156;--acc:#ff5c2e;--acc2:#ff8a5c;--blue:#3d8fe8;--blue2:#70b4ff;--green:#29d47a;--green2:#6beaaa;--amber:#f0a500;--purple:#9b6dff;--red:#e84040;--teal:#0fb8a0;--mono:'DM Mono',monospace;--sans:'Noto Sans TC',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:var(--sans);min-height:100vh}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:2px}
.hdr{position:sticky;top:0;z-index:100;background:rgba(7,8,11,0.96);backdrop-filter:blur(20px);border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between;padding:0 16px;height:52px;gap:10px}
.logo{display:flex;align-items:center;gap:8px}
.logo-m{width:28px;height:28px;background:linear-gradient(135deg,var(--acc),var(--purple));border-radius:7px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px;color:#fff}
.logo-t{font-size:14px;font-weight:700}
.hdr-r{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:pulse 2s infinite}
.dot.scanning{background:var(--amber);box-shadow:0 0 6px var(--amber)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.scan-btn{padding:6px 14px;background:linear-gradient(135deg,var(--acc),#e03010);border:none;border-radius:7px;color:#fff;font-family:var(--mono);font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap}
.scan-btn:disabled{opacity:.5;cursor:not-allowed}
.upd-txt{font-size:11px;font-family:var(--mono);color:var(--t3)}
.wrap{max-width:900px;margin:0 auto;padding:16px 14px 80px}
.prog-wrap{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin-bottom:14px;display:none}
.prog-wrap.show{display:block}
.prog-hdr{display:flex;justify-content:space-between;margin-bottom:7px}
.prog-lbl{font-size:11px;color:var(--t2);font-family:var(--mono)}
.prog-pct{font-size:12px;font-weight:700;font-family:var(--mono);color:var(--amber)}
.prog-bar{height:5px;background:var(--bg4);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--acc),var(--amber));border-radius:3px;transition:width .5s}
.prog-st{font-size:10px;color:var(--t3);margin-top:5px;font-family:var(--mono)}
.filter-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.fbtn{padding:4px 11px;border:1px solid var(--bd2);border-radius:20px;background:transparent;color:var(--t2);font-size:11px;cursor:pointer;font-family:var(--mono)}
.fbtn.on{background:rgba(255,92,46,.12);border-color:var(--acc);color:var(--acc2)}
.fcnt{font-size:11px;color:var(--t3);font-family:var(--mono);margin-left:auto}
.cards{display:flex;flex-direction:column;gap:10px}
.card{background:var(--bg2);border:1px solid var(--bd);border-radius:13px;overflow:hidden;position:relative}
.card:hover{border-color:var(--bd2)}
.stripe{position:absolute;left:0;top:0;bottom:0;width:3px}
.stripe.breakout{background:var(--green)}.stripe.wash{background:var(--blue)}
.stripe.coil{background:var(--purple)}.stripe.dist{background:var(--red)}.stripe.normal{background:var(--t3)}
.ctop{display:flex;align-items:flex-start;justify-content:space-between;padding:12px 14px 0 18px;flex-wrap:wrap;gap:8px}
.cbody{padding:10px 14px 12px 18px}
.sname{font-size:15px;font-weight:700}
.scode{font-size:11px;font-family:var(--mono);color:var(--t3);margin-left:5px}
.smkt{font-size:10px;padding:2px 5px;background:var(--bg4);border-radius:4px;color:var(--t3);font-family:var(--mono)}
.sprice{font-size:17px;font-weight:700;font-family:var(--mono)}
.spct{font-size:11px;font-family:var(--mono);margin-top:2px}
.badge{font-size:10px;padding:2px 7px;border-radius:5px;font-weight:600;border:1px solid transparent;display:inline-block;margin:2px}
.b-grn{background:rgba(41,212,122,.12);color:var(--green2);border-color:rgba(41,212,122,.25)}
.b-blue{background:rgba(61,143,232,.12);color:var(--blue2);border-color:rgba(61,143,232,.25)}
.b-amb{background:rgba(240,165,0,.12);color:var(--amber);border-color:rgba(240,165,0,.25)}
.b-pur{background:rgba(155,109,255,.12);color:var(--purple);border-color:rgba(155,109,255,.25)}
.b-red{background:rgba(232,64,64,.15);color:#fc8181;border-color:rgba(232,64,64,.3)}
.b-tel{background:rgba(15,184,160,.12);color:var(--teal);border-color:rgba(15,184,160,.25)}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:9px 0}
.g2{display:grid;grid-template-columns:repeat(2,1fr);gap:7px;margin:9px 0}
@media(max-width:500px){.g3{grid-template-columns:1fr 1fr}}
.met{background:var(--bg3);border:1px solid var(--bd);border-radius:7px;padding:8px 10px}
.ml{font-size:10px;color:var(--t3);font-family:var(--mono);margin-bottom:2px}
.mv{font-size:13px;font-weight:700;font-family:var(--mono)}
.up{color:var(--acc)}.dn{color:var(--blue2)}.ok{color:var(--green)}.amb{color:var(--amber)}.danger{color:#fc8181}
.scrow{display:flex;align-items:center;gap:7px;margin-bottom:4px}
.scl{font-size:10px;color:var(--t3);font-family:var(--mono);min-width:50px}
.sctr{flex:1;height:4px;background:var(--bg4);border-radius:2px;overflow:hidden}
.scf{height:100%;border-radius:2px}
.scv{font-size:11px;font-family:var(--mono);font-weight:700;min-width:24px;text-align:right}
.exbox{background:var(--bg3);border:1px solid var(--bd);border-radius:7px;padding:9px 11px;margin-top:9px}
.ext{font-size:10px;font-weight:600;color:var(--t2);font-family:var(--mono);margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em}
.exi{font-size:12px;color:var(--t2);padding:3px 0;border-bottom:1px solid var(--bd);line-height:1.5}
.exi:last-child{border-bottom:none}
.exi strong{color:var(--t)}
.ring{width:46px;height:46px;flex-shrink:0;position:relative}
.ring svg{transform:rotate(-90deg)}
.rnum{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;font-family:var(--mono)}
.info-b{background:rgba(61,143,232,.07);border:1px solid rgba(61,143,232,.2);border-radius:8px;padding:10px 13px;font-size:12px;color:var(--blue2);line-height:1.7;margin-bottom:12px}
.loading{display:flex;align-items:center;justify-content:center;gap:8px;padding:50px;font-size:12px;color:var(--t3);font-family:var(--mono)}
.spin{width:13px;height:13px;border:2px solid var(--bd2);border-top-color:var(--acc);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<header class="hdr">
  <div class="logo">
    <div class="logo-m">台</div>
    <span class="logo-t">台股選股雷達</span>
  </div>
  <div class="hdr-r">
    <div class="dot" id="dot"></div>
    <span class="upd-txt" id="updTxt">載入中...</span>
    <button class="scan-btn" id="scanBtn" onclick="startScan()">🔍 全市場掃描</button>
  </div>
</header>

<div class="wrap">
  <div class="prog-wrap" id="progWrap">
    <div class="prog-hdr">
      <span class="prog-lbl" id="progLbl">掃描中...</span>
      <span class="prog-pct" id="progPct">0%</span>
    </div>
    <div class="prog-bar"><div class="prog-fill" id="progFill" style="width:0%"></div></div>
    <div class="prog-st" id="progSt"></div>
  </div>

  <div class="filter-bar" id="filterBar" style="display:none">
    <button class="fbtn on" onclick="filter('all',this)">全部</button>
    <button class="fbtn" onclick="filter('breakout',this)">🚀 突破</button>
    <button class="fbtn" onclick="filter('wash',this)">🔄 洗籌碼</button>
    <button class="fbtn" onclick="filter('coil',this)">⚡ 蓄勢</button>
    <button class="fbtn" onclick="filter('dist',this)">⚠ 出貨</button>
    <span class="fcnt" id="fcnt"></span>
  </div>

  <div id="body"><div class="loading"><div class="spin"></div>載入中...</div></div>
</div>

<script>
let all=[], pollT=null;

function ring(s){
  const c=s>=75?'#29d47a':s>=60?'#f0a500':s>=45?'#3d8fe8':'#e84040';
  const r=17,cx=23,ci=2*Math.PI*r,d=(s/100)*ci;
  return `<div class="ring"><svg width="46" height="46" viewBox="0 0 46 46">
    <circle cx="${cx}" cy="${cx}" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="4"/>
    <circle cx="${cx}" cy="${cx}" r="${r}" fill="none" stroke="${c}" stroke-width="4"
      stroke-dasharray="${d.toFixed(1)} ${ci.toFixed(1)}" stroke-linecap="round"/></svg>
    <div class="rnum" style="color:${c}">${s}</div></div>`;
}
function ptBadge(t,l){
  const m={breakout:'b-grn',wash:'b-blue',coil:'b-pur',dist:'b-red',normal:'b-amb'};
  return `<span class="badge ${m[t]||'b-amb'}">${l}</span>`;
}
function actBadge(a){
  const m={'積極布局':'b-grn','建議布局':'b-grn','建議觀察':'b-amb','暫時觀望':'b-amb','出貨警示':'b-red'};
  return `<span class="badge ${m[a]||'b-amb'}">${a}</span>`;
}

function renderCards(stocks){
  if(!stocks||!stocks.length){
    document.getElementById('body').innerHTML='<div class="info-b">此篩選條件暫無股票。</div>';
    return;
  }
  let h='<div class="cards">';
  for(const s of stocks){
    const uc=s.pct>=0?'var(--acc)':'var(--blue2)';
    const sigs=(s.signals||[]).slice(0,5).map(x=>`<span class="badge b-tel" style="font-size:9px">${x}</span>`).join('');
    const warns=(s.warnings||[]).map(x=>`<span class="badge b-red" style="font-size:9px">${x}</span>`).join('');
    const noEntry=s.pattern_type==='dist';
    h+=`<div class="card">
      <div class="stripe ${s.pattern_type}"></div>
      <div class="ctop">
        <div>
          <div><span class="sname">${s.name}</span><span class="scode">${s.code}</span><span class="smkt">${s.market}</span></div>
          <div style="margin-top:6px">${ptBadge(s.pattern_type,s.pattern)} ${actBadge(s.action)}</div>
          <div style="margin-top:4px">${sigs}${warns}</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          ${ring(s.score)}
          <div style="text-align:right">
            <div class="sprice" style="color:${uc}">${s.price}</div>
            <div class="spct" style="color:${uc}">${s.pct>=0?'+':''}${s.pct}%</div>
          </div>
        </div>
      </div>
      <div class="cbody">
        <div class="scrow"><span class="scl">評分</span>
          <div class="sctr"><div class="scf" style="width:${s.score}%;background:${s.score>=70?'var(--green)':s.score>=55?'var(--amber)':'var(--red)'}"></div></div>
          <span class="scv" style="color:${s.score>=70?'var(--green)':s.score>=55?'var(--amber)':'var(--red)'}">${s.score}</span></div>
        <div class="g3">
          <div class="met"><div class="ml">量能</div><div class="mv ${s.vol_ratio>=2?'ok':'amb'}">${s.vol_ratio}x</div></div>
          <div class="met"><div class="ml">RSI</div><div class="mv ${s.rsi>75?'danger':'ok'}">${s.rsi||'---'}</div></div>
          <div class="met"><div class="ml">MA20乖離</div><div class="mv">${s.ma20?((s.price-s.ma20)/s.ma20*100).toFixed(1)+'%':'---'}</div></div>
        </div>
        <div class="g3">
          <div class="met"><div class="ml">MA5</div><div class="mv">${s.ma5||'---'}</div></div>
          <div class="met"><div class="ml">MA10</div><div class="mv">${s.ma10||'---'}</div></div>
          <div class="met"><div class="ml">MA20</div><div class="mv">${s.ma20||'---'}</div></div>
        </div>
        ${!noEntry&&s.entry?`<div class="g2">
          <div class="met"><div class="ml">📍 進場</div><div class="mv ok">${s.entry}元</div></div>
          <div class="met"><div class="ml">🛑 停損</div><div class="mv danger">${s.stop}元</div></div>
          <div class="met"><div class="ml">🎯 目標</div><div class="mv up">${s.target}元</div></div>
          <div class="met"><div class="ml">⏱ 持倉</div><div class="mv amb">${s.hold}</div></div>
        </div>
        <div class="exbox"><div class="ext">出場條件</div>
          <div class="exi">跌破 <strong>${s.stop}</strong> 元 → 立即出場</div>
          <div class="exi">量連2日低於均量50% → 減碼</div>
          <div class="exi">達 <strong>${s.target}</strong> 元 → 分批獲利</div>
          ${s.pattern_type==='wash'?'<div class="exi">⚡ 洗完確認：量放大+突破整理區上緣</div>':''}
          ${s.pattern_type==='coil'?'<div class="exi">⚡ 糾結突破：均線發散+量增2倍以上</div>':''}
        </div>`:`<div class="exbox" style="border-color:rgba(232,64,64,.2)"><div class="ext" style="color:#fc8181">⚠ 出貨警示</div>
          <div class="exi">建議不追高，停損 <strong>${s.stop}</strong> 元</div></div>`}
      </div></div>`;
  }
  h+='</div>';
  document.getElementById('body').innerHTML=h;
}

function filter(t,btn){
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  const f=t==='all'?all:all.filter(s=>s.pattern_type===t);
  document.getElementById('fcnt').textContent=`顯示 ${f.length} 支`;
  renderCards(f);
}

async function loadResult(){
  const r=await fetch('/api/result').then(x=>x.json()).catch(()=>null);
  if(!r||!r.stocks||!r.stocks.length){
    document.getElementById('body').innerHTML='<div class="info-b">📡 系統自動掃描中（約30~40分鐘），請稍候...</div>';
    document.getElementById('updTxt').textContent='掃描中...';
    return;
  }
  all=r.stocks||[];
  const upd=r.updated?r.updated.replace('T',' ').slice(0,16):'---';
  document.getElementById('updTxt').textContent=`${upd}｜${r.total_found}支`;
  document.getElementById('filterBar').style.display='flex';
  document.getElementById('fcnt').textContent=`顯示 ${all.length} 支`;
  renderCards(all);
}

function updateProg(){
  fetch('/api/progress').then(r=>r.json()).then(p=>{
    const w=document.getElementById('progWrap');
    if(p.total>0){
      w.classList.add('show');
      document.getElementById('progLbl').textContent=`${p.current}/${p.total}`;
      document.getElementById('progPct').textContent=p.pct+'%';
      document.getElementById('progFill').style.width=p.pct+'%';
      document.getElementById('progSt').textContent=p.status||'';
      document.getElementById('dot').className='dot scanning';
    } else if(p.status&&p.status.includes('完成')){
      document.getElementById('progSt').textContent=p.status;
      document.getElementById('dot').className='dot';
      setTimeout(()=>{w.classList.remove('show');loadResult();},2000);
    }
  }).catch(()=>{});
}

function startPolling(){
  if(pollT) clearInterval(pollT);
  pollT=setInterval(async()=>{
    const s=await fetch('/api/status').then(r=>r.json()).catch(()=>null);
    if(!s) return;
    if(s.scanning){ updateProg(); }
    else{
      clearInterval(pollT); pollT=null;
      document.getElementById('scanBtn').disabled=false;
      document.getElementById('scanBtn').textContent='🔍 全市場掃描';
      document.getElementById('dot').className='dot';
      await loadResult();
    }
  },5000);
}

async function startScan(){
  const btn=document.getElementById('scanBtn');
  btn.disabled=true; btn.textContent='⏳ 掃描中...';
  document.getElementById('dot').className='dot scanning';
  document.getElementById('progWrap').classList.add('show');
  document.getElementById('body').innerHTML='<div class="loading"><div class="spin"></div>全市場掃描中（約30~40分鐘）...</div>';
  await fetch('/api/start_scan').catch(()=>{});
  startPolling();
}

(async()=>{
  const s=await fetch('/api/status').then(r=>r.json()).catch(()=>null);
  if(s&&s.scanning){
    document.getElementById('scanBtn').disabled=true;
    document.getElementById('scanBtn').textContent='⏳ 掃描中...';
    document.getElementById('dot').className='dot scanning';
    document.getElementById('progWrap').classList.add('show');
    startPolling();
  } else { await loadResult(); }
})();
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(HTML)

if __name__ == '__main__':
    threading.Thread(target=auto_scan_on_start, daemon=True).start()
    port = int(os.environ.get('PORT', 5003))
    print('='*50)
    print('  台股選股雷達 雲端版')
    print(f'  http://localhost:{port}')
    print('='*50)
    app.run(host='0.0.0.0', port=port, debug=False)
