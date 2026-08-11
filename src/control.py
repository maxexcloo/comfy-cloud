from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

from .control_config import ControlSettings
from .controller import (
    Controller,
    exception_message,
    history_parameters,
    rewrite_json_model,
)


class RequestBodyTooLarge(ValueError):
    pass


LOGIN_MAXIMUM_BYTES = 16 * 1024
DASHBOARD_PAGE_SIZE = 20
PROVIDER_TEST_MAXIMUM_BYTES = 16 * 1024
SESSION_COOKIE = "comfy_control_session"
SESSION_SECONDS = 12 * 60 * 60


def error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "server_error" if status >= 500 else "invalid_request_error",
                "code": code,
            }
        },
    )


def archived_image_content(
    request: Request, controller: Controller, history_id: str, response: httpx.Response
) -> bytes:
    try:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return response.content
        archived = iter(controller.store.media_for_history(history_id))
        for item in data:
            if not isinstance(item, dict):
                continue
            if not (item.get("b64_json") or item.get("url")):
                continue
            media = next(archived, None)
            if media is not None and item.get("url"):
                item["url"] = str(
                    request.url_for(
                        "history_media", history_id=history_id, media_id=media.id
                    )
                )
        return json.dumps(payload, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return response.content


def bearer_authorised(request: Request, settings: ControlSettings) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value, settings.api_key)


def session_secret(settings: ControlSettings) -> bytes:
    return (settings.ui_password or settings.api_key).encode()


def session_token(settings: ControlSettings, expires: int) -> str:
    payload = str(expires)
    signature = hmac.new(
        session_secret(settings), payload.encode(), hashlib.sha512
    ).hexdigest()
    return f"{payload}.{signature}"


