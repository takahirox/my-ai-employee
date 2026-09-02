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
.readonly,
.connection.live{
color:#8de6b0}
.connection.reconnecting{
color:#facc6b}
.connection.disconnected,
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
.run-warning-summary{
border:1px solid #f59e0b;
border-radius:10px;
background:#211d19;
padding:.7rem .85rem;
margin:0 0 .8rem}
.run-warning-summary h2{
font-size:1rem;
margin:0}
.warning-list{
margin:.5rem 0 0;
padding-left:1.25rem}
.warning-item{
margin:.45rem 0}
.warning-row{
display:flex;
align-items:center;
gap:.5rem;
flex-wrap:wrap}
.warning-row button{
padding:.3rem .5rem}
.warning-item details{
margin-top:.25rem}
.warning-item pre{
white-space:pre-wrap;
overflow-wrap:anywhere;
background:#080f1c;
padding:.55rem;
border-radius:6px;
max-height:15rem;
overflow:auto}
.tabs button[aria-selected=true]{
background:#29496f}
.overview-head{
display:flex;
justify-content:space-between;
align-items:center;
gap:1rem;
margin-bottom:1rem}
.run-grid{
display:grid;
grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr));
gap:.8rem;
margin-bottom:1.4rem}
.run-card{
display:block;
width:100%;
min-width:0;
text-align:left;
background:var(--panel);
border:1px solid #293752;
border-radius:10px;
padding:.7rem .8rem}
.run-card:hover,
.run-card:focus-visible{
border-color:#60a5fa}
.run-card.attention-card{
border-color:#f59e0b;
background:#211d19}
.job-children{
display:grid;
gap:.35rem;
margin-top:.65rem;
padding-top:.65rem;
border-top:1px solid #293752}
.job-child{
display:flex;
align-items:center;
gap:.5rem;
width:100%;
padding:.45rem .55rem;
text-align:left}
.job-child-label{
flex:1;
min-width:0;
overflow:hidden;
text-overflow:ellipsis;
white-space:nowrap}
.job-child .badge{
flex:none}
.run-card h3{
display:-webkit-box;
margin:0 0 .3rem;
font-size:1rem;
line-height:1.25;
max-height:2.5em;
-webkit-line-clamp:2;
-webkit-box-orient:vertical;
overflow:hidden;
overflow-wrap:anywhere}
.run-repository,
.run-activity{
overflow:hidden;
text-overflow:ellipsis;
white-space:nowrap}
.run-repository{
margin-bottom:.45rem}
.run-card-meta{
display:flex;
align-items:center;
gap:.4rem;
min-width:0;
margin-bottom:.4rem}
.run-status{
max-width:45%;
overflow:hidden;
text-overflow:ellipsis;
white-space:nowrap}
.run-progress{
display:flex;
align-items:center;
gap:.3rem;
color:var(--muted);
font-size:.75rem;
min-width:0}
.run-progress progress{
width:3.5rem;
height:.45rem;
accent-color:#60a5fa}
.attention{
color:#ffcf86;
margin-left:auto;
white-space:nowrap}
.run-card-footer{
display:flex;
align-items:center;
gap:.6rem;
min-width:0}
.run-activity{
flex:1;
min-width:0;
margin:0;
color:var(--muted);
font-size:.82rem}
.run-updated{
flex:none;
font-size:.75rem;
margin-left:auto;
white-space:nowrap}
.empty{
padding:1rem;
border:1px dashed #42516d;
border-radius:8px;
color:var(--muted)}
details.history{
margin-top:1rem}
details.history>summary{
cursor:pointer;
font-size:1.25rem;
font-weight:700;
margin-bottom:.8rem}
#back-to-fleet{
margin-bottom:.8rem}
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
min-height:150px;
text-align:left;
border-width:2px;
padding:.5rem .6rem;
box-shadow:0 6px 20px #0005;
overflow:hidden}
.node strong{
display:block;
margin:.25rem 0;
line-height:1.2;
max-height:2.4em;
overflow:hidden;
overflow-wrap:anywhere}
.node-status{
display:inline-block;
font-size:.65rem;
font-weight:700;
letter-spacing:.04em}
.node-facts{
display:grid;
grid-template-columns:repeat(2,minmax(0,1fr));
gap:.2rem .4rem;
margin-top:.35rem}
.node-fact{
display:block;
min-width:0;
font-size:.68rem;
color:#c5d0df;
overflow:hidden;
text-overflow:ellipsis;
white-space:nowrap}
.node-fact b{
display:block;
font-size:.58rem;
font-weight:600;
color:var(--muted);
text-transform:uppercase}
.node.current{
outline:3px solid #fff8;
outline-offset:3px}
.node.selected{
outline:3px solid #60a5fa;
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
.state-overdue{
border-color:#fbbf24;
background:#4a3010;
box-shadow:0 0 0 2px #fbbf2455,0 6px 20px #0005}
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
.task-summary,
.task-activity{
border:1px solid #293752;
border-radius:8px;
padding:.7rem .8rem;
margin-bottom:.8rem}
.task-summary h3,
.task-activity h3{
font-size:.9rem;
margin:0 0 .4rem}
.task-summary p{
margin:.25rem 0;
line-height:1.45}
.task-activity ol{
list-style:none;
padding:0;
margin:.2rem 0 0}
.task-activity li{
display:grid;
grid-template-columns:minmax(7rem,auto) 1fr;
gap:.55rem;
padding:.3rem 0;
border-top:1px solid #293752}
.task-activity li:first-child{
border-top:0}
.activity-time{
color:var(--muted);
font-size:.75rem}
.details details,
#revision-story details{
border-top:1px solid #293752;
padding:.6rem 0}
.details summary,
#revision-story summary{
cursor:pointer;
font-weight:600}
.details pre,
#revision-story pre,
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
@media(max-width:600px){
header{
position:static;
flex-wrap:wrap}
header label,
header select,
#refresh{
width:100%}
main,
.panel{
padding:.65rem}
.changes{
grid-template-columns:repeat(2,minmax(0,1fr))}
}

</style>
</head>
<body>
<header>
<h1>Fleet Inspector</h1>
<span class="badge readonly">Read only</span>
<span id="connection-status" class="badge connection reconnecting">Connecting</span>
<label>Repository <select id="repository-filter" aria-label="Repository filter">
<option value="">All repositories</option>
</select>
</label>
<button id="refresh">Refresh latest persisted state</button>
</header>
<main>
<div id="message" class="muted">Loading persisted Fleet runs. Inspection never invokes workers,
planners,
reviewers,
or evaluators.</div>
<section id="fleet-overview">
<div class="overview-head">
<h2>Fleet Jobs and standalone runs</h2>
<span class="muted">Open a Job child to inspect its Run, revisions, and tasks.</span>
</div>
<h2>Active <span id="active-count" class="muted"></span></h2>
<div id="active-runs" class="run-grid"></div>
<details id="history-section" class="history">
<summary>History <span id="history-count" class="muted"></span></summary>
<div id="history-runs" class="run-grid"></div>
</details>
</section>
<section id="app" class="hidden">
<button id="back-to-fleet">← Fleet overview</button>
<div class="toolbar">
<label>Graph revision <select id="revision">
</select>
</label>
<span id="history" class="badge">
</span>
</div>
<div id="summary" class="summary">
</div>
<section id="warning-summary" class="run-warning-summary hidden" aria-live="polite">
</section>
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
<span>overdue</span>
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
selectedTask=null,
selectedTab='dag',
selectedRun=null,
overview=null,
eventSource=null,
reconnectFailures=0,
relativeTimeTimer=null,
refreshQueued=false,
refreshActive=false;
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
const shortTime=v=>{
if(!v)return'Not recorded';
const date=new Date(v);
return Number.isNaN(date.getTime())?'Not recorded':date.toLocaleString()};
const relativeTime=(v,
now=Date.now())=>{
const date=new Date(v),
timestamp=date.getTime();
if(Number.isNaN(timestamp))return'Not recorded';
const distance=Math.abs(now-timestamp);
if(distance<60000)return'just now';
const units=[
['day',86400000],
['hour',3600000],
['min',60000]],
unit=units.find(item=>distance>=item[1])||
units.at(-1),
amount=Math.floor(distance/unit[1]),
label=unit[0]+(amount===1?'':'s');
return timestamp>now?
'in '+amount+' '+label:
amount+' '+label+' ago'};
function refreshRelativeTimes(now=Date.now()){
for(const updated of document.querySelectorAll(
'.run-updated[data-timestamp]')){
updated.textContent='Updated: '+
relativeTime(updated.dataset.timestamp,
now)}
}
function startRelativeTimeRefresh(){
if(relativeTimeTimer!==null)return;
relativeTimeTimer=window.setInterval(
refreshRelativeTimes,
30000)}
const duration=v=>{
const seconds=Number(v);
if(v===null||
v===undefined||
!Number.isFinite(seconds)||
seconds<0)return'Not recorded';
const whole=Math.floor(seconds),
hours=Math.floor(whole/3600),
minutes=Math.floor((whole%3600)/60),
remainder=whole%60;
return hours?
hours+'h '+minutes+'m':minutes?
minutes+'m '+remainder+'s':whole+'s'};

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

function showOverview(){
selectedRun=null;
$('#app').classList.add('hidden');
$('#fleet-overview').classList.remove('hidden');
history.replaceState(null,'',location.pathname);
}

function openRun(id){
selectedRun=id;
load();
}

function renderRunGroup(rootId,runs,emptyLabel){
const root=$('#'+rootId);
root.replaceChildren();
if(!runs.length){
const empty=document.createElement('p');
empty.className='empty';
empty.textContent=emptyLabel;
root.append(empty);
return}
for(const run of runs){
const isJob=run.kind==='job',
children=maps(run.child_graph_runs),
card=document.createElement(isJob?'section':'button');
card.className='run-card';
card.classList.toggle('attention-card',Boolean(run.requires_attention));
const title=document.createElement('h3');
title.textContent=run.goal||run.run_id;
title.title=run.goal||run.run_id;
const repositoryText=run.repository||
'Legacy / unassigned repository';
const repository=document.createElement('div');
repository.className='run-repository muted';
repository.textContent=repositoryText;
repository.title=repositoryText;
const status=document.createElement('span');
status.className='badge run-status state-'+
({
completed:'passed',
succeeded:'passed',
waiting_approval:'routed'}
[run.status]||
(['ready',
'waiting',
'routed',
'running',
'passed',
'failed',
'blocked',
'cancelled',
'retained'].includes(run.status)?
run.status:'waiting'));
status.textContent=text(run.status);
status.title='Run status: '+text(run.status);
const progressData=run.progress||{},
completed=Number(progressData.completed)||0,
total=Number(progressData.total)||0;
const progressGroup=document.createElement('span');
progressGroup.className='run-progress';
const progress=document.createElement('progress');
progress.max=Math.max(total,1);
progress.value=Math.min(completed,progress.max);
progress.setAttribute(
'aria-label',
completed+' of '+total+' tasks completed');
const progressText=document.createElement('span');
progressText.textContent=completed+'/'+total;
progressGroup.append(progress,progressText);
const attention=document.createElement('span');
attention.className='attention';
if(run.attention_available===false){
attention.title='Persisted warning facts were not recorded for this historical run';
attention.textContent='Warnings unknown'}
else{
const attentionConditions=maps(run.attention).map(x=>x.task_id?
x.task_id+': '+x.condition:x.condition),
attentionCount=Number.isInteger(run.attention_count)?
run.attention_count:attentionConditions.length;
attention.title=attentionConditions.length?
attentionConditions.join(', '):'No persisted attention conditions';
attention.textContent=attentionCount+' warning'+
(attentionCount===1?'':'s')}
const activity=document.createElement('p');
activity.className='run-activity';
const taskOrPhase=run.active_task||run.phase||
'No active task or phase recorded';
const displayedTaskOrPhase=isJob?
'Current: '+text(run.current_status)+' · '+children.length+' child Graph Run(s)':
taskOrPhase;
activity.textContent=displayedTaskOrPhase;
activity.title=displayedTaskOrPhase;
const ownership=document.createElement('p');
ownership.className='run-activity muted';
const ownershipFacts=[];
if(run.diagnostic_code)ownershipFacts.push(run.diagnostic_code);
if(run.owner_instance_id)ownershipFacts.push('Owner: '+run.owner_instance_id);
if(run.last_heartbeat)ownershipFacts.push('Heartbeat: '+run.last_heartbeat);
if(run.lease_expiry)ownershipFacts.push('Lease expiry: '+run.lease_expiry);
ownership.textContent=ownershipFacts.join(' · ');
ownership.title=ownership.textContent;
const updated=document.createElement('span');
updated.className='run-updated muted';
if(run.last_updated_at){
updated.dataset.timestamp=run.last_updated_at;
updated.title='Last updated at '+run.last_updated_at;
updated.setAttribute(
'aria-label',
'Last updated at '+run.last_updated_at);
updated.textContent='Updated: '+relativeTime(run.last_updated_at)}
else{
updated.title='No trustworthy persisted update timestamp was recorded';
updated.setAttribute(
'aria-label',
'Last updated: Not recorded');
updated.textContent='Updated: Not recorded'}
const meta=document.createElement('div');
meta.className='run-card-meta';
meta.append(status,progressGroup,attention);
const footer=document.createElement('div');
footer.className='run-card-footer';
footer.append(activity);
if(ownershipFacts.length)footer.append(ownership);
footer.append(updated);
card.append(title,repository,meta,footer);
if(isJob){
const childList=document.createElement('div');
childList.className='job-children';
for(const child of children){
const childButton=document.createElement('button');
childButton.className='job-child';
const childLabel=document.createElement('span');
childLabel.className='job-child-label';
childLabel.textContent='#'+text(child.job_sequence)+' '+
(child.goal||child.run_id);
childLabel.title=child.goal||child.run_id;
const childStatus=document.createElement('span');
childStatus.className='badge';
childStatus.textContent=text(child.status);
childButton.append(childLabel,childStatus);
childButton.setAttribute(
'aria-label','Open child Graph Run '+child.run_id);
childButton.addEventListener('click',()=>openRun(child.run_id));
childList.append(childButton)}
card.append(childList)}
else card.addEventListener('click',()=>openRun(run.run_id));
root.append(card)}
}

async function loadOverview(){
const repository=$('#repository-filter').value;
const suffix=repository?'?repository_id='+encodeURIComponent(repository):'';
overview=await getJSON('/api/overview'+suffix);
renderRunGroup('active-runs',maps(overview.active),'No active Fleet runs.');
renderRunGroup('history-runs',maps(overview.history),'No historical Fleet runs.');
$('#active-count').textContent='('+maps(overview.active).length+')';
$('#history-count').textContent='('+maps(overview.history).length+')';
message('Showing '+maps(overview.active).length+
' active and '+maps(overview.history).length+
' historical run(s).');
}

async function refreshRunCatalog(){
const selected=$('#repository-filter').value,
payload=await getJSON('/api/runs'),
repositories=new Map();
for(const item of maps(payload.runs))if(item.repository_id&&
item.repository)repositories.set(item.repository_id,
item.repository);
const all=document.createElement('option');
all.value='';
all.textContent='All repositories';
$('#repository-filter').replaceChildren(all,
...Array.from(repositories.entries()).map(([id,
repository])=>{
const option=document.createElement('option');
option.value=id;
option.textContent=repository;
return option}
));
if(repositories.has(selected))$('#repository-filter').value=selected;
await loadOverview()}

async function load(preserve=false){
const id=selectedRun;
if(!id)return message('Select a persisted run.',
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
$('#fleet-overview').classList.add('hidden');
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

function latestRevisionSelected(){
return !selectedRevision||
selectedRevision.digest===story.graph?.digest}
function graphAccepted(){
return story.source_kind==='legacy_run'||
story.graph?.accepted===true}
function currentRevision(){
return graphAccepted()&&
latestRevisionSelected()}
function revisionTasks(){
return latestRevisionSelected()?
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
(graphAccepted()?
' · current accepted':' · current unaccepted'):' · historical accepted');
option.selected=item.digest===(selectedRevision?.digest||
story.graph?.digest);
return option}
));
$('#history').textContent=latestRevisionSelected()?
(graphAccepted()?
'Current accepted revision':'Current unaccepted proposal'):'Historical accepted revision';
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
renderWarningSummary();
renderGraph();
if(selectedTask&&
!revisionTasks().some(x=>x.id===selectedTask))selectedTask=null;
renderDetails(selectedTask&&
taskView(selectedTask));
selectTab(selectedTab)}

function attentionEvidence(item){
const digest=selectedRevision?.digest||story.graph?.digest;
if(item.kind==='task'){
const matches=maps(raw.node_history).concat(maps(raw.nodes)).filter(x=>
x.node_id===item.task_id&&
(!digest||!x.accepted_graph_revision_digest||
x.accepted_graph_revision_digest===digest));
const record=matches.at(-1);
return record?{source:'persisted node record',record}:null}
if(item.kind==='run')return{
source:'persisted run and explanation records',
state:raw.state,
failure_code:raw.failure_code||raw.run?.failure_code,
failure_path:story.failure_path};
if(item.kind==='loop'){
const record=maps(raw.loop_transitions).filter(x=>
String(x.action||x.decision||'').toLowerCase()===item.condition).at(-1);
return record?{source:'persisted loop transition',record}:null}
if(item.kind==='approval'){
const record=maps(raw.approvals).concat(maps(raw.approval_requests)).filter(x=>
String(x.decision||x.status||'').toLowerCase()==='pending').at(-1);
return record?{source:'persisted approval record',record}:null}
if(item.kind==='plan_review')return raw.plan_review?
{source:'persisted plan review',record:raw.plan_review}:null;
if(item.kind==='control'){
const record=maps(raw.controls).filter(x=>
String(x.action||'').toLowerCase()===item.condition).at(-1);
return record?{source:'persisted control record',record}:null}
return null}

function focusWarningTask(taskId){
const current=maps(story.graph?.evolution).find(x=>
x.digest===story.graph?.digest);
if(current)selectedRevision=current;
selectedTask=taskId;
selectedTab='dag';
render();
requestAnimationFrame(()=>{
const node=[...document.querySelectorAll('.node')].find(x=>
x.dataset.taskId===taskId);
node?.focus();
$('#details').scrollIntoView({block:'nearest'})})}

function renderWarningSummary(){
const root=$('#warning-summary');
root.replaceChildren();
root.classList.remove('hidden');
const heading=document.createElement('h2');
if(raw.attention_available===false){
heading.textContent='Warning details unavailable';
const note=document.createElement('p');
note.className='muted';
note.textContent='Persisted attention facts were not recorded for this historical run.';
root.append(heading,note);
return}
const warnings=maps(raw.attention);
if(!warnings.length){
root.classList.add('hidden');
return}
const count=Number.isInteger(raw.attention_count)?
raw.attention_count:warnings.length;
heading.textContent='Warnings ('+count+')';
const list=document.createElement('ul');
list.className='warning-list';
for(const item of warnings){
const row=document.createElement('li'),
line=document.createElement('div'),
label=document.createElement('strong');
row.className='warning-item';
line.className='warning-row';
label.textContent=item.kind==='task'?
'Task '+text(item.task_id)+': '+text(item.condition):
text(item.kind)+': '+text(item.condition);
line.append(label);
const action=document.createElement('button');
if(item.kind==='task'){
action.textContent='Focus task';
action.setAttribute('aria-label','Focus warning task '+text(item.task_id));
action.addEventListener('click',()=>focusWarningTask(item.task_id))}
else{
action.textContent='Open run explanation';
action.addEventListener('click',()=>{
selectTab('explanation');
$('#explanation').tabIndex=-1;
$('#explanation').focus()})}
line.append(action);
row.append(line);
const evidence=attentionEvidence(item);
if(evidence){
const details=document.createElement('details'),
summary=document.createElement('summary'),
pre=document.createElement('pre');
summary.textContent='Persisted source';
pre.textContent=JSON.stringify(evidence,null,2);
details.append(summary,pre);
row.append(details)}
else{
const absent=document.createElement('span');
absent.className='muted';
absent.textContent='Persisted source not recorded';
row.append(absent)}
list.append(row)}
root.append(heading,list)}

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
['Redone (replacement or rerun not distinguished)',
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
root.append(boxes);
add(root,
'Revision provenance',
{
triggered_by_task_ids:item.triggered_by_task_ids||
[],
evidence_digests:item.evidence_digests||
[],
added_task_summaries:item.added_task_summaries||
[],
removed_task_summaries:item.removed_task_summaries||
[]}
)}

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
const status=task.operational_status||
task.execution_state||
task.historical_state||
task.state||
'pending';
if(['succeeded',
'completed'].includes(status))return'passed';
return ['routed',
'running',
'overdue',
'passed',
'failed',
'blocked',
'cancelled'].includes(status)?
status:position(task)==='completed'?
'passed':position(task)}

