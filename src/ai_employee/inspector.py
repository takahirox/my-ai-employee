"""Read-only loopback Inspector for Goal, graph, authority and execution evidence."""

from __future__ import annotations

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from .cli import projection
from .history import Journal

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Inspector</title><style>
body{font:16px system-ui;margin:0;background:#f6f7f9;color:#18212d}
header{padding:24px;background:#15283c;color:white}main{display:grid;
grid-template-columns:280px 1fr;gap:24px;padding:24px}button{display:block;
width:100%;text-align:left;padding:12px;margin:6px 0;border:1px solid #cbd3df;
border-radius:6px;background:white;cursor:pointer}article,details{background:white;
padding:20px;margin-bottom:16px;border-radius:8px}pre{white-space:pre-wrap;
overflow-wrap:anywhere;font-size:13px}.task{border-left:4px solid #537fa8}
.passed{border-color:#27805a}small{color:#556778}h2{margin-top:0}
@media(max-width:700px){main{display:block}}
</style><header><h1>Fleet Inspector</h1><div>Goals, execution and verification</div></header>
<main><nav id="runs"></nav><section id="detail">Select a run.</section></main>
<script>
const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
let selected=null;
async function api(path){const r=await fetch(path,{headers:{Authorization:'Bearer '+token}});
if(!r.ok)throw new Error('Inspector access failed: '+r.status);return await r.json()}
function node(tag,text,parent){const e=document.createElement(tag);e.textContent=text;
parent.append(e);return e}
async function show(id){selected=id;const r=await api('/api/runs/'+id);
if(selected!==id)return;const d=document.getElementById('detail');d.replaceChildren();
const goal=node('article','',d);node('h2',r.goal?.specification.clarified_goal||id,goal);
node('p',r.status+(r.external_outcome_uncertain?' · external outcome uncertain':''),goal);
const budget=node('details','',d);node('summary','Run budget and accounting',budget);
node('pre',JSON.stringify(r.budget,null,2),budget);if(r.goal){node('small',r.goal.original_input,goal);
for(const c of r.goal.specification.criteria)node('p',c.id+': '+c.description,goal);
for(const m of r.goal.specification.requirements)
node('p',m.original_fragment+' → '+m.criteria.join(', '),goal)}
const diagnostics=node('details','',d);
node('summary','Stage validation, review and readiness',diagnostics);
node('pre',JSON.stringify(r.stage_diagnostics,null,2),diagnostics);
const invocations=node('details','',d);
node('summary','Invocation counts and contract bindings',invocations);
node('pre',JSON.stringify(r.stage_invocations,null,2),invocations);
const accepted=new Set(r.events.filter(e=>e.kind==='accepted').map(e=>e.body.task));
for(const t of r.plan?.tasks||[]){const a=node('article','',d);
a.className='task'+(accepted.has(t.id)?' passed':'');node('h3',t.description,a);
node('small',t.id+' · '+t.kind+' · '+(accepted.has(t.id)?'verified':'pending'),a);
node('p','Depends on: '+(t.dependencies.join(', ')||'none'),a);
for(const c of t.criteria)node('p',c.description,a)}
for(const e of r.events.slice(-100).reverse()){const x=node('details','',d);
node('summary',e.kind+' · '+new Date(e.at*1000).toLocaleTimeString(),x);
node('pre',JSON.stringify(e.body,null,2),x)}}
async function refresh(){try{const list=await api('/api/runs');
const n=document.getElementById('runs');n.replaceChildren();node('h2','Runs',n);
for(const r of list){const b=node('button',r.title+' · '+r.status,n);
b.onclick=()=>show(r.run_id)}if(selected)await show(selected)}catch(e){
document.getElementById('detail').textContent=e.message}}
refresh();setInterval(refresh,3000);
</script></html>"""


def run_list(journal: Journal) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for run in journal.runs():
        try:
            view = projection(journal, run)
            title = journal.original(run)[:200]
        except (ValueError, KeyError):
            result.append({"run_id": run, "title": "Unreadable run", "status": "unreadable"})
            continue
        result.append({"run_id": run, "title": title, "status": view["status"]})
    return result


def create_server(journal: Journal, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            server = cast(ThreadingHTTPServer, self.server)
            if self.headers.get("Host") not in {
                f"127.0.0.1:{server.server_port}",
                f"localhost:{server.server_port}",
            }:
                self.send_error(403)
                return
            if self.path == "/":
                body = PAGE.encode()
                content_type = "text/html; charset=utf-8"
            else:
                if not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode("utf-8"),
                    ("Bearer " + token).encode("ascii"),
                ):
                    self.send_error(403)
                    return
                try:
                    if self.path == "/api/runs":
                        payload: Any = run_list(journal)
                    elif self.path.startswith("/api/runs/"):
                        payload = projection(journal, self.path.removeprefix("/api/runs/"))
                    else:
                        self.send_error(404)
                        return
                    body = json.dumps(payload).encode()
                    content_type = "application/json"
                except (KeyError, ValueError):
                    self.send_error(404)
                    return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), token


def serve(journal: Journal, port: int = 8765) -> None:
    server, token = create_server(journal, port)
    with server:
        print(f"Fleet Inspector: http://127.0.0.1:{server.server_port}/#{token}", flush=True)
        server.serve_forever()
