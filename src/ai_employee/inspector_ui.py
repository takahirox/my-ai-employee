"""Dependency-free static asset for the local read-only Inspector."""

# The embedded HTML/CSS/JavaScript is intentionally dependency-free and browser-native.

INDEX = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Fleet Inspector</title>
<style>
:root{
color-scheme:dark;
--bg:#09111f;
--panel:#111c30;
--line:#53627b;
--muted:#9eacc1;
--text:#edf3fa}
*{
box-sizing:border-box}
body{
margin:0;
background:var(--bg);
color:var(--text);
font:14px system-ui,
sans-serif}
header{
display:flex;
gap:.7rem;
align-items:center;
padding:1rem 1.2rem;
border-bottom:1px solid #293752;
position:sticky;
top:0;
background:#09111ff2;
z-index:5}
h1{
font-size:1.1rem;
margin:0}
input{
width:min(32rem,
42vw)}
input,
select,
button{
color:inherit;
background:#17243b;
border:1px solid #42516d;
border-radius:6px;
padding:.55rem .7rem}
button{
cursor:pointer}
.badge{
font-size:.72rem;
padding:.15rem .45rem;
border:1px solid #64748b;
border-radius:99px}
.readonly{
color:#8de6b0}
.error{
color:#ff9a9a}
main{
padding:1rem}
.toolbar,
.summary,
.legend,
.tabs{
display:flex;
gap:.6rem;
align-items:center;
flex-wrap:wrap;
margin-bottom:.8rem}
.summary,
.muted{
color:var(--muted)}
.tabs button[aria-selected=true]{
background:#29496f}
.workspace{
display:grid;
grid-template-columns:minmax(520px,
1fr) minmax(300px,
420px);
gap:1rem}
.panel{
background:var(--panel);
border:1px solid #293752;
border-radius:10px;
padding:1rem;
min-width:0}
.graph-scroll{
overflow:auto;
min-height:440px}
.graph{
position:relative;
min-height:420px}
.graph svg{
position:absolute;
inset:0;
overflow:visible}
.edge{
stroke:var(--line);
stroke-width:2;
fill:none;
marker-end:url(#arrow)}

.node{
position:absolute;
width:210px;
min-height:94px;
text-align:left;
border-width:2px;
box-shadow:0 6px 20px #0005}
.node strong,
.node small{
display:block}
.node small{
color:#c5d0df;
margin-top:.25rem}
.node.current{
outline:3px solid #fff8;
outline-offset:3px}
.node.entry:before{
content:'ENTRY';
font-size:.62rem}
.node.terminal:after{
content:'TERMINAL';
font-size:.62rem;
float:right}
.state-ready{
border-color:#60a5fa}
.state-waiting,
.state-not_accepted{
border-color:#64748b}
.state-routed{
border-color:#f59e0b}
.state-running{
border-color:#22d3ee;
background:#10364a}
.state-passed{
border-color:#34d399;
background:#113a31}
.state-failed{
border-color:#fb7185;
background:#451b2a}
.state-blocked{
border-color:#f97316;
background:#3f2418}
.state-cancelled{
border-color:#94a3b8;
background:repeating-linear-gradient(135deg,
#17243b,
#17243b 8px,
#293752 8px,
#293752 16px)}
.state-retained{
border-color:#c084fc;
background:#33204d}
.legend span{
font-size:.75rem;
color:var(--muted)}
.details h2{
font-size:1rem;
margin-top:0}
.details details{
border-top:1px solid #293752;
padding:.6rem 0}
.details summary{
cursor:pointer;
font-weight:600}
.details pre,
.raw{
white-space:pre-wrap;
overflow-wrap:anywhere;
color:#d9e2ef;
background:#080f1c;
padding:.7rem;
border-radius:6px;
max-height:24rem;
overflow:auto}
.hidden{
display:none}
.changes{
display:grid;
grid-template-columns:repeat(4,
minmax(0,
1fr));
gap:.4rem}
.changes div{
background:#0b1527;
padding:.5rem;
border-radius:5px}
.changes b{
display:block;
font-size:.7rem;
color:var(--muted)}
@media(max-width:900px){
.workspace{
grid-template-columns:1fr}
input{
width:45vw}
}

</style>
</head>
<body>
<header>
<h1>Fleet Inspector</h1>
<span class="badge readonly">Read only</span>
<input id="run" placeholder="Run ID" aria-label="Run ID">
<button id="inspect">Inspect</button>
<button id="refresh">Refresh latest persisted state</button>
</header>
<main>
<div id="message" class="muted">Enter a persisted run ID. Inspection never invokes workers,
planners,
reviewers,
or evaluators.</div>
<section id="app" class="hidden">
<div class="toolbar">
<label>Accepted revision <select id="revision">
</select>
</label>
<span id="history" class="badge">
</span>
</div>
<div id="summary" class="summary">
</div>
<div class="tabs" role="tablist">
<button data-tab="dag" aria-selected="true">DAG</button>
<button data-tab="raw" aria-selected="false">Raw Inspector record</button>
<button data-tab="explanation" aria-selected="false">Run explanation record</button>
</div>
<div id="dag">
<div class="legend">
<span>ready</span>
<span>waiting</span>
<span>routed</span>
<span>running</span>
<span>passed</span>
<span>failed</span>
<span>blocked</span>
<span>cancelled</span>
<span>retained after replan</span>
</div>
<div class="workspace">
<section class="panel">
<div id="revision-story">
</div>
<div class="graph-scroll">
<div id="graph" class="graph">
</div>
</div>
</section>
<aside id="details" class="panel details">
<h2>Task details</h2>
<p class="muted">Select a task. Facts not persisted are shown as Not recorded.</p>
</aside>
</div>
</div>
<pre id="raw" class="panel raw hidden">
</pre>
<pre id="explanation" class="panel raw hidden">
</pre>
</section>
</main>
<script>
const $=s=>document.querySelector(s);
let raw=null,
story=null,
selectedRevision=null,
selectedTask=null;
const text=v=>v===null||
v===undefined||
v===''?
'Not recorded':typeof v==='string'?
v:JSON.stringify(v,
null,
2);
const maps=v=>Array.isArray(v)?
v.filter(x=>x&&
typeof x==='object'):[];

async function getJSON(url){
const response=await fetch(url,
{
method:'GET',
cache:'no-store'}
);
if(!response.ok)throw new Error('Run not found');
return response.json()}
function message(value,
error=false){
$('#message').textContent=value;
$('#message').className=error?
'error':'muted'}

async function load(preserve=false){
const id=$('#run').value.trim();
if(!id)return message('Enter a run ID.',
true);
message('Loading latest persisted records…');
try{
[raw,
story]=await Promise.all([getJSON('/api/runs/'+
encodeURIComponent(id)),
getJSON('/api/runs/'+
encodeURIComponent(id)+
'/explanation')]);
const revisions=maps(story.graph?.evolution);
selectedRevision=preserve&&
selectedRevision&&
revisions.some(x=>x.digest===selectedRevision.digest)?
revisions.find(x=>x.digest===selectedRevision.digest):
(revisions.find(x=>x.digest===story.graph?.digest)||
revisions.at(-1)||
null);
$('#app').classList.remove('hidden');
message('Loaded persisted state. Refresh is read only.');
render();
history.replaceState(null,
'',
'?run='+
encodeURIComponent(id))}
catch(error){
$('#app').classList.add('hidden');
message(error.message,
true)}
}

function currentRevision(){
return !selectedRevision||
selectedRevision.digest===story.graph?.digest}
function revisionTasks(){
return currentRevision()?
maps(story.graph?.tasks):maps(selectedRevision?.tasks)}
function graphRecord(){
const accepted=maps(raw.graph_revisions).map(x=>x.accepted_revision).find(
x=>x?.content_digest===(selectedRevision?.digest||
story.graph?.digest));
return accepted?.graph||
raw.proposed_graph?.graph||
raw.graph||
{
}
}

function render(){
const revisions=maps(story.graph?.evolution);
$('#revision').replaceChildren(...(revisions.length?
revisions:[{
revision:story.graph?.revision,
digest:story.graph?.digest}
]).map(item=>{
const option=document.createElement('option');
option.value=item.digest||
'';
option.textContent='Revision '+
(item.revision??
'unaccepted')+
(item.digest===story.graph?.digest?
' · current':' · historical');
option.selected=item.digest===(selectedRevision?.digest||
story.graph?.digest);
return option}
));
$('#history').textContent=currentRevision()?
'Current accepted revision':'Historical accepted revision';
const remaining=revisionTasks().filter(x=>position(x)!=='completed').map(x=>x.id);
$('#summary').textContent='Run '+
story.run_id+
' · '+
text(story.current_state?.status)+
' · remaining: '+
remaining.length+
(story.current_state?.next_action?
' · next: '+
story.current_state.next_action:'');
renderRevision();
renderGraph();
if(selectedTask&&
!revisionTasks().some(x=>x.id===selectedTask))selectedTask=null;
renderDetails(selectedTask&&
taskView(selectedTask))}

function renderRevision(){
const root=$('#revision-story'),
item=selectedRevision||
{
}
,
changes=[['Added',
'added_task_ids'],
['Removed',
'removed_task_ids'],
['Retained',
'retained_task_ids'],
['Replaced or rerun',
'redone_task_ids']];
root.replaceChildren();
const p=document.createElement('p');
p.textContent=(item.trigger||
'No replan reason recorded')+
(item.previous_revision_digest?
' · follows '+
item.previous_revision_digest:'');
root.append(p);
const boxes=document.createElement('div');
boxes.className='changes';
for(const [label,
key] of changes){
const box=document.createElement('div'),
b=document.createElement('b');
b.textContent=label;
box.append(b,
document.createTextNode((item[key]||
[]).join(', ')||
'None recorded'));
boxes.append(box)}
root.append(boxes)}

function position(task){
const status=task.execution_state||
task.historical_state||
task.state||
'pending';
if(task.position)return task.position;
return {
passed:'completed',
succeeded:'completed',
completed:'completed',
ready:'ready',
waiting:'waiting',
routed:'active',
running:'active',
active:'active',
failed:'failed',
blocked:'blocked',
cancelled:'cancelled'}
[status]||
'waiting'}
function style(task){
if(selectedRevision?.retained_task_ids?.includes(task.id))return'retained';
const status=task.execution_state||
task.historical_state||
task.state||
'pending';
if(['succeeded',
'completed'].includes(status))return'passed';
return ['routed',
'running',
'passed',
'failed',
'blocked',
'cancelled'].includes(status)?
status:position(task)==='completed'?
'passed':position(task)}

function taskView(id){
const task=revisionTasks().find(x=>x.id===id)||
{
}
,
record=graphRecord(),
definition=maps(record.nodes).find(x=>x.id===id)||
{
}
,
dependencies=task.dependencies?.length?
task.dependencies:maps(record.edges).filter(x=>x.target_id===id).map(x=>x.source_id),
digest=selectedRevision?.digest||
story.graph?.digest,
nodeStory=currentRevision()?
maps(story.task_stories).find(x=>x.task_id===id)||
{
}
:{
}
,
records=name=>maps(raw[name]).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest),
attempts=records('node_history'),
resultDigests=new Set(attempts.map(x=>x.worker_result_digest).filter(Boolean)),
reviews=raw.task_reviews||
{
}
;
return{
id,
label:definition.name||
task.name||
id,
objective:definition.objective||
task.objective||
nodeStory.objective,
completion_criteria:definition.completion_criteria||
nodeStory.completion_criteria||
[],
dependencies,
recorded_state:task.execution_state||
task.historical_state||
task.state||
definition.state||
'pending',
position:position(task),
style_state:style(task),
details:{
state_reason:nodeStory.why_this_state||
[],
routing:records('routes').length?
records('routes'):nodeStory.routing,
predecessor_context:records('worker_context_manifests').length?
records('worker_context_manifests'):nodeStory.information_flow,
attempts:attempts.length?
attempts:nodeStory.execution_attempts||
[],
results:maps(raw.worker_results).filter(x=>resultDigests.has(x.content_digest)),
typed_result_acceptances:records('typed_result_acceptances'),
artifacts:attempts.flatMap(x=>maps(x.artifact_descriptors)).concat(nodeStory.artifacts||
[]),
evidence:records('node_evidence').length?
records('node_evidence'):nodeStory.evidence,
evaluation:records('node_evaluator_decisions').length?
records('node_evaluator_decisions'):nodeStory.evaluation,
reviews:Object.fromEntries(['requests',
'results',
'decisions',
'stale_results'].map(k=>[k,
maps(reviews[k]).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest)])),
retry_repair_replan_decisions:maps(raw.loop_transitions).filter(x=>(x.node_id===id||
x.node_id===null)&&
x.accepted_graph_revision_digest===digest),
retention:records('retained_node_bindings'),
revision_decision:{
reason:selectedRevision?.trigger,
evidence_digests:selectedRevision?.evidence_digests||
[],
triggered_by_task_ids:selectedRevision?.triggered_by_task_ids||
[]}
}
}
}

function renderGraph(){
const root=$('#graph'),
record=graphRecord(),
tasks=revisionTasks().map(x=>taskView(x.id)),
byId=Object.fromEntries(tasks.map(x=>[x.id,
x])),
edges=maps(record.edges).map(x=>({
source_id:x.source_id,
target_id:x.target_id}
));
if(!edges.length)for(const task of tasks)for(const source of task.dependencies)edges.push({
source_id:source,
target_id:task.id}
);
const entries=new Set(record.entry_node_ids||
record.entry_task_ids||
tasks.filter(x=>!x.dependencies.length).map(x=>x.id)),
terminals=new Set(record.terminal_node_ids||
record.terminal_task_ids||
tasks.filter(x=>!edges.some(e=>e.source_id===x.id)).map(x=>x.id)),
depth={
}
;
function level(id,
seen=new Set()){
if(depth[id]!==undefined)return depth[id];
if(seen.has(id))return 0;
seen.add(id);
const deps=(byId[id]?.dependencies||
[]).filter(x=>byId[x]);
return depth[id]=deps.length?
1+
Math.max(...deps.map(x=>level(x,
new Set(seen)))):0}
tasks.forEach(x=>level(x.id));
const groups={
}
;
tasks.forEach(x=>(groups[depth[x.id]]??=[]).push(x));
const points={
}
;
let rows=1;
for(const [column,
items] of Object.entries(groups)){
rows=Math.max(rows,
items.length);
items.forEach((x,
row)=>points[x.id]={
x:30+
Number(column)*260,
y:35+
row*130}
)}
const width=Math.max(760,
80+
(Math.max(0,
...Object.values(depth))+
1)*260),
height=Math.max(420,
80+
rows*130);
root.replaceChildren();
root.style.width=width+
'px';
root.style.height=height+
'px';
const svg=document.createElementNS('http://www.w3.org/2000/svg',
'svg');
svg.setAttribute('width',
width);
svg.setAttribute('height',
height);
svg.innerHTML=
'<defs><marker id="arrow" markerWidth="8" markerHeight="8" '+
'refX="7" refY="3" orient="auto">'+
'<path d="M0,0 L0,6 L8,3 z" fill="#53627b"/>'+
'</marker></defs>';
for(const edge of edges){
const a=points[edge.source_id],
b=points[edge.target_id];
if(!a||
!b)continue;
const line=document.createElementNS(svg.namespaceURI,
'path');
line.setAttribute('class',
'edge');
line.setAttribute('d',
`M${a.x+210},${a.y+47} C${a.x+235},${a.y+47} ${b.x-25},${b.y+47} ${b.x},${b.y+47}`);
svg.append(line)}
root.append(svg);
for(const task of tasks){
const button=document.createElement('button');
button.className='node state-'+
task.style_state+
(entries.has(task.id)?
' entry':'')+
(terminals.has(task.id)?
' terminal':'')+
(currentRevision()&&
task.position==='active'?
' current':'');
button.style.left=points[task.id].x+
'px';
button.style.top=points[task.id].y+
'px';
for(const [tag,
value] of [['strong',
task.label],
['small',
task.recorded_state+
' · '+
task.position],
['small',
style(task)==='retained'?
'retained after replan':selectedRevision?.redone_task_ids?.includes(task.id)?
'replaced or rerun':selectedRevision?.added_task_ids?.includes(task.id)?
'added':'']]){
const element=document.createElement(tag);
element.textContent=value;
button.append(element)}
button.addEventListener('click',
()=>{
selectedTask=task.id;
renderDetails(task)}
);
root.append(button)}
}

function renderDetails(task){
const root=$('#details');
root.replaceChildren();
const h=document.createElement('h2');
h.textContent=task?
'Task · '+
task.id:'Task details';
root.append(h);
if(!task){
const p=document.createElement('p');
p.className='muted';
p.textContent='Select a task. Facts not persisted are shown as Not recorded.';
root.append(p);
return}
add(root,
'Objective, criteria, and state',
{
objective:task.objective,
completion_criteria:task.completion_criteria,
recorded_state:task.recorded_state,
position:task.position,
state_reason:task.details.state_reason,
dependencies:task.dependencies}
,
true);
for(const [label,
key] of [['Strategy and routing reasons',
'routing'],
['Predecessor context',
'predecessor_context'],
['Attempts and failure codes',
'attempts'],
['Artifacts or results',
'results'],
['Typed result acceptances',
'typed_result_acceptances'],
['Artifacts',
'artifacts'],
['Evidence and evaluation',
'evidence'],
['Reviews',
'reviews'],
['Retry, repair, or replan decisions',
'retry_repair_replan_decisions'],
['Retained-after-replan binding',
'retention'],
['Revision decision',
'revision_decision']])add(root,
label,
task.details[key])}

function add(root,
label,
value,
open=false){
const details=document.createElement('details'),
summary=document.createElement('summary'),
pre=document.createElement('pre');
details.open=open;
summary.textContent=label;
const absent=value===null||
value===undefined||
(Array.isArray(value)&&
!value.length)||
(typeof value==='object'&&
!Array.isArray(value)&&
!Object.keys(value).length);
pre.textContent=absent?
'Not recorded':text(value);
details.append(summary,
pre);
root.append(details)}

function selectTab(name){
for(const button of document.querySelectorAll('[data-tab]'))button.setAttribute('aria-selected',
String(button.dataset.tab===name));
for(const id of ['dag',
'raw',
'explanation'])$('#'+
id).classList.toggle('hidden',
id!==name);
if(name==='raw')$('#raw').textContent=JSON.stringify(raw,
null,
2);
if(name==='explanation')$('#explanation').textContent=JSON.stringify(story,
null,
2)}

$('#inspect').addEventListener('click',
()=>load());
$('#refresh').addEventListener('click',
()=>load(true));
$('#revision').addEventListener('change',
event=>{
selectedRevision=maps(story.graph?.evolution).find(x=>x.digest===event.target.value)||
null;
selectedTask=null;
render()}
);
$('#run').addEventListener('keydown',
event=>{
if(event.key==='Enter')load()}
);
for(const button of document.querySelectorAll('[data-tab]'))button.addEventListener('click',
()=>selectTab(button.dataset.tab));
const initial=new URLSearchParams(location.search).get('run');
if(initial){
$('#run').value=initial;
load()}

</script>
</body>
</html>"""