function activityTimestamp(record){
return record?.transitioned_at||
record?.created_at||
record?.last_persisted_activity_at||
record?.finished_at||
record?.running_started_at||
null}

function taskActivities(id,
digest,
attempts,
resultDigests,
records,
reviews,
loopTransitions){
const activity=[],
push=(record,label,detail=null)=>activity.push({
timestamp:activityTimestamp(record),
label,
detail});
for(const record of attempts){
const attempt=Number(record.attempt)+1,
status=record.status;
if(status==='pending')push(record,'Worker attempt '+attempt+' queued');
else if(status==='routed')push(record,'Worker strategy selected for attempt '+attempt);
else if(status==='running')push(record,'Worker attempt '+attempt+' started');
else if(status==='passed')push(record,'Task passed evaluation');
else if(status==='failed')push(record,
'Worker attempt '+attempt+' failed',record.failure_code);
else if(status==='blocked')push(record,'Task blocked',record.failure_code);
else if(status==='cancelled')push(record,'Task cancelled',record.failure_code)}
for(const result of maps(raw.worker_results).filter(x=>
resultDigests.has(x.content_digest))){
push(result,'Worker result persisted: '+text(result.status));
for(const proposal of maps(result.proposals)){
if(proposal.kind==='edit_intent'){
const paths=Array.isArray(proposal.payload?.paths)?proposal.payload.paths:[];
push(proposal,'Proposed edits to '+paths.length+' file'+
(paths.length===1?'':'s'),paths.slice(0,3).join(', '))}
else push(proposal,'Proposed '+text(proposal.kind).replaceAll('_',' ')+' action')}}
for(const descriptor of attempts.flatMap(x=>maps(x.artifact_descriptors)))push(
descriptor,
'Produced '+text(descriptor.logical_kind).replaceAll('_',' ')+' artifact');
for(const acceptance of records('typed_result_acceptances'))push(
acceptance,
text(acceptance.status)+' typed result',acceptance.failure_code);
for(const evidence of records('node_evidence'))push(
evidence,'Completion evidence recorded');
for(const decision of records('node_evaluator_decisions'))push(
decision,'Evaluation decision recorded',
decision.decision?.outcome||decision.decision?.status||decision.decision);
for(const diagnostic of maps(raw.worker_boundary_diagnostics).filter(x=>
x.node_id===id&&
(!digest||x.accepted_graph_revision_digest===digest)))push(
diagnostic,'Worker boundary event: '+text(diagnostic.stage),diagnostic.code);
for(const transition of loopTransitions)push(
transition,'Loop decision: '+text(transition.action),transition.reason_code);
for(const authority of maps(raw.worker_timeout_authorities).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest))push(
authority,'Worker deadline applied: '+duration(authority.effective_timeout_seconds));
for(const watchdog of maps(raw.node_watchdogs).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest))push(
watchdog,'Scheduler watchdog: '+text(watchdog.outcome));
for(const control of maps(raw.node_control_propagations).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest))push(
control,'Parent cancellation propagated',
control.cleanup_confirmed?'cleanup confirmed':'cleanup not confirmed');
for(const decision of maps(reviews.decisions).filter(x=>
x.node_id===id&&
(!digest||x.accepted_graph_revision_digest===digest)))push(
decision,'Task review decision recorded',decision.action||decision.decision);
const unique=[],
seen=new Set();
for(const item of activity.sort((a,b)=>
String(b.timestamp||'').localeCompare(String(a.timestamp||'')))){
const key=[item.timestamp,item.label,text(item.detail)].join('|');
if(!seen.has(key)){
seen.add(key);
unique.push(item)}}
return unique.slice(0,6)}

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
nodeStory=latestRevisionSelected()?
maps(story.task_stories).find(x=>x.task_id===id)||
{
}
:{
}
,
records=name=>maps(raw[name]).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest),
attempts=records('node_history'),
latest=maps(raw.nodes).find(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest)||
attempts.at(-1)||
{
}
,
routeRecords=records('routes'),
selectedStrategy=latest.selected_strategy_id||
nodeStory.routing?.selected_strategy?.id||
routeRecords.at(-1)?.selected_strategy?.id,
operationalStatus=latest.operational_status||
latest.status||
task.execution_state||
task.historical_state||
task.state||
'pending',
stateReasons=Array.isArray(nodeStory.why_this_state)&&
nodeStory.why_this_state.length?
nodeStory.why_this_state:attempts.map(x=>({
status:x.status,
transitioned_at:x.transitioned_at,
failure_code:x.failure_code,
generation:x.generation,
attempt:x.attempt,
sequence:x.sequence}
)),
resultDigests=new Set(attempts.map(x=>x.worker_result_digest).filter(Boolean)),
reviews=raw.task_reviews||
{
},
loopTransitions=maps(raw.loop_transitions).filter(x=>(x.node_id===id||
(x.node_id===null&&
Array.isArray(selectedRevision?.triggered_by_task_ids)&&
selectedRevision.triggered_by_task_ids.includes(id)))&&
x.accepted_graph_revision_digest===digest),
childRunIds=new Set(maps(raw.worker_timeout_authorities).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest).map(x=>x.child_run_id)),
activity=taskActivities(id,digest,attempts,resultDigests,records,reviews,loopTransitions)
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
recorded_state:latest.status||
task.execution_state||
task.historical_state||
task.state||
definition.state||
'pending',
operational_status:operationalStatus,
attempt:latest.attempt,
selected_strategy_id:selectedStrategy,
activity,
latest_loop_transition:loopTransitions.at(-1),
latest_watchdog:maps(raw.node_watchdogs).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest).at(-1),
operational:{
status:operationalStatus,
running_started_at:latest.running_started_at,
last_persisted_activity_at:latest.last_persisted_activity_at,
finished_at:latest.finished_at,
elapsed_seconds:latest.elapsed_seconds,
wall_time_budget_seconds:latest.wall_time_budget_seconds,
deadline_at:latest.deadline_at,
overdue:Boolean(latest.overdue),
failure_code:latest.failure_code,
verification_count:Number(latest.verification_count)||0},
position:position(task),
style_state:style({...task,
operational_status:operationalStatus}),
details:{
state_reason:stateReasons,
operational_facts:{
status:operationalStatus,
attempt:latest.attempt,
selected_strategy_id:selectedStrategy,
running_started_at:latest.running_started_at,
last_persisted_activity_at:latest.last_persisted_activity_at,
finished_at:latest.finished_at,
elapsed_seconds:latest.elapsed_seconds,
wall_time_budget_seconds:latest.wall_time_budget_seconds,
deadline_at:latest.deadline_at,
overdue:Boolean(latest.overdue),
failure_code:latest.failure_code,
verification_count:Number(latest.verification_count)||0},
routing:routeRecords.length?
routeRecords:nodeStory.routing,
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
deadline_and_containment:{
timeout_authority:maps(raw.worker_timeout_authorities).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest),
watchdogs:maps(raw.node_watchdogs).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest),
control_propagation:maps(raw.node_control_propagations).filter(x=>x.node_id===id&&
x.accepted_graph_revision_digest===digest),
child_worker_outcomes:Object.fromEntries(Object.entries(raw.child_worker_outcomes||{}).map(
([key,items])=>[key,maps(items).filter(x=>childRunIds.has(x.run_id))])),
doctor_incidents:maps(raw.doctor?.incidents).filter(x=>x.node_id===id)},
revision_decision:{
reason:selectedRevision?.trigger,
evidence_digests:selectedRevision?.evidence_digests||
[],
triggered_by_task_ids:selectedRevision?.triggered_by_task_ids||
[]}
}
}
}

