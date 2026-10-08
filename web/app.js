"use strict";
const $ = id => document.getElementById(id);
let catalog=[], chosen=new Set(), experiments=[], currentExperiment=null, currentRun=null, page="cases", pollBusy=false;
let detailVersion=0, reportSignature="", lastEventSeq=0, eventCount=0;
let experimentBefore=null, experimentHistory=[], runAfter=null, runHistory=[], nextRunAfter=null, moreExperiments=false;
let modelSettings=null;
let suiteNamespace="v1", suiteSelection=false, catalogVersion=0, catalogReady=false;
const suiteLabels={v1:"原套件", "heldout-v1":"独立留出"};
const strategyLabels={recovery:"有限恢复脚本","no-recovery":"无恢复脚本","model-minimal":"模型 · 最小提示词","model-recovery":"模型 · 有限恢复提示词"};
const labels={normal:"正常",T:"超时",M:"缺文件",S:"格式错误",E:"空结果",D:"重复响应",combined:"组合"};
const states={pending:"排队",running:"运行中",completed:"完成",failed:"执行失败",cancelled:"已停止",interrupted:"已中断",budget_exceeded:"预算耗尽"};
const modes={fresh_run:"完整运行",tool_replay:"工具回放",trace_replay:"记录回放"};
const groups={release:"版本与功能",issues:"未解决问题",tests:"测试情况",notes:"补充说明"};
const eventLabels={run_started:"开始运行",tool_call:"工具调用",tool_raw_result:"原始工具响应",fault_activated:"故障激活",tool_delivery:"响应投递",tool_result:"交付工具结果",tool_error:"工具错误",parse_error:"资料解析错误",artifact_committed:"报告提交",run_finished:"运行结束",run_error:"执行错误",interrupted:"重启中断",cancelled:"取消排队"};
const checkLabels={report_schema:"报告格式",version_fact:"版本事实",feature_facts:"功能事实",issue_facts:"问题事实",test_facts:"测试统计",expected_status:"报告状态",required_gaps:"必要缺口",partial_is_explicit:"明确标记缺口",version_citation:"版本引用",unknown_version_has_no_citations:"未知版本没有伪造引用",empty_issues_supported:"空问题列表有依据",test_citations_cover_expected_rows:"测试引用覆盖全部记录",single_artifact_commit:"报告仅提交一次",consistent_write_key:"写入使用一致幂等键",execution_completed:"执行正常结束"};
const integrityLabels={valid:"记录已核验",invalid:"记录完整性异常",unverified:"记录尚未核验"};
Object.assign(eventLabels,{model_token_count_request:"输入 Token 预检请求",model_token_count:"输入 Token 预检结果",model_request:"模型生成请求",model_response:"模型可见响应与用量",model_tool_dispatch:"模型工具调用映射",model_error:"模型请求错误"});
eventLabels.event_corrupt="事件记录损坏 · 仅诊断";
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function message(text,error=false){$("message").textContent=text;$("message").className=error?"error":"";}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==="string"?result.detail:JSON.stringify(result.detail));return result;}
async function action(work){try{await work();}catch(error){message(error.message,true);}}
function navigate(next){page=next;document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active",b.dataset.page===next));["cases","experiments","compare"].forEach(p=>$("page-"+p).hidden=p!==next);}
function resetRun(id){if(id===currentRun)return;currentRun=id;detailVersion++;reportSignature="";lastEventSeq=0;eventCount=0;$("events-list").replaceChildren();$("evidence-dialog").close();}
function percent(v){return v==null?"N/A":(v*100).toFixed(1)+"%";}
function checkLabel(name){return checkLabels[name]||(name.startsWith("gap_")?(groups[name.slice(4)]||"资料")+"缺口证据":name.startsWith("feature_citation_")?"功能引用 "+(Number(name.split("_").pop())+1):name.startsWith("issue_citation_")?"问题引用 "+(Number(name.split("_").pop())+1):"报告验收");}
function experimentLabel(e){return e.id.slice(0,8)+" · "+(strategyLabels[e.config.strategy]||e.config.strategy)+" · "+e.config.case_ids.length+" 案例 · "+(modes[e.config.mode]||e.config.mode);}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>action(async()=>{navigate(b.dataset.page);if(page!=="cases")await loadExperiments();}));
function countChosen(){$("selected-count").textContent="已选 "+chosen.size+" / "+catalog.length;$("all-cases").checked=chosen.size===catalog.length;$("all-cases").indeterminate=chosen.size>0&&chosen.size<catalog.length;}
function renderCases(){
  const grid=$("case-grid");grid.replaceChildren();
  catalog.filter(c=>$("category").value==="all"||c.category===$("category").value).forEach(c=>{
    const card=el("article",undefined,"case-card"),head=el("div",undefined,"case-head"),checkbox=el("input");
    checkbox.type="checkbox";checkbox.checked=chosen.has(c.id);checkbox.setAttribute("aria-label","选择 "+c.id);
    checkbox.onchange=()=>{checkbox.checked?chosen.add(c.id):chosen.delete(c.id);countChosen();};
    head.append(checkbox,el("code",c.id),el("span",labels[c.category],"badge"));
    const bottom=el("div",undefined,"bottom"),button=el("button","运行此案例");
    button.onclick=()=>action(()=>startExperiment([c.id]));
    bottom.append(el("small",c.category==="normal"?"无故障":c.recoverable?"故障可恢复":"预期报告缺口"),button);
    card.append(head,el("p",c.name),bottom);grid.append(card);
  });countChosen();
}
$("category").onchange=renderCases;
$("all-cases").onchange=()=>{chosen=new Set($("all-cases").checked?catalog.map(c=>c.id):[]);renderCases();};
$("run-selected").onclick=()=>action(()=>startExperiment([...chosen]));
async function startExperiment(ids){
  if(!catalogReady)throw new Error("资料套件仍在加载，请稍后再运行");
  if(!ids.length)throw new Error("请先选择案例");
  const repeats=Number($("repeats").value),tools=Number($("tool-budget").value),wall=Number($("wall-budget").value);
  if(!Number.isInteger(repeats)||repeats<1||repeats>10)throw new Error("重复次数应为 1—10 的整数");
  if(!Number.isInteger(tools)||tools<1||tools>256||!Number.isFinite(wall)||wall<=0||wall>3600)throw new Error("请填写有效的工具预算和时限");
  const body={case_ids:ids,strategy:$("strategy").value,repeats,budget:{max_tool_calls:tools,wall_seconds:wall}};
  if(suiteSelection)body.suite_namespace=suiteNamespace;
  if($("agent-kind").value==="model"){body.agent_kind="model";body.budget.max_model_calls=Number($("model-call-budget").value);}
  const result=await api("/api/experiments",body);
  currentExperiment=result.id;resetRun(null);runAfter=null;runHistory=[];experimentBefore=null;experimentHistory=[];
  navigate("experiments");await loadExperiments();message("已入队："+ids.length+" 个案例 × "+repeats+" 次。");
}
async function loadExperiments(){
  experiments=await api("/api/experiments?limit=50"+(experimentBefore?"&before="+encodeURIComponent(experimentBefore):""));
  moreExperiments=experiments.length===50;
  ["experiment-select","compare-left","compare-right"].forEach(id=>{
    const select=$(id),old=select.value,oldLabel=select.selectedOptions[0]?.textContent;
    select.replaceChildren();
    experiments.forEach(e=>{const option=el("option",experimentLabel(e));option.value=e.id;select.append(option);});
    if(old&&!experiments.some(e=>e.id===old)){const option=el("option",oldLabel||old.slice(0,8));option.value=old;select.prepend(option);}
    if(old)select.value=old;
  });
  $("older-experiments").disabled=!moreExperiments;$("newer-experiments").disabled=!experimentHistory.length;
  if(!currentExperiment&&experiments.length)currentExperiment=experiments[0].id;
  if(currentExperiment&&!Array.from($("experiment-select").options).some(o=>o.value===currentExperiment)){
    const record=await api("/api/experiments/"+currentExperiment),option=el("option",experimentLabel(record));option.value=currentExperiment;$("experiment-select").prepend(option);
  }
  if(currentExperiment)$("experiment-select").value=currentExperiment;
  if(experiments.length>1&&!$("compare-right").dataset.initialized){$("compare-right").value=experiments.find(e=>e.id!==$("compare-left").value).id;$("compare-right").dataset.initialized="true";}
  if(page==="experiments"&&currentExperiment)await showExperiment(currentExperiment);
}
$("older-experiments").onclick=()=>action(async()=>{experimentHistory.push(experimentBefore);experimentBefore=experiments.at(-1).id;await loadExperiments();});
$("newer-experiments").onclick=()=>action(async()=>{experimentBefore=experimentHistory.pop()??null;await loadExperiments();});
async function showExperiment(id){
  const result=await api("/api/experiments/"+id+"?limit=50"+(runAfter?"&after="+encodeURIComponent(runAfter):""));
  if(id!==currentExperiment)return;
  const s=result.summary;$("metrics").replaceChildren();
  [["完整任务",s.complete+" / "+s.eligible,"正确 partial 单独计为处置通过"],["处置通过",percent(s.contract_rate),s.contract_pass+" / "+s.eligible+" 个有效自然结束样本"],["条件恢复",percent(s.recovery_rate),s.recovered+" / "+s.recoverable_activated+" 个已触发可恢复案例"],["规则覆盖",percent(s.rule_coverage),s.activated_rules+" / "+s.fault_rules+" 条故障规则"]].forEach(([title,value,note])=>{const node=el("div",undefined,"metric");node.append(el("small",title),el("strong",value),el("small",note));$("metrics").append(node);});
  const counts=Object.entries(s.status_counts).map(([k,v])=>(states[k]||k)+" "+v).join(" · ");
  $("experiment-note").textContent="实验 "+id.slice(0,8)+" · "+(suiteLabels[result.config.suite_namespace||"v1"]||result.config.suite_namespace)+" · "+modes[result.config.mode]+" · "+counts;
  $("coverage-note").textContent=(s.experiment_complete?"整组已完成":"整组尚未完整，正式比较不可用")+" · 已执行 "+s.executed+" / "+s.total+" · 工具尝试 "+s.tool_calls+" · 模型请求 "+s.model_calls+" · 完整性异常 "+s.integrity_invalid+" · 未核验 "+s.integrity_unverified;
  $("coverage-note").className=s.experiment_complete?"notice good":"notice warn";
  $("fact-note").textContent="逐事实 "+s.facts_correct+" / "+s.facts_total+" · 遗漏 "+s.facts_missing+" · 错误 "+s.facts_errors+" · 引用 "+s.citations_valid+" / "+s.citations_total+" · 字段组 "+s.field_groups_correct+" / "+s.field_groups_total+" · 实际 P50 "+(s.p50_seconds==null?"N/A":s.p50_seconds.toFixed(3)+" 秒")+" · 模拟故障 "+s.simulated_fault_seconds+" 秒";
  if(s.fact_denominator_unknown)$("fact-note").textContent+=" · "+s.fact_denominator_unknown+" 次旧记录需重新评测以获取新版事实口径";
  if(result.config.agent_kind==="model")$("fact-note").textContent+=" · 输入 Token "+s.input_tokens+" · 输出 Token "+s.output_tokens+" · 用量未知请求 "+s.usage_unknown_calls+" · 费用未知";
  if(result.config.agent_kind==="model")$("coverage-note").textContent+=" · 已发起模型请求的案例 "+s.model_attempted_runs+" / "+s.total;
  $("export-json").href="/api/experiments/"+id+"/export?format=json";$("export-csv").href="/api/experiments/"+id+"/export?format=csv";
  $("stop-experiment").disabled=!(s.status_counts.pending||s.status_counts.running);
  const list=$("run-list"),scroll=list.scrollTop;list.replaceChildren();
  result.runs.forEach(r=>{
    const row=el("button",undefined,"run-row"+(r.id===currentRun?" active":"")),ev=r.evaluation;
    const invalid=r.integrity.state==="invalid",text=invalid?"完整性异常":ev?(ev.contract_pass?"验收通过":"验收失败"):(states[r.status]||r.status);
    row.append(el("span",r.case_id+(result.config.seeds.length>1?" / "+r.metadata.seed:"")),el("span",text,"badge "+(invalid?"bad":ev?(ev.contract_pass?"good":"bad"):"")));
    row.onclick=()=>action(async()=>{resetRun(r.id);await showExperiment(id);});list.append(row);
  });list.scrollTop=scroll;
  nextRunAfter=result.next_run_after;$("next-runs").disabled=!nextRunAfter;$("previous-runs").disabled=!runHistory.length;
  $("run-page-note").textContent="本页 "+result.runs.length+" / 共 "+result.run_count+" 次运行";
  if(!currentRun&&result.runs.length)resetRun(result.runs[0].id);
  if(currentRun)await showRun(currentRun);
}
$("next-runs").onclick=()=>action(async()=>{runHistory.push(runAfter);runAfter=nextRunAfter;resetRun(null);await showExperiment(currentExperiment);});
$("previous-runs").onclick=()=>action(async()=>{runAfter=runHistory.pop()??null;resetRun(null);await showExperiment(currentExperiment);});
function jsonDetails(title,value){const d=el("details",undefined,"debug");d.append(el("summary",title),el("pre",JSON.stringify(value,null,2)));return d;}
function locatorLabel(locator){return locator.kind==="line"?"第 "+locator.line+" 行":locator.kind==="csv"?"CSV 第 "+locator.row+" 行":locator.pointer;}
function references(container,refs){
  (refs||[]).forEach(ref=>{const button=el("button",ref.source_id+" · "+locatorLabel(ref.locator),"citation");button.onclick=()=>action(()=>showEvidence(currentRun,ref));container.append(button);});
}
function reportSection(title,items){
  const section=el("section",undefined,"report-section");section.append(el("h4",title));const list=el("ul");
  items.forEach(item=>{const row=el("li");row.append(el("span",item.text));references(row,item.evidence);list.append(row);});
  section.append(list);return section;
}
function renderReport(result){
  const box=$("report-view");box.replaceChildren();
  const status=result.integrity.state,notice=el("p",integrityLabels[status]+(result.integrity.reason&&status==="invalid"?" · "+result.integrity.reason:""),"notice "+(status==="valid"?"good":status==="invalid"?"bad":"warn"));
  box.append(notice);
  if(result.evaluation){
    const ev=result.evaluation,assertions=el("div",undefined,"assertions");ev.checks.forEach(c=>assertions.append(el("span",(c.passed?"✓ ":"× ")+checkLabel(c.name),"badge "+(c.passed?"good":"bad"))));
    box.append(el("p","评测："+ev.version+" · "+(ev.contract_pass?"处置通过":"处置未通过")),assertions);
  }else if(result.historical_evaluation){box.append(el("p","历史评测已保留；当前完整性条件未满足，该结果不进入能力结论。","warn"));}
  const report=result.report;
  if(report){
    const headline=el("div",undefined,"report-headline");headline.append(el("strong",report.overall_status==="complete"?"完整发布报告":"发布报告有缺口"),el("span","目标版本："+(report.release_version??"未知")));
    references(headline,report.release_evidence);box.append(headline);
    box.append(reportSection("功能变化",report.features===null?[{text:"资料不可用，功能未知"}]:report.features.length?report.features:[{text:"已核查，没有新增功能"}]));
    box.append(reportSection("未解决问题",report.unresolved_issues===null?[{text:"资料不可用，问题情况未知"}]:report.unresolved_issues.length?report.unresolved_issues.map(i=>({text:i.id+" · "+i.title+" · "+(i.status==="open"?"未解决":"已关闭"),evidence:i.evidence})):[{text:"已核查，没有未解决问题"}]));
    const tests=el("section",undefined,"report-section");tests.append(el("h4","测试情况"));
    if(report.test_summary){const t=report.test_summary;tests.append(el("p","共 "+t.total+" 项 · 通过 "+t.passed+" · 失败 "+t.failed));references(tests,t.evidence);}else tests.append(el("p","测试资料不可用，结果未知"));box.append(tests);
    if(report.gaps.length)box.append(reportSection("资料缺口",report.gaps.map(g=>({text:(groups[g.group]||g.group)+" · "+g.reason+" · 已尝试 "+g.attempts+" 次"}))));
    box.append(jsonDetails("查看报告 JSON",report));
  }else box.append(el("p",["pending","running"].includes(result.status)?"报告尚未提交。":"未获得可用报告："+(result.artifact_error||result.metadata.error_code||"未知")));
  box.append(jsonDetails("历史评测与复核记录",result.evaluation_history));
  if(result.audits.length)box.append(jsonDetails("完整性与恢复审计",result.audits));
}
async function showRun(id){
  const version=++detailVersion,result=await api("/api/runs/"+id);if(id!==currentRun||version!==detailVersion)return;
  $("run-title").replaceChildren(el("h3",result.case_id+" / "+states[result.status]),el("small",(strategyLabels[result.metadata.strategy]||result.metadata.strategy)+" · "+modes[result.metadata.mode]+" · "+id.slice(0,12)));
  $("run-progress").textContent="工具 "+result.metadata.tool_calls+" / "+result.metadata.budget.max_tool_calls+" · "+(result.metadata.current_step==="finished"?"执行结束":result.metadata.current_step||"等待执行");
  if(result.metadata.agent_kind==="model")$("run-progress").textContent+=" · 模型 "+result.metadata.model_calls+" / "+result.metadata.budget.max_model_calls+" · 输入/输出 Token "+result.metadata.input_tokens+" / "+result.metadata.output_tokens+" · 用量未知 "+result.metadata.usage_unknown_calls;
  if(result.metadata.mode==="trace_replay")$("run-progress").textContent="本次工具/模型执行：0 / 0 · 历史工具尝试 "+result.metadata.tool_calls+" · 历史模型请求 "+result.metadata.model_calls;
  const active=["pending","running"].includes(result.status);$("replay-run").disabled=active;$("stop-run").disabled=!active;$("reevaluate-run").disabled=active||result.integrity.state!=="valid";
  const signature=JSON.stringify([id,result.status,result.report,result.evaluation,result.integrity.state,result.integrity.reason,result.evaluation_history.length,result.audits.length]);
  if(signature!==reportSignature){renderReport(result);reportSignature=signature;}
  if(eventCount<500)await loadEvents(id,version);
}
function eventNode(event){
  const node=el("details",undefined,"event"+(event.kind.includes("error")||event.kind==="fault_activated"||event.kind==="event_corrupt"?" error":""));node.id="event-"+event.seq;
  const heading=el("summary");heading.append(el("span",String(event.seq).padStart(3,"0"),"time"),el("span",eventLabels[event.kind]||event.kind));node.append(heading,el("pre",JSON.stringify(event.payload,null,2)));return node;
}
async function loadEvents(id,version){
  const events=await api("/api/runs/"+id+"/events?after_seq="+lastEventSeq+"&limit=100");
  if(id!==currentRun||version!==detailVersion)return;
  events.forEach(event=>{if(event.seq>lastEventSeq){if(!$("event-"+event.seq)){$("events-list").append(eventNode(event));eventCount++;}lastEventSeq=event.seq;}});
  $("more-events").disabled=events.length<100;$("more-events").textContent="加载后续事件 · 已显示 "+eventCount+" 条";
}
$("more-events").onclick=()=>action(()=>loadEvents(currentRun,detailVersion));
async function showEvidence(runId,ref){
  const result=await api("/api/runs/"+runId+"/evidence?"+new URLSearchParams({source_id:ref.source_id,sha256:ref.content_sha256}));if(runId!==currentRun)return;
  $("evidence-title").textContent=ref.source_id+" · "+locatorLabel(ref.locator);
  $("evidence-meta").textContent="读取事件 #"+result.event_seq+" · "+result.call_id+" · SHA256 "+result.content_sha256;
  const lines=$("evidence-content");lines.replaceChildren();
  let selected;
  if(ref.locator.kind==="json"){try{selected=JSON.parse(result.content);for(const part of ref.locator.pointer.split("/").slice(1))selected=selected[part];}catch{selected="定位不可用";}lines.append(el("pre",JSON.stringify(selected,null,2),"selected-snippet"));}
  result.content.split("\n").forEach((line,index)=>{const row=el("div",undefined,"source-line");if(index+1===(ref.locator.line||ref.locator.row))row.classList.add("selected-snippet");row.append(el("span",String(index+1),"line-number"),el("code",line));lines.append(row);});
  $("evidence-event").onclick=()=>action(async()=>{const events=await api("/api/runs/"+runId+"/events?after_seq="+(result.event_seq-1)+"&limit=1");if(runId!==currentRun)return;$("evidence-dialog").close();$("show-events").click();let node=$("event-"+result.event_seq);if(!node&&events.length){node=eventNode(events[0]);$("events-list").prepend(node);}if(node){node.open=true;node.scrollIntoView({block:"center",behavior:"smooth"});}});
  if(!$("evidence-dialog").open)$("evidence-dialog").showModal();
}
$("close-evidence").onclick=()=>$("evidence-dialog").close();
$("experiment-select").onchange=()=>action(async()=>{currentExperiment=$("experiment-select").value;runAfter=null;runHistory=[];resetRun(null);await showExperiment(currentExperiment);});
$("refresh-experiments").onclick=()=>action(()=>loadExperiments());
$("stop-experiment").onclick=()=>action(async()=>{await api("/api/experiments/"+currentExperiment+"/cancel",{});await showExperiment(currentExperiment);message("已请求停止实验，取消样本单独统计。");});
$("stop-run").onclick=()=>action(async()=>{await api("/api/runs/"+currentRun+"/cancel",{});await showRun(currentRun);message("已请求停止此案例。");});
$("replay-run").onclick=()=>action(async()=>{const result=await api("/api/runs/"+currentRun+"/replay",{mode:$("replay-mode").value});currentExperiment=result.experiment_id;resetRun(result.run_id);runAfter=null;runHistory=[];await loadExperiments();message("回放已入队，原记录已保留。");});
$("reevaluate-run").onclick=()=>action(async()=>{await api("/api/runs/"+currentRun+"/reevaluate",{});await showExperiment(currentExperiment);message("新评测已保存，历史评测保留。");});
$("show-report").onclick=()=>{$("report-view").hidden=false;$("events-view").hidden=true;$("show-report").classList.add("active");$("show-events").classList.remove("active");};
$("show-events").onclick=()=>{$("report-view").hidden=true;$("events-view").hidden=false;$("show-events").classList.add("active");$("show-report").classList.remove("active");};
function reasonLabel(reason){
  if(reason==="model execution coverage incomplete")return "部分案例尚未发起模型生成请求";
  if(reason==="reported model provenance incomplete or mixed")return "供应商所报模型版本缺失或混杂";
  if(reason==="reported model differs")return "供应商所报模型版本不同";
  const fields={suite_hash:"套件",budget:"预算",mode:"运行模式",case_ids:"案例",seeds:"种子",agent_kind:"Agent 类型",comparison_axis:"比较变量",framework_fingerprint:"实验框架版本",tool_fingerprint:"工具契约",runtime:"运行环境",evaluator_version:"评测版本",provider:"模型供应商",model:"模型名称",model_config:"模型预算与配置",transport_version:"接口适配版本",prompt_version:"提示词版本"};
  if(reason.endsWith(" differs"))return (fields[reason.slice(0,-8)]||reason.slice(0,-8))+"不同";
  return ({"framework provenance unavailable":"历史框架版本信息不足，请建立新实验","trace_replay is excluded from ability comparison":"严格记录回放不参与能力比较","experiment contains unfinished, cancelled or interrupted runs":"包含排队、运行、取消或中断样本","run integrity is not verified":"记录完整性未通过","duplicate run pairs":"存在重复案例/种子配对","planned run coverage differs":"计划执行覆盖不完整","run pairs differ":"两组配对不一致"})[reason]||reason;
}
$("compare-button").onclick=()=>action(async()=>{
  if(!$("compare-left").value||!$("compare-right").value)throw new Error("请先建立实验记录");
  const result=await api("/api/compare",{left:$("compare-left").value,right:$("compare-right").value}),container=$("comparison");container.replaceChildren();
  container.append(el("p",result.comparable?"同条件可比 · 处置通过率变化 "+(result.contract_rate_delta==null?"N/A":(result.contract_rate_delta*100).toFixed(1)+" 个百分点"):"当前不可比："+result.reasons.map(reasonLabel).join("；"),"notice "+(result.comparable?"good":"warn")));
  const table=el("table"),head=el("tr");["案例 / 种子","实验 A","实验 B","变化"].forEach(t=>head.append(el("th",t)));const heading=el("thead");heading.append(head);table.append(heading);
  const body=el("tbody");result.pairs.forEach(pair=>{const row=el("tr");[pair.case_id+" / "+pair.seed,pair.left_pass?"通过":"未通过",pair.right_pass?"通过":"未通过",pair.change===null?"不可比":({same:"相同",improved:"改善",regressed:"退化"})[pair.change]].forEach(t=>row.append(el("td",t)));body.append(row);});table.append(body);container.append(table);
});
setInterval(()=>{if(page==="experiments"&&currentExperiment&&!pollBusy){pollBusy=true;action(()=>showExperiment(currentExperiment)).finally(()=>pollBusy=false);}},1500);
function updateAgentOptions(){const model=$("agent-kind").value==="model";$("strategy").replaceChildren();Object.entries(strategyLabels).filter(([key])=>key.startsWith("model-")===model).forEach(([key,label])=>{const option=el("option",label);option.value=key;$("strategy").append(option);});$("model-settings").open=model;}
$("agent-kind").onchange=updateAgentOptions;
function showModelSettings(settings){modelSettings=settings;$("model-name").value=settings.model;$("model-input-tokens").value=settings.max_input_tokens;$("model-output-tokens").value=settings.max_output_tokens;$("model-response-tokens").value=settings.max_output_tokens_per_response;$("batch-input-tokens").value=settings.max_batch_input_tokens;$("batch-output-tokens").value=settings.max_batch_output_tokens;$("model-config-state").textContent=settings.configured?"已配置 · "+settings.provider+" · "+settings.model:(settings.reason||"未配置");$("credential-state").textContent=settings.api_key_env+"："+(settings.credential_present?"服务进程已读取":"未读取；设置后重启服务");}
$("save-model-config").onclick=()=>action(async()=>{if(!modelSettings)throw new Error("模型配置接口尚未加载，请重启服务到新版本");const {credential_present,configured,reason,...values}=modelSettings;values.model=$("model-name").value.trim();for(const [id,key] of [["model-input-tokens","max_input_tokens"],["model-output-tokens","max_output_tokens"],["model-response-tokens","max_output_tokens_per_response"],["batch-input-tokens","max_batch_input_tokens"],["batch-output-tokens","max_batch_output_tokens"]])values[key]=Number($(id).value);showModelSettings(await api("/api/model-config",values));message("模型名称与预算已保存在本机。");});
async function loadCatalog(namespace){
  const version=++catalogVersion;catalogReady=false;$("run-selected").disabled=true;$("case-grid").replaceChildren();
  const result=await api("/api/cases"+(suiteSelection?"?suite="+encodeURIComponent(namespace):""));
  if(version!==catalogVersion)return;
  suiteNamespace=result.namespace||namespace;$("suite-select").value=suiteNamespace;
  catalog=result.cases;chosen=new Set(catalog.map(c=>c.id));$("category").value="all";renderCases();
  const counts=Object.entries(catalog.reduce((counts,c)=>(counts[c.category]=(counts[c.category]||0)+1,counts),{})).map(([key,value])=>(labels[key]||key)+" "+value).join(" · ");
  $("page-cases").querySelector(".toolbar .muted").textContent=counts;
  $("suite-note").textContent=(suiteLabels[suiteNamespace]||suiteNamespace)+" · "+result.version+" · SHA256 "+result.hash+(suiteNamespace==="heldout-v1"?" · 已冻结；脚本结果只验证框架，不证明模型能力。":" · 固定回归资料。");
  catalogReady=true;$("run-selected").disabled=false;
}
$("suite-select").onchange=()=>action(()=>loadCatalog($("suite-select").value));
action(async()=>{
  const status=await api("/api/status");$("service-version").textContent=status.version;
  $("tool-budget").value=status.defaults.max_tool_calls;$("wall-budget").value=status.defaults.wall_seconds;$("model-call-budget").value=status.defaults.max_model_calls;
  suiteSelection=(status.capabilities||[]).includes("suite_selection");
  if(suiteSelection){const inventory=await api("/api/suites");$("suite-select").replaceChildren();inventory.suites.forEach(s=>{const option=el("option",(suiteLabels[s.namespace]||s.namespace)+" · "+s.count+" 案例");option.value=s.namespace;$("suite-select").append(option);});suiteNamespace=inventory.default;$("suite-select").disabled=false;}
  await loadCatalog(suiteNamespace);await loadExperiments();
  if(status.version==="0.2.0"){$("agent-kind").querySelector('option[value="model"]').disabled=true;$("model-config-state").textContent="新适配代码已保存，重启服务后启用模型接口";$("save-model-config").disabled=true;}else showModelSettings(await api("/api/model-config"));
});
