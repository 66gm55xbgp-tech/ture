// Ture — self-hosted trading console
// API base is dynamic: saved server URL (localStorage 'gb.server.url') is health-tested
// on load. When the dashboard is served BY the trading server itself, origin is the
// default. When hosted elsewhere (Vercel), the user must set/test the server once.
const SERVER_URL_KEY='gb.server.url';
let API=(function(){
  var saved=lsGet2(SERVER_URL_KEY);
  if(saved)return saved;
  // Default: same origin (works when server.py serves this dashboard).
  // If the page is served from a known static host (Vercel), origin is NOT the API.
  var o=(window.location.protocol+'//'+window.location.host);
  if(/\.vercel\.app$/.test(window.location.host)||/ai-signal\.live$/.test(window.location.host))return '';
  return o;
})();
function lsGet2(k){try{return localStorage.getItem(k)||''}catch(e){return ''}}
// Probe server: any HTTP response (even 401/403) proves the trading server is alive.
async function testServer(url,timeoutMs){
  try{
    var ctl=new AbortController();
    var to=setTimeout(function(){ctl.abort()},timeoutMs||5000);
    var r=await fetch(url.replace(/\/+$/,'')+'/health',{method:'GET',signal:ctl.signal});
    clearTimeout(to);
    return r.status>0; // reached an HTTP server — good enough
  }catch(e){return false}
}
window.saveServerUrl=function(){return lsGet2(SERVER_URL_KEY)};
window.showServerModal=function(msg){
  var o=document.getElementById('serverOverlay');if(!o)return;
  o.classList.remove('hidden');
  var m=document.getElementById('serverModalMsg');if(m)m.textContent=msg||'';
  var inp=document.getElementById('serverUrlInput');if(inp&&!inp.value)inp.value=lsGet2(SERVER_URL_KEY)||'https://';
  if(inp)inp.focus();
};
window.hideServerModal=function(){var o=document.getElementById('serverOverlay');if(o)o.classList.add('hidden')};
window.testServerUrl=async function(){
  var inp=document.getElementById('serverUrlInput');
  var st=document.getElementById('serverUrlStatus');
  var btn=document.getElementById('serverTestBtn');
  if(!inp)return;
  var url=(inp.value||'').trim().replace(/\/+$/,'');
  if(!/^https?:\/\//.test(url)){if(st){st.textContent='URL must start with http:// or https://';st.style.color='var(--r)'}return}
  if(btn){btn.disabled=true;btn.textContent='Testing…'}
  if(st){st.textContent='Connecting to '+url+' …';st.style.color='var(--t2)'}
  var ok=await testServer(url,6000);
  if(btn){btn.disabled=false;btn.textContent='Test connection'}
  if(ok){
    try{localStorage.setItem(SERVER_URL_KEY,url)}catch(e){}
    API=url;
    if(st){st.textContent='✅ Connected';st.style.color='var(--g)'}
    setTimeout(function(){
      window.hideServerModal();
      location.reload();
    },700);
  }else{
    if(st){st.textContent='❌ No trading server responded at '+url;st.style.color='var(--r)'}
  }
};
window.changeServer=function(){
  try{localStorage.removeItem(SERVER_URL_KEY)}catch(e){}
  API='';window.showServerModal('Enter your trading server address');
};
const ADMIN_TOKEN_KEY='gb.dashboard.admin-token';
const JWT_KEY='gb.dashboard.jwt';
const USER_KEY='gb.dashboard.user';
const PLAT_KEY='gb.dashboard.platform';
const DESK_KEY='gb.dashboard.desk';
const E={XAUUSDT:'🥇',XAUINR:'🥇',XAUUSD:'🥇',GOLD:'🥇',BTCUSDT:'₿',BTCINR:'₿',ETHUSDT:'💎',ETHINR:'💎',SOLUSDT:'🔮',SOLINR:'🔮',INJUSDT:'💉',DEXEUSDT:'🔥',BNBUSDT:'🟡',DOGEUSDT:'🐕',BONKUSDT:'🐶',PEPEUSDT:'🐸',NIFTY:'📊',BANKNIFTY:'🏦',SILVER:'🔘',CRUDEOIL:'🛢️'};
const PLATS={binance:{label:'Binance',sub:'USDT-M futures'},mt5:{label:'MT5',sub:'EA bridge'}};
function validDesk(p){return p&&PLATS[p]?p:''}
function readDesk(){
  try{
    var q=new URLSearchParams(location.search||'').get('desk');
    if(validDesk(q))return q;
  }catch(e){}
  try{var s=sessionStorage.getItem(DESK_KEY);if(validDesk(s))return s}catch(e){}
  try{var l=localStorage.getItem(PLAT_KEY);if(validDesk(l))return l}catch(e){}
  return 'binance';
}
function writeDesk(p){
  p=validDesk(p);if(!p)return;
  try{sessionStorage.setItem(DESK_KEY,p)}catch(e){}
  try{localStorage.setItem(PLAT_KEY,p)}catch(e){}
  try{
    var u=new URL(location.href);
    if(u.searchParams.get('desk')!==p){
      u.searchParams.set('desk',p);
      history.replaceState(null,'',u.pathname+u.search+u.hash)
    }
  }catch(e){}
}
function ssGet(k){try{return sessionStorage.getItem(k)}catch(e){return null}}
function ssSet(k,v){try{sessionStorage.setItem(k,v)}catch(e){}}
function lsGet(k){try{return localStorage.getItem(k)}catch(e){return null}}
function lsSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function readJwt(){return ssGet(JWT_KEY)||''}
function readUser(){return ssGet(USER_KEY)||''}
function seedTabAuth(){
  // New tab with no session inherits the last login as a starting point.
  // Once seeded, this tab's JWT stays independent (other tab can log in as someone else).
  if(ssGet(JWT_KEY))return;
  var j=lsGet(JWT_KEY);if(!j)return;
  ssSet(JWT_KEY,j);
  var u=lsGet(USER_KEY);if(u)ssSet(USER_KEY,u)
}
function writeTabAuth(token,email){
  ssSet(JWT_KEY,token||'');
  if(email)ssSet(USER_KEY,email);
  lsSet(JWT_KEY,token||'');
  if(email)lsSet(USER_KEY,email)
}
function clearTabAuth(){
  var mine=ssGet(JWT_KEY);
  try{sessionStorage.removeItem(JWT_KEY);sessionStorage.removeItem(USER_KEY)}catch(e){}
  try{
    if(mine&&localStorage.getItem(JWT_KEY)===mine){
      localStorage.removeItem(JWT_KEY);localStorage.removeItem(USER_KEY)
    }
  }catch(e){}
}
seedTabAuth();
function botsCacheKey(plat){
  return 'gb.dashboard.bots.'+(readUser()||'-')+'.'+(plat||activeTab||'binance')
}
let expandedBot=null,recognition=null;
let activeTab=readDesk();
let bots=(function(){
  try{return JSON.parse(sessionStorage.getItem(botsCacheKey(activeTab))||'[]')||[]}
  catch(e){return []}
})();
let _loginPlat=activeTab;
var _symbols=[],_selectedSymbol='XAUUSDT';
function platformGold(){
  if(activeTab==='mt5')return 'XAUUSD';
  return 'XAUUSDT'
}
function applyGoldChartDefault(){
  var g=platformGold();
  _chartFocusId='';
  _chartFocusSym=g;
  if(activeTab==='binance'){
    _selectedSymbol='XAUUSDT';
    var ss=$('ss');if(ss)ss.value='XAUUSDT'
  }else if(activeTab==='mt5'){
    var t=$('mt5Sym');if(t&&!(t.value||'').trim())t.value='XAUUSD'
  }
  var label=$('chartSym');if(label)label.textContent=g
}
const $=id=>document.getElementById(id);
function toast(msg,t){var el=document.createElement('div');el.className='toast '+(t||'success');el.textContent=msg;document.body.appendChild(el);setTimeout(function(){el.remove()},3500)}
function adminHeaders(){var h={};var jwt=readJwt();if(jwt)h['Authorization']='Bearer '+jwt;var t=lsGet(ADMIN_TOKEN_KEY);if(t)h['X-GB-Admin-Token']=t;return h}
function isLoggedIn(){return !!readJwt()}
function configureDashboardAccess(){var t=prompt('Enter the GB dashboard access token');if(t){localStorage.setItem(ADMIN_TOKEN_KEY,t.trim());location.reload()}}
async function api(u,b,m){
  if(!API){window.showServerModal('No trading server configured');return null}
  try{var o={method:m||(b?'POST':'GET'),headers:Object.assign({'Content-Type':'application/json'},adminHeaders())};if(b)o.body=JSON.stringify(b);var ctl=new AbortController();var to=setTimeout(function(){ctl.abort()},45000);var r=await fetch(API+u,Object.assign(o,{signal:ctl.signal}));clearTimeout(to);var t=await r.text();var body=null;try{body=t?JSON.parse(t):{}}catch(e){body={detail:t}}
  if(r.status===401){
    var det=String((body&&body.detail)||'').toLowerCase();
    var sessionDead=!readJwt()||det.indexOf('invalid or expired')>=0||det.indexOf('dashboard authorization required')>=0;
    if(sessionDead){
      clearTabAuth();
      toast('Session expired — please log in','error');
      showLogin()
    }
    return null
  }
  return body
}catch(e){return null}}

// ── Hardware capacity (memory) guard ─────────────────────────────────────
// Polls the server's memory usage; if the critical threshold is hit we warn
// the operator and disable spawning (the server also hard-blocks spawns).
var _memCritical=false;
function _spawnButtons(){
  return [].slice.call(document.querySelectorAll('.btn-primary,#guruBtn'))
}
function updateSpawnButtons(){
  var btns=_spawnButtons();
  btns.forEach(function(b){
    if(_memCritical){b.disabled=true;b.style.opacity='.5';b.title='Critical hardware limit — expand the server to continue'}
    else{b.disabled=false;b.style.opacity='';b.title=''}
  });
}
async function checkResources(){
  var d=await api('/api/system/resources');
  if(!d||!d.memory)return;
  var mem=d.memory,was=_memCritical;
  _memCritical=!!mem.critical && !mem.mem_guard_disabled;
  if(d.fleet&&d.fleet.guru_max){
    _guruMax=d.fleet.guru_max;
    _guruN=Math.max(1,Math.min(_guruMax,_guruN));
    var el=$('guruNVal');if(el)el.textContent=_guruN
    }
  updateSpawnButtons();
  if(_memCritical&&!was){
    toast('⚠️ Critical hardware limit ('+mem.used_pct+'% RAM) — expand server to continue','error')
  }else if(!_memCritical&&was){
    toast('✅ Memory pressure cleared','success')
  }else if(d.fleet&&d.fleet.remaining===0&&d.can_spawn===false&&!mem.critical){
    var b=$('guruBtn');
    if(b){b.title='Fleet cap is '+d.fleet.max+' bots — stop one first'}
  }
}

// ── Login (Supabase Auth via backend) ─────────────────────────────────────
window.showLogin=function(){
  var so=$('settingsOverlay');if(so)so.classList.add('hidden');
  var o=$('loginOverlay');if(!o)return;
  o.classList.remove('hidden');
  pickLoginPlat(_loginPlat||readDesk());
};
window.hideLogin=function(){var o=$('loginOverlay');if(o)o.classList.add('hidden')};
window.doLogin=async function(){
  var email=($('loginEmail')||{}).value, pass=($('loginPassword')||{}).value;
  if(!email||!pass){toast('Enter email and password','error');return}
  var sel=$('loginPlatSelect');
  var desk=(sel&&sel.value)||_loginPlat||readDesk();
  if(!PLATS[desk])desk='binance';
  pickLoginPlat(desk);
  var btn=$('loginBtn');if(btn){btn.disabled=true;btn.textContent='Signing in…'}
  try{
    var r=await fetch(API+'/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email.trim(),password:pass})});
    var d=await r.json();
    if(r.ok&&d.access_token){
      writeTabAuth(d.access_token,(d.user&&d.user.email)||email.trim());
      var lu=$('loggedInUser');if(lu)lu.textContent='👤 '+((d.user&&d.user.email)||email.trim());
      hideLogin();
      toast('✅ '+((d.user&&d.user.email)||'')+' · '+(PLATS[desk]||PLATS.binance).label);
      try{applyWorkspace(desk)}catch(e){console.warn('applyWorkspace',e);applyWorkspace('binance')}
      refresh();
    }else{
      toast('❌ '+(d.detail||'Invalid email or password'),'error')
    }
  }catch(e){toast('❌ Login failed','error')}
  if(btn){btn.disabled=false;btn.textContent='Sign in'}
};
window.doLogout=function(){
  clearTabAuth();
  var lu=$('loggedInUser');if(lu)lu.textContent='';
  var so=$('settingsOverlay');if(so)so.classList.add('hidden');
  showLogin();toast('Signed out of this tab')
};
window.pickLoginPlat=function(p){
  if(!PLATS[p])return;
  _loginPlat=p;
  try{sessionStorage.setItem(DESK_KEY,p)}catch(e){}
  document.querySelectorAll('#loginPlatPick .plat-card').forEach(function(el){
    var on=(el.getAttribute('data-plat')||'')===p;
    el.classList.toggle('sel',on);
    el.setAttribute('aria-pressed',on?'true':'false')
  });
  var sel=$('loginPlatSelect');
  if(sel&&sel.value!==p)sel.value=p;
  var hint=$('loginPlatHint');
  if(hint)hint.textContent='Selected: '+PLATS[p].label+' — sign in to open this desk';
};
window.applyWorkspace=function(t){
  t=t||readDesk();
  if(!PLATS[t])t='binance';
  writeDesk(t);
  _loginPlat=t;
  activeTab=t;
  try{document.documentElement.setAttribute('data-desk',t)}catch(e){}
  document.body.setAttribute('data-platform',t);
  try{document.title='Ture · '+PLATS[t].label}catch(e){}
  var title=$('workspaceTitle');if(title)title.textContent=PLATS[t].label;
  var sub=$('workspaceSub');if(sub)sub.textContent=PLATS[t].sub;
  var sw=$('platSwitch');if(sw&&sw.value!==t)sw.value=t;
  document.querySelectorAll('.set-section[data-plat]').forEach(function(s){
    s.style.display=s.dataset.plat===t?'':'none'
  });
  try{switchTab(t)}catch(e){console.warn('applyWorkspace',t,e)}
  if(isLoggedIn())refresh();
};
(function bindLoginDesk(){
  function fromCard(e){
    var b=e.target.closest&&e.target.closest('[data-plat]');
    if(!b)return;
    e.preventDefault();
    e.stopPropagation();
    pickLoginPlat(b.getAttribute('data-plat'))
  }
  var g=$('loginPlatPick');
  if(g){
    g.addEventListener('pointerdown',fromCard,true);
    g.addEventListener('click',fromCard,true);
    g.addEventListener('keydown',function(e){
      if(e.key!=='Enter'&&e.key!==' ')return;
      fromCard(e)
    })
  }
  var sel=$('loginPlatSelect');
  if(sel)sel.addEventListener('change',function(){pickLoginPlat(sel.value)});
  var sw=$('platSwitch');
  if(sw)sw.addEventListener('change',function(){applyWorkspace(sw.value)});
  var btn=$('loginBtn');
  if(btn)btn.addEventListener('click',function(e){e.preventDefault();doLogin()});
  var pw=$('loginPassword');
  if(pw)pw.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();doLogin()}});
  var em=$('loginEmail');
  if(em)em.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();doLogin()}});
})();