function cardFacts(task){
const facts=[['Attempt',text(task.attempt)],
['Strategy',text(task.selected_strategy_id)]],
state=task.operational_status;
if(state==='running'||state==='overdue')facts.push(
['Started',shortTime(task.operational.running_started_at)],
['Activity',shortTime(task.operational.last_persisted_activity_at)],
['Elapsed / budget',duration(task.operational.elapsed_seconds)+' / '+
duration(task.operational.wall_time_budget_seconds)],
['Deadline',shortTime(task.operational.deadline_at)]);
else if(state==='failed')facts.push(
['Failed',shortTime(task.operational.finished_at)],
['Elapsed',duration(task.operational.elapsed_seconds)],
['Failure',text(task.operational.failure_code)]);
else if(state==='blocked')facts.push(
['Blocked',shortTime(task.operational.finished_at)],
['Reason',text(task.operational.failure_code)]);
else if(state==='passed')facts.push(
['Finished',shortTime(task.operational.finished_at)],
['Elapsed',duration(task.operational.elapsed_seconds)],
['Verification',String(task.operational.verification_count)]);
return facts}

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
row*180}
)}
const width=Math.max(760,
80+
(Math.max(0,
...Object.values(depth))+
1)*260),
height=Math.max(420,
80+
rows*180);
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
' current':'')+
(selectedTask===task.id?
' selected':'');
button.dataset.taskId=task.id;
if(selectedTask===task.id)button.setAttribute('aria-current','true');
button.style.left=points[task.id].x+
'px';
button.style.top=points[task.id].y+
'px';
const status=document.createElement('span');
status.className='node-status';
status.textContent=task.operational_status.toUpperCase();
const name=document.createElement('strong');
name.textContent=task.label;
name.title=task.label;
const facts=document.createElement('span');
facts.className='node-facts';
const factValues=cardFacts(task);
for(const [label,value] of factValues){
const fact=document.createElement('span'),
key=document.createElement('b');
fact.className='node-fact';
key.textContent=label;
fact.append(key,document.createTextNode(value));
fact.title=label+': '+value;
facts.append(fact)}
button.setAttribute('aria-label',
task.operational_status+' task '+task.label+'; '+
factValues.map(x=>x[0]+': '+x[1]).join('; '));
button.append(status,name,facts);
button.addEventListener('click',
()=>{
selectedTask=task.id;
for(const node of document.querySelectorAll('.node')){
node.classList.toggle('selected',node.dataset.taskId===task.id);
node.toggleAttribute('aria-current',node.dataset.taskId===task.id)}
renderDetails(task)}
);
root.append(button)}
}