def ui_authorised(request: Request, settings: ControlSettings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        expires = int(payload)
    except ValueError:
        return False
    expected = session_token(settings, expires).partition(".")[2]
    return expires >= int(time.time()) and hmac.compare_digest(signature, expected)


def secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").partition(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def login_html(settings: ControlSettings, invalid: bool = False) -> str:
    message = (
        '<p class="error" role="alert">Incorrect username or password.</p>'
        if invalid
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sign in · Comfy Control</title><style>
:root{{color-scheme:dark;font:15px system-ui;background:#101418;color:#e8edf2}}
body{{display:grid;margin:0;min-height:100vh;place-items:center}}main{{background:#181e24;border:1px solid #2c3640;border-radius:10px;padding:2rem;width:min(320px,calc(100vw - 4rem))}}
h1{{font-size:1.5rem;margin-top:0}}label{{display:grid;gap:.35rem;margin:1rem 0}}input{{background:#101418;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.65rem}}
button{{background:#263442;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.65rem;width:100%;cursor:pointer}}.error{{color:#ff7b72}}
</style></head><body><main><h1>Comfy Control</h1>{message}<form method="post" action="/login">
<label>Username<input name="username" value="{escape(settings.ui_username)}" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required autofocus></label>
<button type="submit">Sign in</button></form></main></body></html>"""


async def limited_body(request: Request, maximum_bytes: int) -> bytes:
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > maximum_bytes:
        raise RequestBodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise RequestBodyTooLarge
    request._body = bytes(body)  # Starlette form parsing reuses the bounded body.
    return request._body


def html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Comfy Control</title><style>
:root{color-scheme:dark;font:15px Inter,ui-sans-serif,system-ui,sans-serif;--accent:#5ee0c2;--bg:#080d12;--border:#24313b;--danger:#ffaaa5;--muted:#8d9ba7;--surface:#10171e;--raised:#172129;background:var(--bg);color:#edf3f6}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:linear-gradient(180deg,#0e171e 0,#080d12 25rem);color:#edf3f6}header,main,.tabbar{margin-inline:auto;max-width:1280px;width:calc(100% - 3rem)}header{align-items:center;display:flex;justify-content:space-between;padding:2.2rem 0 1.4rem}.brand{align-items:center;display:flex;gap:.8rem}.brand-mark{align-items:center;background:linear-gradient(145deg,#54dbbd,#2d806f);border-radius:10px;color:#07130f;display:flex;font-size:1.1rem;font-weight:900;height:2.65rem;justify-content:center;width:2.65rem}.eyebrow{color:var(--accent);font-size:.66rem;font-weight:750;letter-spacing:.13em;text-transform:uppercase}h1{font-size:1.45rem;letter-spacing:-.03em;margin:.1rem 0 0}.subtitle{color:var(--muted);font-size:.88rem;margin:.15rem 0 0}.header-actions{align-items:center;display:flex;gap:.8rem}.header-actions form{margin:0}.tabbar{border-bottom:1px solid var(--border);display:flex;gap:1.4rem;margin-bottom:1.4rem}.tabbar button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;color:var(--muted);font-weight:650;margin-bottom:-1px;padding:.75rem .1rem}.tabbar button.active{border-bottom-color:var(--accent);color:#f5fbfa}main{padding-bottom:3rem}section{background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:0 18px 45px #0002;overflow:hidden;padding:1rem}.section-head{align-items:flex-start;display:flex;justify-content:space-between;margin-bottom:1rem}.section-head h2{font-size:1.05rem;margin:0}.section-head small{margin-top:.15rem}.provider-grid{display:grid;gap:.8rem;grid-template-columns:repeat(3,minmax(0,1fr))}.provider-card{background:#0c1319;border:1px solid #202e38;border-radius:10px;display:flex;flex-direction:column;min-height:260px;padding:1rem;transition:border-color .15s ease,transform .15s ease}.provider-card:hover{border-color:#354955;transform:translateY(-1px)}.provider-head{align-items:flex-start;display:flex;gap:.8rem;justify-content:space-between}.provider-name{font-size:1rem;font-weight:750}.provider-kind{color:var(--muted);font-size:.74rem;margin-top:.18rem}.badge{background:#172129;border:1px solid #3a4852;border-radius:999px;display:inline-flex;font-size:.7rem;font-weight:700;padding:.22rem .52rem;white-space:nowrap}.badge.ready,.badge.completed{background:#102c23;border-color:#255b49;color:#76dfb6}.badge.failed,.badge.error,.badge.unavailable{background:#311c20;border-color:#65343b;color:#ffaaa5}.badge.busy,.badge.starting,.badge.queued,.badge.in_progress{background:#302817;border-color:#67552c;color:#f0c46c}.badge.not-deployed,.badge.stopped,.badge.scaled-down{color:#b5c0c8}.provider-error{color:#ef9a96;font-size:.75rem;margin:.7rem 0 0}.metrics{display:grid;gap:.5rem;grid-template-columns:repeat(2,minmax(0,1fr));margin:.9rem 0}.metric{background:#111b22;border-radius:7px;min-width:0;padding:.55rem .65rem}.metric strong{color:var(--muted);display:block;font-size:.66rem;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.metric span{display:block;font-size:.9rem;font-weight:700;margin-top:.16rem}.metric-empty{color:var(--muted);font-size:.78rem;margin:.9rem 0}.resource{border-top:1px solid #1c2831;color:var(--muted);font-family:ui-monospace,monospace;font-size:.7rem;margin-top:auto;overflow:hidden;padding-top:.65rem;text-overflow:ellipsis;white-space:nowrap}.actions{border-top:1px solid #1c2831;display:grid;gap:.55rem;margin-top:.65rem;padding-top:.7rem}.primary-actions,.utility-actions,.viewer-controls{display:flex;flex-wrap:wrap;gap:.4rem}.primary-actions button{background:#173c32;border-color:#296653;color:#8ce7d1;font-weight:700}.primary-actions button:only-child{flex:1}.utility-actions{align-items:center}.utility-actions button,.utility-actions .button{background:transparent;border-color:#2b3943;color:#b5c2ca;font-size:.75rem;padding:.35rem .55rem}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #202c35;padding:.72rem .55rem;text-align:left;vertical-align:top}th{color:#7f909c;font-size:.67rem;letter-spacing:.08em;text-transform:uppercase}tbody tr:last-child td{border-bottom:0}
button,input,select,textarea,.button{background:var(--raised);border:1px solid #3a4a55;border-radius:7px;color:#edf3f6;font:inherit;padding:.48rem .7rem}button,.button{cursor:pointer;text-decoration:none;transition:background .15s ease,border-color .15s ease}button:hover,.button:hover{background:#21303a;border-color:#58707d}button:focus-visible,.button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.secondary{background:transparent}.danger,.primary-actions .danger{background:#351e22;border-color:#713b44;color:var(--danger)}button:disabled{cursor:not-allowed;opacity:.4}label{display:grid;gap:.35rem;margin:.8rem 0}textarea{min-height:7rem;min-width:min(36rem,75vw)}pre{background:#080d11;border:1px solid #202d36;border-radius:8px;line-height:1.5;overflow:auto;padding:.8rem;white-space:pre-wrap}dialog{background:#111a21;border:1px solid #40515c;border-radius:12px;box-shadow:0 30px 100px #000b;color:#edf3f6;max-height:92vh;max-width:min(1050px,92vw);padding:1rem;width:max-content}dialog::backdrop{backdrop-filter:blur(4px);background:#03070bc7}.viewer-head{align-items:flex-start;display:flex;gap:2rem;justify-content:space-between;margin-bottom:.8rem}.viewer-head h2{margin:.1rem 0}.viewer-media{align-items:center;background:#060a0d;border:1px solid #202d36;border-radius:9px;display:flex;justify-content:center;min-height:240px;overflow:hidden}.viewer-media img,.viewer-media video{max-height:65vh;max-width:100%;object-fit:contain}.action-summary{background:#10241e;border:1px solid #285644;border-radius:8px;margin:.7rem 0;padding:.7rem}.action-summary[data-state="failed"]{background:#301d20;border-color:#693438}.pagination{align-items:center;display:flex;gap:.5rem;justify-content:flex-end;margin-top:.8rem}.pagination small{min-width:9rem;text-align:center}.ready,.completed{color:#67d391}.busy,.starting,.queued,.in_progress{color:#f2c166}.failed,.error,.unavailable{color:#ff7b72}small{color:var(--muted)}[hidden]{display:none!important}@media(max-width:1000px){.provider-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){header,main,.tabbar{width:calc(100% - 1.5rem)}header{align-items:flex-start;padding:1.2rem 0 1rem}.brand-mark{display:none}.subtitle{display:none}.header-actions{align-items:flex-end;flex-direction:column;gap:.35rem}.header-actions small{font-size:.65rem}.tabbar{gap:1.1rem;overflow-x:auto}.provider-grid{grid-template-columns:1fr}.provider-card{min-height:0}.primary-actions button{flex:1}.section-head{gap:.3rem}.table-wrap{overflow:visible}table,tbody,tr,td{display:block;width:100%}thead{display:none}tbody{display:grid;gap:.7rem}tbody tr{background:#0b1218;border:1px solid #202d36;border-radius:9px;padding:.45rem}tbody td{border:0;display:grid;gap:.4rem;grid-template-columns:minmax(6rem,30%) 1fr;padding:.35rem .4rem}tbody td::before{color:#788a96;content:attr(data-label);font-size:.64rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase}.pagination{justify-content:space-between}.pagination small{min-width:0}.viewer-head{align-items:stretch;flex-direction:column;gap:.7rem}.viewer-controls{justify-content:space-between}dialog{max-width:96vw;width:96vw}.viewer-media{min-height:180px}}</style></head><body><header><div class="brand"><div class="brand-mark">C</div><div><div class="eyebrow">GPU orchestration</div><h1>Comfy Control</h1><p class="subtitle">Compute, spend and generated media in one place.</p></div></div><div class="header-actions"><small id="updated">Loading…</small><form method="post" action="/logout"><button class="secondary">Log Out</button></form></div></header>
<nav class="tabbar" aria-label="Dashboard"><button class="active" data-tab="providersPage">Providers</button><button data-tab="historyPageSection">History</button><button data-tab="eventsPage">Events</button></nav><main>
<section id="providersPage" data-page><div class="section-head"><div><h2>Providers</h2><small>Live compute and account balances</small></div></div><div id="providers" class="provider-grid"></div></section>
<section id="historyPageSection" data-page hidden><div class="section-head"><h2>History</h2><small id="historyCount">Controller-owned requests and media</small></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Operation</th><th>Model</th><th>Provider</th><th>Status</th><th>Media</th><th></th></tr></thead><tbody id="historyRows"></tbody></table></div><div class="pagination"><button id="historyPrevious" class="secondary">Previous</button><small id="historyPage"></small><button id="historyNext" class="secondary">Next</button></div></section>
<section id="eventsPage" data-page hidden><div class="section-head"><h2>Events</h2><small id="eventCount">Persisted controller activity</small></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Level</th><th>Provider</th><th>Message</th></tr></thead><tbody id="events"></tbody></table></div><div class="pagination"><button id="eventPrevious" class="secondary">Previous</button><small id="eventPage"></small><button id="eventNext" class="secondary">Next</button></div></section>
</main>
<dialog id="viewer"><div class="viewer-head"><div><h2 id="viewerTitle"></h2><small id="viewerMeta"></small></div><div class="viewer-controls"><button id="viewerPrevious" title="Previous media (Left arrow)">← Previous</button><button id="viewerNext" title="Next media (Right arrow)">Next →</button><button id="viewerClose" class="secondary" title="Close (Escape)">Close</button></div></div><div id="viewerMedia" class="viewer-media"></div><small id="viewerPosition"></small><h3>Parameters</h3><pre id="viewerParameters"></pre></dialog>
<dialog id="providerDialog"><div class="viewer-head"><h2 id="providerTitle"></h2><button data-close="providerDialog">Close</button></div><pre id="providerDetails"></pre></dialog>
<dialog id="logsDialog"><div class="viewer-head"><div><h2 id="logsTitle"></h2><small>Actions continue if this window is closed. Reopen it with Logs.</small></div><div class="viewer-controls"><button id="logsRefresh">Refresh</button><button data-close="logsDialog" class="secondary">Close</button></div></div><div id="actionSummary" class="action-summary" hidden><strong id="actionStatus"></strong><pre id="actionResult"></pre></div><pre id="logsContent"></pre></dialog>
<dialog id="testDialog"><div class="viewer-head"><h2 id="testTitle"></h2><button data-close="testDialog">Close</button></div><form id="testForm"><label>Model<select id="testModel" required></select></label><label>Size<select id="testSize"><option>512x512</option><option>768x768</option><option>1024x1024</option></select></label><label>Prompt<textarea id="testPrompt" maxlength="2000" required>A cinematic photograph of a wombat operating a compact GPU server, studio lighting</textarea></label><button id="testSubmit">Send test request</button></form><pre id="testResult">No request sent.</pre><div id="testMedia" class="viewer-media"></div></dialog>
<script>const element=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),title=v=>String(v).split(/[-_]/).map(w=>w.charAt(0).toUpperCase()+w.slice(1)).join(' ');
const providers=element('providers'),historyRows=element('historyRows'),events=element('events'),updated=element('updated'),historyCount=element('historyCount'),historyPrevious=element('historyPrevious'),historyPage=element('historyPage'),historyNext=element('historyNext'),eventCount=element('eventCount'),eventPrevious=element('eventPrevious'),eventPage=element('eventPage'),eventNext=element('eventNext'),viewer=element('viewer'),viewerTitle=element('viewerTitle'),viewerMeta=element('viewerMeta'),viewerMedia=element('viewerMedia'),viewerParameters=element('viewerParameters'),viewerPosition=element('viewerPosition'),viewerPrevious=element('viewerPrevious'),viewerNext=element('viewerNext'),viewerClose=element('viewerClose'),providerDialog=element('providerDialog'),providerTitle=element('providerTitle'),providerDetails=element('providerDetails'),logsDialog=element('logsDialog'),logsTitle=element('logsTitle'),logsContent=element('logsContent'),logsRefresh=element('logsRefresh'),actionSummary=element('actionSummary'),actionStatus=element('actionStatus'),actionResult=element('actionResult'),testDialog=element('testDialog'),testTitle=element('testTitle'),testModel=element('testModel'),testForm=element('testForm'),testSubmit=element('testSubmit'),testResult=element('testResult'),testMedia=element('testMedia'),testSize=element('testSize'),testPrompt=element('testPrompt');
let historyData=[],providerData=[],viewerItems=[],viewerIndex=0,testProvider='',logsProvider='',historyPageNumber=1,eventPageNumber=1;const actionStates=new Map(),actionPolls=new Map();
document.querySelectorAll('[data-tab]').forEach(tab=>tab.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(item=>item.classList.toggle('active',item===tab));document.querySelectorAll('[data-page]').forEach(page=>page.hidden=page.id!==tab.dataset.tab)});
function metricText(metric){const number=Number(metric.value),value=metric.unit==='USD'&&Number.isFinite(number)?new Intl.NumberFormat(undefined,{style:'currency',currency:'USD'}).format(number):esc(metric.value)+(metric.unit?' '+esc(metric.unit):'');return `<div class="metric"><strong title="${esc(metric.label)}">${esc(metric.label)}</strong><span>${value}</span></div>`}
function pagination(data,previous,label,next,count){previous.disabled=data.page<=1;next.disabled=data.page>=data.pages;label.textContent=`Page ${data.page} of ${data.pages}`;count.textContent=`${data.count} saved`}
async function refresh(){if(refresh.running)return;refresh.running=true;try{const r=await fetch(`/api/status?history_page=${historyPageNumber}&event_page=${eventPageNumber}`);if(r.status===401){location='/login';return}if(!r.ok)throw new Error('status '+r.status);const d=await r.json();providerData=d.providers;
providers.innerHTML=d.providers.map(p=>`<article class="provider-card"><div class="provider-head"><div><div class="provider-name">${esc(p.platform??p.id)}</div><div class="provider-kind">${esc(title(p.type))} · ${esc(p.id)}${p.active_requests?` · ${p.active_requests} active`:''}</div></div><span class="badge ${esc(p.state)}">${esc(title(p.state))}</span></div>${p.error?`<p class="provider-error">${esc(p.error)}</p>`:''}<div class="metrics">${p.usage.status==='ok'?(p.usage.metrics.map(metricText).join('')||'<div class="metric-empty">No account metrics exposed</div>'):`<div class="metric-empty">${esc(p.usage.error??p.usage.status)}</div>`}</div><div class="resource" title="${esc(p.resource_id??'Not deployed')}">${esc(p.resource_id??'Not deployed')}</div><div class="actions">${p.actions.length?`<div class="primary-actions">${p.actions.map(a=>`<button class="provider-action ${['delete','destroy','terminate'].includes(a.name)?'danger':''}" data-provider="${esc(p.id)}" data-action="${esc(a.name)}" data-confirmation="${esc(a.confirmation)}">${esc(title(a.name))}</button>`).join('')}</div>`:''}<div class="utility-actions"><button class="details" data-provider="${esc(p.id)}">Status</button><button class="logs" data-provider="${esc(p.id)}">Logs</button>${p.models.length?`<button class="test" data-provider="${esc(p.id)}">Test</button>`:''}${p.panel_url?`<a class="button" href="${esc(p.panel_url)}" target="_blank" rel="noopener noreferrer">Panel ↗</a>`:''}</div></div></article>`).join('');
providers.querySelectorAll('.provider-action').forEach(b=>b.onclick=()=>act(b.dataset.provider,b.dataset.action,b.dataset.confirmation));providers.querySelectorAll('.details').forEach(b=>b.onclick=()=>showProvider(b.dataset.provider));providers.querySelectorAll('.logs').forEach(b=>b.onclick=()=>showLogs(b.dataset.provider));providers.querySelectorAll('.test').forEach(b=>b.onclick=()=>showTest(b.dataset.provider));
historyData=d.history;historyRows.innerHTML=historyData.map(j=>`<tr><td data-label="Time">${new Date(j.created_at*1000).toLocaleString()}</td><td data-label="Operation">${esc(title(j.operation))}</td><td data-label="Model">${esc(j.model)}</td><td data-label="Provider">${esc(j.provider||'—')}</td><td data-label="Status"><span class="badge ${esc(j.status)}">${esc(title(j.status))}</span></td><td data-label="Media">${j.media.length}</td><td data-label="View"><button data-history="${esc(j.id)}">View</button></td></tr>`).join('');historyRows.querySelectorAll('button').forEach(b=>b.onclick=()=>showHistory(b.dataset.history));events.innerHTML=d.events.map(e=>`<tr><td data-label="Time">${new Date(e.created_at*1000).toLocaleString()}</td><td data-label="Level" class="${esc(e.level)}">${esc(title(e.level))}</td><td data-label="Provider">${esc(e.provider||'—')}</td><td data-label="Message">${esc(e.message)}</td></tr>`).join('');pagination(d.history_pagination,historyPrevious,historyPage,historyNext,historyCount);pagination(d.event_pagination,eventPrevious,eventPage,eventNext,eventCount);updated.textContent='Updated '+new Date().toLocaleTimeString()}finally{refresh.running=false}}
function renderViewer(){const current=viewerItems[viewerIndex],item=current.item,media=current.media;viewerTitle.textContent=item.model;viewerMeta.textContent=`${title(item.operation)} · ${item.provider||'no provider'} · ${title(item.status)}`;viewerParameters.textContent=JSON.stringify({...item.parameters,error:item.error},null,2);viewerMedia.replaceChildren();if(media){const node=document.createElement(media.content_type.startsWith('video/')?'video':'img');node.src='/api/history/'+encodeURIComponent(item.id)+'/media/'+media.id;node.title=media.filename;if(node.tagName==='VIDEO')node.controls=true;viewerMedia.appendChild(node)}else{const empty=document.createElement('small');empty.textContent='No output media is attached to this request.';viewerMedia.appendChild(empty)}viewerPosition.textContent=media?`${viewerIndex+1} of ${viewerItems.length} · ${media.filename}`:'Request details';viewerPrevious.disabled=viewerIndex===0;viewerNext.disabled=viewerIndex>=viewerItems.length-1}
function showHistory(id){const item=historyData.find(entry=>entry.id===id);if(!item)return;viewerItems=historyData.flatMap(entry=>entry.media.map(media=>({item:entry,media})));viewerIndex=viewerItems.findIndex(entry=>entry.item.id===id);if(viewerIndex<0){viewerItems=[{item,media:null}];viewerIndex=0}renderViewer();if(!viewer.open)viewer.showModal()}
function moveViewer(change){const target=viewerIndex+change;if(target<0||target>=viewerItems.length)return;viewerIndex=target;renderViewer()}
function showProvider(id){const p=providerData.find(item=>item.id===id);if(!p)return;providerTitle.textContent=(p.platform??p.id)+' status';providerDetails.textContent=JSON.stringify({id:p.id,type:p.type,state:p.state,resource_id:p.resource_id,active_requests:p.active_requests,idle_seconds:p.idle_seconds,details:p.details,usage:p.usage,error:p.error},null,2);providerDialog.showModal()}
function renderAction(id){const state=actionStates.get(id);actionSummary.hidden=!state;if(!state)return;actionSummary.dataset.state=state.state;actionStatus.textContent=state.label;actionResult.textContent=state.result}
async function loadLogs(provider=logsProvider){logsContent.textContent='Loading…';const r=await fetch('/api/providers/'+encodeURIComponent(provider)+'/logs?limit=200');const d=await r.json().catch(()=>({}));if(provider!==logsProvider)return;logsContent.textContent=r.ok?(d.entries.length?d.entries.map(e=>`${new Date(e.created_at*1000).toLocaleString()} ${String(e.level).toUpperCase()} ${e.message}${e.request_id?' ['+e.request_id+']':''}`).join('\\n'):'No controller events recorded for this provider.'):d.error?.message??'Unable to load logs'}
function showLogs(id){logsProvider=id;const p=providerData.find(item=>item.id===id);logsTitle.textContent=(p?.platform??id)+' logs';renderAction(id);if(!logsDialog.open)logsDialog.showModal();loadLogs()}
function showTest(id){const p=providerData.find(item=>item.id===id);if(!p)return;testProvider=id;testTitle.textContent='Test '+(p.platform??id);testModel.innerHTML=p.models.map(model=>`<option>${esc(model)}</option>`).join('');testResult.textContent='No request sent.';testMedia.replaceChildren();testDialog.showModal()}
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.close).close());document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{const bounds=dialog.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)dialog.close()}));viewerClose.onclick=()=>viewer.close();viewerPrevious.onclick=()=>moveViewer(-1);viewerNext.onclick=()=>moveViewer(1);logsRefresh.onclick=()=>loadLogs();historyPrevious.onclick=()=>{historyPageNumber--;refresh()};historyNext.onclick=()=>{historyPageNumber++;refresh()};eventPrevious.onclick=()=>{eventPageNumber--;refresh()};eventNext.onclick=()=>{eventPageNumber++;refresh()};
document.addEventListener('keydown',event=>{if(!viewer.open)return;if(event.key==='ArrowLeft'){event.preventDefault();moveViewer(-1)}else if(event.key==='ArrowRight'){event.preventDefault();moveViewer(1)}else if(event.key==='Escape'){viewer.close()}});
testForm.onsubmit=async e=>{e.preventDefault();testSubmit.disabled=true;testResult.textContent='Sending request; this may start billable compute…';testMedia.replaceChildren();const r=await fetch('/api/providers/'+encodeURIComponent(testProvider)+'/test',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({model:testModel.value,prompt:testPrompt.value,size:testSize.value})});const d=await r.json().catch(()=>({}));testResult.textContent=JSON.stringify(d,null,2);if(r.ok)for(const media of d.media){const element=document.createElement('img');element.src='/api/history/'+encodeURIComponent(d.history_id)+'/media/'+media.id;element.title=media.filename;testMedia.appendChild(element)}testSubmit.disabled=false;await refresh()};
async function act(provider,action,confirmation){if(confirmation&&confirmation!=='null'&&!confirm(confirmation))return;const key=provider+'/'+action;actionStates.set(provider,{label:`${title(action)} in progress…`,result:'Waiting for the provider…',state:'running'});showLogs(provider);clearInterval(actionPolls.get(provider));actionPolls.set(provider,setInterval(()=>loadLogs(provider),1000));try{const r=await fetch('/api/providers/'+encodeURIComponent(provider)+'/actions/'+encodeURIComponent(action),{method:'POST',headers:{'x-comfy-control-action':key}});const d=await r.json().catch(()=>({}));actionStates.set(provider,{label:r.ok?`${title(action)} completed`:`${title(action)} failed`,result:JSON.stringify(d,null,2),state:r.ok?'completed':'failed'});if(logsProvider===provider)renderAction(provider)}catch(error){actionStates.set(provider,{label:`${title(action)} failed`,result:String(error),state:'failed'});if(logsProvider===provider)renderAction(provider)}finally{clearInterval(actionPolls.get(provider));actionPolls.delete(provider);if(logsProvider===provider)await loadLogs(provider);await refresh()}}refresh().catch(e=>updated.textContent='Unable to load: '+e.message);setInterval(()=>refresh().catch(()=>{}),5000)</script></body></html>"""


def create_app(settings: ControlSettings | None = None) -> FastAPI:
    settings = settings or ControlSettings.from_env()
    controller = Controller(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for pending_job in controller.store.pending_jobs():
            controller.start_video(pending_job)
        reaper = asyncio.create_task(controller.idle_reaper())
        yield
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        await controller.close()

    app = FastAPI(title="Comfy Control", version="0.1.0", lifespan=lifespan)
    app.state.controller = controller

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/health")
    async def health() -> dict[str, int | str]:
        return {
            "status": "ready",
            "models": len(controller.config.models),
            "providers": len(controller.providers),
        }

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(html(), headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def login(request: Request) -> Response:
        if ui_authorised(request, settings):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_html(settings), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def create_session(request: Request) -> Response:
        try:
            await limited_body(request, LOGIN_MAXIMUM_BYTES)
            form = await request.form()
        except RequestBodyTooLarge:
            return HTMLResponse(
                login_html(settings, invalid=True),
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        expected_password = settings.ui_password or settings.api_key
        valid = hmac.compare_digest(
            username, settings.ui_username
        ) and hmac.compare_digest(password, expected_password)
        if not valid:
            return HTMLResponse(
                login_html(settings, invalid=True),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        expires = int(time.time()) + SESSION_SECONDS
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_token(settings, expires),
            httponly=True,
            max_age=SESSION_SECONDS,
            path="/",
            samesite="lax",
            secure=secure_cookie(request),
        )
        return response

    @app.post("/logout")
    async def delete_session() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/status")
    async def status(request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        try:
            event_page = max(1, int(request.query_params.get("event_page", "1")))
            history_page = max(1, int(request.query_params.get("history_page", "1")))
        except ValueError:
            return error("page must be a number", 400, "invalid_request")
        event_count = controller.store.event_count()
        history_count = controller.store.history_count()
        usage, provider_statuses = await asyncio.gather(
            controller.usage(), controller.provider_statuses()
        )
        return JSONResponse(
            {
                "providers": [
                    {
                        "id": runtime.config.id,
                        "platform": runtime.config.platform,
                        "type": runtime.config.type,
                        "usage": usage[runtime.config.id],
                        "details": provider_statuses[runtime.config.id]["details"],
                        "error": provider_statuses[runtime.config.id].get("error"),
                        "panel_url": provider_statuses[runtime.config.id]["panel_url"],
                        "resource_id": controller.resource_id(runtime.config.id),
                        "state": runtime.state,
                        "active_requests": runtime.active_requests,
                        "actions": [
                            {
                                "name": name,
                                "confirmation": action.confirmation,
                            }
                            for name, action in controller.available_actions(
                                runtime.config.id
                            ).items()
                        ],
                        "idle_seconds": runtime.config.idle_seconds,
                        "models": sorted(
                            model.id
                            for model in controller.config.models
                            if model.operation == "image_generation"
                            and any(
                                target.provider == runtime.config.id
                                for target in model.targets
                            )
                        ),
                    }
                    for runtime in controller.providers.values()
                ],
                "history": controller.store.histories(
                    DASHBOARD_PAGE_SIZE,
                    (history_page - 1) * DASHBOARD_PAGE_SIZE,
                ),
                "history_pagination": {
                    "count": history_count,
                    "page": history_page,
                    "pages": max(
                        1,
                        (history_count + DASHBOARD_PAGE_SIZE - 1)
                        // DASHBOARD_PAGE_SIZE,
                    ),
                },
                "jobs": controller.store.jobs(50),
                "events": controller.store.events(
                    DASHBOARD_PAGE_SIZE,
                    (event_page - 1) * DASHBOARD_PAGE_SIZE,
                ),
                "event_pagination": {
                    "count": event_count,
                    "page": event_page,
                    "pages": max(
                        1,
                        (event_count + DASHBOARD_PAGE_SIZE - 1) // DASHBOARD_PAGE_SIZE,
                    ),
                },
            }
        )

    @app.get("/api/providers/{provider_id}/logs")
    async def provider_logs(provider_id: str, request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        try:
            limit = min(max(int(request.query_params.get("limit", "200")), 1), 500)
            return JSONResponse(controller.provider_logs(provider_id, limit))
        except ValueError:
            return error("log limit must be a number", 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "provider_not_found")

    @app.get("/api/history")
    async def history(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            page_size = min(
                max(1, int(request.query_params.get("page_size", "100"))), 500
            )
        except ValueError:
            return error("page must be a number", 400, "invalid_request")
        count = controller.store.history_count()
        return JSONResponse(
            {
                "data": controller.store.histories(page_size, (page - 1) * page_size),
                "pagination": {
                    "count": count,
                    "page": page,
                    "pages": max(1, (count + page_size - 1) // page_size),
                },
            }
        )

    @app.get("/api/history/{history_id}/media/{media_id}")
    async def history_media(
        history_id: str, media_id: int, request: Request
    ) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        item = controller.store.media(media_id)
        if (
            item is None
            or item.history_id != history_id
            or not Path(item.path).is_file()
        ):
            return error("media was not found", 404, "not_found")
        return FileResponse(
            item.path,
            media_type=item.content_type,
            headers={
                "Content-Disposition": f'inline; filename="{item.filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/providers/{provider_id}/actions/{action_name}")
    async def provider_action(
        provider_id: str, action_name: str, request: Request
    ) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        expected = f"{provider_id}/{action_name}"
        if not hmac.compare_digest(
            request.headers.get("x-comfy-control-action", ""), expected
        ):
            return error(
                "provider action confirmation is missing", 400, "invalid_action"
            )
        request_id = uuid.uuid4().hex[:16]
        controller.store.event(
            "info",
            f"provider action {action_name} started",
            provider=provider_id,
            request_id=request_id,
        )
        try:
            result = await controller.run_provider_action(
                provider_id, action_name, request_id
            )
        except KeyError as exc:
            controller.store.event(
                "error",
                f"provider action {action_name} failed: {exception_message(exc)}",
                provider=provider_id,
                request_id=request_id,
            )
            return error(str(exc), 404, "action_not_found")
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            controller.store.event(
                "error",
                f"provider action {action_name} failed: {exception_message(exc)}",
                provider=provider_id,
                request_id=request_id,
            )
            return error(exception_message(exc), 502, "action_failed")
        return JSONResponse(result)

    @app.post("/api/providers/{provider_id}/test")
    async def provider_test(provider_id: str, request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        try:
            body = await limited_body(request, PROVIDER_TEST_MAXIMUM_BYTES)
            values = json.loads(body)
            if not isinstance(values, dict):
                raise TypeError("request body must be an object")
            prompt = str(values.get("prompt", "")).strip()
            if not prompt or len(prompt) > 2000:
                raise ValueError("prompt must contain between 1 and 2000 characters")
            requested_model = str(values.get("model", ""))
            size = str(values.get("size", "512x512"))
            if size not in {"512x512", "768x768", "1024x1024"}:
                raise ValueError("unsupported test size")
            model, targets = controller.resolve_model(
                requested_model, "image_generation", provider_id
            )
        except RequestBodyTooLarge:
            return error("request body is too large", 413, "request_too_large")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        target = targets[0]
        request_id = uuid.uuid4().hex[:16]
        history_id = f"test_{uuid.uuid4().hex}"
        parameters = {
            "model": requested_model,
            "prompt": prompt,
            "provider": provider_id,
            "size": size,
        }
        controller.store.save_history(
            history_id,
            "image_generation",
            model.id,
            json.dumps(parameters, separators=(",", ":")),
        )
        controller.store.update_history(
            history_id, "in_progress", provider=target.provider
        )
        started = time.monotonic()
        try:
            response = await controller.forward(
                target,
                "POST",
                "/v1/images/generations",
                json.dumps(
                    {
                        "model": target.model,
                        "n": 1,
                        "prompt": prompt,
                        "response_format": "b64_json",
                        "size": size,
                    },
                    separators=(",", ":"),
                ).encode(),
                {"content-type": "application/json"},
                request_id,
            )
            if not response.is_success:
                raise RuntimeError(f"provider returned HTTP {response.status_code}")
            await controller.archive_images(history_id, target.provider, response)
            controller.store.update_history(
                history_id, "completed", provider=target.provider
            )
            controller.store.event(
                "info",
                "dashboard test completed",
                provider=target.provider,
                request_id=request_id,
            )
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            message = exception_message(exc)
            controller.store.update_history(history_id, "failed", error=message)
            controller.store.event(
                "error", message, provider=target.provider, request_id=request_id
            )
            return error(message, 502, "test_failed")
        media = controller.store.media_for_history(history_id)
        return JSONResponse(
            {
                "duration_seconds": round(time.monotonic() - started, 2),
                "history_id": history_id,
                "media": [
                    {
                        "content_type": item.content_type,
                        "filename": item.filename,
                        "id": item.id,
                        "size": item.size,
                    }
                    for item in media
                ],
                "model": model.id,
                "provider": target.provider,
                "status": "completed",
            }
        )

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        model_ids = {model.id for model in controller.config.models}
        for model in controller.config.models:
            for target in model.targets:
                provider = controller.providers[target.provider].config
                for provider_name in (provider.id, *provider.aliases):
                    model_ids.add(f"{provider_name}/{target.model}")
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "comfy-control",
                    }
                    for model_id in sorted(model_ids)
                ],
            }
        )

    async def generation(request: Request, operation: str, path: str) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        try:
            body = await limited_body(request, settings.maximum_request_bytes)
        except RequestBodyTooLarge:
            return error("request body is too large", 413, "request_too_large")
        try:
            original = json.loads(body)
            requested_model = str(original.get("model", ""))
            provider = str(original.get("provider", "")).strip() or None
            model, targets = controller.resolve_model(
                requested_model, operation, provider
            )
        except (AttributeError, json.JSONDecodeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        history_id = f"image_{uuid.uuid4().hex}"
        controller.store.save_history(
            history_id,
            operation,
            model.id,
            json.dumps(history_parameters(original), separators=(",", ":")),
        )
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"authorization", "content-length", "host"}
        }
        failures = []
        forwarded = dict(original)
        forwarded.pop("provider", None)
        forwarded_body = json.dumps(forwarded, separators=(",", ":")).encode()
        for target in targets:
            controller.store.update_history(
                history_id, "in_progress", provider=target.provider
            )
            try:
                response = await controller.forward(
                    target,
                    request.method,
                    path,
                    rewrite_json_model(forwarded_body, target.model),
                    headers,
                    request_id,
                )
            except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                message = exception_message(exc)
                failures.append(f"{target.provider}: {message}")
                controller.store.event(
                    "error",
                    message,
                    provider=target.provider,
                    request_id=request_id,
                )
                continue
            if response.is_success:
                await controller.archive_images(history_id, target.provider, response)
                controller.store.update_history(
                    history_id, "completed", provider=target.provider
                )
                controller.store.event(
                    "info",
                    f"{operation} completed",
                    provider=target.provider,
                    request_id=request_id,
                )
                return Response(
                    content=archived_image_content(
                        request, controller, history_id, response
                    ),
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-comfy-history-id": history_id,
                        "x-request-id": request_id,
                    },
                )
            failures.append(f"{target.provider}: HTTP {response.status_code}")
            if response.status_code < 500 and response.status_code != 429:
                controller.store.update_history(
                    history_id,
                    "failed",
                    provider=target.provider,
                    error=f"HTTP {response.status_code}",
                )
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )
        failure = "; ".join(failures) or "all providers failed"
        controller.store.update_history(history_id, "failed", error=failure)
        return error(failure, 502, "providers_failed")

    @app.post("/v1/images/generations")
    async def image_generations(request: Request) -> Response:
        return await generation(request, "image_generation", "/v1/images/generations")

    @app.post("/v1/images/edits")
    async def image_edits(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        try:
            await limited_body(request, settings.maximum_request_bytes)
        except RequestBodyTooLarge:
            return error("request body is too large", 413, "request_too_large")
        try:
            form = await request.form()
            model_id = str(form.get("model", ""))
            provider = str(form.get("provider", "")).strip() or None
            model, targets = controller.resolve_model(model_id, "image_edit", provider)
        except ValueError as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        fields: list[tuple[str, str]] = []
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                files.append(
                    (
                        key,
                        (
                            value.filename or "upload",
                            await value.read(),
                            value.content_type or "application/octet-stream",
                        ),
                    )
                )
            elif key not in {"model", "provider"}:
                fields.append((key, str(value)))
        history_id = f"edit_{uuid.uuid4().hex}"
        parameters = dict(fields)
        parameters["model"] = model_id
        if provider:
            parameters["provider"] = provider
        parameters["input_media"] = [
            {
                "content_type": content_type,
                "filename": filename,
                "size": len(content),
            }
            for _, (filename, content, content_type) in files
        ]
        controller.store.save_history(
            history_id,
            "image_edit",
            model.id,
            json.dumps(history_parameters(parameters), separators=(",", ":")),
        )
        failures: list[str] = []
        for target in targets:
            controller.store.update_history(
                history_id, "in_progress", provider=target.provider
            )
            encoded = httpx.Request(
                "POST",
                "http://multipart.invalid",
                data=dict(fields) | {"model": target.model},
                files=files,
            )
            try:
                response = await controller.forward(
                    target,
                    "POST",
                    "/v1/images/edits",
                    encoded.read(),
                    {"content-type": encoded.headers["content-type"]},
                    request_id,
                )
            except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                failures.append(f"{target.provider}: {exception_message(exc)}")
                continue
            if response.is_success:
                await controller.archive_images(history_id, target.provider, response)
                controller.store.update_history(
                    history_id, "completed", provider=target.provider
                )
                return Response(
                    content=archived_image_content(
                        request, controller, history_id, response
                    ),
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-comfy-history-id": history_id,
                        "x-request-id": request_id,
                    },
                )
            failures.append(f"{target.provider}: HTTP {response.status_code}")
            if response.status_code < 500 and response.status_code != 429:
                controller.store.update_history(
                    history_id,
                    "failed",
                    provider=target.provider,
                    error=f"HTTP {response.status_code}",
                )
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )
        failure = "; ".join(failures) or "all providers failed"
        controller.store.update_history(history_id, "failed", error=failure)
        return error(failure, 502, "providers_failed")

    @app.post("/v1/videos")
    async def create_video(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        try:
            body = await limited_body(request, settings.maximum_request_bytes)
        except RequestBodyTooLarge:
            return error("request body is too large", 413, "request_too_large")
        try:
            if request.headers.get("content-type", "").startswith(
                "multipart/form-data"
            ):
                form = await request.form()
                model_id = str(form.get("model", ""))
                provider = str(form.get("provider", "")).strip() or None
            else:
                values = json.loads(body)
                if not isinstance(values, dict):
                    raise TypeError("request body must be an object")
                model_id = values.get("model", "")
                provider = str(values.get("provider", "")).strip() or None
            model, targets = controller.resolve_model(
                str(model_id), "video_generation", provider
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        public_id = f"video_{uuid.uuid4().hex}"
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            directory = controller.uploads_path / public_id
            directory.mkdir(parents=True)
            fields: list[tuple[str, str]] = []
            files: list[dict[str, str]] = []
            for key, value in form.multi_items():
                if hasattr(value, "read"):
                    path = directory / str(len(files))
                    path.write_bytes(await value.read())
                    files.append(
                        {
                            "content_type": value.content_type
                            or "application/octet-stream",
                            "field": key,
                            "filename": value.filename or "upload",
                            "path": str(path),
                        }
                    )
                elif key != "provider":
                    fields.append((key, str(value)))
            request_json = json.dumps(
                {"_control_multipart": {"fields": fields, "files": files}},
                separators=(",", ":"),
            )
            parameters: object = {
                **dict(fields),
                "input_media": [
                    {
                        "content_type": item["content_type"],
                        "filename": item["filename"],
                        "size": Path(item["path"]).stat().st_size,
                    }
                    for item in files
                ],
            }
        else:
            forwarded = dict(values)
            forwarded.pop("provider", None)
            request_json = json.dumps(forwarded, separators=(",", ":"))
            parameters = values
        selected_provider = targets[0].provider if provider else None
        controller.store.save_job(
            public_id, model.id, request_json, provider=selected_provider
        )
        controller.store.save_history(
            public_id,
            "video_generation",
            model_id,
            json.dumps(history_parameters(parameters), separators=(",", ":")),
        )
        job = controller.store.job(public_id)
        assert job is not None
        controller.start_video(job)
        return JSONResponse(
            {
                "id": public_id,
                "object": "video",
                "model": model_id,
                "status": "queued",
                "created_at": job.created_at,
            }
        )

    @app.get("/v1/videos/{job_id}")
    async def get_video(job_id: str, request: Request) -> Response:
        return await video_request(job_id, request, content=False)

    @app.get("/v1/videos/{job_id}/content")
    async def video_content(job_id: str, request: Request) -> Response:
        return await video_request(job_id, request, content=True)

    async def video_request(
        job_id: str, request: Request, *, content: bool
    ) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        job = controller.store.job(job_id)
        if job is None:
            return error("video job was not found", 404, "not_found")
        if not content:
            if job.response_json:
                response_data = json.loads(job.response_json)
                if controller.store.media_for_history(job_id):
                    response_data["output_url"] = str(
                        request.url_for("video_content", job_id=job_id)
                    )
                return JSONResponse(response_data)
            return JSONResponse(
                {
                    "id": job.id,
                    "object": "video",
                    "model": job.model,
                    "status": job.status,
                    "created_at": job.created_at,
                    "error": job.error,
                    "output_url": None,
                }
            )
        if job.status != "completed":
            return error("video is not ready", 409, "video_not_ready")
        response_data = json.loads(job.response_json or "{}")
        archived = controller.store.media_for_history(job_id)
        if archived:
            item = archived[0]
            return FileResponse(item.path, media_type=item.content_type)
        if output_url := response_data.get("output_url"):
            return Response(status_code=302, headers={"Location": output_url})
        runtime = controller.providers[job.provider]
        await controller.ensure_ready(runtime, job.id[:16])
        upstream = await runtime.client.send(
            runtime.client.build_request(
                "GET",
                f"/v1/videos/{job.upstream_id}/content",
                headers={"Authorization": f"Bearer {runtime.config.api_key}"},
            ),
            stream=True,
        )
        if upstream.is_redirect:
            location = upstream.headers.get("location", "")
            await upstream.aclose()
            return Response(
                status_code=upstream.status_code, headers={"Location": location}
            )
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(upstream.aclose),
        )

    return app