// ── 3D Dotted Globe Animation (opencode-style) ──────────────────────────
function startGlobe(canvas,colors,animate){
  if(!canvas||!canvas.getContext)return;
  var ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height,cx=w/2,cy=h/2,r=Math.min(w,h)/2-3;
  var dots=[],num=14;
  var cols=colors||{a:[99,102,241],b:[139,92,246],c:[167,139,250]};
  for(var i=0;i<num;i++){
    var theta=Math.acos(2*i/num-1),phi=Math.PI*(1+Math.sqrt(5))*i;
    dots.push({theta:theta,phi:phi,size:1.8+Math.random()*1.5})
  }
  var angle=0,animId=null;
  canvas._stopGlobe=function(){if(animId)cancelAnimationFrame(animId)};
  function draw(){
    ctx.clearRect(0,0,w,h);
    angle+=0.025;
    var projected=dots.map(function(d){
      var x=r*Math.sin(d.theta)*Math.cos(d.phi+angle);
      var y=r*Math.sin(d.theta)*Math.sin(d.phi+angle);
      var z=r*Math.cos(d.theta);
      var x2=x*Math.cos(angle*0.7)-z*Math.sin(angle*0.7);
      var z2=x*Math.sin(angle*0.7)+z*Math.cos(angle*0.7);
      return{x:x2,y:y,z:z2,size:d.size}
    }).sort(function(a,b){return a.z-b.z});
    projected.forEach(function(p){
      var depth=(p.z+r)/(2*r);
      var alpha=0.15+depth*0.85;
      var size=Math.max(0.8,p.size*(0.5+depth*0.5));
      var isFront=p.z>0;
      ctx.beginPath();
      ctx.arc(cx+p.x,cy+p.y,size,0,Math.PI*2);
      if(isFront&&depth>0.6){
        var grad=ctx.createRadialGradient(cx+p.x-size*0.3,cy+p.y-size*0.3,0,cx+p.x,cy+p.y,size);
        grad.addColorStop(0,'rgba(255,255,255,'+(0.4+0.6*depth)+')');
        grad.addColorStop(1,'rgba('+cols.a.join(',')+','+alpha+')');
        ctx.fillStyle=grad
      }else{
        ctx.fillStyle='rgba('+cols.a.join(',')+','+alpha+')'
      }
      ctx.fill()
    });
    var hx=r*0.7*Math.sin(angle*0.9),hy=r*0.7*Math.cos(angle*0.9),hz=Math.abs(r*0.3*Math.sin(angle*0.5));
    if(hz>0){
      ctx.beginPath();
      ctx.arc(cx+hx,cy+hy,2.5,0,Math.PI*2);
      var hg=ctx.createRadialGradient(cx+hx,cy+hy,0,cx+hx,cy+hy,2.5);
      hg.addColorStop(0,'rgba(255,255,255,0.9)');
      hg.addColorStop(0.5,'rgba(255,255,255,0.4)');
      hg.addColorStop(1,'rgba(255,255,255,0)');
      ctx.fillStyle=hg;ctx.fill()
    }
    if(animate!==false)animId=requestAnimationFrame(draw)
  }
  draw()
}
function stopGlobe(canvas){
  if(canvas&&canvas._stopGlobe)canvas._stopGlobe();
  // Fallback: stop all if no canvas specified (legacy)
  else if(!canvas){var c=document.querySelector('.aiGlobe');if(c&&c._stopGlobe)c._stopGlobe()}
}

// ── Tab switching ────────────────────────────────────────────────────────
window.switchTab=function(t){
  try{
    if(PLATS[t]){activeTab=t;writeDesk(t)}
    else activeTab=t;
    var forms={binance:'formBinance',mt5:'formMt5'};
    var tabs={binance:'tabBinance',mt5:'tabMt5'};
    ['formBinance','formMt5'].forEach(function(id){
      var el=$(id);if(el)el.classList.toggle('visible',id===(forms[t]||'formBinance'))
    });
    ['tabBinance','tabMt5'].forEach(function(id){
      var el=$(id);if(!el)return;
      el.classList.toggle('active',id===(tabs[t]||''));
    });
    renderAccounts();
    renderBotList();
    pollChatter();
    loadChatHistory();
    if(t==='mt5'){window.loadMt5Status&&window.loadMt5Status();window.loadMt5Symbols&&window.loadMt5Symbols()}
    if(t==='binance'&&!_symLoaded)loadSymbols();
    loadMoverTicker();
    applyGoldChartDefault();
    if(window.loadDeskChart)window.loadDeskChart(true)
  }catch(e){console.warn('switchTab',t,e)}
};

// ── Binance ──────────────────────────────────────────────────────────────
var _symLoaded=false,_symLoading=false;
async function loadSymbols(){
  if(_symLoading)return _symbols;
  _symLoading=true;
  var d=await api('/api/symbols');
  if(d&&d.symbols&&d.symbols.length)_symbols=d.symbols;
  _symLoading=false;_symLoaded=true;
  return _symbols
}
function renderSymbolList(filter){
  var list=$('ssList');if(!list)return;
  var q=(filter||'').toUpperCase();
  var items=_symbols.filter(function(s){return !q||s.symbol.indexOf(q)>=0});
  if(!items.length){list.innerHTML='<div class="combo-empty">No symbol match</div>';return}
  list.innerHTML=items.map(function(s){
    var up=s.change>=0;
    return '<div class="combo-item'+(s.symbol===_selectedSymbol?' selected':'')+'" data-sym="'+s.symbol+'">'+
      '<span class="csym">'+s.symbol+'</span>'+
      '<span class="cprice">$'+fmt(s.price)+'</span>'+
      '<span class="cchg" style="color:'+(up?'var(--g)':'var(--r)')+'">'+(up?'+':'')+s.change.toFixed(2)+'%</span>'+
      '</div>'
  }).join('');
  list.querySelectorAll('.combo-item').forEach(function(el){
    el.addEventListener('mousedown',function(e){e.preventDefault();selectSymbol(el.dataset.sym)})
  })
}
function selectSymbol(sym){
  _selectedSymbol=sym;
  var inp=$('ss');if(inp)inp.value=sym;
  closeSymbolList();
  _chartFocusId='';
  _chartFocusSym=(sym||'').toUpperCase();
  var label=$('chartSym');if(label)label.textContent=_chartFocusSym||'—';
  if(window.loadDeskChart)window.loadDeskChart(true);
  toast('✅ '+sym)
}
function openSymbolList(){var l=$('ssList');if(l){l.classList.add('open');renderSymbolList($('ss').value)}}
function closeSymbolList(){var l=$('ssList');if(l)l.classList.remove('open')}
function setupSymbolCombo(){
  var inp=$('ss');if(!inp)return;
  inp.addEventListener('focus',async function(){
    if(!_symLoaded){await loadSymbols();if(_symbols.length)renderSymbolList('')}
    else if(!_symbols.length){await loadSymbols()}
    openSymbolList()
  });
  inp.addEventListener('input',function(){openSymbolList()});
  inp.addEventListener('keydown',function(e){
    var l=$('ssList');if(!l)return;
    if(e.key==='Escape'){closeSymbolList();inp.blur();return}
    if(e.key==='Enter'){
      var sel=l.querySelector('.combo-item.selected')||l.querySelector('.combo-item');
      if(sel){selectSymbol(sel.dataset.sym);inp.blur()}
      return
    }
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){
      e.preventDefault();
      var items=[].slice.call(l.querySelectorAll('.combo-item'));
      if(!items.length)return;
      var idx=items.findIndex(function(x){return x.classList.contains('hover')});
      if(idx>=0)items[idx].classList.remove('hover');
      if(e.key==='ArrowDown')idx=(idx+1)%items.length;else idx=(idx<=0?items.length-1:idx-1);
      items[idx].classList.add('hover');items[idx].scrollIntoView({block:'nearest'});
    }
  });
  document.addEventListener('click',function(e){if(!e.target.closest('#ssCombo'))closeSymbolList()})
}

var _gridMode=lsGet('gb.grid.mode')||'scalp';if(['scalp','swing','auto'].indexOf(_gridMode)<0)_gridMode='scalp';
window.onGridModeChange=function(v){_gridMode=(['scalp','swing','auto'].indexOf(String(v))>=0)?String(v):'scalp';lsSet('gb.grid.mode',_gridMode);var inp=$('gridMode');if(inp)inp.value=_gridMode;};
(function(){var inp=$('gridMode');if(inp)inp.value=_gridMode;})();
var _compound=true;
try{_compound=lsGet('gb.compound')!=='0'}catch(e){_compound=true}
window.onCompoundChange=function(on){_compound=!!on;try{lsSet('gb.compound',_compound?'1':'0')}catch(e){}var el=$('compoundChk');if(el)el.checked=_compound;};
var _compoundFrac=parseFloat(lsGet('gb.compound.frac')||'0.02')||0.02;if([0.02,0.05].indexOf(_compoundFrac)<0)_compoundFrac=0.02;
window.onCompoundFracChange=function(v){var f=parseFloat(v);_compoundFrac=(f===0.05)?0.05:0.02;try{lsSet('gb.compound.frac',String(_compoundFrac))}catch(e){}var el=$('compoundFrac');if(el)el.value=String(_compoundFrac);};
(function(){var el=$('compoundFrac');if(el)el.value=String(_compoundFrac);})();
(function(){var el=$('compoundChk');if(el)el.checked=_compound;})();
var _spawnVol=parseInt(lsGet('gb.spawn.vol')||'50',10);if(_spawnVol<10||_spawnVol>100||isNaN(_spawnVol))_spawnVol=50;
window.onSpawnVolChange=function(v){_spawnVol=Math.max(10,Math.min(100,parseInt(v,10)||50));lsSet('gb.spawn.vol',String(_spawnVol));var el=$('spawnVolVal');if(el)el.textContent=_spawnVol+'%';var inp=$('spawnVol');if(inp&&String(inp.value)!==String(_spawnVol))inp.value=_spawnVol;};
(function(){var el=$('spawnVolVal');if(el)el.textContent=_spawnVol+'%';var inp=$('spawnVol');if(inp)inp.value=_spawnVol;})();
var _spawnWallet=parseInt(lsGet('gb.spawn.wallet')||'70',10);if(_spawnWallet<10||_spawnWallet>100||isNaN(_spawnWallet))_spawnWallet=70;
window.onSpawnWalletChange=function(v){_spawnWallet=Math.max(10,Math.min(100,parseInt(v,10)||70));lsSet('gb.spawn.wallet',String(_spawnWallet));var el=$('spawnWalletVal');if(el)el.textContent=_spawnWallet+'%';var inp=$('spawnWallet');if(inp&&String(inp.value)!==String(_spawnWallet))inp.value=_spawnWallet;};
(function(){var el=$('spawnWalletVal');if(el)el.textContent=_spawnWallet+'%';var inp=$('spawnWallet');if(inp)inp.value=_spawnWallet;})();
var _spawnLev=lsGet('gb.spawn.lev')||'10';if(['MAX','5','10','25','50','75','100'].indexOf(String(_spawnLev))<0)_spawnLev='10';else _spawnLev=String(_spawnLev);
window.onSpawnLevChange=function(v){var s=String(v);_spawnLev=['MAX','5','10','25','50','75','100'].indexOf(s)>=0?s:'10';lsSet('gb.spawn.lev',_spawnLev);var inp=$('spawnLev');if(inp&&String(inp.value)!==_spawnLev)inp.value=_spawnLev;};
(function(){var inp=$('spawnLev');if(inp)inp.value=_spawnLev;})();
window.spawnBinance=async function(){
  var btn=$('spawnBtn')||document.querySelector('.btn-primary');
  var orig=btn.textContent;btn.disabled=true;btn.textContent='⏳ Spawning…';
  var sym=($('ss').value||'').trim().toUpperCase();
  if(!sym){toast('❌ Pick a symbol first','error');btn.disabled=false;btn.textContent=orig||'＋ Spawn';return}
  if(_memCritical){toast('⚠️ Critical hardware limit — expand server to continue','error');btn.disabled=false;btn.textContent=orig||'＋ Spawn';return}
  var r=await api('/spawn',{name:$('sn').value.trim()||'BOT',symbol:sym,market_type:'futures',fee_mode:$('sf').value,platform:'binance',env:$('senv').value,vol_pct:_spawnVol,leverage:_spawnLev,mode:_gridMode,compound:_compound,compound_frac:_compoundFrac});
  if(r&&r.bot_id){toast('✅ Bot spawned: '+sym);refresh()}else{toast('❌ '+(r&&r.detail?r.detail:'Spawn failed'),'error')}
  btn.disabled=false;btn.textContent=orig||'＋ Spawn'
};
window.stopBot=async function(id){expandedBot=null;renderBotList();await api('/stop/'+id,{});toast('🛑 Bot closed');await refresh()};
window.deleteBot=async function(id,sym){if(!confirm('Delete '+sym+' bot definition from the registry? (positions/orders are already flat — no exchange impact)'))return;expandedBot=null;renderBotList();var r=await api('/api/bots/'+id,{},'DELETE');if(r&&r.status==='deleted'){toast('🗑 '+sym+' deleted from sidebar');await refresh()}else{toast('❌ '+(r&&r.detail?r.detail:'Delete failed'),'error');await refresh()}};
var _propMode=false;
window.onPropModeChange=async function(on){
  _propMode=!!on;
  try{
    var r=await api('/api/prop/mode',{enabled:_propMode});
    if(r&&r.ok){_propMode=!!r.enabled;toast(_propMode?'🛡 Prop mode ON (Fri-flat + news blackout)':'Prop mode OFF')}
    else{_propMode=false;toast('❌ prop toggle failed','error')}
  }catch(e){_propMode=false}
  var el=$('propMode');if(el)el.checked=_propMode;
};
(async function(){try{var r=await api('/api/prop/mode');if(r&&r.ok){_propMode=!!r.enabled;var el=$('propMode');if(el)el.checked=_propMode}}catch(e){}})();
// v1.0 production: Prop evaluation panel (risk-only view, strategy untouched)
var _propTimer=null;
async function refreshPropPanel(){
  var panel=$('propPanel'),body=$('propBody');
  if(!panel||!body)return;
  if(!_propMode){panel.style.display='none';return}
  panel.style.display='block';
  try{
    var r=await api('/api/prop/status');
    if(!r||!r.ok){body.textContent='risk engine unavailable';return}
    var dd=(r.max_drawdown-(r.dd_remaining||0));
    var col=r.risk_state==='TRADING'?'var(--t2)':(r.risk_state==='RISK_WARNING'?'#d4a017':'#e5484d');
    var legs=r.open_legs||{};
    body.innerHTML='Target $'+r.profit_target+' · P&L $'+(r.equity-10000).toFixed(0)
      +' ('+(r.target_progress_pct||0)+'%)'
      +' · DD $'+dd.toFixed(0)+' / $'+r.max_drawdown+' (left $'+(r.dd_remaining||0).toFixed(0)+')'
      +' · Float $'+(r.floating||0).toFixed(0)
      +' · <b style="color:'+col+'">'+r.risk_state+'</b>'
      +' · legs ETH:'+(legs.ETHUSDT||0)+' BTC:'+(legs.BTCUSDT||0)+' XAU:'+(legs.XAUUSDT||0)
      +(r.reason?(' · '+r.reason):'');
  }catch(e){body.textContent='prop status error'}
}
setInterval(refreshPropPanel,15000);
var _guruN=10;
var _guruMax=10;
window.guruN=function(d){
  _guruN=Math.max(1,Math.min(_guruMax,_guruN+(d||0)));
  var el=$('guruNVal');if(el)el.textContent=_guruN
};
window.startGuruAI=async function(){
  var b=$('guruBtn');if(b.disabled)return;b.disabled=true;b.textContent='🔍 Scanning top coins…';
  if(_memCritical){toast('⚠️ Critical hardware limit — expand server to continue','error');b.disabled=false;b.textContent='🧠 GuruAI';return}
  var r=await api('/api/guru/start',{env:$('senv').value,n_bots:_guruN,vol_pct:_spawnVol,leverage:_spawnLev,wallet_pct:_spawnWallet,mode:_gridMode,compound:_compound,compound_frac:_compoundFrac});
  if(r&&r.ok){
    var syms=(r.started||[]).map(function(s){return s.symbol}).join(', ');
    toast('✅ GuruAI: '+(r.started||[]).length+' grids on '+syms);
    b.textContent='🧠 GuruAI';
    await refresh()
  }else{
    toast('❌ GuruAI: '+(r&&r.detail?r.detail:(r&&r.error||'failed')),'error');
    b.textContent='🧠 GuruAI'
  }
  b.disabled=false
};
window.startBacktest=async function(){
  var isMt5=(typeof activeTab!=='undefined'&&activeTab==='mt5');
  var b=$(isMt5?'btBtnM':'btBtn');if(b.disabled)return;
  var sym=isMt5?((($('mt5Sym')||{}).value||'').trim().toUpperCase()||'XAUUSD')
               :((($('ss').value||'').trim().toUpperCase())||'BTCUSDT');
  var hz=(($(isMt5?'btHorizonM':'btHorizon')||{}).value)||'1W';
  var sz=parseFloat((($(isMt5?'btSizeM':'btSize')||{}).value)||'1000')||1000;
  var box=$(isMt5?'btResultM':'btResult');
  b.disabled=true;b.textContent='⏳ Backtesting '+sym+' '+hz+'…';
  if(box){box.style.display='block';box.textContent='Running offline replay (math+gates'+(hz==='1W'?', JEV on':'')+') — touch nothing live…'}
  try{
      var r=await api('/api/backtest/run',{symbol:sym,horizon:hz,platform:activeTab==='mt5'?'mt5':'binance',env:($('senv')||{}).value||'demo',mode:_gridMode,per_level_usd:sz,compound:_compound,compound_frac:_compoundFrac,start_equity:parseFloat((($(isMt5?'btEquityM':'btEquity')||{}).value)||'10000')||10000});
    if(!(r&&r.ok&&r.job_id)){if(box)box.textContent='❌ '+(r&&r.detail?r.detail:'start failed');b.disabled=false;b.textContent='📊 Backtest';return}
    var tries=0;
    while(tries++<40){
      await new Promise(function(res){setTimeout(res,5000)});
      var j=await api('/api/backtest/result/'+r.job_id);
      if(!j){if(box)box.textContent='❌ lost job';break}
      if(j.status==='done'&&j.result){
        var t=j.result;
        if(box)box.innerHTML='📊 <b>'+t.symbol+' '+t.horizon+'</b> ('+t.bars+' bars'+(t.jev_on?', '+t.jev_calls+' JEV calls':'')+'): '
          +t.trades+' trades, WR '+(t.win_rate*100).toFixed(0)+'%, net <b style="color:'+(t.total>=0?'var(--g)':'var(--r)')+'">$'+t.total.toFixed(2)+'</b>'
          +' (maxDD $'+t.max_dd.toFixed(2)+', '+t.elapsed_s+'s'+(t.liquidated?' <b style="color:var(--r)">LIQUIDATED</b>':'')+') <span style="color:var(--t3)">fills assume touch; live lands 5-15% lower</span>';
        break
      }
      if(j.status==='error'){if(box)box.textContent='❌ '+(j.error||'failed');break}
      if(box)box.textContent='⏳ replaying… ('+(tries*5)+'s)';
    }
  }catch(e){if(box)box.textContent='❌ '+String(e).slice(0,120)}
  b.disabled=false;b.textContent='📊 Backtest'
};
window.toggleExpand=function(id){
  selectChartBot(id);
  expandedBot=expandedBot===id?null:id;
  renderBotList();
  if(window.loadDeskChart)window.loadDeskChart(true)
};
window.stopGuruAI=async function(){
  var btn=$('guruBtn');btn.disabled=true;btn.textContent='⏳ Stopping GuruAI…';
  await Q('/api/guru/stop',{});toast('🛑 GuruAI grids stopped');btn.disabled=false;btn.textContent='🧠 GuruAI';await refresh()
};
window.killSwitch=async function(){
  var b=$('killBtn');if(b.disabled)return;
  if(!confirm('🛑 EMERGENCY KILL SWITCH?\n\nStop ALL Binance bots on THIS desk, cancel open orders and close positions on '+$('senv').value.toUpperCase()+'.'))return;
  b.disabled=true;b.textContent='🛑 KILLING…';
  try{
    var r=await api('/api/guru/kill',{env:$('senv').value});
    if(r&&r.status==='killed'){
      if(r.flat===false){
        toast('⚠️ KILLED but residue remains: '+((r.residue||{}).orders||0)+' order(s), '+((r.residue||{}).positions||0)+' position(s) — check account','error')
      }else{
        toast('🛑 KILLED: '+r.stopped+' bot(s) stopped, '+r.cleaned+' order(s)/position(s) swept — account verified FLAT')
      }
    }else{
      toast('❌ Kill failed: '+(r&&r.detail?r.detail:'unknown error'),'error')
    }
  }finally{
    b.disabled=false;b.textContent='🛑 KILL';
  }
  // Clear slate: wipe cached state, overlays, chart, then full re-sync
  try{sessionStorage.removeItem(botsCacheKey('binance'))}catch(e){}
  try{sessionStorage.removeItem(botsCacheKey('mt5'))}catch(e){}
  bots=[];
  _overlayLevels=[];_sessionMarks=[];_lastBar=null;_lastLine=null;
  clearChartLines();clearTradeLines();
  var mk=$('chartMarks');if(mk)mk.innerHTML='';
  try{if(_tvSeries&&_tvSeries.setMarkers)_tvSeries.setMarkers([])}catch(e){}
  _tvSym=''; // force chart re-init on next load (auto-resize to new symbol state)
  await refresh();
  checkResources();
  if(window.loadDeskChart)window.loadDeskChart(true);
  if(window.refreshMovers)window.refreshMovers();
};