function taskStageSummary(task){
const status=task.operational_status,
attempt=task.attempt===null||task.attempt===undefined?
'Not recorded':String(Number(task.attempt)+1),
failure=task.operational.failure_code;
let stage;
if(status==='running'||status==='overdue')stage=
'Worker attempt '+attempt+' is '+status+'. Elapsed: '+
duration(task.operational.elapsed_seconds)+'.';
else if(status==='routed')stage='Worker attempt '+attempt+' is routed to '+
text(task.selected_strategy_id)+'.';
else if(status==='passed')stage='The task passed evaluation with '+
task.operational.verification_count+' verification record'+
(task.operational.verification_count===1?'':'s')+'.';
else if(status==='failed')stage='The task failed'+
(failure?' with '+failure:'')+'.';
else if(status==='blocked')stage='The task is blocked'+
(failure?' by '+failure:'')+'.';
else if(status==='cancelled')stage='The task was cancelled'+
(failure?' with '+failure:'')+'.';
else stage='The task is '+text(status)+'.';
const loop=task.latest_loop_transition;
if(loop)stage+=' Latest loop decision: '+text(loop.action)+
(loop.reason_code?' ('+loop.reason_code+')':'')+'.';
if(task.latest_watchdog)stage+=' Watchdog: '+text(task.latest_watchdog.outcome)+'.';
return stage}

