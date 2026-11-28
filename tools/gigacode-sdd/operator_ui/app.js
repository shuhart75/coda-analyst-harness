/* Session grouping/disclosure approach adapted from Orca v1.4.274,
 * CrewRowView.tsx, CrewRowDetails.tsx and crew-party-status.ts.
 * Copyright (c) 2026 Lovecast Inc. MIT; see ../third_party/orca/LICENSE.
 * DOM implementation, controller API and answer workflow are specific to GigaCode SDD. */
'use strict';
const $ = id => document.getElementById(id);
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const storage = {get(k) {try{return sessionStorage.getItem(k);}catch{return null;}}, set(k,v){try{sessionStorage.setItem(k,v);}catch{}}, remove(k){try{sessionStorage.removeItem(k);}catch{}}};
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get('token') || storage.get('operator-token') || '';
if (fragment.has('token')) { storage.set('operator-token', token); history.replaceState(null, '', location.pathname); }
const cards = new Map();
let latest, refreshing = false, toastTimer;
const statuses = {unknown:'Нет подтверждения',idle:'Создайте или выберите запуск',done:'Принято', running:'В работе', pending:'Ожидает', blocked:'Заблокировано', 'waiting-user':'Нужен ответ', 'waiting-scope':'Ожидает область работы', 'waiting-retry':'Повтор позже', interrupted:'Остановлено', complete:'Завершено', accepted:'Вариант проверен', stalled:'Приостановлено', 'not-observed':'Нет наблюдения', 'external-blocker':'Нужны внешние данные'};
const phases = {execute:'Разработка', review:'Независимое ревью', checks:'Проверки', adjudicate:'Разбор замечаний', accepted:'Проверен'};
const label = s => statuses[s] || s || 'Состояние неизвестно';
const duration = value => {if(!Number.isFinite(value)) return '—'; const n=Math.max(0,Math.floor(value)); return [Math.floor(n/3600),Math.floor(n/60)%60,n%60].map(v=>String(v).padStart(2,'0')).join(':');};
const date = stamp => typeof stamp==='number' ? new Date(stamp*1000).toLocaleString('ru-RU') : 'Время неизвестно';
function toast(text) {$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,7000);}
async function api(path, body, runKey) {
  const headers={Authorization:'Bearer '+token}; const options={headers,cache:'no-store'};
  if(path==='/api/answers')headers['X-Operator-Run']=runKey||latest?.run.key||'';
  if(body!==undefined){options.method='POST';headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch(path,options); const value=await response.json();
  if(!response.ok) throw new Error(value.error || 'Не удалось выполнить запрос.');
  return value;
}
function renderSummary(value) {
  const links=$('project-links'), focusedUrl=links.contains(document.activeElement)?document.activeElement.href:null;links.replaceChildren();
  for(const item of value.project_links||[]){
    const row=node('div','project-link');
    if(item.available){const a=node('a','button','Открыть проект ↗');a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';row.append(a);}
    else row.append(node('strong',null,item.checking?'Проверяем сервер проекта…':'Сервер проекта недоступен'));
    row.append(node('span','muted',item.url+(item.checked_at?' · Проверено '+date(item.checked_at):'')));links.append(row);
  }
  if(focusedUrl){const link=[...links.querySelectorAll('a')].find(a=>a.href===focusedUrl);link?.focus({preventScroll:true});}
  if(!links.childNodes.length)links.append(node('p','muted','Ссылка на проект появится, когда исполнитель укажет адрес стенда для браузерной проверки.'));

  const run=value.run, count=value.questions.length;
  $('run-name').textContent=run.name;
  $('accepted').textContent=run.accepted_tasks+' / '+run.total_tasks;
  $('progress').max=Math.max(1,run.total_tasks);$('progress').value=run.accepted_tasks;
  $('remaining').textContent=run.total_tasks ? run.total_tasks-run.accepted_tasks : '—';
  $('remaining-note').textContent=run.ready?'Итоговая проверка пройдена':run.total_tasks?'И итоговая проверка · без оценки времени':'План ещё не закреплён';
  const uncertain=value.execution.telemetry_stale||value.execution.telemetry_incomplete;
  const active=value.execution.operations.filter(op=>op.current && op.kind==='llm');
  $('session-count').textContent=uncertain?'?':String(active.length);
  $('nav-sessions').textContent=uncertain?'?':String(active.length);
  $('nav-questions').textContent=String(count);$('question-count').textContent=String(count);
  $('session-note').textContent=uncertain?'Наблюдение требует обновления':run.requires_resume?'Контроллер не выполняет работу':'Включая аудиторов';
  $('status-heading').textContent=value.summary||label(run.status);
  const finalStage=value.stages?.find(s=>s.id==='integration');
  $('status-description').textContent=run.ready?'Все задачи и итоговая приёмка завершены.':
    run.accepted_tasks===run.total_tasks&&run.total_tasks?`Все ${run.total_tasks} задач приняты. ${uncertain?'Текущее исполнение итоговой приёмки не подтверждено.':finalStage?.status==='running'?'Идёт итоговая приёмка результата.':finalStage?.status==='done'?'Итоговая приёмка пройдена, ожидается подтверждение готовности.':'Итоговая приёмка: '+label(finalStage?.status)+'.'}`:
    run.requires_resume?'Контроллер остановлен. Продолжите работу через мастер выше.':'Подробности текущей работы показаны в этапах и сессиях ниже.';
  document.querySelector('.status-banner').classList.toggle('attention',Boolean(count||uncertain||run.attention.requires_action||run.status==='interrupted'));
  if(!run.ready&&run.accepted_tasks===run.total_tasks&&run.total_tasks){$('remaining').textContent='Итоговая приёмка';$('remaining-note').textContent=label(finalStage?.status);}
  $('answer-shortcut').hidden=!count;
  $('log-path').textContent=run.log_path;
}
function renderRecovery(value){
  const recovery=value.recovery||{}, list=$('recovery-list');
  const signature=JSON.stringify(recovery);
  if(list.renderSignature===signature)return;
  list.renderSignature=signature;
  const opened=new Set([...list.querySelectorAll('details[open]')].map(row=>row.dataset.incident));
  const previous=new Set([...list.querySelectorAll('details[data-incident]')].map(row=>row.dataset.incident));
  list.replaceChildren();
  const items=recovery.incidents||[];
  $('recovery-panel').hidden=!items.length&&!recovery.unavailable&&!recovery.truncated;
  $('recovery-note').textContent=recovery.unavailable||recovery.truncated?'История неполна. Показаны доступные записи; отсутствие других записей не подтверждает успех.':'Контроллер проверяет результат каждого действия. Восстановление само по себе не означает приёмку задачи.';
  for(const [index,item] of items.entries()){
    const details=node('details','answer-item');details.dataset.incident=item.id;
    details.open=opened.has(item.id)||(!previous.has(item.id)&&index===0);
    details.append(node('summary',null,(item.task_id||'Запуск')+' · '+item.status_label),
      node('p','context','Причина: '+item.reason),
      node('p','context','Действие: '+item.action_label),
      node('p','muted','Попытка '+(item.attempt??'?')+(item.model?' · '+item.model:'')));
    if(item.updated_at){const stamp=Date.parse(item.updated_at);details.append(node('p','muted','Обновлено: '+(Number.isFinite(stamp)?new Date(stamp).toLocaleString('ru-RU'):'время неизвестно')));}
    if(item.status==='pending'||item.status==='running')details.append(node('p','context','Результат восстановления ещё не подтверждён.'));
    const proof=node('details');proof.append(node('summary',null,'Основания и результат проверки'),node('pre',null,JSON.stringify({evidence:item.evidence,verification:item.verification},null,2)));details.append(proof);
    if(item.history?.length){const history=node('details');history.append(node('summary',null,'Предыдущие действия'),node('pre',null,JSON.stringify(item.history,null,2)));details.append(history);}
    list.append(details);
  }
}
function renderRoute(value) {
  const browserChecks=value.browser_checks||[], browserList=$('browser-check-list');
  const expandedChecks=new Set([...browserList.querySelectorAll('details[open]')].map(row=>row.dataset.checkId));
  $('browser-check-panel').hidden=!browserChecks.length;browserList.replaceChildren();
  for(const item of browserChecks){
    const row=node('details','answer-item');row.dataset.checkId=item.id;row.open=expandedChecks.has(item.id);
    row.append(node('summary',null,item.check.requirements.join(', ')+' · '+item.task));
    if(item.clarification)row.append(node('p','context','Уточнение сценария: '+item.clarification.followup),node('p','muted','Исходный чек-лист ниже дан для справки. Исполнитель проверит актуальные условия.'));
    row.append(node('p','context',item.check.url),node('p','context','Сборка: '+item.check.artifact),node('p','context',item.check.preconditions));
    for(const step of item.check.steps)row.append(node('p','context',step.action+' → '+step.expected));
    browserList.append(row);
  }

  const events=$('controller-events');events.replaceChildren();for(const event of value.controller_events||[])events.append(node('p','context',date(event.at)+' · '+event.message));if(!events.childNodes.length)events.append(node('p','muted','Подробные события контроллера ещё не получены.'));
  const stages=$('stage-list');
  const operationText=op=>(op.current?'Сейчас: ':'Последнее наблюдение: ')+op.activity+(op.model?' · '+op.model:'')+' · '+duration(op.elapsed_seconds);
  const routeSignature=JSON.stringify({run:value.run.key,stages:(value.stages||[]).map(stage=>({...stage,operations:(stage.operations||[]).map(({elapsed_seconds,...op})=>op)})),tasks:value.tasks.map(task=>({...task,candidates:(task.candidates||[]).map(({updated_at,...candidate})=>candidate)}))});
  if(stages.renderSignature===routeSignature){
    const live=(value.stages||[]).flatMap(stage=>stage.operations||[]);
    stages.querySelectorAll('.stage-operation').forEach((element,index)=>{element.textContent=operationText(live[index]);});
    return;
  }
  stages.renderSignature=routeSignature;
  const existing=stages.querySelector('[data-stage-tasks]');
  const taskListOpen=existing?.open;
  const runChanged=existing && existing.dataset.runKey!==(value.run.key||'');
  const focused=stages.contains(document.activeElement)?document.activeElement?.dataset?.focusKey:null;
  stages.replaceChildren();
  let list;
  for(const stage of value.stages||[]){
    const row=node('li','route-item '+stage.status);row.dataset.stageId=stage.id;
    row.append(node('span','route-symbol',stage.status==='done'?'✓':stage.status==='running'?'▶':stage.status==='blocked'?'!':'○'));
    if(stage.status==='running')row.setAttribute('aria-current','step');
    const body=node('div','stage-body');
    body.append(node('div','route-title',stage.title+' · '+label(stage.status)),node('p','context',stage.description));
    if(stage.error)body.append(node('p','question-message',stage.error));
    for(const op of stage.operations||[]){
      body.append(node('p','stage-operation',operationText(op)));
    }
    if(stage.id==='tasks'){
      const disclosure=node('details','stage-tasks');disclosure.dataset.stageTasks='true';disclosure.dataset.runKey=value.run.key||'';
      disclosure.open=taskListOpen===undefined||runChanged?stage.status!=='done':taskListOpen;
      const toggle=node('summary',null,`Задачи · ${value.run.accepted_tasks} из ${value.run.total_tasks} принято`);toggle.dataset.focusKey='task-toggle';
      list=node('ol','route task-route');list.id='route';list.setAttribute('aria-label','Задачи этапа выполнения');
      disclosure.append(toggle,list);body.append(disclosure);
    }
    row.append(body);stages.append(row);
  }
  if(focused==='task-toggle')stages.querySelector('[data-focus-key="task-toggle"]')?.focus({preventScroll:true});
  if(!list)return;
  if(!value.tasks.length){list.append(node('li','empty','План ещё не закреплён.'));return;}
  for(const task of value.tasks){
    const item=node('li','route-item '+(task.accepted?'done':task.status));
    item.append(node('span','route-symbol',task.accepted?'✓':task.status==='running'?'▶':task.status==='blocked'?'!':'○'));
    const body=node('div');body.append(node('div','route-title',task.title));
    const dependencies=task.waiting_for.map(id=>value.tasks.find(t=>t.id===id)?.title||id);
    body.append(node('div','route-caption',label(task.status)+(dependencies.length?' · После: '+dependencies.join(', '):'')));
    if(task.error){const details=node('details');details.append(node('summary','route-caption','Причина остановки'),node('p','context',task.error));body.append(details);}
    for(const c of task.candidates||[]){
      if(task.accepted || c.status==='not-observed') continue;
      const wait=c.resource_wait;
      const text=wait?.state==='waiting'?'ждёт ресурс':label(c.status);
      body.append(node('div','route-caption',`${c.model} · ${phases[c.stage]||c.stage||''} · ${text}`));
    }
    item.append(body);list.append(item);
  }
}
function renderSessions(value){
  const list=$('session-list'); const opened=new Set([...list.querySelectorAll('details[open]')].map(n=>n.dataset.key));list.replaceChildren();
  const ops=value.execution.operations;
  $('sessions-summary').textContent=ops.some(op=>!op.current)?'СОХРАНЁННЫЙ СНИМОК':'ТЕКУЩИЕ';
  if(!ops.length){list.append(node('p','empty',value.run.requires_resume?'Сессии контроллера остановлены. Состояние служб проверяется отдельно.':'Активных сессий в последнем снимке нет.'));return;}
  // Orca crew rows group attention/work before settled rows; keep stale observations distinct.
  const ordered=[...ops].sort((a,b)=>Number(b.current)-Number(a.current)||Number(a.purpose==='observation')-Number(b.purpose==='observation'));
  for(const [index,op] of ordered.entries()){
    const row=node('article','session-row'); const header=node('div','session-header');
    header.append(node('span','session-name',op.model||op.title||'Операция'),node('span','elapsed',duration(op.elapsed_seconds)));
    row.append(header,node('div','session-detail',op.label||op.title||phases[op.stage]||''));
    if(!op.current)row.append(node('div','session-detail','Последнее наблюдение, не текущая сессия'));
    const details=node('details');details.dataset.key=[op.task_id,op.model,op.phase,op.purpose,index].join(':');details.open=opened.has(details.dataset.key);
    details.append(node('summary',null,'Подробности'),node('p','session-detail',`Без новых событий: ${duration(op.idle_seconds)} · Лимит: ${duration(op.timeout_seconds)}`));
    const activity=op.activity||{};
    details.append(node('p','context',op.task_id==='integration'?'Цель: проверить весь собранный результат по требованиям, устранить межкомпонентные дефекты и пройти независимое ревью.':'Этап: '+(op.phase||op.stage||op.title||'не указан')));
    if(Number.isFinite(activity.tool_calls))details.append(node('p','session-detail',`Вызовов инструментов: ${activity.tool_calls}; ответов: ${activity.tool_results}; ожидают ответа: ${activity.pending_calls}.`));
    for(const action of activity.pending_actions||[])details.append(node('p','context','Ожидается результат: '+action));
    if(!(activity.recent_events||[]).length)details.append(node('p','muted','Подробные события этой сессии ещё не получены. По одному времени работы нельзя определить, что делает модель.'));
    for(const event of activity.recent_events||[])details.append(node('p','context',date(event.at)+' · '+event.action+' · '+event.outcome));
    details.append(node('p','muted','События показывают действия, но не подтверждают прохождение проверок.'));
    row.append(details);list.append(row);
  }
}
function renderObservers(value){
  const list=$('observer-list');
  const signature=JSON.stringify(value.observers);
  if(list.renderSignature===signature)return;
  list.renderSignature=signature;
  const opened=new Set([...list.querySelectorAll('details[open]')].map(row=>row.dataset.finding));
  list.replaceChildren();
  for(const line of value.observers.lines||[])list.append(node('p','observer-line',line));
  for(const [role,result] of Object.entries(value.observers.results||{})){
    if(!result || !Array.isArray(result.evidence) || !result.evidence.length)continue;
    const details=node('details','answer-item');details.dataset.finding='evidence-'+role;details.open=opened.has(details.dataset.finding);
    details.append(node('summary',null,'Основания: '+(role==='progress'?'прогресс':'соответствие задаче')));
    for(const fact of result.evidence)details.append(node('p','context',fact.ref+' · '+fact.excerpt));
    list.append(details);
  }
  for(const finding of value.observers.findings||[]){
    const details=node('details','answer-item');details.dataset.finding=finding.id;details.open=opened.has(finding.id);
    details.append(node('summary',null,finding.task_id+' · '+finding.problem),
      node('p','muted','Замечание аудитора, не подтверждённый дефект. Первое наблюдение: '+date(finding.first_seen)),
      node('p','context','Предлагаемый шаг: '+finding.recommendation),
      node('p','context','Ожидаемый результат: '+finding.expected_result));
    for(const event of finding.history||[]){
      details.append(node('p','context',date(event.at)+' · '+event.problem));
      for(const fact of event.evidence||[])details.append(node('p','muted',fact.ref+' · '+fact.excerpt));
    }
    list.append(details);
  }
  if(!list.childNodes.length)list.append(node('p','empty','Оценок аудиторов пока нет.'));
}
function draftKey(q){return 'operator-draft:'+latest.run.name+':'+q.id+':'+q.revision;}
function questionCard(q){
  const card=node('article','question-card');card.dataset.questionId=q.id;
  card.append(node('div','question-task',latest.tasks.find(t=>t.id===q.task)?.title||q.task),node('h3',null,q.question));
  const details=node('details');details.append(node('summary',null,'Контекст и условия проверки'),node('div','context',q.context));card.append(details);
  if(q.manual_check){
    try{const url=new URL(q.manual_check.url);if(['http:','https:'].includes(url.protocol)){const link=node('a','button secondary','Открыть страницу проверки ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}}catch{}
    details.open=true;
  }
  const form=node('form');const fieldset=node('fieldset','options');fieldset.append(node('legend',null,'Выберите ответ'));
  const options=[...(q.options||[]),'Свой ответ'];
  for(const [index,option] of options.entries()){
    const wrapper=node('label','option');const input=node('input');input.type='radio';input.name='answer-'+q.id;input.value=String(index);input.required=true;
    wrapper.append(input,node('span',null,option));fieldset.append(wrapper);
  }
  const customLabel=node('label','field-label','Ваш ответ');const custom=node('textarea');custom.id='custom-'+q.id;customLabel.htmlFor=custom.id;custom.maxLength=32000;custom.hidden=true;customLabel.hidden=true;
  const explanationLabel=node('label','field-label','Пояснение, если нужно');const explanation=node('textarea');explanation.id='explanation-'+q.id;explanationLabel.htmlFor=explanation.id;explanation.maxLength=32000;
  const message=node('p','question-message');message.setAttribute('role','status');
  const actions=node('div','question-actions');const submit=node('button','button','Сохранить ответ');submit.type='submit';
  const later=node('button','button secondary','Ответить позже');later.type='button';later.onclick=()=>{details.open=false;message.textContent='Вопрос остаётся открытым. Ответ можно дать позже.';};
  actions.append(submit,later);form.append(fieldset,customLabel,custom,explanationLabel,explanation,actions,message);card.append(form);
  const entry={card,q,form,submit,message,runKey:latest.run.key};
  function selected(){return form.querySelector('input:checked')?.value;}
  function changed(){const isCustom=selected()===String(options.length-1);custom.hidden=!isCustom;customLabel.hidden=!isCustom;custom.required=isCustom;storage.set(draftKey(q),JSON.stringify({selected:selected(),custom:custom.value,explanation:explanation.value}));}
  try{const draft=JSON.parse(storage.get(draftKey(q))||'null');if(draft){const radio=[...form.querySelectorAll('input')].find(i=>i.value===draft.selected);if(radio)radio.checked=true;custom.value=draft.custom||'';explanation.value=draft.explanation||'';changed();}}catch{}
  form.addEventListener('input',changed);
  form.onsubmit=async event=>{
    event.preventDefault();if(entry.pending||entry.changed)return;
    const chosen=selected();if(chosen===undefined)return;
    const text=chosen===String(options.length-1)?custom.value.trim():options[Number(chosen)];
    if(!text){message.textContent='Введите ответ.';return;}
    entry.pending=true;submit.disabled=true;fieldset.disabled=true;custom.disabled=true;explanation.disabled=true;later.disabled=true;message.textContent='Сохраняем ответ…';
    const payload={id:q.id,revision:q.revision,text,explanation:explanation.value.trim()};
    try{const receipt=await api('/api/answers',payload,entry.runKey);if(!receipt.saved||receipt.question_id!==q.id)throw new Error('Сохранение ответа не подтверждено.');
      storage.remove(draftKey(q));toast('Ответ сохранён: '+receipt.answer.text+(latest.run.requires_resume?' Нажмите «Продолжить / запустить».':''));await refresh();
    }catch(error){message.textContent=error.message;}finally{entry.pending=false;submit.disabled=Boolean(entry.changed);fieldset.disabled=false;custom.disabled=false;explanation.disabled=false;later.disabled=false;if(latest)renderQuestions(latest);}
  };
  return entry;
}
function renderQuestions(value){
  const list=$('question-list');const ids=new Set(value.questions.map(q=>q.id));
  for(const [id,entry] of cards){if(!ids.has(id)&&!entry.pending){entry.card.remove();cards.delete(id);}}
  for(const q of value.questions){
    let entry=cards.get(q.id);
    if(!entry){entry=questionCard(q);cards.set(q.id,entry);list.append(entry.card);}
    else if(entry.q.revision!==q.revision&&!entry.changed){
      entry.changed=true;entry.submit.disabled=true;entry.message.textContent='Условия вопроса изменились. Просмотрите новую редакцию перед ответом.';
      const replace=node('button','button secondary','Показать новую редакцию');replace.type='button';replace.onclick=()=>{entry.card.remove();cards.delete(q.id);renderQuestions(latest);};entry.card.append(replace);
    }
  }
  list.querySelector('.empty')?.remove();
  if(!value.questions.length&&!cards.size)list.append(node('p','empty','Открытых вопросов нет. Новые появятся здесь автоматически.'));
}
function renderAnswers(value){
  const list=$('answer-list');const opened=new Set([...list.querySelectorAll('details[open]')].map(n=>n.dataset.id));list.replaceChildren();
  if(!value.answers.length){list.append(node('p','empty','Сохранённых ответов пока нет.'));return;}
  for(const q of value.answers){const item=node('details','answer-item');item.dataset.id=q.id;item.open=opened.has(q.id);item.append(node('summary',null,q.question),node('p','answer-text',q.answer.text));if(q.answer.explanation)item.append(node('p','context',q.answer.explanation));item.append(node('p','answer-meta','Сохранён '+date(q.answer.answered_at)));list.append(item);}
}
async function refresh(){
  if(refreshing)return;refreshing=true;$('refresh').disabled=true;
  try{const next=await api('/api/state');if(latest&&latest.run.key!==next.run.key){cards.clear();$('question-list').replaceChildren();}latest=next;renderManagement(latest);renderSummary(latest);renderRecovery(latest);renderRoute(latest);renderSessions(latest);renderObservers(latest);renderQuestions(latest);renderAnswers(latest);
    $('connection-error').hidden=true;$('connection').textContent='Подключено';$('connection-dot').classList.add('connected');$('updated').textContent='Получено '+new Date().toLocaleTimeString('ru-RU');
  }catch(error){$('connection-error').hidden=false;$('connection-error').textContent=error.message+' Данные на экране не обновляются.';$('connection').textContent='Нет связи';$('connection-dot').classList.remove('connected');}
  finally{refreshing=false;$('refresh').disabled=false;}
}
const actionNames={resume:'Продолжение работы',new:'Новая работа',stop:'Остановка',restart:'Перезапуск',diagnostics:'Диагностика',export:'Экспорт результата'};
const operationStates={queued:'В очереди',running:'Выполняется',waiting:'Нужен ответ в мастере',succeeded:'Операция завершена',failed:'Не выполнено',stopped:'Работа остановлена'};
let wizardKey='', actionPending=false;
const diagnosticDownloads=new Map();
const activeOperation=op=>['queued','running','waiting'].includes(op.status);
function diagnosticFilename(op){return 'sdd-diagnostics-'+new Date(op.created_at*1000).toISOString().replace(/[-:]/g,'').replace(/\.\d{3}Z$/,'Z')+'-'+op.id.slice(0,8)+'.txt';}
function pendingDiagnostic(){try{return JSON.parse(storage.get('operator-diagnostic-download')||'null');}catch{return null;}}
function trackDiagnostic(payload){if(payload.action==='diagnostics')storage.set('operator-diagnostic-download',JSON.stringify({id:payload.id,selected:latest.management.selected}));}
async function sendAction(action, revision=latest?.management.revision){
  if(actionPending||!latest)return;
  actionPending=true;
  renderManagement(latest);
  const payload={id:crypto.randomUUID().replaceAll('-',''),action,revision};
  storage.set('operator-pending-action',JSON.stringify(payload));
  trackDiagnostic(payload);
  try{await api('/api/actions',payload);storage.remove('operator-pending-action');$('control-confirm').hidden=true;await refresh();}
  catch(error){toast(error.message+' Если связь прервалась, повторите ту же операцию кнопкой ниже.');showRetry(payload);}
  finally{actionPending=false;if(latest)renderManagement(latest);}
}
function showRetry(payload){
  const panel=$('control-confirm');panel.hidden=false;panel.replaceChildren(node('p',null,'Подтверждение операции не получено. Повторная отправка не создаёт второй запуск.'));
  const retry=node('button','button','Проверить / повторить отправку');retry.onclick=async()=>{retry.disabled=true;try{await api('/api/actions',payload);storage.remove('operator-pending-action');panel.hidden=true;await refresh();}catch(error){toast(error.message);}finally{retry.disabled=false;}};
  const dismiss=node('button','button secondary','Закрыть');dismiss.onclick=()=>{panel.hidden=true;storage.remove('operator-pending-action');};panel.append(retry,dismiss);
}
function chooseAction(action){
  if(!['stop','restart'].includes(action)){sendAction(action);return;}
  const revision=latest.management.revision;
  const panel=$('control-confirm');panel.hidden=false;panel.replaceChildren(node('h3',null,actionNames[action]+' · '+latest.run.name),node('p',null,action==='stop'?'Остановить выполнение текущего запуска с сохранением результатов? Состояние служб будет проверено штатным механизмом.':'Остановить текущий контроллер и продолжить этот же запуск после подтверждения остановки? Если остановка не подтвердится, новый контроллер не запустится.'));
  const yes=node('button','button danger',action==='stop'?'Остановить':'Перезапустить');yes.onclick=()=>sendAction(action,revision);
  const no=node('button','button secondary','Отмена');no.onclick=()=>panel.hidden=true;panel.append(yes,no);
}
async function downloadArtifact(op){
  try{const response=await fetch('/api/artifact/'+op.id,{headers:{Authorization:'Bearer '+token},cache:'no-store'});if(!response.ok){const error=await response.json();throw new Error(error.error);}
    const url=URL.createObjectURL(await response.blob());const link=node('a');link.href=url;link.download=op.action==='export'?'sdd-result.patch':diagnosticFilename(op);document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);return true;
  }catch(error){toast(error.message);return false;}
}
async function downloadDiagnostic(op){
  if(diagnosticDownloads.get(op.id)==='loading')return;
  diagnosticDownloads.set(op.id,'loading');
  if(latest)renderDiagnostics(latest.management);
  const success=await downloadArtifact(op);
  diagnosticDownloads.set(op.id,success?'sent':'failed');
  if(pendingDiagnostic()?.id===op.id)storage.remove('operator-diagnostic-download');
  if(latest)renderDiagnostics(latest.management);
}
function renderDiagnostics(m){
  const panel=$('diagnostics-result'), selected=m.runs.find(run=>run.key===m.selected);
  const reports=m.operations.filter(op=>op.action==='diagnostics'&&op.config===selected?.config);
  const pending=pendingDiagnostic(), requested=pending?.selected===m.selected?pending:null;
  const op=requested?reports.find(item=>item.id===requested.id):reports[0];
  const signature=JSON.stringify([m.selected,op?.id,op?.status,op?.artifact,op?.created_at,op?.error,requested?.id,diagnosticDownloads.get(op?.id)]);
  if(panel.dataset.signature===signature)return;panel.dataset.signature=signature;
  panel.hidden=!op&&!requested;panel.replaceChildren();
  if(panel.hidden)return;
  if(!op){panel.append(node('strong',null,'Запрашиваем диагностику…'),node('p','muted','Ожидаем подтверждение сервера. Повторное нажатие не требуется.'));return;}
  const state=diagnosticDownloads.get(op.id);
  const title=activeOperation(op)?'Собираем диагностику…':op.status==='succeeded'?'Диагностика готова':'Диагностику собрать не удалось';
  panel.append(node('strong',null,title),node('p','muted','Создана: '+date(op.created_at)));
  if(op.error)panel.append(node('p','question-message',op.error));
  if(activeOperation(op)){panel.append(node('p','muted','После подготовки файл будет передан браузеру для скачивания. Работа запуска продолжается.'));return;}
  if(op.status==='succeeded'&&op.artifact){
    panel.append(node('p','path',diagnosticFilename(op)));
    const button=node('button','button',state==='loading'?'Передаём файл…':state==='failed'?'Повторить скачивание':'Скачать этот отчёт');
    button.disabled=state==='loading';button.onclick=()=>downloadDiagnostic(op);panel.append(button);
    panel.append(node('p','muted',state==='sent'?'Файл передан браузеру. Если скачивание заблокировано, нажмите «Скачать этот отчёт».':state==='failed'?'Не удалось передать файл. Отчёт сохранён; попробуйте скачать его ещё раз.':'Это последний отчёт выбранного запуска. Дата отличает его от прежних снимков.'));
    if(requested?.id===op.id&&!state)downloadDiagnostic(op);
  }else if(requested?.id===op.id)storage.remove('operator-diagnostic-download');
}
function renderManagement(value){
  const m=value.management;if(!m)return;const runtime=m.runtime;
  const startup=$('startup-result'), attempt=m.startup;
  startup.replaceChildren();startup.hidden=!attempt||runtime.active||!['queued','running','waiting','failed'].includes(attempt.status);
  if(!startup.hidden){
    startup.append(node('strong',null,attempt.status==='failed'?'Не удалось запустить работу':attempt.status==='waiting'?'Запуск ожидает вашего ответа':'Запуск выполняется…'));
    startup.append(node('p',null,attempt.error||attempt.notice||'Подтверждение принято. Ожидаем результат запуска контроллера.'));
    startup.append(node('p','muted','Попытка от '+date(attempt.created_at)));
  }
  const picker=$('run-picker');const signature=JSON.stringify(m.runs.map(r=>[r.key,r.name,r.status]));
  if(picker.dataset.signature!==signature){picker.replaceChildren();if(!m.runs.length){const option=node('option',null,'Сохранённых запусков нет');option.value='';picker.append(option);}for(const run of m.runs){const option=node('option',null,run.name+' · '+label(run.status));option.value=run.key;picker.append(option);}picker.dataset.signature=signature;}
  picker.value=m.runs.length?m.selected:'';
  const busy=m.operations.some(op=>['queued','running','waiting'].includes(op.status)&&['new','resume','restart','export'].includes(op.action));
  $('controller-state').textContent=runtime.active?(runtime.stop_requested?'ОСТАНАВЛИВАЕТСЯ':'КОНТРОЛЛЕР РАБОТАЕТ'):'КОНТРОЛЛЕР ОСТАНОВЛЕН';
  const selected=m.runs.some(r=>r.key===m.selected);
  $('action-resume').disabled=!selected||runtime.active||busy;
  $('action-new').disabled=runtime.active||busy;
  $('action-stop').disabled=!runtime.controllable||runtime.stop_requested;
  $('action-restart').disabled=!runtime.controllable||runtime.stop_requested||m.operations.some(op=>['queued','running','waiting'].includes(op.status)&&op.action==='restart');
  const collecting=m.operations.some(op=>op.action==='diagnostics'&&activeOperation(op));
  $('action-diagnostics').disabled=!selected||actionPending||collecting;
  $('action-diagnostics').textContent=collecting?'Собираем диагностику…':'Снять диагностику';
  renderDiagnostics(m);
  $('action-export').disabled=!selected||runtime.active||busy||!value.run.ready;
  $('control-note').textContent=runtime.reason||(runtime.stop_requested?'Запрос принят. Ожидаем завершения работы и освобождения блокировки.':'Закрытие вкладки не останавливает работу. Все подтверждения мастера появятся здесь.');
  const brief=$('operation-latest');brief.replaceChildren();const recent=m.operations.find(op=>['queued','running','waiting'].includes(op.status))||m.operations[0];if(recent){brief.append(node('p','muted',(actionNames[recent.action]||recent.action)+' · '+(operationStates[recent.status]||recent.status)));if(recent.notice)brief.append(node('p','muted',recent.notice));if(recent.error)brief.append(node('p','question-message',recent.error));if(recent.artifact&&recent.status==='succeeded'){const button=node('button','button secondary','Скачать результат операции');button.onclick=()=>downloadArtifact(recent);brief.append(button);}}
  brief.hidden=!startup.hidden&&recent?.id===attempt?.id;
  const list=$('operation-list');list.replaceChildren();
  for(const op of m.operations){const row=node('div','operation-row');row.append(node('strong',null,(actionNames[op.action]||op.action)+' · '+(operationStates[op.status]||op.status)),node('p','muted',date(op.created_at)));if(op.error)row.append(node('p','question-message',op.error));if(op.notice)row.append(node('p','muted',op.notice));if(op.artifact&&op.status==='succeeded'){const button=node('button','button secondary',op.action==='diagnostics'?'Скачать отчёт от '+date(op.created_at):'Скачать файл');button.onclick=()=>op.action==='diagnostics'?downloadDiagnostic(op):downloadArtifact(op);row.append(button);}list.append(row);}
  const op=m.operations.find(o=>o.status==='waiting'&&o.question);
  const key=op?op.id+':'+op.question.id:'';
  if(key===wizardKey)return;wizardKey=key;const area=$('wizard');area.replaceChildren();if(!op)return;
  const q=op.question, card=node('article','question-card wizard-card'),form=node('form');
  card.append(node('div','question-task',actionNames[op.action]),node('h3','wizard-question',q.title));
  for(const message of op.messages||[])card.append(node('p','context',message));
  const choices=node('fieldset','options');choices.append(node('legend',null,q.kind==='confirm'?'Подтвердите действие':'Выберите вариант'));
  let input;
  if(q.options.length){for(const [i,text] of q.options.entries()){const wrapper=node('label','option'),radio=node('input');radio.type='radio';radio.name='wizard-choice';radio.value=String(i);radio.required=true;wrapper.append(radio,node('span',null,text));choices.append(wrapper);}form.append(choices);}
  else {const caption=node('label','field-label','Полный путь на компьютере, где запущена обвязка');input=node('input','wizard-path');input.id='wizard-path';caption.htmlFor=input.id;input.type='text';input.required=true;input.maxLength=4096;form.append(caption,input);}
  const draftKey='operator-wizard:'+key;try{const draft=JSON.parse(storage.get(draftKey)||'null');if(draft!==null){if(input)input.value=draft;else{const radio=[...choices.querySelectorAll('input')].find(r=>Number(r.value)===draft);if(radio)radio.checked=true;}}}catch{}
  const valueOf=()=>input?input.value:Number(choices.querySelector('input:checked')?.value);
  form.oninput=()=>storage.set(draftKey,JSON.stringify(valueOf()));
  const submit=node('button','button',q.kind==='confirm'?'Подтвердить выбор':'Продолжить'),cancel=node('button','button secondary','Отменить мастер'),message=node('p','question-message');submit.type='submit';cancel.type='button';const actions=node('div','question-actions');actions.append(submit,cancel);form.append(actions,message);card.append(form);area.append(card);
  form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;cancel.disabled=true;choices.disabled=true;if(input)input.disabled=true;try{await api('/api/wizard/reply',{operation:op.id,question:q.id,value:valueOf()});storage.remove(draftKey);message.textContent='Ответ сохранён. Мастер продолжает работу.';await refresh();}catch(error){message.textContent=error.message;submit.disabled=false;cancel.disabled=false;choices.disabled=false;if(input)input.disabled=false;}};
  cancel.onclick=async()=>{try{await api('/api/wizard/cancel',{operation:op.id});await refresh();}catch(error){message.textContent=error.message;}};
}
for(const action of Object.keys(actionNames))$('action-'+action).onclick=()=>chooseAction(action);
$('run-picker').onchange=async event=>{try{await api('/api/select',{key:event.target.value});await refresh();}catch(error){toast(error.message);}};
try{const pending=JSON.parse(storage.get('operator-pending-action')||'null');if(pending)showRetry(pending);}catch{}
$('refresh').onclick=refresh;
$('show-log').onclick=async()=>{const button=$('show-log');button.disabled=true;try{const result=await api('/api/log');$('log').textContent=result.text;$('log').hidden=false;$('log-note').textContent=result.truncated?'Показаны последние 64 КБ. Полный журнал находится по пути выше.':'Последние записи журнала.';}catch(error){$('log-note').textContent=error.message;}finally{button.disabled=false;}};
refresh();setInterval(()=>{if(!document.hidden)refresh();},3000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