window.toggleSet=function(el){
  var sec=el.closest('.set-section');
  if(sec)sec.classList.toggle('collapsed')
};

window.saveBinanceKeys=async function(){
  var payload={
    live:{api_key:($('binanceLiveKey')||{}).value||'',api_secret:($('binanceLiveSecret')||{}).value||''},
    demo:{api_key:($('binanceDemoKey')||{}).value||'',api_secret:($('binanceDemoSecret')||{}).value||''}
  };
  var any=payload.live.api_key||payload.live.api_secret||payload.demo.api_key||payload.demo.api_secret;
  if(!any){toast('⚠ Paste at least one API key','error');return}
  var r=await api('/api/binance/keys',payload);
  if(r&&r.ok){
    toast('✅ Binance keys saved'+(r.saved&&r.saved.length?(' ('+r.saved.join(', ')+')'):''));
    ['binanceLiveKey','binanceLiveSecret','binanceDemoKey','binanceDemoSecret'].forEach(function(id){var e=$(id);if(e)e.value=''});
    refreshBinanceAccount();
  }else{
    toast('❌ Save failed'+(r&&r.detail?' — '+(typeof r.detail==='string'?r.detail:'error'):''),'error')
  }
};


// ── Accounts rendering ──────────────────────────────────────────────────
var acctCache={binance:null,binanceAt:0};
var ACCT_TTL=60000; // Exchange account reads are expensive and rate limited.

async function getBinanceAccts(force){
  var now=Date.now();
  if(force||!acctCache.binance||now-acctCache.binanceAt>ACCT_TTL){
    var ba=await api('/api/accounts'+(force?'?fresh=1':''));
    acctCache.binance=(ba&&ba.accounts)?ba.accounts.filter(function(a){return a.platform==='binance'}):[];
    acctCache.binanceAt=now
  }
  return acctCache.binance
}

window.refreshBinanceAccount=async function(){
  var btn=document.querySelector('.acct-refresh');
  if(btn)btn.classList.add('spinning');
  acctCache.binance=null;acctCache.binanceAt=0;
  await getBinanceAccts(true);
  await renderAccounts();
  toast('↻ Binance account refreshed','success');
  if(btn)setTimeout(function(){btn.classList.remove('spinning')},600)
};

window.saveOpenRouter=async function(){
  var payload={api_key:($('orKey')||{}).value||'',model:(($('orModel')||{}).value||'').trim()};
  if(!payload.api_key&&!payload.model){toast('⚠ Paste an API key or model','error');return}
  var r=await api('/api/settings/openrouter',payload);
  if(r&&r.ok){
    toast('✅ AI settings saved'+(r.configured?' (key set)':' (model only — add a key to enable)'));
    var k=$('orKey');if(k)k.value='';
    refreshOpenRouterState();
  }else{toast('❌ Save failed','error')}
};
async function refreshOpenRouterState(){
  try{
    var r=await api('/api/settings/openrouter');
    var el=$('orKeyState');
    if(el&&r){el.textContent=r.configured?'· ✅ key saved':(r.model?('· model: '+r.model):'· not configured');el.style.color=r.configured?'var(--g)':'var(--t3)'}
    var m=$('orModel');if(m&&r&&r.model&&!m.value)m.value=r.model;
  }catch(e){}
}
setTimeout(refreshOpenRouterState,2500);

window.closeAllBinance=async function(btnEl,env){
  var label=(env==='live'?'LIVE':'DEMO');
  if(!confirm('Close ALL positions + cancel all open/conditional orders on Binance '+label+'?'))return;
  var orig=btnEl.textContent;btnEl.disabled=true;btnEl.textContent='⏳ Closing…';
  var r=await api('/api/binance/close-all',{env:env});
  if(r&&r.ok){
    toast('✅ Binance '+label+' flat — '+r.positions+' position(s), '+r.orders+' order group(s), conditional '+(r.conditional?'cancelled':'none'));
  }else{
    toast('❌ Close-all failed','error')
  }
  btnEl.disabled=false;btnEl.textContent=orig;
  acctCache.binance=null;acctCache.binanceAt=0;
  await renderAccounts();
};


window.toggleAcct=function(key,el){
  var collapsed=!isAcctCollapsed(key);
  setAcctCollapsed(key,collapsed);
  var card=el?el.closest('.acct-card'):null;
  if(card)card.classList.toggle('collapsed',collapsed)
};

// Account widget collapse state — default collapsed, persisted per account so
// auto-refresh / data-refresh re-renders keep the user's choice.
var ACCT_COLLAPSE_KEY='gb.acct.collapsed';
function isAcctCollapsed(key){
  try{
    var s=JSON.parse(localStorage.getItem(ACCT_COLLAPSE_KEY)||'{}');
    if(Object.prototype.hasOwnProperty.call(s,key))return !!s[key];
    // MT5 source-of-truth book should be visible (positions/orders), not hidden.
    return key==='mt5'?false:true
  }catch(e){return key!=='mt5'}
}
function setAcctCollapsed(key,collapsed){
  try{
    var s=JSON.parse(localStorage.getItem(ACCT_COLLAPSE_KEY)||'{}');
    s[key]=!!collapsed;
    localStorage.setItem(ACCT_COLLAPSE_KEY,JSON.stringify(s))
  }catch(e){}
}

function acctPosTable(positions,isInr,dp){
  if(!positions||!positions.length)return '<div class="acct-det-empty">No open positions</div>';
  var h='<div class="acct-tbl-wrap"><table class="acct-tbl"><tr><th>Symbol</th><th>Side</th><th style="text-align:right">Qty</th><th style="text-align:right">Entry</th><th style="text-align:right">Mark</th><th style="text-align:right">uPnL</th></tr>';
  positions.forEach(function(p){
    var pc=p.upnl||0;
    var inrRow=isInr||p.currency==='INR';
    var cur=inrRow?'₹':'$';
    var pdp=dp!=null?dp:(inrRow?2:6);
    var qdp=inrRow?4:4;
    h+='<tr><td>'+p.symbol+'</td><td style="color:'+(p.side==="LONG"||p.side==="BUY"?'var(--g)':'var(--r)')+'">'+p.side+'</td><td style="text-align:right">'+(+p.qty).toFixed(qdp)+'</td><td style="text-align:right">'+(+p.entry).toFixed(pdp)+'</td><td style="text-align:right">'+(+p.mark).toFixed(pdp)+'</td><td style="text-align:right;color:'+(pc>=0?'var(--g)':'var(--r)')+'">'+cur+(inrRow?inr(pc):fmt(pc))+'</td></tr>'
  });
  return h+'</table></div>'
}
function acctOrdersTable(orders){
  if(!orders||!orders.length)return '<div class="acct-det-empty">No open orders</div>';
  var h='<div class="acct-tbl-wrap"><table class="acct-tbl"><tr><th>Symbol</th><th>Side</th><th>Type</th><th style="text-align:right">Price</th><th style="text-align:right">Stop</th><th style="text-align:right">Qty</th></tr>';
  orders.forEach(function(o){
    var cond=o.is_conditional?'<span style="color:var(--y)" title="Conditional">⚡</span>':'';
    h+='<tr><td>'+o.symbol+' '+cond+'</td><td style="color:'+(o.side==="BUY"?'var(--g)':'var(--r)')+'">'+o.side+'</td><td>'+o.type+'</td><td style="text-align:right">'+(+o.price||'-')+'</td><td style="text-align:right">'+(+o.stop_price||'-')+'</td><td style="text-align:right">'+(+o.qty).toFixed(4)+'</td></tr>'
  });
  return h+'</table></div>'
}

async function renderAccounts(){
  var h='',isMt5=activeTab==='mt5';
  if(isMt5){
    // ── MT5: account card from the EA link ──
    h+='<div class="sidebar-section"><h3><span class="accent" style="background:#d4a017"></span>🟡 MT5</h3>';
    try{
      var md=await api('/api/mt5/status');
      if(!md||!md.has_token){
        h+='<div class="acct-card"><div class="acct-err">⚠ No MT5 token — Settings ▸ MT5 ▸ Generate</div></div>'
      }else if(!md.connected){
        var a=md.account||{};
        var age=md.last_seen_s;
        var why=age==null?'waiting for first POST this process':'last POST '+Math.round(age)+'s ago';
        h+='<div class="acct-card"><div class="acct-header"><span class="acct-name">'+(a.login||'MT5')+'</span><span class="acct-env demo">QUIET</span></div>';
        if(a.server)h+='<div class="acct-grid"><div class="metric"><span class="lbl">Server</span><span class="val" style="font-size:9px">'+a.server+'</span></div></div>';
        h+='<div class="acct-err">⚠ EA quiet — '+why+' (not a RAM issue; compile EA v3.11 if a limit is open)</div></div>'
      }else{
        var a=md.account||{};
        var mode=(a.trade_mode||'demo').toUpperCase();
        var modeCls=mode==='LIVE'?'live':'demo';
        var mkey='mt5';
        var pos=md.positions||[], ords=md.open_orders||[];
        _eaBook={positions:pos,open_orders:ords};
        // Live EA book stays visible — collapsing hid BUY/SELL while MT5 had them.
        var mcol=(pos.length||ords.length)?'':(isAcctCollapsed(mkey)?' collapsed':'');
        var cur=a.currency||'';
        h+='<div class="acct-card'+mcol+'"><div class="acct-header" onclick="toggleAcct(\''+mkey+'\',this)"><span class="acct-name">'+(a.login||'MT5')+'</span><span class="acct-env '+modeCls+'">'+mode+'</span><span class="acct-chev">▾</span></div>';
        h+='<div class="acct-grid"><div class="metric"><span class="lbl">Balance</span><span class="val" style="color:var(--g)">'+cur+' '+fmt(a.balance)+'</span></div><div class="metric"><span class="lbl">Equity</span><span class="val">'+cur+' '+fmt(a.equity)+'</span></div><div class="metric"><span class="lbl">Free</span><span class="val">'+cur+' '+fmt(md.free_margin||a.free_margin||0)+'</span></div><div class="metric"><span class="lbl">Server</span><span class="val" style="font-size:9px">'+(a.server||'?')+'</span></div></div>';
        h+='<div class="acct-ok">✓ EA Connected</div>';
        h+='<div class="acct-collapsible"><div class="acct-det">';
        h+='<div class="acct-det-title">POSITIONS ('+pos.length+')</div>'+acctPosTable(pos,false,5);
        h+='<div class="acct-det-title">OPEN ORDERS ('+ords.length+')</div>'+acctOrdersTable(ords);
        h+='</div><button class="btn-close-all" onclick="closeAllMt5(this)">✕ Close All Positions &amp; Orders</button></div></div>'
      }
    }catch(e){
      h+='<div class="acct-card"><div class="acct-err">⚠ status failed</div></div>'
    }
    h+='</div>'
  }else{
    var binanceAccts=await getBinanceAccts(false);
    if(binanceAccts.length){
      h+='<div class="sidebar-section"><h3><span class="accent" style="background:var(--b)"></span>₿ BINANCE<span class="acct-refresh" title="Refresh now" onclick="refreshBinanceAccount()">↻</span></h3>';
      binanceAccts.forEach(function(a){
        var envCls=a.env==='live'?'live':'demo';
        var bkey='binance:'+a.env;
        var bcol=isAcctCollapsed(bkey)?' collapsed':'';
        h+='<div class="acct-card'+bcol+'"><div class="acct-header" onclick="toggleAcct(\''+bkey+'\',this)"><span class="acct-name">'+(a.env==='live'?'Live':'Demo')+' Account</span><span class="acct-env '+envCls+'">'+a.env.toUpperCase()+'</span><span class="acct-chev">▾</span></div>';
        if(a.connected){
          h+='<div class="acct-grid"><div class="metric"><span class="lbl">Balance</span><span class="val" style="color:var(--g)">$'+fmt(a.balance)+'</span></div><div class="metric"><span class="lbl">Equity</span><span class="val">$'+fmt(a.equity)+'</span></div><div class="metric"><span class="lbl">Margin</span><span class="val">$'+fmt(a.margin)+'</span></div><div class="metric"><span class="lbl">Free</span><span class="val" style="color:'+(a.free_margin>0?'var(--g)':'var(--t2)')+'">$'+fmt(a.free_margin)+'</span></div></div>';
          h+='<div class="acct-ok">✓ Connected</div>';
          h+='<div class="acct-collapsible"><div class="acct-det">';
          h+='<div class="acct-det-title">POSITIONS ('+(a.positions?a.positions.length:0)+')</div>'+acctPosTable(a.positions,false);
          h+='<div class="acct-det-title">OPEN ORDERS ('+(a.open_orders?a.open_orders.length:0)+')</div>'+acctOrdersTable(a.open_orders);
          h+='</div><button class="btn-close-all" onclick="closeAllBinance(this,\''+a.env+'\')">✕ Close All Positions &amp; Orders</button></div>'
        }else{
          h+='<div class="acct-err">⚠ '+(a.error||'Disconnected')+'</div>'
        }
        h+='</div>'
      });
      h+='</div>'
    }else{
      h+='<div class="sidebar-section"><h3><span class="accent" style="background:var(--b)"></span>₿ BINANCE</h3><div class="acct-card"><div class="acct-err">⚠ No Binance credentials in .env</div></div></div>'
    }
  }
  $('accountsSection').innerHTML=h;
  renderAccountsBar()
}