function renderTaskSummary(root,task){
const section=document.createElement('section'),
heading=document.createElement('h3'),
objective=document.createElement('p'),
stage=document.createElement('p'),
objectiveLabel=document.createElement('b'),
stageLabel=document.createElement('b');
section.className='task-summary';
heading.textContent='Task Summary';
objectiveLabel.textContent='Objective: ';
stageLabel.textContent='Stage: ';
objective.append(objectiveLabel,
document.createTextNode(task.objective||'Not recorded'));
stage.append(stageLabel,document.createTextNode(taskStageSummary(task)));
section.append(heading,objective,stage);
root.append(section)}

function renderTaskActivity(root,task){
const section=document.createElement('section'),
heading=document.createElement('h3'),
list=document.createElement('ol'),
items=[...task.activity];
section.className='task-activity';
heading.textContent='Current / Recent Activity';
if(['running','overdue'].includes(task.operational_status))items.unshift({
current:true,
timestamp:task.operational.last_persisted_activity_at||
task.operational.running_started_at,
label:'Worker attempt '+
(task.attempt===null||task.attempt===undefined?
'Not recorded':String(Number(task.attempt)+1))+' is '+
task.operational_status,
detail:null});
if(!items.length){
const absent=document.createElement('p');
absent.className='muted';
absent.textContent='Not recorded';
section.append(heading,absent);
root.append(section);
return}
for(const item of items){
const row=document.createElement('li'),
time=document.createElement('time'),
description=document.createElement('span');
time.className='activity-time';
if(item.timestamp){
time.dateTime=item.timestamp;
time.title=item.timestamp;
time.textContent=item.current?'Current · '+shortTime(item.timestamp):
shortTime(item.timestamp)}
else time.textContent=item.current?'Current':'Time not recorded';
description.textContent=item.label+
(item.detail?' · '+text(item.detail):'');
row.append(time,description);
list.append(row)}
section.append(heading,list);
root.append(section)}

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
renderTaskSummary(root,task);
renderTaskActivity(root,task);
add(root,
'Operational facts',
task.details.operational_facts,
true);
add(root,
'Objective, criteria, and state',
{
objective:task.objective,
completion_criteria:task.completion_criteria,
recorded_state:task.recorded_state,
position:task.position,
state_reason:task.details.state_reason,
dependencies:task.dependencies}
);
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
['Deadline, cancellation, and Fleet Doctor',
'deadline_and_containment'],
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
selectedTab=name;
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

