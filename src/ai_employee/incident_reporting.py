"""Privacy-safe, opt-in incident Issue pipeline."""
from __future__ import annotations
import hashlib, hmac, json, os, re, sqlite3
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .serialization import canonical_digest, canonical_json, canonical_json_bytes

class IncidentError(RuntimeError): pass
class Mode(str, Enum):
    OFF="off"; APPROVAL_REQUIRED="approval_required"; AUTO="auto"
class Category(str, Enum):
    KERNEL="trust_kernel_failure"; STORAGE="persistence_failure"; WORKER="worker_boundary_failure"
class Component(str, Enum):
    RUNTIME="runtime"; STORAGE="storage"; POLICY="policy"; WORKER="worker_boundary"
class ExceptionClass(str, Enum):
    ASSERTION="AssertionError"; OS="OSError"; RUNTIME="RuntimeError"; TYPE="TypeError"; VALUE="ValueError"

class Diagnosis(BaseModel):
    model_config=ConfigDict(extra="forbid", frozen=True, strict=True)
    category: Category
    component: Component
    terminal_state: str=Field(pattern="^failed$")
    internal_product_failure: bool
    exception_class: ExceptionClass
    private_detail: str=Field(max_length=100000)

class Report(BaseModel):
    model_config=ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: str=Field(pattern="^1$")
    category: Category
    component: Component
    exception_class: ExceptionClass
    version: str=Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    commit: str=Field(pattern="^[0-9a-f]{7,40}$")
    duration_bucket: int=Field(ge=0, le=3600)
    memory_bucket: int=Field(ge=0, le=8192)
    reproduction: str=Field(pattern="^offline_incident_canary_v1$")
    fingerprint: str=Field(pattern="^[0-9a-f]{64}$")
    occurrences: int=Field(ge=1, le=999)

class Policy(BaseModel):
    model_config=ConfigDict(extra="forbid", frozen=True, strict=True)
    mode: Mode=Mode.OFF
    repository: str|None=None
    auto_categories: tuple[Category,...]=()
    retention_hours: int=Field(default=168, ge=1, le=720)
    approval_hours: int=Field(default=24, ge=1, le=168)
    daily_limit: int=Field(default=3, ge=1, le=20)
    pending_cap: int=Field(default=20, ge=1, le=100)
    @model_validator(mode="after")
    def authority(self):
        if self.mode is not Mode.OFF and not self.repository: raise ValueError("repository required")
        if self.repository and not re.fullmatch(r"[\w.-]+/[\w.-]+",self.repository): raise ValueError("invalid repository")
        if self.mode is Mode.AUTO and not self.auto_categories: raise ValueError("auto allowlist required")
        return self

_SECRET=tuple(re.compile(x,re.I) for x in (
    r"gh[pousr]_[A-Za-z0-9]{20,}",r"PRIVATE KEY",r"https?://",r"/(?:Users|home|tmp)/",
    r"\b(?:prompt|task|conversation|transcript|model|log|stdout|stderr|stack|message|diff|file|path|host|user|argv|environment|branch|workspace)\b"))
def _bucket(value:float,buckets:tuple[int,...])->int:
    if value<0: raise IncidentError("INVALID_METRIC")
    return next((x for x in buckets if value<=x),buckets[-1])
def public_json(report:Report)->str:
    text=canonical_json(report.model_dump(mode="json"))
    if any(p.search(text) for p in _SECRET): raise IncidentError("PUBLIC_SCAN_DENIED")
    Report.model_validate_json(text)
    return text
def compose(d:Diagnosis,key:bytes,version:str,commit:str,duration:float,memory:float)->Report:
    if d.terminal_state!="failed" or not d.internal_product_failure: raise IncidentError("NOT_TERMINAL_INTERNAL")
    if len(key)<32: raise IncidentError("INVALID_KEY")
    stable={"category":d.category.value,"component":d.component.value,"exception_class":d.exception_class.value,"version":version,"commit":commit}
    fingerprint=hmac.new(key,canonical_json_bytes(stable),hashlib.sha256).hexdigest()
    report=Report(schema_version="1",category=d.category,component=d.component,exception_class=d.exception_class,
        version=version,commit=commit,duration_bucket=_bucket(duration,(0,1,5,15,30,60,300,900,3600)),
        memory_bucket=_bucket(memory,(0,64,128,256,512,1024,2048,4096,8192)),
        reproduction="offline_incident_canary_v1",fingerprint=fingerprint,occurrences=1)
    public_json(report); return report

class Transport(Protocol):
    def create_issue(self,repository:str,title:str,body:str,labels:tuple[str,...])->tuple[int,str]: ...
class FakeTransport:
    def __init__(self): self.calls=[]
    def create_issue(self,repository,title,body,labels):
        self.calls.append((repository,title,body,labels)); return 1,f"https://github.com/{repository}/issues/1"

class Outbox:
    def __init__(self,path:str|Path):
        self.path=Path(path).expanduser(); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path); self.db.row_factory=sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS incidents(fingerprint TEXT PRIMARY KEY,body TEXT,digest TEXT,status TEXT,count INTEGER,expires TEXT,approval TEXT,approval_expires TEXT,receipt TEXT)")
        self.db.commit(); os.chmod(self.path,0o600)
    def enqueue(self,report:Report,policy:Policy,now:datetime):
        body=public_json(report); digest=canonical_digest(report.model_dump(mode="json")); expiry=(now.astimezone(UTC)+timedelta(hours=policy.retention_hours)).isoformat()
        row=self.db.execute("SELECT * FROM incidents WHERE fingerprint=?",(report.fingerprint,)).fetchone()
        if row:
            if row["status"]=="published": return row
            report=report.model_copy(update={"occurrences":min(row["count"]+1,999)}); body=public_json(report); digest=canonical_digest(report.model_dump(mode="json"))
            self.db.execute("UPDATE incidents SET body=?,digest=?,status='pending',count=?,approval=NULL,approval_expires=NULL WHERE fingerprint=?",(body,digest,report.occurrences,report.fingerprint))
        else:
            if self.db.execute("SELECT count(*) FROM incidents WHERE status!='published'").fetchone()[0]>=policy.pending_cap: raise IncidentError("OUTBOX_CAP")
            self.db.execute("INSERT INTO incidents VALUES(?,?,?,'pending',1,?,NULL,NULL,NULL)",(report.fingerprint,body,digest,expiry))
        self.db.commit(); return self.db.execute("SELECT * FROM incidents WHERE fingerprint=?",(report.fingerprint,)).fetchone()