async function renderAccountsBar(){
  var parts=[];
  if(activeTab==='mt5'){
    var md=await api('/api/mt5/status');
    if(md&&md.connected){
      var a=md.account||{};
      var mode=(a.trade_mode||'demo').toUpperCase();
      parts.push('<span class="bar-item"><span class="bar-dot" style="background:'+(mode==='LIVE'?'#10b981':'var(--y)')+'"></span>MT5 '+mode+' · '+(a.currency||'$')+fmt(a.equity)+' equity</span>')
    }
  }
  else{
    var binanceAccts=await getBinanceAccts(false);
    binanceAccts.forEach(function(a){if(a.connected)parts.push('<span class="bar-item"><span class="bar-dot" style="background:'+(a.env==='live'?'#10b981':'var(--y)')+'"></span>Binance '+a.env.toUpperCase()+' · $'+fmt(a.balance)+'</span>')})
  }
  $('acctBar').innerHTML=parts.join('')||'<span class="bar-item">No accounts connected</span>'
}

function fmt(n){return Number(n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
function inr(n){return Number(n||0).toLocaleString('en-IN',{maximumFractionDigits:0})}

// ── Voice acknowledgment — 100 short responses ───────────────────────────
var ACK_PHRASES=(function(){
  var pool=[
    "Sure","On it","Got it","Alright","Okay","Done","Let me check","Coming right up","One sec","Working on it",
    "Absolutely","You got it","Consider it done","Right away","No problem","Easy","On it now","Just a moment","Give me a sec","Roger that",
    "Copy that","Will do","Sure thing","Sounds good","Perfect","Great","Understood","Okay then","Alrighty","Let's do it",
    "Checking now","Looking into it","Fetching that","Pulling that up","Hang tight","Bear with me","Almost there","Just a tick","Give me a beat","Starting now",
    "Diving in","Getting on it","Processing that","Let me see","Right up","In a flash","Quickly now","As you wish","Certainly","Indeed",
    "Fine","Very well","Acknowledged","Noted","Understood","Gotcha","Ten four","Yes indeed","Okay dokey","Right then",
    "Let me handle that","I'll take care of it","That's easy","Way ahead of you","Already on it","No worries","All good","Happy to","Sure can","You bet",
    "Absolutely right","Spot on","Exactly","Perfect timing","Alright let's go","Off we go","Here we go","Ready when you are","Going now","Now fetching",
    "Let me check that","Give me a moment","Right on it","At your service","Just for you","Say no more","Understood got it","Makes sense","Good call","Let's get it",
    "On the ball","Quick as can be","Zip zip","Voila coming","Seconds away","Brb","Moment please","Tick tock","Fast as lightning","Here we come"
  ];
  // Fisher-Yates shuffle
  for(var i=pool.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));var t=pool[i];pool[i]=pool[j];pool[j]=t}
  return pool
})();
var _ackIdx=0;

// Pick the most natural available voice: macOS Natural/Enhanced/Premium first,
// then Google/neural voices, then any English voice.
var _bestVoice=null;
function pickVoice(){
  var voices=window.speechSynthesis.getVoices();
  if(!voices||!voices.length)return null;
  // Warm cache only once available
  function eng(v){return v.lang&&v.lang.toLowerCase().indexOf('en')===0}
  function score(v){
    if(!eng(v))return -1;
    var n=(v.name||'');
    var s=0;
    if(/natural|enhanced|premium|premium_quality/i.test(n))s=30;
    if(/zoe|aria|jenny|aaron|samantha|juan|graciela|michelle|sonia/i.test(n))s+=12;
    if(v.localService)s+=8;
    if(/nao|kai|zira|david|catherine/i.test(n))s-=10;
    return s
  }
  var ranked=voices.slice().sort(function(a,b){return score(b)-score(a)});
  _bestVoice=ranked[0]&&score(ranked[0])>=0?ranked[0]:null;
  if(!_bestVoice)return null;
  return _bestVoice
}
if(window.speechSynthesis){
  pickVoice();
  window.speechSynthesis.onvoiceschanged=function(){pickVoice()}
}