function connectionStatus(state,
label){
const status=$('#connection-status');
status.className='badge connection '+
state;
status.textContent=label}

async function refreshFreshState(){
refreshQueued=true;
if(refreshActive)return;
refreshActive=true;
try{
while(refreshQueued){
refreshQueued=false;
await refreshRunCatalog();
if(!$('#app').classList.contains('hidden')&&selectedRun)await load(true)}
}
catch(error){
message(error.message,
true)}
finally{
refreshActive=false}
}

function connectEvents(){
if(!window.EventSource){
connectionStatus('disconnected',
'Disconnected');
return}
eventSource=new EventSource('/api/events');
eventSource.onopen=()=>{
reconnectFailures=0;
connectionStatus('live',
'Live');
refreshFreshState();
};
eventSource.addEventListener('freshness',
()=>refreshFreshState());
eventSource.onerror=()=>{
reconnectFailures+=1;
const disconnected=eventSource.readyState===EventSource.CLOSED||
reconnectFailures>=3;
connectionStatus(disconnected?
'disconnected':'reconnecting',
disconnected?
'Disconnected':'Reconnecting')};
}

$('#refresh').addEventListener('click',
()=>refreshFreshState());
$('#back-to-fleet').addEventListener('click',showOverview);
$('#repository-filter').addEventListener('change',
()=>{
showOverview();
loadOverview().catch(error=>message(error.message,
true))});
$('#revision').addEventListener('change',
event=>{
selectedRevision=maps(story.graph?.evolution).find(x=>x.digest===event.target.value)||
null;
selectedTask=null;
render()}
);
for(const button of document.querySelectorAll('[data-tab]'))button.addEventListener('click',
()=>selectTab(button.dataset.tab));
const initial=new URLSearchParams(location.search).get('run');
if(initial)selectedRun=initial;
else showOverview();
refreshRunCatalog().then(()=>initial?load():undefined).catch(error=>message(error.message,
true)).finally(()=>{
startRelativeTimeRefresh();
connectEvents()});
window.addEventListener('beforeunload',
()=>{
if(eventSource)eventSource.close();
if(relativeTimeTimer!==null)window.clearInterval(relativeTimeTimer);
connectionStatus('disconnected',
'Disconnected')}
);

</script>
</body>
</html>"""
