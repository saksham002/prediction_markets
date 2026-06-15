"""Self-contained browser replay player for a recorded event.

Builds ONE .html file with the event's full order-by-order stream (book deltas,
trades, snapshots) gzip-embedded, plus a JS player: per-market depth ladders in
YES space, play/pause/step/speed/scrub, trade tape, and a full-game mid chart
with click-to-seek. No simulated orders — pure market replay for ideation.
Open the file in any modern browser (needs DecompressionStream: Chrome 80+/
Firefox 113+/Safari 16.4+).

Usage: book_player.py <recording.jsonl.gz> <event_ticker> [--out PATH]
Module: build_player(recording, event_ticker, out_html)
"""
import argparse
import base64
import gzip
import json
from pathlib import Path


def _scan_event_tickers(recording: Path, event_ticker: str):
    """Ticker list + title for the event from the recording's meta lines."""
    with gzip.open(recording, "rt") as f:
        for line in f:
            if '"meta"' not in line[:40]:
                continue
            meta = json.loads(line)["meta"]
            for p in meta.get("pairs", []):
                if p["event_ticker"] == event_ticker:
                    return [p["first_ticker"], p["second_ticker"]], p.get("title", event_ticker)
            for ev in meta.get("events", []):
                if ev["event_ticker"] == event_ticker:
                    return list(ev["tickers"]), ev.get("title", event_ticker)
    return None, None


def collect_events(recording: Path, event_ticker: str):
    """Compact event stream for one event. Returns (tickers, title, events)
    where events are [type, ts, ticker_idx, ...]:
      ["d", ts, k, side01, price, delta, exoff]   book delta
      ["t", ts, k, side01, yes_price, qty, exoff] trade (taker side)
      ["s", ts, k, yes_levels, no_levels]         snapshot reset
    ts is local epoch seconds; exoff = exchange_ts - ts (seconds, may be null),
    so the player can show both exchange and local order times.
    """
    tickers, title = _scan_event_tickers(recording, event_ticker)
    if not tickers:
        raise SystemExit(f"event {event_ticker} not found in {recording.name} meta")
    tidx = {t: i for i, t in enumerate(tickers)}
    events = []
    with gzip.open(recording, "rt") as f:
        for line in f:
            if '"meta"' in line[:40]:
                continue
            if not any(t in line for t in tickers):
                continue
            rec = json.loads(line)
            data = rec["d"]
            typ = data.get("type")
            msg = data.get("msg", {})
            tkr = msg.get("market_ticker")
            if tkr not in tidx:
                continue
            ts = round(rec["lts"], 3)
            k = tidx[tkr]
            # ts_ms is the exchange match/quote time (epoch ms) on both deltas
            # and trades; store the offset from local so the player can show both
            ex_ms = msg.get("ts_ms")
            exoff = round(float(ex_ms) / 1000.0 - rec["lts"], 3) if ex_ms is not None else None
            if typ == "orderbook_delta":
                side = 0 if msg["side"] == "yes" else 1
                events.append(["d", ts, k, side, float(msg["price_dollars"]),
                               float(msg["delta_fp"]), exoff])
            elif typ == "trade":
                side = 0 if msg["taker_side"] == "yes" else 1
                if "yes_price_dollars" in msg:
                    yp = float(msg["yes_price_dollars"])
                else:
                    yp = round(1.0 - float(msg["no_price_dollars"]), 4)
                events.append(["t", ts, k, side, yp, float(msg["count_fp"]), exoff])
            elif typ == "orderbook_snapshot":
                yes = [[float(p), float(q)] for p, q in msg.get("yes_dollars_fp", [])
                       if float(q) > 1e-9]
                no = [[float(p), float(q)] for p, q in msg.get("no_dollars_fp", [])
                      if float(q) > 1e-9]
                events.append(["s", ts, k, yes, no])
    return tickers, title, events


DEFAULT_ALPHAS = ["agg_30s", "agg_60s", "agg_300s", "tfma_pw_30s", "tfma_pw_300s", "obi"]


