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
const statuses = {done:'Принято', running:'В работе', pending:'Ожидает', blocked:'Заблокировано', 'waiting-user':'Нужен ответ', 'waiting-scope':'Ожидает область работы', 'waiting-retry':'Повтор позже', interrupted:'Остановлено', complete:'Завершено', accepted:'Вариант проверен', stalled:'Приостановлено', 'not-observed':'Нет наблюдения', 'external-blocker':'Нужны внешние данные'};
const phases = {execute:'Разработка', review:'Независимое ревью', checks:'Проверки', adjudicate:'Разбор замечаний', accepted:'Проверен'};
const label = s => statuses[s] || s || 'Состояние неизвестно';
const duration = value => {if(!Number.isFinite(value)) return '—'; const n=Math.max(0,Math.floor(value)); return [Math.floor(n/3600),Math.floor(n/60)%60,n%60].map(v=>String(v).padStart(2,'0')).join(':');};
const date = stamp => typeof stamp==='number' ? new Date(stamp*1000).toLocaleString('ru-RU') : 'Время неизвестно';
function toast(text) {$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,7000);}
async function api(path, body) {
  const headers={Authorization:'Bearer '+token}; const options={headers,cache:'no-store'};
  if(body!==undefined){options.method='POST';headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch(path,options); const value=await response.json();
  if(!response.ok) throw new Error(value.error || 'Не удалось выполнить запрос.');
  return value;
}
function renderSummary(value) {
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
  let title,description;
  if(count){title='Нужен ваш ответ';description=`Открытых вопросов: ${count}. ${run.requires_resume?'После ответа продолжите сохранённый запуск в мастере.':'Независимая работа может продолжаться.'}`;}
  else if(run.ready){title='Работа принята';description='Все задачи и итоговая проверка завершены.';}
  else if(run.requires_resume){title=label(run.status);description='Для продолжения используйте мастер запуска в терминале. Сохранённая работа остаётся на месте.';}
  else if(uncertain){title='Состояние требует обновления';description='Текущие сессии нельзя достоверно определить по последнему наблюдению.';}
  else {title='В работе';description='Открытых вопросов нет. Приёмка результатов подтверждается проверками.';}
  $('status-heading').textContent=title;$('status-description').textContent=description;
  document.querySelector('.status-banner').classList.toggle('attention',Boolean(count||uncertain||run.status==='blocked'||run.status==='interrupted'));
  $('answer-shortcut').hidden=!count;
  $('log-path').textContent=run.log_path;
}
function renderRoute(value) {
  const list=$('route');list.replaceChildren();
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
    row.append(details);list.append(row);
  }
}
function renderObservers(value){const list=$('observer-list');list.replaceChildren();for(const line of value.observers.lines||[])list.append(node('p','observer-line',line));if(!list.childNodes.length)list.append(node('p','empty','Оценок аудиторов пока нет.'));}
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
  const entry={card,q,form,submit,message};
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
    try{const receipt=await api('/api/answers',payload);if(!receipt.saved||receipt.question_id!==q.id)throw new Error('Сохранение ответа не подтверждено.');
      storage.remove(draftKey(q));toast('Ответ сохранён: '+receipt.answer.text+(latest.run.requires_resume?' Продолжите запуск в мастере.':''));await refresh();
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
  try{latest=await api('/api/state');renderSummary(latest);renderRoute(latest);renderSessions(latest);renderObservers(latest);renderQuestions(latest);renderAnswers(latest);
    $('connection-error').hidden=true;$('connection').textContent='Подключено';$('connection-dot').classList.add('connected');$('updated').textContent='Получено '+new Date().toLocaleTimeString('ru-RU');
  }catch(error){$('connection-error').hidden=false;$('connection-error').textContent=error.message+' Данные на экране не обновляются.';$('connection').textContent='Нет связи';$('connection-dot').classList.remove('connected');}
  finally{refreshing=false;$('refresh').disabled=false;}
}
$('refresh').onclick=refresh;
$('show-log').onclick=async()=>{const button=$('show-log');button.disabled=true;try{const result=await api('/api/log');$('log').textContent=result.text;$('log').hidden=false;$('log-note').textContent=result.truncated?'Показаны последние 64 КБ. Полный журнал находится по пути выше.':'Последние записи журнала.';}catch(error){$('log-note').textContent=error.message;}finally{button.disabled=false;}};
refresh();setInterval(()=>{if(!document.hidden)refresh();},3000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