// ── Kokoro neural audio player (primary) with macOS speechSynthesis fallback ──
// Uses AudioContext so playback isn't blocked by autoplay policy on HTTP origins.
var _actx=null,_curSrc=null;
function ensureCtx(){
  if(!_actx){var AC=window.AudioContext||window.webkitAudioContext;if(AC){try{_actx=new AC()}catch(e){_actx=null}}}
  if(_actx&&_actx.state==='suspended')_actx.resume().catch(function(){});
  return _actx
}
// Unlock audio on first user gesture (required on HTTP origins)
['pointerdown','touchstart','keydown'].forEach(function(ev){
  document.addEventListener(ev,function(){ensureCtx()},{once:false})
});
function stopAudio(){
  window.speechSynthesis.cancel();
  if(_curSrc){try{_curSrc.stop()}catch(e){}try{_curSrc.disconnect()}catch(e){}_curSrc=null}
}
function playWav(url,cb){
  // cb(ok, httpErr): httpErr=true means the request/server failed (sound is
  // unavailable), while ok=false without httpErr is a transient playback
  // issue (autoplay/decode) that a later user gesture can resolve.
  fetch(url,{headers:adminHeaders()}).then(function(r){
    if(!r.ok)return cb&&cb(false,true);
    return r.arrayBuffer().then(function(buf){
      if(!buf||!buf.byteLength)return cb&&cb(false);
      ensureCtx();
      if(!_actx)return cb&&cb(false);
      return _actx.decodeAudioData(buf).then(function(ab){
        stopAudio();
        var src=_actx.createBufferSource();
        src.buffer=ab;src.connect(_actx.destination);
        src.onended=function(){_curSrc=null;cb&&cb(true)};
        _curSrc=src;
        src.start()
      }).catch(function(){cb&&cb(false)})
    }).catch(function(){cb&&cb(false,true)})
  }).catch(function(){cb&&cb(false,true)})
}
// Play an already-decoded AudioBuffer (start returns false if it couldn't).
function playBuffer(ab,onEnd){
  ensureCtx();
  if(!_actx){onEnd&&onEnd();return false}
  stopAudio();
  var src=_actx.createBufferSource();
  src.buffer=ab;src.connect(_actx.destination);
  src.onended=function(){_curSrc=null;onEnd&&onEnd()};
  _curSrc=src;
  try{src.start()}catch(e){onEnd&&onEnd();return false}
  return true
}
// Fetch + decode without playing — used to queue sentence audio during streaming.
function prefetchWav(url,cb){
  fetch(url,{headers:adminHeaders()}).then(function(r){
    if(!r.ok)return cb&&cb(false,null,true);
    return r.arrayBuffer()
  }).then(function(buf){
    if(!buf||!buf.byteLength)return cb&&cb(false,null,false);
    ensureCtx();
    if(!_actx)return cb&&cb(false,null,false);
    _actx.decodeAudioData(buf).then(function(ab){cb&&cb(true,ab,false)})
      .catch(function(){cb&&cb(false,null,false)})
  }).catch(function(){cb&&cb(false,null,true)})
}
var _kokoroFailStreak=0,_kokoroCooldown=0;
// Cooldown scheme: fall back to speechSynthesis on any failure, but only back
// off Kokoro after 3 consecutive *HTTP* failures (really down), and re-enable
// after 2 min so a transient outage never leaves the old voice stuck forever.
function kokoroOn(){return Date.now()>_kokoroCooldown}
function kokoroSpeak(text){
  if(!kokoroOn()){speakUtterance(text,0.9,1.05);return}
  playWav(ttsUrl(text),function(ok,httpErr){
    if(ok)return;
    if(httpErr&&++_kokoroFailStreak>=3){_kokoroFailStreak=0;_kokoroCooldown=Date.now()+120000}
    speakUtterance(text,0.9,1.05)
  })
}
function speakUtterance(text,rate,pitch){
  var u=new SpeechSynthesisUtterance(text);
  u.lang='en-US';u.rate=rate;u.pitch=pitch;
  var preferred=pickVoice();
  if(preferred)u.voice=preferred;
  window.speechSynthesis.speak(u)
}
// Clean raw answer text to plain speakable words.
function cleanText(t){
  return t.replace(/<[^>]+>/g,' ').replace(/\|[-:\s|]+\|/g,' ').replace(/\|/g,' ')
    .replace(/```[\s\S]*?```/g,' ').replace(/[*_~#>]/g,' ').replace(/https?:\/\/\S+/g,' ')
    .replace(/[^\w\s.,!?\'\"-]/g,' ').replace(/\s+/g,' ').trim()
}
// Complete sentences (terminal punctuation reached), up to 3, each ending '.'.
// Pass trailing=true to also include a final unpunctuated chunk (finished text).
function completeSentences(t,trailing){
  var parts=cleanText(t).split(/[.!?]+/),out=[],upto=trailing?parts.length:parts.length-1;
  for(var i=0;i<upto;i++){
    var p=parts[i].replace(/^\s+|\s+$/g,'');
    if(p.length>10)out.push(p+'.')
  }
  return out.slice(0,3)
}
function speakText(t){return completeSentences(t,true).join(' ')}
// Sentence audio prefetched as the answer streams, played back-to-back so the
// voice reads along with the text instead of one big 3-5s wait at the end.
var _sentQ=[];
function playQueued(){
  var item=_sentQ[0];
  if(!item)return;
  if(item.buf){_sentQ.shift();playBuffer(item.buf,playQueued);return}
  // Prefetch still in-flight — give it a moment, then synth on demand.
  if((item._tries=(item._tries||0)+1)<60){setTimeout(playQueued,100);return}
  _sentQ.shift();
  playWav(ttsUrl(item.text),function(ok,httpErr){
    if(httpErr&&++_kokoroFailStreak>=3){_kokoroFailStreak=0;_kokoroCooldown=Date.now()+120000}
    if(!ok)speakUtterance(item.text,0.9,1.05);
    playQueued()
  })
}
window.speakQueued=function(ft){
  if(_curSrc||window.speechSynthesis.speaking){stopAudio();return}
  if(!_sentQ.length){var c=speakText(ft);if(c)kokoroSpeak(c);return}
  // Make sure a final unpunctuated tail (if any) is queued too.
  var fin=completeSentences(ft,true);
  while(_sentQ.length<fin.length){
    var s=fin[_sentQ.length];if(!s)break;
    var item={text:s,buf:null};
    _sentQ.push(item);
    (function(it){prefetchWav(ttsUrl(it.text),function(ok,ab,bad){
      if(ok&&ab)it.buf=ab;
      else if(bad&&++_kokoroFailStreak>=3){_kokoroFailStreak=0;_kokoroCooldown=Date.now()+120000}
    })})(item)
  }
  playQueued()
}
var VOICE=localStorage.getItem('gb_voice')||'af_heart';
function ttsUrl(text){return API+'/api/tts?text='+encodeURIComponent(text)+'&voice='+encodeURIComponent(VOICE)}
function ackUrl(i){return API+'/api/acks?i='+i+'&voice='+encodeURIComponent(VOICE)}
function speakAck(){
  var phrase=ACK_PHRASES[_ackIdx];
  _ackIdx=(_ackIdx+1)%ACK_PHRASES.length;
  if(kokoroOn()){
    // Backend pre-synthesizes the first handful of acks (per voice) — instant.
    var i=_ackIdx%30;
    playWav(ackUrl(i),function(ok,httpErr){
      if(ok)return;
      if(httpErr&&++_kokoroFailStreak>=3){_kokoroFailStreak=0;_kokoroCooldown=Date.now()+120000}
      speakUtterance(phrase,1.0,1.0);
      toast('🎙 "'+phrase+'"','success')
    });
    return
  }
  speakUtterance(phrase,1.0,1.0);
  toast('🎙 "'+phrase+'"','success')
}

function speakError(msg){
  kokoroSpeak(msg)
}

// ── Streaming indicator — animated border + thinking text ─────────────────
function startStreaming(el){
  var dots=['','·','··','···','····','···','··','·'],i=0;
  el.innerHTML='<span class="thinking-text">Thinking</span>';
  var txt=el.querySelector('.thinking-text');
  txt.dataset.thinkInt=setInterval(function(){
    i=(i+1)%dots.length;
    txt.textContent='Thinking'+dots[i]
  },250)
}
function stopStreaming(el){
  if(el.querySelector('.thinking-text')){
    var txt=el.querySelector('.thinking-text');
    if(txt.dataset.thinkInt)clearInterval(txt.dataset.thinkInt)
  }
}

function nestLevelRows(ld){
  if(!ld||!ld.length)return [];
  var grid=[],sls=[],tps=[],pos=[];
  ld.forEach(function(l){
    var k=(l.kind||'grid');
    if(k==='sl')sls.push(l);
    else if(k==='tp')tps.push(l);
    else if(k==='pos')pos.push(l);
    else grid.push(Object.assign({},l));
  });
  function take(pool,qty){
    for(var i=0;i<pool.length;i++){
      var q=+(pool[i].volume||pool[i].qty||0);
      if(Math.abs(q-qty)<1e-8||!qty){
        var px=+(pool[i].price_open||pool[i].price||0);
        pool.splice(i,1);return px
      }
    }
    if(pool.length){
      var px2=+(pool[0].price_open||pool[0].price||0);
      pool.shift();return px2
    }
    return 0
  }
  grid.forEach(function(g){
    if(!(+g.sl))g.sl=take(sls,+(g.volume||g.qty||0));
    if(!(+g.tp))g.tp=take(tps,+(g.volume||g.qty||0));
  });
  return grid.concat(pos)
}

// ── Render bot list (context-aware) ────────────────────────────────────
var _botsLoaded=!!(bots&&bots.length);
function renderBotList(){
  var bl=$('botList');
  if(!_botsLoaded&&!(bots&&bots.length)){
    if(bl)bl.innerHTML='<div style="padding:20px;color:var(--t3);text-align:center;font-size:12px">⟳ Loading...</div>';
    return
  }
  var filtered=bots.filter(function(b){
    if(activeTab==='mt5')return b.platform==='mt5';
    return !['mt5'].includes(b.platform)
  });
  if(!filtered.length){
    var tabName=({mt5:'MT5'}[activeTab])||'Binance';
    if(activeTab==='mt5'){
      var eaLv=eaBookToLevels(_eaBook||{});
      if(eaLv.length){
        $('botCount').textContent=eaLv.length+' on EA';
        var rows=eaLv.map(function(l,i){
          var side=l.type||'?';
          return '<tr><td>'+(i+1)+'</td><td style="color:'+(side==='BUY'||side==='LONG'?'var(--g)':'var(--r)')+'">'+side+'</td><td style="text-align:right">'+(+l.volume||0)+'</td><td style="text-align:right">'+(+l.price_open||0).toFixed(2)+'</td></tr>'
        }).join('');
        bl.innerHTML='<div class="bot-item running"><span class="badge running">ea</span><div class="name"><span class="emoji">🥇</span>EA book</div><div class="detail"><b>MT5 source of truth</b> · '+eaLv.length+' row(s)</div><div class="bot-detail"><table><tr><th>#</th><th>Side</th><th style="text-align:right">Qty</th><th style="text-align:right">Price</th></tr>'+rows+'</table><div style="font-size:10px;color:var(--t3);margin-top:6px">No Python bot — this is the EA Trade tab. Spawn to manage.</div></div></div>';
        return
      }
    }
    bl.innerHTML='<div style="padding:24px;color:var(--t3);text-align:center;font-size:12px">No '+tabName+' bots</div>';
    return
  }
  $('botCount').textContent=filtered.length+' bot'+(filtered.length!==1?'s':'');
  $('liveDot').style.background=filtered.some(function(b){return b.status==='running'})?'var(--g)':'var(--t3)';
  bl.innerHTML=filtered.map(function(b,i){
    var e=E[b.symbol]||'📊',env=b.env||'unknown',st=b.status,exp=b.bot_id===expandedBot;
    var lv=b.levels||0,ld=b.levels_detail||[],tp=b.total_pnl||0;
    if(b.platform==='mt5'){
      var eaLv=eaBookToLevels(_eaBook||{});
      if(eaLv.length){ld=eaLv;lv=eaLv.length}
      exp=true
    }
    var shortId=(b.bot_id||'').replace(/^bot-/,'').substring(0,8)||('#'+(i+1));
    var badgeCls='badge '+st+(env!=='unknown'?' '+st+'-'+env:'');
    var sides='';
    if(b.platform==='mt5'&&ld&&ld.length){
      var gridLd=ld.filter(function(l){return (l.kind||'grid')==='grid'||l.kind==='pos'});
      sides=' · '+(gridLd.length?gridLd:ld).map(function(l){return (l.type||'?')+' '+(+l.price_open||0).toFixed(2)}).join(' / ')
    }
    var detail='';
    if(exp){
      detail='<div class="bot-detail"><div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:6px"><span>'+b.symbol+' · '+b.market_type+'</span><span style="color:'+(tp>=0?'var(--g)':'var(--r)')+'">$'+tp.toFixed(4)+'</span></div>';
      if(ld&&ld.length){
        var rows=nestLevelRows(ld);
        var showRails=rows.some(function(l){return +l.sl||+l.tp});
        detail+='<table><tr><th>#</th><th>Side</th><th style="text-align:right">Qty</th><th style="text-align:right">Price</th>'+(showRails?'<th style="text-align:right">SL</th><th style="text-align:right">TP</th>':'')+'<th style="text-align:right">P&amp;L</th>'+'</tr>';
        rows.forEach(function(l,i){
          var side=l.type||l.leg||'?';
          var kind=(l.kind||'grid');
          {
            var pc=l.pnl||0;
            var lab=kind==='pos'?(side==='BUY'||side==='LONG'?'LONG':'SHORT'):side;
            var col=(side==='BUY'||side==='LONG')?'var(--g)':'var(--r)';
            var sl=(+l.sl||0), tp=(+l.tp||0);
            detail+='<tr><td>'+(kind==='pos'?'pos':(l.level||i+1))+'</td><td style="color:'+col+'">'+lab+'</td><td style="text-align:right">'+(l.volume||0).toFixed(6)+'</td><td style="text-align:right">$'+(l.price_open||0).toFixed(2)+'</td>';
            if(showRails)detail+='<td style="text-align:right;color:var(--y)">'+(sl?sl.toFixed(2):'—')+'</td><td style="text-align:right;color:var(--b)">'+(tp?tp.toFixed(2):'—')+'</td>';
            detail+='<td style="text-align:right;color:'+(pc>=0?'var(--g)':'var(--r)')+'">$'+pc.toFixed(4)+'</td></tr>'
          }});
        detail+='</table>'
      }else{
        detail+='<div style="font-size:10px;color:var(--t3)">No active grid levels</div>'
      }
      detail+='<div class="chatter-ticker bt-ticker" id="bt-'+b.bot_id+'" onclick="event.stopPropagation()" title="Live activity + HERMES calls for '+b.symbol+'"><span class="tick">Loading '+b.symbol+' activity…</span></div>';
      detail+='<div style="display:flex;gap:4px;margin-top:8px"><button class="btn btn-danger btn-xs" onclick="event.stopPropagation();stopBot(\''+b.bot_id+'\')">✕ Close</button>';
      if(st!=='running'){
        detail+='<button class="btn btn-danger btn-xs" style="background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.25)" onclick="event.stopPropagation();deleteBot(\''+b.bot_id+'\',\''+b.symbol+'\')">🗑 Delete</button>'
      }
      detail+='</div></div>'
    }
    return '<div class="bot-item '+st+(b.bot_id===_chartFocusId?' chart-focus':'')+'" onclick="toggleExpand(\''+b.bot_id+'\')"><span class="'+badgeCls+'">'+st+(env!=='unknown'?'-'+env:'')+'</span><div class="name"><span class="emoji">'+e+'</span>'+shortId+'</div><div class="detail"><b>'+b.symbol+'</b> · lv '+lv+sides+' · $'+Number(tp||0).toFixed(4)+'</div>'+detail+'</div>'
  }).join('')
}

// ── Voice (Juskoe-style waveform overlay) ────────────────────────────────
var audioCtx=null,analyser=null,visFrame=null,micStream=null,voiceBars=[];

function initBars(){
  voiceBars=[];
  for(var i=0;i<48;i++){var e=document.getElementById('vw'+i);if(e)voiceBars.push(e)}
}

window.toggleVoice=function(){
  var btn=$('mic'),overlay=$('voiceOverlay');
  // Cancel any speaking audio immediately
  stopAudio();
  if(recognition){
    recognition.stop();recognition=null;btn.innerHTML='🎤';btn.style.color='';btn.title='Voice input';
    if(micStream){micStream.getTracks().forEach(function(t){t.stop()});micStream=null}
    if(overlay)overlay.style.display='none';stopVis();return
  }
  if(!window.isSecureContext){
    toast('🔒 Voice needs HTTPS. Add http://localhost:9100 to chrome://flags/#unsafely-treat-insecure-origin-as-secure','error');
    return
  }
  if(!navigator.onLine){
    toast('🌐 No internet connection','error');
    speakError('Please check your network connection');
    return
  }
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){toast('Voice not supported in this browser','error');return}
  btn.innerHTML='⏹';btn.style.color='var(--r)';btn.title='Stop recording';initBars();
  // Show overlay IMMEDIATELY — before getUserMedia resolves
  if(overlay)overlay.style.display='block';
  var vt=$('voiceTranscript');if(vt)vt.textContent='Starting mic...';
  navigator.mediaDevices.getUserMedia({audio:true}).then(function(s){
    micStream=s;if(overlay)overlay.style.display='block';
    var t=$('voiceTranscript');if(t)t.textContent='Listening...';
    try{
      audioCtx=new(window.AudioContext||window.webkitAudioContext)();
      analyser=audioCtx.createAnalyser();analyser.fftSize=128;
      audioCtx.createMediaStreamSource(s).connect(analyser);startVis()
    }catch(e){}
    recognition=new SR();
    recognition.lang='en-US';recognition.interimResults=true;recognition.continuous=false;
    recognition.onresult=function(e){
      var txt=Array.from(e.results).map(function(r){return r[0].transcript}).join('');
      $('ci').value=txt;var vt=$('voiceTranscript');if(vt)vt.textContent=txt||'Listening...'
    };
    recognition.onend=function(){
      var t=$('ci').value.trim();btn.innerHTML='🎤';btn.style.color='';btn.title='Voice input';recognition=null;
      if(micStream){micStream.getTracks().forEach(function(x){x.stop()});micStream=null}
      if(overlay)overlay.style.display='none';stopVis();
      if(t){speakAck();setTimeout(function(){sendMsg(t,true)},500)}
    };
    recognition.onerror=function(e){
      btn.innerHTML='🎤';btn.style.color='';btn.title='Voice input';recognition=null;
      if(micStream){micStream.getTracks().forEach(function(x){x.stop()});micStream=null}
      if(overlay)overlay.style.display='none';stopVis();
      if(e.error==='not-allowed')toast('Microphone access denied','error');
      else if(e.error==='no-speech'){toast('No speech detected','error');speakError('No speech detected')}
      else toast('Voice error: '+e.error,'error')
    };
    recognition.start();btn.innerHTML='⏹';btn.style.color='var(--r)';btn.title='Stop recording';
    toast('🎤 Listening...','success')
  }).catch(function(e){
    btn.innerHTML='🎤';btn.style.color='';
    if(e.name==='NotAllowedError'||e.name==='PermissionDeniedError'){toast('🔒 Mic blocked. Voice needs HTTPS.','error');speakError('Microphone blocked, please check your browser settings')}
    else toast('Mic error: '+e.message,'error')
  })
};

function startVis(){
  if(!analyser||!voiceBars.length){initBars();if(!voiceBars.length)return}
  var d=new Uint8Array(analyser.frequencyBinCount),n=voiceBars.length;
  (function draw(){
    if(!analyser)return;analyser.getByteFrequencyData(d);
    var step=Math.floor(d.length/n);
    for(var i=0;i<n;i++){
      var idx=Math.min(i*step,d.length-1),val=d[idx]/255,h=Math.max(3,val*44),bar=voiceBars[i];
      if(bar){
        bar.style.height=h+'px';bar.style.opacity=0.2+val*0.8;
        bar.style.background=val<0.3?'var(--b)':val<0.6?'var(--g)':'var(--r)'
      }
    }
    visFrame=requestAnimationFrame(draw)
  })()
}
function stopVis(){
  if(visFrame)cancelAnimationFrame(visFrame);
  voiceBars.forEach(function(b){b.style.height='4px';b.style.opacity='.3';b.style.background='var(--b)'})
}

// ── Platform-aware Chat ─────────────────────────────────────────────────
// ── HERMES agent feed (one-way: agent → user; Supabase hermes_chat bus) ──
// The agent pushes status updates, decisions and market calls as agent rows.
// This panel polls and renders them as cards. Two-way chat + voice are hidden
// for now (2026-09-02) — the input bar is display:none in index.html.
var _hermesLastId=0;
var _hermesPollTimer=null;
var _hermesRows=[];
function _hfTime(iso){
  try{var d=new Date(iso);return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}catch(e){return ''}
}
function _hfTag(t){
  if(t==='advisory')return ['MARKET CALL','var(--y)','advisory'];
  if(t==='summary')return ['AI SUMMARY','var(--g)','summary'];
  if(t==='decision')return ['DECISION','var(--r)','decision'];
  return ['STATUS','var(--b)',''];
}
function hermesRender(rows){
  var msgs=$('chatMsgs');
  if(!msgs)return;
  var w=msgs.querySelector('.welcome');
  if(w&&rows.length)w.remove();
  var autoscroll=msgs.scrollHeight-msgs.scrollTop-msgs.clientHeight<80; // near bottom
  for(var i=0;i<rows.length;i++){
    var r=rows[i];
    if(!r||!(r.id>_hermesLastId))continue;
    if(r.role!=='agent')continue; // one-way: render agent messages only
    var card=document.createElement('div');
    var tag=_hfTag(r.msg_type);
    card.className='hf-item '+(r.msg_type||'');
    card.innerHTML='<div class="hf-head"><span style="color:'+tag[1]+';font-weight:700">🛰️ HERMES · '+tag[0]+'</span><span class="hf-time">'+_hfTime(r.created_at)+'</span></div><div class="hf-text">'+formatMsg(r.content||'')+'</div>';
    msgs.appendChild(card);
    _hermesLastId=Math.max(_hermesLastId,r.id);
  }
  if(autoscroll)msgs.scrollTop=msgs.scrollHeight;
}
function hermesPollOnce(){
  api('/api/hermes/messages?limit=40&platform='+activeTab).then(function(d){
    if(d&&d.messages){_hermesRows=d.messages;hermesRender(d.messages);refreshAllBotTickers()}
  })
}
function loadChatHistory(){
  var msgs=$('chatMsgs');
  msgs.innerHTML='<div class="loading" style="padding:24px;text-align:center;color:var(--t3)">⟳ Loading HERMES feed…</div>';
  _hermesLastId=0;
  api('/api/hermes/messages?limit=60&platform='+activeTab).then(function(d){
    var rows=(d&&d.messages)||[];
    _hermesRows=rows;
    msgs.innerHTML='';
    if(!rows.filter(function(r){return r.role==='agent'}).length){
      msgs.innerHTML='<div class="welcome"><div style="font-size:36px;opacity:.2">🛰️</div><p><b style="color:var(--t2)">HERMES Agent feed</b> — live status, decisions & market calls will appear here.</p></div>';
      return
    }
    hermesRender(rows)
  }).catch(function(){
    msgs.innerHTML='<div class="welcome"><div style="font-size:36px;opacity:.2">🛰️</div><p>HERMES feed unavailable.</p></div>'
  })
  if(_hermesPollTimer)clearInterval(_hermesPollTimer);
  _hermesPollTimer=setInterval(hermesPollOnce,5000);
}

// ── AI text correction for voice (clean up transcription artifacts) ─────
function correctVoiceText(raw){
  var t=raw.trim();
  if(!t)return '';
  // Remove repeated words and stutters
  t=t.replace(/\b(\w+)\s+\1\b/gi,'$1');
  // Remove trailing filler words
  t=t.replace(/\s+(um|uh|like|you know|actually|basically|literally)\s*$/gi,'');
  // Capitalize first letter
  t=t.charAt(0).toUpperCase()+t.slice(1);
  // Ensure terminal punctuation
  if(!/[.!?]$/.test(t))t+='.';
  return t
}

// ── Send message with platform context ──────────────────────────────────
window.sendMsg=async function(){}; // two-way chat hidden 2026-09-02

function escapeHtml(t){return String(t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function formatMsg(t){return escapeHtml(t).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\n/g,'<br>')}

// ── Text-to-Speech (Kokoro neural, macOS speechSynthesis fallback) ─────
window.speakMsg=function(t){
  // Toggle: click while speaking stops it
  if(_curSrc||window.speechSynthesis.speaking){stopAudio();return}
  var c=speakText(t);
  if(!c||c.length<5)return;
  kokoroSpeak(c)
};

window.clearChat=async function(){}; // hidden

// ── Settings: voice language picker ─────────────────────────────────────
var _voiceData={groups:{},default:'af_heart'};
window.toggleSettings=function(){
  var o=$('settingsOverlay');
  if(!o)return;
  var showing=!o.classList.contains('hidden');
  o.classList.toggle('hidden',showing);
  if(!showing){loadVoices()}
};
window.onVoiceGroupChange=function(){
  var g=$('voiceGroup'),sel=$('voiceSelect');
  var group=g.options[g.selectedIndex].value;
  var current=VOICE;
  sel.innerHTML='';
  (_voiceData.groups[group]||[]).forEach(function(v){
    var o=document.createElement('option');
    o.value=v;o.textContent=v+(v==='am_santa'||v==='em_santa'||v==='pm_santa'?' 🎅':v==='af_heart'?' ♥':'');
    sel.appendChild(o)
  });
  if([].slice.call(sel.options).some(function(o){return o.value===current})){
    sel.value=current
  }
  updateVoicePreview()
};
window.onVoiceChange=function(){
  var sel=$('voiceSelect');
  if(sel&&sel.value)VOICE=sel.value;   // applied live; persisted on "Save Voice"
  updateVoicePreview();
  speakPreview()
};
window.saveVoice=function(){
  if(!VOICE){toast('⚠ Pick a voice first','error');return}
  localStorage.setItem('gb_voice',VOICE);
  toast('✅ Voice saved: '+VOICE)
};
function updateVoicePreview(){
  var p=$('voicePreview');
  if(!p)return;
  var group=$('voiceGroup');
  var lang=group?group.options[group.selectedIndex].textContent:'-';
  p.innerHTML='<div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="color:var(--t3);font-size:10px;text-transform:uppercase">'+lang+'</span><span style="font-size:10px;color:var(--t3)">'+VOICE+'</span></div><div><span style="font-weight:600;color:var(--t)">"Sure, I just spawned your grid bot."</span></div><div style="margin-top:6px;display:flex;gap:6px"><button onclick="speakPreview()" style="background:var(--sf2);border:1px solid var(--br);color:var(--t2);padding:4px 10px;border-radius:5px;font-size:11px;cursor:pointer;font-family:inherit">▶ Preview</button></div>'
}
window.speakPreview=function(){
  if(_curSrc){stopAudio();return}
  playWav(ttsUrl("Sure, I just spawned your grid bot on Nifty futures"),function(){})
}
async function loadVoices(){
  var d=await api('/api/voices');
  if(d&&d.groups&&Object.keys(d.groups).length){
    _voiceData=d;
    var g=$('voiceGroup');
    g.innerHTML='';
    var order=['English (US)','English (UK)','Hindi','Spanish','Italian','French','Japanese','Portuguese','Chinese (Mandarin)'];
    var groups=d.groups;
    order.forEach(function(ln){
      if(!groups[ln])return;
      var o=document.createElement('option');
      o.value=ln;o.textContent=ln+' ('+groups[ln].length+')';
      g.appendChild(o)
    });
    Object.keys(groups).forEach(function(ln){
      if(order.indexOf(ln)>=0)return;
      var o=document.createElement('option');
      o.value=ln;o.textContent=ln+' ('+groups[ln].length+')';
      g.appendChild(o)
    });
    // Preselect the group containing the saved voice (default English US first)
    var savedGroup=null;
    Object.keys(groups).forEach(function(ln){if(groups[ln].indexOf(VOICE)>=0)savedGroup=ln});
    g.value=savedGroup||(groups['English (US)']?'English (US)':g.options[0].value);
    onVoiceGroupChange()
  }
}

// ── Main refresh ─────────────────────────────────────────────────────────
var _eaBook={positions:[],open_orders:[]};
function eaBookToLevels(book){
  var out=[];
  (book.positions||[]).forEach(function(p){
    out.push({level:out.length+1,kind:'pos',filled:true,type:p.side||'?',
      volume:+p.qty||0,price_open:+p.entry||0,mark_price:+p.mark||0,pnl:+p.upnl||0})
  });
  (book.open_orders||[]).forEach(function(o){
    out.push({level:out.length+1,kind:'grid',filled:false,type:o.side||'?',
      volume:+o.qty||0,price_open:+o.price||0,mark_price:0,pnl:0})
  });
  return out
}
async function refresh(){
  var d=await api('/health?platform='+encodeURIComponent(activeTab));
  if(!d)return;
  $('bootMode').textContent='Boot: '+(d.boot_mode||'--');
  bots=d.bots||[];
  _eaBook=d.ea_book||{positions:[],open_orders:[]};
  _botsLoaded=true;
  try{sessionStorage.setItem(botsCacheKey(activeTab),JSON.stringify(bots))}catch(e){}
  await renderAccounts();
  renderBotList();
  try{syncBacktestBoxes()}catch(e){}
// Backtest boxes auto-fill from the LIVE selected-env account (no hardcoded
// account assumptions): Eq$ = account equity, $ legs = equity × wallet% ÷
// ladder orders (floored at $5). Respects manual edits (only refills boxes
// still holding defaults or the last autofill).
var _autoEq=null,_autoLeg=null;
function syncBacktestBoxes(){
  var env=($('senv')||{}).value||'demo';
  var accts=(typeof acctCache!=='undefined'&&acctCache.binance)?acctCache.binance:[];
  var a=null;
  for(var i=0;i<accts.length;i++){if(accts[i].env===env){a=accts[i];break}}
  if(!a||!(a.equity>0))return;
  var orders=(_gridMode==='swing')?8:12;
  var leg=Math.max(5,Math.floor(a.equity*(_spawnWallet/100)/orders));
  var eq=Math.floor(a.equity);
  [['btEquity',eq,'btSize',leg],['btEquityM',eq,'btSizeM',leg]].forEach(function(ids){
    var eEl=$(ids[0]),sEl=$(ids[1]);
    if(eEl&&(eEl.value===''||eEl.value==='10000'||eEl.value==='1000'||Number(eEl.value)===_autoEq))eEl.value=eq;
    if(sEl&&(sEl.value===''||sEl.value==='1000'||sEl.value==='100'||Number(sEl.value)===_autoLeg))sEl.value=leg;
  });
  _autoEq=eq;_autoLeg=leg;
}
  if(!_chartFocusSym)applyGoldChartDefault();
  if(window.loadDeskChart)window.loadDeskChart(false)
}

// ── Activity chatter (per platform tab) ────────────────────────────────
var chatter={open:false,byPlat:{
  binance:{items:[],after:0,sum:null},
  mt5:{items:[],after:0,sum:null}
}};
window.toggleChatter=function(){
  chatter.open=!chatter.open;
  var l=$('chatterList');
  if(l)l.classList.toggle('open',chatter.open);
  var t=$('chatterTicker');
  if(t)t.classList.toggle('paused',chatter.open)
};
window.chatterSummarize=async function(btn){
  if(btn)btn.disabled=true;
  try{
    var plat=activeTab||'binance';
    var r=await api('/api/feed/summarize?platform='+plat+'&minutes=60');
    var c=chatter.byPlat[plat];
    c.sum=r&&r.summary?r.summary:'AI summary unavailable';
    renderChatterSum(plat)
  }finally{
    if(btn)setTimeout(function(){btn.disabled=false},2000)
  }
};
function renderChatterSum(plat){
  var el=$('chatterSummary');if(!el)return;
  var c=chatter.byPlat[plat];
  if(!c.sum){el.innerHTML='';return}
  el.innerHTML='<div class="chatter-sum"><span style="flex:1">✨ '+c.sum+'</span></div>'
}
async function pollChatter(){
  var plat=activeTab||'binance';
  if(!chatter.byPlat[plat])chatter.byPlat[plat]={items:[],after:0,sum:null};
  var c=chatter.byPlat[plat];
  var r=await api('/api/feed?platform='+plat+'&after='+c.after);
  if(r&&r.items&&r.items.length){
    c.items=c.items.concat(r.items);
    c.after=r.items[r.items.length-1].ts;
    if(c.items.length>80)c.items=c.items.slice(-80)
  }
  refreshAllBotTickers()
}
function botTickerData(sym){
  // Merge engine chatter (symbol-filtered) + HERMES agent rows for this symbol
  var out=[];
  sym=String(sym||'').toUpperCase();
  var plat=activeTab||'binance';
  var c=chatter.byPlat[plat];
  (c&&c.items||[]).forEach(function(i){
    if(!sym||String(i.symbol||'').toUpperCase()===sym)out.push({t:i.t,src:i.symbol||i.platform,text:i.text,kind:'act'})
  });
  (_hermesRows||[]).forEach(function(m){
    if(m.role!=='agent')return;
    var ms=((m.meta||{}).symbol||'').toUpperCase();
    if(sym&&ms===sym)out.push({t:_hfTime(m.created_at),src:'HERMES',text:m.content,kind:m.msg_type||'status'})
  });
  out.sort(function(a,b){return String(a.t).localeCompare(String(b.t))});
  return out.slice(-30)
}
function renderBotTicker(botId,sym){
  var el=document.getElementById('bt-'+botId);
  if(!el)return;
  var items=botTickerData(sym);
  if(!items.length){el.innerHTML='<span class="tick">No activity for '+sym+' yet…</span>';el.classList.remove('scrolling');return}
  var marq=items.slice(-8).map(function(i){return i.t+' · '+i.src+' · '+i.text}).join('   •   ');
  var content=marq+'&nbsp;&nbsp;•&nbsp;&nbsp;';
  el.innerHTML='<span class="tick">'+content+'</span><span class="tick">'+content+'</span>';
  var tick=el.querySelector('.tick'),track=el;
  var half=tick?tick.offsetWidth:0,box=el.clientWidth;
  el.classList.remove('scrolling');
  if(half>box){el.classList.add('scrolling');el.style.animationDuration=Math.max(20,Math.round(half/28))+'s'}
}
function refreshAllBotTickers(){
  (bots||[]).forEach(function(b){
    if(b.bot_id===expandedBot)renderBotTicker(b.bot_id,b.symbol)
  })
}

// ── Keyboard shortcut: hold M for 2s to toggle mic ─────────────────────
var mHoldTimer=null;
document.addEventListener('keydown',function(e){
  if(e.key==='m'||e.key==='M'){
    if(!mHoldTimer)mHoldTimer=setTimeout(function(){
      mHoldTimer=null;
      toggleVoice()
    },2000)
  }
});
document.addEventListener('keyup',function(e){
  if(e.key==='m'||e.key==='M'){if(mHoldTimer){clearTimeout(mHoldTimer);mHoldTimer=null}}
});

// ── Desk chart (TradingView Lightweight Charts, memory snapshot) ─────────
var _tv=null,_tvSeries=null,_tvLines=[],_tvSym='',_tvTf='',_lastBar=null,_lastLine=null,_tickBusy=false,_overlayLevels=[],_sessionMarks=[],_tvTrades=[];
var _chartGen=0,_chartFocusId='',_chartFocusSym='';
var _TF_SEC={ '1s':1,'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400};
function selectChartBot(id){
  var b=bots.find(function(x){return x.bot_id===id});
  _chartFocusId=id||'';
  _chartFocusSym=((b&&b.symbol)||'').toUpperCase();
  var label=$('chartSym');if(label)label.textContent=_chartFocusSym||'—';
}
function botOnTab(b){
  if(!b)return false;
  var p=(b.platform||'').toLowerCase();
  if(activeTab==='mt5')return p==='mt5';
  return !p||p==='binance'
}
function deskChartSymbol(){
  var focused=bots.find(function(x){return x.bot_id===_chartFocusId});
  if(focused&&botOnTab(focused)&&focused.symbol)return String(focused.symbol).toUpperCase();
  if(_chartFocusSym)return String(_chartFocusSym).toUpperCase();
  return platformGold()
}
function tvLib(){
  var LC=window.LightweightCharts;
  if(!LC||!LC.createChart)throw new Error('chart library not loaded');
  return LC
}
function destroyDeskChart(){
  _tvLines=[];
  _lastLine=null;
  _overlayLevels=[];
  _sessionMarks=[];
  clearTradeLines();
  var mk=$('chartMarks');if(mk)mk.innerHTML='';
  if(_tv){try{_tv.remove()}catch(e){}_tv=null;_tvSeries=null}
}
function sizeDeskChart(){
  var el=$('tvChart');if(!el||!_tv)return;
  var r=el.getBoundingClientRect();
  _tv.applyOptions({width:Math.max(80,Math.floor(r.width)),height:Math.max(80,Math.floor(r.height))});
  paintChartMarks()
}
function chartPriceFormat(px){
  px=Math.abs(Number(px)||0);
  if(px>=1000)return {type:'price',precision:2,minMove:0.01};
  if(px>=10)return {type:'price',precision:3,minMove:0.001};
  if(px>=1)return {type:'price',precision:4,minMove:0.0001};
  return {type:'price',precision:6,minMove:0.000001}
}
function clearChartLines(){
  if(_tvSeries){
    _tvLines.forEach(function(row){
      var ln=row&&row.ln?row.ln:row;
      try{_tvSeries.removePriceLine(ln)}catch(e){}
    });
    if(_lastLine){try{_tvSeries.removePriceLine(_lastLine)}catch(e){}}
  }
  _tvLines=[];
  _lastLine=null
}
function overlayKey(lv){
  var filled=!!lv.filled||lv.kind==='pos';
  var side=(lv.side||'').toUpperCase()||(filled?'POS':'?');
  return side+':'+(filled?'1':'0')+':'+Number(lv.price||0).toFixed(8)
}
function fmtOverlayPnl(n){
  var x=Number(n||0);
  return (x>=0?'+$':'-$')+Math.abs(x).toFixed(2)
}
function chartIsMax(){
  var a=$('chatArea');
  return !!(a&&a.classList.contains('chart-max'))
}
function overlaySpec(lv, LC){
  var kind=(lv.kind||'grid');
  var buy=(lv.side||'')==='BUY'||(lv.side||'')==='LONG';
  var color=kind==='sl'?'#eab308':(kind==='tp'?'#818cf8':(buy?'#22c55e':'#ef4444'));
  var title=kind==='sl'?'SL':(kind==='tp'?'TP':(buy?'BUY':'SELL'));
  return {
    price:Number(lv.price),
    color:color,
    lineWidth:kind==='grid'?2:1,
    lineStyle:(LC.LineStyle?LC.LineStyle.Dashed:2),
    axisLabelVisible:false,
    title:title
  }
}
function overlayLegend(lv){
  if(lv.filled||lv.kind==='pos')return '';
  var buy=(lv.side||'')==='BUY'||(lv.side||'')==='LONG';
  var extra=lv.dist_pct?((lv.dist_pct>=0?'+':'')+Number(lv.dist_pct).toFixed(2)+'%'):'wait';
  return '<span class="chart-chip '+(buy?'buy':'sell')+'">'+(buy?'B ':'S ')+Number(lv.price).toPrecision(6)+' · '+extra+'</span>'
}
function levelsFromRunningBots(sym){
  var out=[];
  sym=String(sym||'').toUpperCase();
  function add(b){
    (b&&b.levels_detail||[]).forEach(function(l){
      var k=(l.kind||'grid');
      if(k==='sl'||k==='tp')return;
      var side=(l.type||l.leg||'').toUpperCase();
      if(side==='LONG')side='BUY';
      if(side==='SHORT')side='SELL';
      var px=+l.price_open||+l.price||0;
      if(px>0)out.push({side:side,price:px,qty:+l.volume||0,filled:k==='pos',kind:k,pnl:+l.pnl||0,dist_pct:+l.dist_pct||0})
    })
  }
  var match=bots.filter(function(x){return x.status==='running'&&botOnTab(x)});
  var hit=match.find(function(x){return String(x.symbol||'').toUpperCase()===sym});
  if(hit)add(hit);
  else if(match[0])add(match[0]);
  return out
}
function bucketMarkTime(t){
  var s=_TF_SEC[(($('chartTf')||{}).value)||'1m']||60;
  t=Number(t||0);
  if(!(t>0))return 0;
  if(t>1e12)t=Math.floor(t/1000);
  return Math.floor(t/s)*s
}
function clearTradeLines(){
  (_tvTrades||[]).forEach(function(s){try{_tv.removeSeries(s)}catch(e){}});
  _tvTrades=[]
}
function paintTradeLines(marks){
  clearTradeLines();
  if(!_tv||!chartIsMax())return;
  var LC=null;
  try{LC=tvLib()}catch(e){return}
  (marks||[]).forEach(function(m){
    if((m.kind||'')!=='close')return;
    var t1=bucketMarkTime(m.open_time), t2=bucketMarkTime(m.time);
    var p1=Number(m.open_price), p2=Number(m.price);
    if(!(t1>0&&t2>0&&p1>0&&p2>0)||t1===t2)return;
    var col=Number(m.pnl||0)>=0?'#22c55e':'#ef4444';
    try{
      var s=_tv.addLineSeries({
        color:col, lineWidth:1,
        lineStyle:(LC.LineStyle?LC.LineStyle.Dotted:1),
        lastValueVisible:false, priceLineVisible:false,
        crosshairMarkerVisible:false
      });
      var a=t1<t2?{time:t1,value:p1}:{time:t2,value:p2};
      var b=t1<t2?{time:t2,value:p2}:{time:t1,value:p1};
      s.setData([a,b]);
      _tvTrades.push(s)
    }catch(e){}
  })
}
function paintChartMarks(){
  var el=$('chartMarks');
  if(!chartIsMax()){
    if(el)el.innerHTML='';
    try{if(_tvSeries&&_tvSeries.setMarkers)_tvSeries.setMarkers([])}catch(e){}
    clearTradeLines();
    return
  }
  if(!el||!_tvSeries){if(el)el.innerHTML='';return}
  var levels=_overlayLevels||[];
  var sess=_sessionMarks||[];
  var w=el.clientWidth||0, h=el.clientHeight||0;
  if(w<20||h<20){el.innerHTML='';return}
  var html='';
  var marks=[];
  // Left labels for working LIMIT / SL / TP (fixed to price, not last candle)
  levels.forEach(function(lv){
    var px=Number(lv.price);
    if(!(px>0))return;
    var kind=lv.kind||'grid';
    if(kind==='pos')return;
    var y=null;
    try{y=_tvSeries.priceToCoordinate(px)}catch(e){y=null}
    if(y==null||y<0||y>h)return;
    var buy=(lv.side||'')==='BUY'||(lv.side||'')==='LONG';
    var cls=kind==='sl'?'sl':(kind==='tp'?'tp':(buy?'buy':'sell'));
    var lab=kind==='sl'?'SL':(kind==='tp'?'TP':(buy?'BUY':'SELL'));
    html+='<div class="chart-plabel '+cls+'" style="top:'+y+'px">'+lab+' '+px.toFixed(px>=100?2:4)+'</div>'
  });
  // Open/close pinned to the fill bar (TV setMarkers: time = that candle)
  sess.forEach(function(m){
    var px=Number(m.price), t=bucketMarkTime(m.time);
    if(!(px>0)||!(t>0))return;
    var buy=(m.side||'')==='BUY'||(m.side||'')==='LONG'||(m.kind||'')==='open'&&(m.side||'')==='BUY';
    if((m.kind||'')==='close')buy=(m.open_side||m.side||'')==='BUY';
    var isOpen=(m.kind||'')==='open';
    var x=null,y=null;
    try{x=_tv.timeScale().timeToCoordinate(t)}catch(e){x=null}
    try{y=_tvSeries.priceToCoordinate(px)}catch(e){y=null}
    if(x!=null&&y!=null&&y>=-8&&y<=h+8&&x>=0&&x<=w){
      var cls=(isOpen?'':'pos ')+(buy?'buy':'sell');
      var arr=isOpen?(buy?'▲':'▼'):(buy?'▼':'▲');
      var lab=isOpen?'':('<span class="pnl">'+fmtOverlayPnl(m.pnl)+'</span>');
      html+='<div class="chart-tri '+cls+'" style="left:'+x+'px;top:'+y+'px"><span class="arr">'+arr+'</span>'+lab+'</div>'
    }
    marks.push({
      time:t,
      position:isOpen?(buy?'belowBar':'aboveBar'):(buy?'aboveBar':'belowBar'),
      color:isOpen?(buy?'#22c55e':'#ef4444'):(Number(m.pnl||0)>=0?'#22c55e':'#ef4444'),
      shape:isOpen?(buy?'arrowUp':'arrowDown'):'circle',
      text:isOpen?(buy?'B':'S'):(fmtOverlayPnl(m.pnl))
    })
  });
  marks.sort(function(a,b){return a.time-b.time});
  el.innerHTML=html;
  try{if(_tvSeries.setMarkers)_tvSeries.setMarkers(marks)}catch(e){}
  paintTradeLines(sess)
}
function syncGridOverlays(LC, levels, last, replaceAll){
  // Keep existing price lines until new overlay data arrives or the
  // ladder actually changes. Remove+recreate is what made the grid blink.
  if(!_tvSeries)return;
  if(!chartIsMax()){
    clearChartLines();
    clearTradeLines();
    var el=$('chartMarks');if(el)el.innerHTML='';
    try{if(_tvSeries.setMarkers)_tvSeries.setMarkers([])}catch(e){}
    if(last>0){
      if(_lastLine && _lastLine.applyOptions){
        try{_lastLine.applyOptions({price:last,title:fmtLiveTick(last)})}catch(e){}
      }
    }
    return
  }
  if(replaceAll)clearChartLines();
  var raw=levels||[];
  _overlayLevels=raw;
  var incoming=raw.filter(function(lv){
    if(lv.filled||lv.kind==='pos')return false;
    var k=lv.kind||'grid';
    if(k==='sl'||k==='tp')return false;
    var lastPx=last||(_lastBar&&_lastBar.close)||0;
    var px=Number(lv.price)||0;
    if(lastPx>0&&px>0&&Math.abs(px-lastPx)/lastPx>0.02)return false;
    return true
  });
  if(!raw.length && _tvLines.length && !replaceAll){
    if(last>0 && _lastLine && _lastLine.applyOptions){
      try{_lastLine.applyOptions({price:last})}catch(e){}
    }
    paintChartMarks();
    return
  }
  var next={};
  incoming.forEach(function(lv){
    if(!(Number(lv.price)>0))return;
    next[overlayKey(lv)]=lv
  });
  var kept=[];
  _tvLines.forEach(function(row){
    var lv=next[row.key];
    if(!lv){
      try{_tvSeries.removePriceLine(row.ln)}catch(e){}
      return
    }
    try{row.ln.applyOptions(overlaySpec(lv, LC))}catch(e){}
    kept.push(row);
    delete next[row.key]
  });
  Object.keys(next).forEach(function(k){
    var ln=_tvSeries.createPriceLine(overlaySpec(next[k], LC));
    kept.push({key:k, ln:ln})
  });
  _tvLines=kept;
  if(last>0){
    if(_lastLine && _lastLine.applyOptions){
      try{_lastLine.applyOptions({price:last})}catch(e){}
    }else{
      _lastLine=_tvSeries.createPriceLine({
        price:last,color:'#6366f1',lineWidth:1,
        lineStyle:LC.LineStyle?LC.LineStyle.Dotted:1,
        axisLabelVisible:true,title:fmtLiveTick(last)
      })
    }
  }
  var legend=$('chartLegend');
  if(legend && incoming.length){
    legend.innerHTML=incoming.filter(function(lv){return Number(lv.price)>0}).map(overlayLegend).join('')
  }else if(legend && replaceAll){
    legend.innerHTML='<span style="font-size:9px;color:var(--t3)">No grid on this symbol</span>'
  }
  paintChartMarks()
}
function addCandleSeries(LC){
  return _tv.addCandlestickSeries({
    upColor:'#22c55e',downColor:'#ef4444',
    borderVisible:false,wickUpColor:'#22c55e',wickDownColor:'#ef4444'
  })
}
function fitChartToData(){
  // TradingView Lightweight Charts: fitContent() is TIME only. After a
  // symbol change (BTC ~100k → XAU ~3k) re-enable price autoScale or the
  // new candles render as a flat line on the old axis.
  // https://tradingview.github.io/lightweight-charts/docs/api/interfaces/PriceScaleOptions
  if(!_tv||!_tvSeries)return;
  try{
    _tv.priceScale('right').applyOptions({
      autoScale:true,
      scaleMargins:{top:0.08,bottom:0.12}
    })
  }catch(e){}
  try{
    if(_tvSeries.priceScale)_tvSeries.priceScale().applyOptions({autoScale:true})
  }catch(e){}
  try{_tv.timeScale().fitContent()}catch(e){}
}
function applyChartMax(on){
  var area=$('chatArea');if(!area)return;
  area.classList.toggle('chart-max', !!on);
  var btn=$('chartMaxBtn');
  if(btn){
    btn.textContent=on?'✕':'⛶';
    btn.title=on?'Minimize chart to corner':'Expand chart'
  }
  try{localStorage.setItem('gb.chart.max', on?'1':'0')}catch(e){}
  setTimeout(function(){sizeDeskChart();if(window.paintChartMarks)paintChartMarks()},40)
}
window.toggleChartMax=function(){
  var area=$('chatArea');if(!area)return;
  applyChartMax(!area.classList.contains('chart-max'))
};
function initChartMax(){
  var saved=null;
  try{saved=localStorage.getItem('gb.chart.max')}catch(e){}
  applyChartMax(saved!=='0')
}
window.loadDeskChart=async function(force){
  var el=$('tvChart'), empty=$('chartEmpty');
  if(!el||!isLoggedIn())return;
  var gen=++_chartGen;
  var prevSym=_tvSym;
  var sym=deskChartSymbol();
  var tf=(($('chartTf')||{}).value)||'1m';
  var label=$('chartSym');if(label)label.textContent=sym||'—';
  try{
    var LC=tvLib();
    var d=await api('/api/chart?symbol='+encodeURIComponent(sym)+'&platform='+encodeURIComponent(activeTab)+'&interval='+encodeURIComponent(tf));
    if(gen!==_chartGen)return;
    if(d&&d.symbol&&d.symbol!==sym)return;
    if(!d){
      if(empty){empty.style.display='flex';empty.textContent='Chart data unavailable (auth?)'}
      return
    }
    var pnlEl=$('chartPnl');
    if(pnlEl){
      if(d.bot){
        var p=d.pnl||0;
        pnlEl.textContent=(p>=0?'+':'')+p.toFixed(2);
        pnlEl.style.color=p>=0?'var(--g)':'var(--r)'
      }else{pnlEl.textContent='';pnlEl.style.color=''}
    }
    var candles=(d.candles||[]).filter(function(c){return c.time&&c.close>0});
    if(!candles.length){
      if(empty){empty.style.display='flex';empty.textContent='No candles in memory for '+sym}
      return
    }
    if(empty)empty.style.display='none';
    if(!_tv){
      _tv=LC.createChart(el,{
        layout:{background:{color:'#0e0e12'},textColor:'#8a8a9a'},
        grid:{vertLines:{color:'#1e1e28'},horzLines:{color:'#1e1e28'}},
        rightPriceScale:{
          borderColor:'#1e1e28',
          autoScale:true,
          scaleMargins:{top:0.08,bottom:0.12}
        },
        timeScale:{borderColor:'#1e1e28',timeVisible:true,secondsVisible:false},
        crosshair:{mode:LC.CrosshairMode?LC.CrosshairMode.Normal:0},
        handleScroll:true,handleScale:true
      });
      _tvSeries=addCandleSeries(LC);
      if(!_tv._ro){
        _tv._ro=new ResizeObserver(function(){sizeDeskChart()});
        _tv._ro.observe(el)
      }
      if(!_tv._markSub){
        _tv._markSub=true;
        try{_tv.timeScale().subscribeVisibleLogicalRangeChange(function(){paintChartMarks()})}catch(e){}
        try{_tv.timeScale().subscribeVisibleTimeRangeChange(function(){paintChartMarks()})}catch(e){}
      }
    }
    var instrumentChanged=prevSym!==sym;
    if(instrumentChanged){
      _lastTps=0;_lastTickTape=[];_sessionMarks=[];_overlayLevels=[];
      _lastBar=null;_lastLine=null;
      clearChartLines();
      clearTradeLines();
      var mk=$('chartMarks');if(mk)mk.innerHTML='';
      try{if(_tvSeries&&_tvSeries.setMarkers)_tvSeries.setMarkers([])}catch(e){}
    }
    var tfChanged=_tvTf!==tf;
    var rescaled=force||instrumentChanged||tfChanged||!_tvSeries;
    if(rescaled){
      if(_tvSeries&&instrumentChanged){
        try{_tv.removeSeries(_tvSeries)}catch(e){}
        _tvSeries=addCandleSeries(LC)
      }
      if(d.last>0){
        var lb=candles[candles.length-1];
        lb.high=Math.max(lb.high,d.last);
        lb.low=Math.min(lb.low,d.last);
        lb.close=d.last
      }
      var lastPx=candles[candles.length-1].close;
      try{_tvSeries.applyOptions({priceFormat:chartPriceFormat(lastPx)})}catch(e){}
      _tvSeries.setData(candles);
      if(instrumentChanged||tfChanged)fitChartToData();
      _tvTf=tf;
      _lastBar=candles[candles.length-1]||null
    }else{
      var nb=candles[candles.length-1];
      if(nb){
        _tvSeries.update(nb);
        _lastBar=nb
      }
    }
    var levels=d.levels||[];
    if(!levels.length)levels=levelsFromRunningBots(sym);
    _sessionMarks=d.marks||[];
    syncGridOverlays(LC, levels, d.last||0, instrumentChanged);
    if(d.last>0){
      if(instrumentChanged)paintLiveTick(d.last);
      else applyLiveTick(d.last,tf)
    }
    sizeDeskChart();
    _tvSym=sym;
    requestAnimationFrame(function(){paintChartMarks()})
  }catch(e){
    if(empty){empty.style.display='flex';empty.textContent='Chart unavailable: '+String(e&&e.message||e).slice(0,80)}
  }
};
function tfBucket(tf,ts){
  var s=_TF_SEC[tf]||60;
  ts=ts||Math.floor(Date.now()/1000);
  return Math.floor(ts/s)*s
}
var _lastTps=0,_lastTickTape=[];
function fmtLiveTick(px,tps){
  if(!(px>0))return '';
  var n=(tps==null||tps==='')?_lastTps:Number(tps);
  if(isFinite(n))_lastTps=Math.max(0,n);
  var f=chartPriceFormat(px);
  var s=Number(px).toFixed(f.precision);
  return s+'  '+_lastTps+' ticks/s'
}
function paintTickTape(ticks){
  if(Array.isArray(ticks))_lastTickTape=ticks;
  var tape=$('chartTickTape');if(!tape)return;
  if(!_lastTickTape.length){
    tape.innerHTML='<div class="tick-empty">no prints this second</div>';
    return
  }
  var f=chartPriceFormat(_lastTickTape[0]||0);
  tape.innerHTML=_lastTickTape.map(function(px){
    return '<div>'+Number(px).toFixed(f.precision)+'</div>'
  }).join('')
}
function paintLiveTick(px,tps,ticks){
  var txt=fmtLiveTick(px,tps);
  var el=$('chartTick');if(el)el.textContent=txt;
  paintTickTape(ticks);
  if(_lastLine&&_lastLine.applyOptions){
    try{_lastLine.applyOptions({price:px,title:txt})}catch(e){}
  }
}
function applyLiveTick(px,tf,tps,ticks){
  if(!_tvSeries||!(px>0))return;
  // Ignore a tick from a different instrument while the series is still
  // on the previous scale (BTC last vs XAU last).
  if(_lastBar&&_lastBar.close>0){
    var r=px/_lastBar.close;
    if(r>3||r<1/3)return
  }
  tf=tf||(($('chartTf')||{}).value)||'1m';
  var t=tfBucket(tf);
  var step=_TF_SEC[tf]||60;
  if(!_lastBar){
    _lastBar={time:t,open:px,high:px,low:px,close:px}
  }else if(_lastBar.time>0 && Math.abs(t-_lastBar.time)<=step*2){
    _lastBar.high=Math.max(_lastBar.high,px);
    _lastBar.low=Math.min(_lastBar.low,px);
    _lastBar.close=px
  }else if(t>_lastBar.time){
    _lastBar={time:t,open:px,high:px,low:px,close:px}
  }else{
    _lastBar.high=Math.max(_lastBar.high,px);
    _lastBar.low=Math.min(_lastBar.low,px);
    _lastBar.close=px
  }
  try{_tvSeries.update(_lastBar)}catch(e){}
  paintLiveTick(px,tps,ticks);
  paintChartMarks()
}
async function tickDeskChart(){
  if(_tickBusy||!isLoggedIn()||!_tvSeries)return;
  var sym=deskChartSymbol();
  if(!sym||(_tvSym&&sym!==_tvSym))return;
  _tickBusy=true;
  try{
    // 2.5s cap — long `api()` timeouts made kline stalls freeze the tape.
    var ctl=new AbortController();
    var to=setTimeout(function(){try{ctl.abort()}catch(e){}},2500);
    var r=await fetch(API+'/api/chart/tick?symbol='+encodeURIComponent(sym)+
      '&platform='+encodeURIComponent(activeTab),{
        headers:adminHeaders(), signal:ctl.signal
      });
    clearTimeout(to);
    var d=null;
    try{d=await r.json()}catch(e){d=null}
    if(d&&d.last>0)applyLiveTick(d.last,(($('chartTf')||{}).value)||'1m',d.tps,d.ticks)
  }catch(e){}
  _tickBusy=false
}

// ── MT5 platform (EA bridge) ─────────────────────────────────────────────
var MT5_SYMS=[];

function mt5Badge(txt,color){
  var b=$('mt5ConnBadge'), s=$('mt5SettingsBadge');
  if(b){b.textContent=txt;b.style.color=color}
  if(s){s.textContent=txt;s.style.color=color}
}

var _mt5AlertAt=0;
function applyMt5Alert(d){
  var al=d&&d.alert;
  if(!al||!al.msg||!al.at)return;
  if(al.at<=_mt5AlertAt)return;
  _mt5AlertAt=al.at;
  toast(al.msg,'error')
}
window.loadMt5Status=async function(){
  try{
    var d=await api('/api/mt5/status');
    if(!d){mt5Badge('· status failed','var(--r)');return}
    if(d.token){var tf=$('mt5Token');if(tf)tf.value=d.token}   // auto-populate
    applyMt5Alert(d);
    _eaBook={positions:d.positions||[],open_orders:d.open_orders||[]};
    if(activeTab==='mt5')renderBotList();
    if(!d.has_token){mt5Badge('no token','var(--r)');return}
    var st=d.ea_state||(d.connected?'live':'lost');
    if(st==='lost'||st==='unknown'||!d.connected){
      var age=d.last_seen_s;
      if(age==null)mt5Badge('EA offline — waiting for next POST','var(--y)');
      else mt5Badge('EA lost '+Math.round(age)+'s — bots will stop','var(--r)');
      return
    }
    var a=d.account||{};
    var mode=(a.trade_mode||'demo').toUpperCase();
    if(st==='stale'||st==='quiet'){
      mt5Badge('EA quiet '+Math.round(d.last_seen_s||0)+'s — waiting','var(--y)')
    }else{
      mt5Badge('· 🟢 '+mode+' · '+(a.login||'?')+' · '+(a.server||''),'var(--g)')
    }
    var card=$('mt5AccountCard');
    if(card)card.innerHTML='<b>'+mode+'</b> · login '+(a.login||'?')+'<br>'+(a.server||'')+' · '+(a.company||'')+
      '<br>Equity '+(a.equity||0)+' '+(a.currency||'')+' · Balance '+(a.balance||0)+
      '<br>Leverage 1:'+(a.leverage||'?')+' · '+d.symbols_count+' symbols';
    var bar=$('mt5InfoBar');
    if(bar){bar.style.display='block';
      bar.innerHTML='EA connected ('+mode+') — ServerURL: <b>'+d.ea_server_url+'</b> · AuthToken: Settings ▸ MT5'}
    window.MT5_MODE=mode;   // auto-detected — used by spawn/GuruAI
  }catch(e){mt5Badge('· status failed','var(--r)')}
};

window.testMt5Connection=async function(){
  toast('🔌 Testing MT5 connection…');
  var r=await api('/api/mt5/test',{});
  if(r&&r.ok){
    var a=r.account||{};
    toast('✅ Connection successful — '+((a.trade_mode||'demo').toUpperCase())+' · login '+(a.login||'?')+' · '+(a.server||'')+' · '+r.symbols_count+' symbols')
  }else{
    toast('❌ Connection failed — '+((r&&r.error)||'try again'),'error')
    if(r&&r.error)console.warn('MT5 test', r.error)
  }
};

window.resetMt5Token=async function(){
  if(!confirm('Rotate the MT5 token?\n\nThe OLD token stops working immediately — you must paste the NEW token into the EA AuthToken input.'))
    return;
  var r=await api('/api/mt5/token',{});
  if(r&&r.token){
    var t=$('mt5Token');if(t)t.value=r.token;
    toast('♻️ Token rotated — update the EA AuthToken input now')
  }else toast('❌ Token reset failed','error')
};

// ── MT5 symbol picker (broker list + EA-status marks + always-on seeds) ─
var MT5_READY={};   // symbol -> true when an EA instance posts it live
var MT5_SEED=['XAUUSD','XAGUSD','EURUSD','GBPUSD','USDJPY','USDCHF','USDCAD','AUDUSD','NZDUSD','BTCUSD','ETHUSD','US30','NAS100','GER40','UK100'];

function mt5SymStatus(){
  var inp=$('mt5Sym');
  var sym=((inp&&inp.value)||'').trim().toUpperCase();
  var el=$('mt5SymStatus');
  if(!el)return;
  if(!sym){el.textContent=MT5_SYMS.length?('· '+MT5_SYMS.length+' symbols'):'·';return}
  if(MT5_READY[sym]){el.textContent='🟢 EA ready';el.style.color='var(--g)'}
  else{el.textContent='⚪ type or pick — attach EA to this chart';el.style.color='var(--y)'}
}

function _mt5Name(s){
  if(!s)return '';
  if(typeof s==='string')return s;
  return s.name||s.symbol||s.Symbol||s.chart_symbol||''
}
function _mt5MergeSyms(a){
  var out=[],seen={};
  (a||[]).forEach(function(s){
    var n=_mt5Name(s);
    if(!n||seen[n])return;
    seen[n]=true;out.push(n)
  });
  return out
}
window.loadMt5Symbols=async function(){
  var d=null,st=null;
  try{d=await api('/api/mt5/symbols')}catch(e){}
  try{st=await api('/api/mt5/status')}catch(e){}
  MT5_READY={};
  var assigned=_mt5MergeSyms((d&&(d.assigned||d.assigned_symbols))||(st&&st.assigned_symbols)||[]);
  var charts=_mt5MergeSyms((d&&d.charts)||[]);
  assigned.concat(charts).forEach(function(n){if(n)MT5_READY[n]=true});
  var fromApi=_mt5MergeSyms(d&&d.symbols);
  var fromBots=bots.filter(function(b){return b.platform==='mt5'&&b.symbol}).map(function(b){return b.symbol});
  var cached=[];
  try{cached=JSON.parse(localStorage.getItem('gb.mt5.symbols')||'[]')}catch(e){cached=[]}
  MT5_SYMS=_mt5MergeSyms(assigned.concat(charts, fromApi, fromBots, cached, MT5_SEED));
  if(MT5_SYMS.length)try{localStorage.setItem('gb.mt5.symbols',JSON.stringify(MT5_SYMS.slice(0,400)))}catch(e){}
  var inp=$('mt5Sym');
  if(inp&&!(inp.value||'').trim()){
    if(assigned.length)inp.value=assigned[0]
    else if(charts.length)inp.value=charts[0]
    else inp.value='XAUUSD'
  }
  mt5SymStatus();
  return MT5_SYMS
};

window.genMt5Token=async function(){
  var r=await api('/api/mt5/token',{});
  if(r&&r.token){var t=$('mt5Token');if(t)t.value=r.token;toast('✅ MT5 token generated — copy it into the EA AuthToken input')}
  else toast('❌ Token generation failed','error')
};

window.copyMt5Token=function(){
  var t=$('mt5Token');if(!t||!t.value){toast('Generate a token first','error');return}
  t.select();document.execCommand('copy');toast('📋 Token copied')
};
window.fillMt5ServerUrl=function(){
  var base=(typeof API==='string'&&API?API.replace(/\/+$/,''):(lsGet2(SERVER_URL_KEY)||'').replace(/\/+$/,''));
  if(!base){toast('Connect a trading server first','error');return}
  var u=base+'/api/mt5';var el=$('mt5ServerUrl');if(el)el.value=u;
  try{localStorage.setItem('gb.mt5.serverurl',u)}catch(e){}
  toast('✅ ServerURL filled: '+u)
};
window.copyMt5ServerUrl=function(){
  var t=$('mt5ServerUrl');if(!t||!t.value){toast('Fill the URL first','error');return}
  t.select();document.execCommand('copy');toast('📋 ServerURL copied')
};
(function(){try{var s=localStorage.getItem('gb.mt5.serverurl');var el=$('mt5ServerUrl');if(s&&el&&!el.value)el.value=s}catch(e){}})();


function renderMt5SymbolList(filter){
  var list=$('mt5SymList');if(!list)return;
  if(!MT5_SYMS.length)MT5_SYMS=MT5_SEED.slice();
  var q=(filter||'').toUpperCase();
  var src=MT5_SYMS.slice();
  src.sort(function(a,b){return (MT5_READY[b]?1:0)-(MT5_READY[a]?1:0)});
  var hits=src.filter(function(s){return !q||String(s).toUpperCase().indexOf(q)>=0}).slice(0,80);
  if(!hits.length){
    list.innerHTML='<div class="combo-empty">No match — type the MT5 name (e.g. XAUUSD) and spawn anyway</div>';
    list.classList.add('open');
    return
  }
  list.innerHTML=hits.map(function(s){
    return '<div class="combo-item" data-sym="'+s+'">'+(MT5_READY[s]?'🟢 ':'⚪ ')+s+'</div>'
  }).join('');
  list.querySelectorAll('.combo-item').forEach(function(el){
    el.onmousedown=function(e){e.preventDefault();e.stopPropagation();pickMt5Sym(el.getAttribute('data-sym'))}
  });
  list.classList.add('open')
}
function pickMt5Sym(s){
  var inp=$('mt5Sym');if(inp)inp.value=s;
  closeMt5List();mt5SymStatus();
  _chartFocusId='';_chartFocusSym=(s||'').toUpperCase();
  if(window.loadDeskChart)window.loadDeskChart(true)
}
function closeMt5List(){var l=$('mt5SymList');if(l)l.classList.remove('open')}
(function(){
  var inp=document.getElementById('mt5Sym');
  if(!inp)return;
  function openList(){
    var list=$('mt5SymList');
    if(list&&!MT5_SYMS.length){
      list.innerHTML='<div class="combo-empty">Loading symbols…</div>';
      list.classList.add('open')
    }else{
      renderMt5SymbolList(inp.value)
    }
  }
  inp.addEventListener('focus',async function(){
    openList();
    await loadMt5Symbols();
    renderMt5SymbolList(inp.value)
  });
  inp.addEventListener('input',function(){renderMt5SymbolList(inp.value);mt5SymStatus()});
  inp.addEventListener('click',function(e){e.stopPropagation();openList()});
  document.addEventListener('click',function(e){if(!e.target.closest('#mt5SymCombo'))closeMt5List()})
})();

window.spawnMt5=async function(){
  var sym=($('mt5Sym').value||'').trim().toUpperCase();
  if(!sym){toast('❌ Pick an MT5 symbol first','error');return}
  var btn=document.querySelector('#formMt5 .btn-primary');
  var orig=btn?btn.textContent:'＋ Spawn';
  if(btn){btn.disabled=true;btn.textContent='⏳ Spawning…'}
  var r=await api('/spawn',{name:($('mt5Name').value||'').trim()||'MT5',symbol:sym,
    market_type:'futures',fee_mode:'maker',platform:'mt5',env:(window.MT5_MODE||'demo').toLowerCase()});
  if(r&&r.bot_id){
    toast('✅ MT5 spawned: '+sym);
    _chartFocusId=r.bot_id;_chartFocusSym=sym;
    await refresh();
    if(window.loadDeskChart)window.loadDeskChart(true)
  }else toast('❌ '+(r&&r.detail?r.detail:'MT5 spawn failed'),'error');
  if(btn){btn.disabled=false;btn.textContent=orig}
};

window.closeAllMt5=async function(btn){
  if(!confirm('✕ Close ALL MT5 positions & orders?'))return;
  var orig=btn.textContent;btn.disabled=true;btn.textContent='⏳ Closing…';
  var syms=[];
  bots.forEach(function(b){if(b.platform==='mt5'&&syms.indexOf(b.symbol)<0)syms.push(b.symbol)});
  var r=await api('/api/mt5/close-all',{symbols:syms});
  if(r&&r.ok)toast('✅ MT5 closed: '+r.closed+' position(s)');
  else toast('❌ Close failed','error');
  btn.disabled=false;btn.textContent=orig;refresh()
};

// ── MT5 GuruAI (DynamicGuruAI + risk layer) ─────────────────────────
window.startMt5Guru=async function(){
  var sym=$('mt5Sym').value.trim().toUpperCase();
  if(!sym){toast('❌ Pick an MT5 symbol first','error');return}
  var btn=$('mt5GuruBtn');
  var orig=btn.textContent;btn.disabled=true;btn.textContent='⏳ Starting…';
  var ftmo=!($('mt5Ftmo')&&!$('mt5Ftmo').checked);
  var r=await api('/api/mt5/guru/start',{symbol:sym,ftmo_rules:ftmo});
  if(r&&r.ok){
    toast('🧠 MT5 GuruAI started: '+r.symbol+' ('+(r.env||'').toUpperCase()+
          ') qty='+r.qty+' · levels='+r.levels+' · '+(r.ftmo_rules!==false?'proprules ON':'clean GuruAI'));
    btn.textContent='🧠 Running';
    _chartFocusSym=(r.symbol||sym).toUpperCase();
    await refresh();
    if(window.loadDeskChart)window.loadDeskChart(true)
  }else{
    toast('❌ '+(r&&r.detail?r.detail:'MT5 GuruAI start failed'),'error');
    btn.disabled=false;btn.textContent=orig
  }
};

window.killMt5=async function(){
  if(!confirm('🛑 EMERGENCY KILL SWITCH?\n\nClose ALL MT5 positions, cancel ALL orders and stop MT5 GuruAI.'))return;
  var btn=$('mt5KillBtn');
  var orig=btn.textContent;btn.disabled=true;btn.textContent='⏳ Killing…';
  try{
    var r=await api('/api/mt5/kill',{});
    if(r&&r.ok){toast('🛑 MT5 killed — '+r.closed+' position(s) closed, '+((r.symbols||[]).length)+' symbol(s) swept')}
    else toast('❌ Kill failed','error');
  }finally{
    btn.disabled=false;btn.textContent=orig||'🛑 KILL';
  }
  // Clear slate for the MT5 desk
  try{sessionStorage.removeItem(botsCacheKey('mt5'))}catch(e){}
  bots=[];
  _overlayLevels=[];_sessionMarks=[];_lastBar=null;_lastLine=null;_tvSym='';
  clearChartLines();clearTradeLines();
  var mk=$('chartMarks');if(mk)mk.innerHTML='';
  try{if(_tvSeries&&_tvSeries.setMarkers)_tvSeries.setMarkers([])}catch(e){}
  await refresh();
  if(window.loadDeskChart)window.loadDeskChart(true);
  if(window.loadMt5Status)window.loadMt5Status()
};



function paintMoverTicker(items, label){
  var wrap=$('moverTicker'), track=$('moverTrack');
  if(!wrap||!track)return;
  items=items||[];
  if(!items.length){
    wrap.classList.remove('on');
    track.innerHTML='';
    return
  }
  wrap.classList.add('on');
  var html=items.map(function(s){
    var ch=Number(s.chg_10m||0);
    var col=ch>=0?'var(--g)':'var(--r)';
    return '<span class="mover-item"><b>'+s.symbol+'</b><span style="color:'+col+'">'+(ch>=0?'+':'')+ch.toFixed(2)+'%</span></span>'
  }).join('');
  var tag='<span class="mover-item" style="color:var(--t3);font-weight:700">'+(label||'10-min movers')+'</span>';
  var seq=tag+html;
  track.innerHTML='<div class="mover-seq">'+seq+'</div><div class="mover-seq" aria-hidden="true">'+seq+'</div>'
}

window.loadMoverTicker=async function(){
  var wrap=$('moverTicker');
  if(!wrap)return;
  if(activeTab!=='binance'){
    wrap.classList.remove('on');
    return
  }
  try{
    var d=await api('/api/movers?platform=binance');
    var rows=(d&&d.movers)||[];
    paintMoverTicker(rows, '10-min movers')
  }catch(e){
    wrap.classList.remove('on')
  }
};

window.refreshMovers=async function(){
  var btn=$('moverRefresh');
  if(btn){btn.disabled=true;btn.textContent='…'}
  try{
    await loadMoverTicker()
  }finally{
    if(btn){btn.disabled=false;btn.textContent='↻'}
  }
};


// Init last — window.loadMt5Symbols handlers must already exist
window.speechSynthesis.getVoices();
setupSymbolCombo();
initChartMax();
loadChatHistory();
(function(){var lu=$('loggedInUser');if(lu){var u=readUser();if(u)lu.textContent='👤 '+u}})();

// ── Boot gate: verify trading server before showing app ─────────────────
// Any HTTP response = server alive (even 401). Network failure = server
// unreachable → ask user for the server address, test, save, reload.
var _booted=false;
window.enterLocal=function(plat){
  if(plat&&PLATS[plat]){try{sessionStorage.setItem(DESK_KEY,plat)}catch(e){}}
  writeTabAuth('local','local');
  var lu=$('loggedInUser');if(lu)lu.textContent='👤 local';
  hideLogin();
  applyWorkspace(readDesk());
  refresh();
};
function bootApp(){
  if(_booted)return;_booted=true;
  pickLoginPlat(readDesk());
  showLogin();
}
function bootServerGate(){
  if(!API){window.showServerModal('Enter your trading server address (e.g. https://your-server:9100)');return}
  testServer(API,6000).then(function(alive){
    if(alive)bootApp();
    else window.showServerModal('Trading server unreachable at '+API+' — enter the current address (e.g. after the server IP changed)');
  });
}
bootServerGate();
setInterval(function(){if(_booted&&isLoggedIn()){refresh();checkResources()}},30000);
if(isLoggedIn())checkResources();
pollChatter();
setInterval(function(){if(isLoggedIn())pollChatter()},4000);
setInterval(function(){
  if(isLoggedIn()&&activeTab==='mt5'&&window.loadMt5Status)window.loadMt5Status()
},5000);
setInterval(function(){if(isLoggedIn())tickDeskChart()},1000);
setInterval(function(){
  if(isLoggedIn()&&activeTab==='binance')loadMoverTicker()
},60000);
setInterval(function(){
  if(isLoggedIn()&&window.loadDeskChart)window.loadDeskChart()
},20000);
renderChatter(activeTab);