def collect_alpha_series(recording: Path, event_ticker: str, tickers: list,
                         alpha_names: list) -> dict:
    """1s-sampled alpha series via StudyConsumer. Returns
    {leg_label: {alpha: [[ts, value], ...]}}. MLB pairs produce one pair-space
    leg; 3-outcome events one leg per market."""
    from replay import Replayer
    from tick_study import StudyConsumer
    replayer = Replayer(recording)
    consumer = StudyConsumer(replayer, 1.0)
    replayer.run(consumer)
    cols = {n: i for i, n in enumerate(consumer.alpha_names)}
    out = {}
    for key, rows in consumer.samples.items():
        if key.split(":")[0] != event_ticker:
            continue
        label = key.split(":")[1] if ":" in key else "pair"
        series = {a: [] for a in alpha_names}
        for lts, _mid, _s1, _s2, alphas in rows:
            for a in alpha_names:
                v = alphas[cols[a]]
                if v is not None:
                    series[a].append([round(lts, 1), round(v, 6)])
        out[label] = series
    return out


def build_player(recording: Path, event_ticker: str, out_html: Path,
                 alphas: list | None = DEFAULT_ALPHAS):
    tickers, title, events = collect_events(recording, event_ticker)
    alpha_series = (collect_alpha_series(recording, event_ticker, tickers, alphas)
                    if alphas else {})
    # Goal / red-card markers from ESPN (cached by espn_clock.py); empty list
    # for non-WC events or games not in the cache.
    from espn_clock import events_for
    markers = sorted(events_for(event_ticker), key = lambda m: m["wc"])
    payload = {"event": event_ticker, "title": title, "tickers": tickers,
               "recording": recording.name, "events": events,
               "alphas": alphas or [], "alpha_series": alpha_series,
               "markers": markers}
    raw = json.dumps(payload, separators = (",", ":")).encode()
    b64 = base64.b64encode(gzip.compress(raw, 6)).decode()
    html = _HTML_TEMPLATE.replace("__DATA_B64__", b64)
    out_html.parent.mkdir(parents = True, exist_ok = True)
    out_html.write_text(html)
    print(f"wrote {out_html} ({out_html.stat().st_size/1e6:.1f} MB, "
          f"{len(events)} events, {len(tickers)} markets)")
    return out_html


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Kalshi book replay</title>
<style>
 body{background:#111;color:#ddd;font-family:Menlo,Consolas,monospace;margin:0;padding:10px}
 #top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:8px}
 button{background:#222;color:#ddd;border:1px solid #555;padding:4px 10px;cursor:pointer;font-family:inherit}
 button:hover{background:#333} .spd.on,.win.on{background:#2a6;color:#000}
 #scrub{flex:1;min-width:200px}
 #main{display:flex;gap:16px;align-items:flex-start}
 .ladder{border:1px solid #333;padding:6px;min-width:230px}
 .ladder h3{margin:2px 0 6px 0;font-size:12px;color:#9cf}
 table{border-collapse:collapse;font-size:11px}
 td{padding:1px 6px;text-align:right;min-width:52px}
 .ask td.px{color:#f66}.bid td.px{color:#6f6}
 .qbar{position:relative} .qbar div{position:absolute;right:0;top:1px;bottom:1px;opacity:.25}
 .ask .qbar div{background:#f66}.bid .qbar div{background:#6f6}
 .best{background:#1d2d1d}
 #tape{border:1px solid #333;padding:6px;font-size:11px;min-width:240px;max-height:560px;overflow:hidden}
 #tape h3{margin:2px 0 6px 0;font-size:12px;color:#9cf}
 .tb{color:#6f6}.ts{color:#f66}
 #chartwrap{margin-top:10px}
 canvas{border:1px solid #333;display:block;width:100%}
 #clock{font-size:14px;color:#fc6;min-width:230px}
 #counter{color:#888;font-size:11px}
 #gameevent{font-weight:bold;padding:2px 8px;border-radius:3px;font-size:13px}
 #gameevent.ev-goal{background:#fd0;color:#000}
 #gameevent.ev-red{background:#d22;color:#fff}
 #ordertime{font-size:12px;color:#9cf;background:#123;padding:2px 8px;border-radius:3px}
 @keyframes flashbg{0%{filter:brightness(2.6)}100%{filter:brightness(1)}}
 .flash{animation:flashbg .7s ease-out}
</style></head><body>
<div id="top">
 <b id="title">loading…</b>
 <button id="play">&#9654;</button>
 <button id="stepb">&minus;1</button><button id="stepf">+1</button>
 <span>
  <button class="spd" data-s="0.01">0.01x</button><button class="spd" data-s="0.1">0.1x</button>
  <button class="spd" data-s="1">1x</button><button class="spd" data-s="10">10x</button>
  <button class="spd on" data-s="60">60x</button><button class="spd" data-s="600">600x</button>
  <button class="spd" data-s="0">max</button>
 </span>
 <span id="clock"></span><span id="counter"></span>
 <span id="gameevent"></span>
 <span id="ordertime" style="display:none"></span>
 <input type="range" id="scrub" min="0" max="1000000" value="0">
</div>
<div id="main"><div id="ladders" style="display:flex;gap:16px"></div>
 <div id="tape"><h3>trade tape</h3><div id="tapebody"></div></div>
 <div id="livecharts" style="flex:1;min-width:380px">
  <div style="font-size:11px;margin-bottom:4px">window:
   <button class="win" data-w="120">2m</button><button class="win on" data-w="600">10m</button>
   <button class="win" data-w="1800">30m</button><button class="win" data-w="7200">2h</button>
  </div>
  <div style="font-size:11px;color:#9cf">odds (window-scaled)</div>
  <canvas id="wodds" height="200"></canvas>
  <div style="font-size:11px;color:#9cf;margin-top:4px">alphas — <span id="wleg"></span></div>
  <canvas id="walpha" height="200"></canvas>
 </div></div>
<div id="chartwrap"><canvas id="chart" height="240"></canvas></div>
<div id="alphawrap" style="display:none;margin-top:8px">
 <div id="alphactl" style="font-size:11px;margin-bottom:4px"></div>
 <canvas id="achart" height="170"></canvas>
 <div id="aread" style="font-size:11px;color:#9cf;margin-top:4px"></div>
</div>
<script id="data" type="text/plain">__DATA_B64__</script>
<script>
"use strict";
let D=null, EV=[], NT=0, idx=0, playing=false, speed=60, books=[], keyframes=[], KF=50000;
let chartPts=[], trades=[], lastDraw=0;

async function load(){
  const b64=document.getElementById('data').textContent.trim();
  const bytes=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  const ds=new DecompressionStream('gzip');
  const stream=new Blob([bytes]).stream().pipeThrough(ds);
  const text=await new Response(stream).text();
  D=JSON.parse(text); EV=D.events; NT=D.tickers.length;
  document.getElementById('title').textContent=D.title+'  ['+D.event+']  '+EV.length.toLocaleString()+' events  ('+D.recording+')';
  buildLadders(); buildKeyframes(); buildAlphaUI(); seek(0); draw(); requestAnimationFrame(loop);
}

let selLeg=null, selAlphas=new Set();
function buildAlphaUI(){
  const legs=Object.keys(D.alpha_series||{});
  if(!legs.length||!D.alphas.length) return;
  document.getElementById('alphawrap').style.display='block';
  selLeg=legs[0]; selAlphas=new Set(['agg_300s','obi'].filter(a=>D.alphas.includes(a)));
  if(!selAlphas.size) selAlphas.add(D.alphas[0]);
  const ctl=document.getElementById('alphactl');
  let h='leg: <select id="legsel">'+legs.map(l=>'<option>'+l+'</option>').join('')+'</select>  ';
  for(const a of D.alphas)
    h+='<label style="margin-right:10px"><input type="checkbox" data-a="'+a+'" '
      +(selAlphas.has(a)?'checked':'')+'> '+a+'</label>';
  ctl.innerHTML=h;
  document.getElementById('legsel').onchange=e=>{selLeg=e.target.value;drawAlpha();renderAlphaRead();};
  ctl.querySelectorAll('input[type=checkbox]').forEach(cb=>cb.onchange=()=>{
    cb.checked?selAlphas.add(cb.dataset.a):selAlphas.delete(cb.dataset.a);drawAlpha();});
}
const ACOLORS={agg_30s:'#6cf',agg_60s:'#39f',agg_300s:'#fc6',tfma_pw_30s:'#f6c',tfma_pw_300s:'#c6f',obi:'#6f6'};
function drawAlpha(cursorTs){
  const wrap=document.getElementById('alphawrap');
  if(wrap.style.display==='none')return;
  const cv=document.getElementById('achart'); const W=cv.clientWidth; cv.width=W;
  const H=170, c=cv.getContext('2d');
  c.fillStyle='#161616'; c.fillRect(0,0,W,H);
  const X=ts=>(ts-t0)/(t1-t0)*W;
  let li=12;
  for(const a of selAlphas){
    const s=(D.alpha_series[selLeg]||{})[a]||[]; if(!s.length)continue;
    let lo=Infinity,hi=-Infinity;
    for(const [,v] of s){if(v<lo)lo=v;if(v>hi)hi=v;}
    if(hi<=lo){hi=lo+1;}
    const Y=v=>H-8-(v-lo)/(hi-lo)*(H-16);
    c.strokeStyle=ACOLORS[a]||'#fff'; c.lineWidth=1; c.beginPath();
    for(let i=0;i<s.length;i++){const x=X(s[i][0]),y=Y(s[i][1]);
      i?c.lineTo(x,y):c.moveTo(x,y);}
    c.stroke();
    if(lo<0&&hi>0){c.strokeStyle='#444';c.beginPath();c.moveTo(0,Y(0));c.lineTo(W,Y(0));c.stroke();}
    c.fillStyle=ACOLORS[a]||'#fff'; c.fillText(a+' (norm)',8,li); li+=12;
  }
  drawMarkers(c,X,0,H,W);
  if(cursorTs!==undefined){c.strokeStyle='#fff';c.beginPath();
    c.moveTo(X(cursorTs),0);c.lineTo(X(cursorTs),H);c.stroke();}
  cv.onclick=ev=>{const ts=t0+(ev.offsetX/W)*(t1-t0);
    let lo=0,hi=EV.length-1;while(lo<hi){const m=(lo+hi)>>1;if(EV[m][1]<ts)lo=m+1;else hi=m;}
    seek(lo);};
}
let winLen=600;
function sliceWin(series, t0w, t1w){
  let lo=0,hi=series.length;
  while(lo<hi){const m=(lo+hi)>>1; if(series[m][0]<t0w)lo=m+1;else hi=m;}
  let lo2=lo,hi2=series.length;
  while(lo2<hi2){const m=(lo2+hi2)>>1; if(series[m][0]<=t1w)lo2=m+1;else hi2=m;}
  return series.slice(Math.max(0,lo-1), lo2);
}
function renderLive(){
  const now=EV[Math.max(0,idx-1)][1], t0w=now-winLen, t1w=now;
  // odds window: chartPts rows [ts, mid0, mid1, ...] + trades ticks
  const ocv=document.getElementById('wodds'); const W=ocv.clientWidth; ocv.width=W;
  const H=200, c=ocv.getContext('2d');
  c.fillStyle='#161616'; c.fillRect(0,0,W,H);
  const pts=sliceWin(chartPts,t0w,t1w);
  let lo=Infinity,hi=-Infinity;
  for(const row of pts) for(let k=0;k<NT;k++){const v=row[k+1];
    if(v!==null){if(v<lo)lo=v;if(v>hi)hi=v;}}
  if(hi<=lo){lo-=0.01;hi+=0.01;} const pad=(hi-lo)*0.08; lo-=pad; hi+=pad;
  const X=ts=>(ts-t0w)/(t1w-t0w)*W, Y=v=>H-6-(v-lo)/(hi-lo)*(H-12);
  for(const t of sliceWin(trades,t0w,t1w)){
    c.fillStyle=t[3]===0?'#4f4':'#f44'; c.globalAlpha=.5;
    c.fillRect(X(t[1])-1,Y(t[4])-1,3,3);}
  c.globalAlpha=1;
  for(let k=0;k<NT;k++){
    c.strokeStyle=COLORS[k%4]; c.lineWidth=1.2; c.beginPath(); let st=false; let lastv=null;
    for(const row of pts){const v=row[k+1]; if(v===null)continue; lastv=v;
      const x=X(row[0]),y=Y(v); st?c.lineTo(x,y):c.moveTo(x,y); st=true;}
    c.stroke();
    if(lastv!==null){c.fillStyle=COLORS[k%4];
      c.fillText(D.tickers[k].split('-').pop()+' '+lastv.toFixed(3),W-110,14+12*k);}
  }
  c.fillStyle='#888'; c.fillText((hi-pad).toFixed(2),4,12); c.fillText((lo+pad).toFixed(2),4,H-4);
  drawMarkers(c,X,0,H,W);
  // alpha window for selected leg
  const acv=document.getElementById('walpha'); acv.width=W;
  const ac=acv.getContext('2d');
  ac.fillStyle='#161616'; ac.fillRect(0,0,W,H);
  document.getElementById('wleg').textContent=selLeg||'';
  if(selLeg){
    let li=12;
    for(const a of selAlphas){
      const s=sliceWin((D.alpha_series[selLeg]||{})[a]||[],t0w,t1w);
      if(!s.length)continue;
      let alo=Infinity,ahi=-Infinity;
      for(const [,v] of s){if(v<alo)alo=v;if(v>ahi)ahi=v;}
      if(ahi<=alo)ahi=alo+1;
      const AY=v=>H-6-(v-alo)/(ahi-alo)*(H-12);
      ac.strokeStyle=ACOLORS[a]||'#fff'; ac.lineWidth=1.1; ac.beginPath();
      for(let i=0;i<s.length;i++){const x=X(s[i][0]),y=AY(s[i][1]);
        i?ac.lineTo(x,y):ac.moveTo(x,y);}
      ac.stroke();
      if((alo<0&&ahi>0)){ac.strokeStyle='#3a3a3a';ac.beginPath();
        ac.moveTo(0,AY(0));ac.lineTo(W,AY(0));ac.stroke();}
      const lastv=s[s.length-1][1];
      ac.fillStyle=ACOLORS[a]||'#fff';
      ac.fillText(a+' '+(Math.abs(lastv)>=100?lastv.toFixed(0):lastv.toFixed(3)),8,li); li+=12;
    }
  }
}
function renderAlphaRead(){
  if(!selLeg)return;
  const now=EV[Math.max(0,idx-1)][1];
  let h='@cursor  ';
  for(const a of D.alphas){
    const s=(D.alpha_series[selLeg]||{})[a]||[];
    let lo=0,hi=s.length;
    while(lo<hi){const m=(lo+hi)>>1; if(s[m][0]<=now)lo=m+1;else hi=m;}
    const v=lo>0?s[lo-1][1]:null;
    h+=a+': '+(v===null?'—':(Math.abs(v)>=100?v.toFixed(0):v.toFixed(4)))+'   ';
  }
  document.getElementById('aread').textContent=h;
}

function newBook(){return {yes:new Map(), no:new Map()};}
function cloneBook(b){return {yes:new Map(b.yes), no:new Map(b.no)};}
function applyEvent(e, record){
  const k=e[2], b=books[k];
  if(e[0]==='d'){ const side=e[3]===0?b.yes:b.no; const q=(side.get(e[4])||0)+e[5];
    if(q<=1e-6) side.delete(e[4]); else side.set(e[4],q); }
  else if(e[0]==='s'){ b.yes=new Map(e[3]); b.no=new Map(e[4]); }
  else if(e[0]==='t' && record) trades.push(e);
}
function bestBid(m){let b=-1;for(const p of m.keys()) if(p>b)b=p;return b<0?null:b;}
function midOf(k){const yb=bestBid(books[k].yes), nb=bestBid(books[k].no);
  if(yb===null||nb===null) return null; return (yb+(1-nb))/2;}

function buildKeyframes(){
  books=Array.from({length:NT},newBook); trades=[]; chartPts=[];
  for(let i=0;i<EV.length;i++){
    if(i%KF===0) keyframes.push({i:i, books:books.map(cloneBook), nTrades:trades.length});
    applyEvent(EV[i], true);
    if(i%200===0||EV[i][0]==='t'){
      const row=[EV[i][1]]; for(let k=0;k<NT;k++) row.push(midOf(k)); chartPts.push(row);
    }
  }
}
function seek(target){
  let kf=keyframes[0];
  for(const f of keyframes){ if(f.i<=target) kf=f; else break; }
  books=kf.books.map(cloneBook); idx=kf.i;
  while(idx<target){ applyEvent(EV[idx],false); idx++; }
  // Re-anchor the play clock to the sought position. Without this a scrub
  // (a rewind especially) leaves lastSim ahead of idx, so the next loop()
  // frame's simTarget=lastSim+dt*speed fast-forwards right back, making the
  // slider feel like it can't rewind while playing.
  lastSim=EV[Math.max(0,idx-1)][1]; lastReal=performance.now();
  render();
}
function step(n){ const t=Math.max(0,Math.min(EV.length-1,idx+n)); if(t<idx) seek(t);
  else { while(idx<t){applyEvent(EV[idx],false);idx++;} } render(); }

function buildLadders(){
  const host=document.getElementById('ladders');
  for(let k=0;k<NT;k++){
    const div=document.createElement('div'); div.className='ladder';
    div.innerHTML='<h3>'+D.tickers[k]+'</h3><table id="lt'+k+'"></table>';
    host.appendChild(div);
  }
}
function renderLadder(k){
  const b=books[k];
  const asks=[...b.no.entries()].map(([p,q])=>[+(1-p).toFixed(4),q]).sort((a,c)=>c[0]-a[0]);
  const bids=[...b.yes.entries()].sort((a,c)=>c[0]-a[0]);
  const maxq=Math.max(1,...asks.slice(-12).map(x=>x[1]),...bids.slice(0,12).map(x=>x[1]));
  let h='';
  for(const [p,q] of asks.slice(-12))
    h+='<tr class="ask"><td class="px">'+p.toFixed(2)+'</td><td class="qbar"><div style="width:'+Math.min(100,q/maxq*100)+'%"></div>'+Math.round(q)+'</td></tr>';
  const yb=bestBid(b.yes), nb=bestBid(b.no);
  const sp=(yb!==null&&nb!==null)?((1-nb)-yb).toFixed(2):'—';
  h+='<tr><td colspan="2" style="color:#fc6;text-align:center">spread '+sp+'</td></tr>';
  for(const [p,q] of bids.slice(0,12))
    h+='<tr class="bid"><td class="px">'+p.toFixed(2)+'</td><td class="qbar"><div style="width:'+Math.min(100,q/maxq*100)+'%"></div>'+Math.round(q)+'</td></tr>';
  document.getElementById('lt'+k).innerHTML=h;
}
function renderTape(){
  const now=EV[Math.max(0,idx-1)][1]; let lo=0,hi=trades.length;
  while(lo<hi){const m=(lo+hi)>>1; if(trades[m][1]<=now)lo=m+1;else hi=m;}
  let h='';
  for(let i=lo-1;i>=Math.max(0,lo-22);i--){const t=trades[i];
    h+='<div class="'+(t[3]===0?'tb':'ts')+'">'+fmtT(t[1])+'  '+D.tickers[t[2]].split('-').pop()
      +' '+(t[3]===0?'BUY ':'SELL')+' '+Math.round(t[5])+' @ '+t[4].toFixed(2)+'</div>';}
  document.getElementById('tapebody').innerHTML=h;
}
function fmtT(ts){const d=new Date(ts*1000);
  return d.toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'});}
function fmtFull(ts){let ms=Math.round((ts-Math.floor(ts))*1000); if(ms>999)ms=999;
  return fmtT(ts)+'.'+String(ms).padStart(3,'0')+' ET';}
function drawMarkers(c,X,top,bot,W){
  for(const mk of (D.markers||[])){
    const x=X(mk.wc); if(x<-1||x>W+1)continue;
    const red=mk.kind==='red';
    c.strokeStyle=red?'#f44':'#fd0'; c.lineWidth=1; c.setLineDash([4,3]);
    c.beginPath(); c.moveTo(x,top); c.lineTo(x,bot); c.stroke(); c.setLineDash([]);
    c.fillStyle=red?'#f88':'#fe5'; c.fillText((red?'R:':'G:')+mk.team+mk.min,x+2,top+9);
  }
}
let _lastMk=null;
function updateGameEvent(now){
  const el=document.getElementById('gameevent');
  let last=null;
  for(const mk of (D.markers||[])){ if(mk.wc<=now) last=mk; else break; }
  if(!last){ if(_lastMk!==null){el.textContent='';el.className='';_lastMk=null;} return; }
  const red=last.kind==='red';
  el.textContent=(red?'RED CARD ':'GOAL ')+last.team+' '+last.min+' (+'+Math.round(now-last.wc)+'s)';
  if(last!==_lastMk){ _lastMk=last; el.className=(red?'ev-red':'ev-goal')+' flash'; }
}
function updateOrderTime(e){
  const el=document.getElementById('ordertime');
  if(!(speed>0 && speed<=0.1)){ el.style.display='none'; return; }
  el.style.display='inline';
  const kind=e[0]==='d'?'ORDER':(e[0]==='t'?'TRADE':'SNAP');
  const exoff=e[6];
  const exStr=(exoff===undefined||exoff===null)?'—':fmtFull(e[1]+exoff);
  el.textContent=kind+'  local '+fmtFull(e[1])+'  |  exch '+exStr;
}
function render(){
  for(let k=0;k<NT;k++) renderLadder(k);
  renderTape();
  renderLive();
  const e=EV[Math.max(0,idx-1)];
  document.getElementById('clock').textContent=fmtT(e[1])+' ET';
  updateGameEvent(e[1]); updateOrderTime(e);
  document.getElementById('counter').textContent='event '+idx.toLocaleString()+' / '+EV.length.toLocaleString();
  document.getElementById('scrub').value=Math.round(idx/EV.length*1000000);
  drawCursor();
}

const COLORS=['#6cf','#fc6','#f6c','#6f6'];
let chartCtx=null, chartW=0, chartH=240, t0=0, t1=1;
function draw(){
  const cv=document.getElementById('chart'); chartW=cv.clientWidth; cv.width=chartW;
  chartCtx=cv.getContext('2d'); const c=chartCtx;
  c.fillStyle='#161616'; c.fillRect(0,0,chartW,chartH);
  t0=EV[0][1]; t1=EV[EV.length-1][1];
  const X=ts=>(ts-t0)/(t1-t0)*chartW, Y=v=>chartH-6-v*(chartH-12);
  for(let k=0;k<NT;k++){
    c.strokeStyle=COLORS[k%4]; c.lineWidth=1; c.beginPath(); let started=false;
    for(const row of chartPts){ const v=row[k+1]; if(v===null)continue;
      const x=X(row[0]),y=Y(v); if(!started){c.moveTo(x,y);started=true;} else c.lineTo(x,y);}
    c.stroke();
  }
  c.fillStyle='#fff';
  for(const t of trades){ c.globalAlpha=.35; c.fillStyle=t[3]===0?'#4f4':'#f44';
    c.fillRect(X(t[1]),Y(t[4])-1,2,2);}
  c.globalAlpha=1;
  for(let k=0;k<NT;k++){c.fillStyle=COLORS[k%4];
    c.fillText(D.tickers[k].split('-').pop(),8,14+12*k);}
  drawMarkers(c,X,0,chartH,chartW);
  cv.onclick=ev=>{const frac=(ev.offsetX/chartW); const ts=t0+frac*(t1-t0);
    let lo=0,hi=EV.length-1; while(lo<hi){const m=(lo+hi)>>1; if(EV[m][1]<ts)lo=m+1;else hi=m;}
    seek(lo);};
  drawCursor();
}
let cursorCv=null;
function drawCursor(){
  if(!chartCtx)return;
  const e=EV[Math.max(0,idx-1)];
  if(!cursorCv){cursorCv=document.createElement('canvas');}
  draw2();
  function draw2(){ /* redraw base rarely is expensive; draw cursor as overlay line */ }
  // lightweight: redraw a 3px column region is complex; full redraw throttled:
  const now=performance.now();
  if(now-lastDraw>100){ lastDraw=now; redrawWithCursor(e[1]); drawAlpha(e[1]); renderAlphaRead(); }
}
function redrawWithCursor(ts){
  draw0(); const c=chartCtx, x=(ts-t0)/(t1-t0)*chartW;
  c.strokeStyle='#fff'; c.lineWidth=1; c.beginPath(); c.moveTo(x,0); c.lineTo(x,chartH); c.stroke();
}
function draw0(){
  const c=chartCtx; c.fillStyle='#161616'; c.fillRect(0,0,chartW,chartH);
  const X=ts=>(ts-t0)/(t1-t0)*chartW, Y=v=>chartH-6-v*(chartH-12);
  for(let k=0;k<NT;k++){
    c.strokeStyle=COLORS[k%4]; c.lineWidth=1; c.beginPath(); let started=false;
    for(const row of chartPts){ const v=row[k+1]; if(v===null)continue;
      const x=X(row[0]),y=Y(v); if(!started){c.moveTo(x,y);started=true;} else c.lineTo(x,y);}
    c.stroke();}
  for(const t of trades){ c.globalAlpha=.35; c.fillStyle=t[3]===0?'#4f4':'#f44';
    c.fillRect(X(t[1]),Y(t[4])-1,2,2);}
  c.globalAlpha=1;
  for(let k=0;k<NT;k++){c.fillStyle=COLORS[k%4];
    c.fillText(D.tickers[k].split('-').pop(),8,14+12*k);}
  drawMarkers(c,X,0,chartH,chartW);
}

let lastReal=0, lastSim=0;
function loop(now){
  if(playing && idx<EV.length){
    if(speed===0){ step(Math.min(5000, EV.length-idx)); }
    else{
      const dtReal=(now-lastReal)/1000; lastReal=now;
      let simTarget=lastSim+dtReal*speed;
      let n=0;
      while(idx<EV.length && EV[idx][1]<=simTarget && n<20000){applyEvent(EV[idx],false);idx++;n++;}
      lastSim=idx<EV.length?Math.max(simTarget,EV[Math.max(0,idx-1)][1]):simTarget;
      render();
    }
    if(idx>=EV.length){playing=false;document.getElementById('play').innerHTML='&#9654;';}
  } else { lastReal=now; if(idx>0&&EV.length) lastSim=EV[Math.max(0,idx-1)][1]; }
  requestAnimationFrame(loop);
}
document.getElementById('play').onclick=()=>{playing=!playing;
  document.getElementById('play').innerHTML=playing?'&#10074;&#10074;':'&#9654;';};
document.getElementById('stepf').onclick=()=>step(1);
document.getElementById('stepb').onclick=()=>step(-1);
document.querySelectorAll('.spd').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.spd').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); speed=+b.dataset.s; if(D)render();});
document.querySelectorAll('.win').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.win').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); winLen=+b.dataset.w; renderLive();});
document.getElementById('scrub').oninput=ev=>{
  seek(Math.round(+ev.target.value/1000000*(EV.length-1)));};
document.addEventListener('keydown',ev=>{
  if(ev.key===' '){ev.preventDefault();document.getElementById('play').click();}
  if(ev.key==='ArrowRight')step(ev.shiftKey?1000:1);
  if(ev.key==='ArrowLeft')step(ev.shiftKey?-1000:-1);});
window.addEventListener('resize',()=>{draw();});
load();
</script></body></html>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording")
    parser.add_argument("event_ticker")
    parser.add_argument("--out", default = None)
    args = parser.parse_args()
    rec = Path(args.recording)
    out = Path(args.out) if args.out else Path(f"player_{args.event_ticker}.html")
    build_player(rec, args.event_ticker, out)


if __name__ == "__main__":
    main()
