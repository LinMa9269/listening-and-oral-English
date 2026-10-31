'use strict';
const $=id=>document.getElementById(id), video=$('video');
const {timestamp,parseSubtitles,matches,normalize}=ListenCore;
let library=[],current=null,selectedWord='',segment=null,connected=false,selectionVersion=0;
let pendingSettings=new Map(),writingSettings=false,lastPositionAt=0,sourceId=null,renderedSignature='',importBusy=false,pollBusy=false;
let stateEpoch=0,trashBusy=false;
const busyStates=['queued','downloading','transcribing'];
const stateLabel={queued:'等待识别',downloading:'下载中',transcribing:'字幕识别中',ready:'已存档',error:'需要处理'};
function notice(text,error=false){$('connection').textContent=text;$('connection').className='notice'+(error?' error':'');$('connection').hidden=!text;}
function jobNotice(text,error=false){$('jobStatus').textContent=text;$('jobStatus').className='status'+(error?' error':'');}
async function api(path,options={}){const epoch=stateEpoch,response=await fetch(path,{cache:'no-store',...options});const data=await response.json().catch(()=>({error:'服务返回了无法读取的结果，请重新启动程序。'}));if(epoch!==stateEpoch){const e=new Error('列表已更新，请重试刚才的操作。');e.stale=true;throw e;}if(!response.ok){const e=new Error(data.error||`请求失败 (${response.status})`);e.status=response.status;throw e;}return data;}
const post=(path,data)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
function stop(){segment=null;video.pause();}
function rememberVideo(id){try{localStorage.setItem('listen-lab-active-video',id);}catch{}}
function rememberedVideo(){try{return localStorage.getItem('listen-lab-active-video');}catch{return null;}}
function textNode(tag,text,className){const n=document.createElement(tag);n.textContent=text;if(className)n.className=className;return n;}
function controls(){
  $('media').disabled=!connected||importBusy;$('importLink').disabled=!connected||importBusy;
  for(const id of ['subs','query','addWord','offset','speed'])$(id).disabled=!connected||!current;
  $('transcribe').disabled=!connected||!current||busyStates.includes(current.status);
  $('transcribe').textContent=current&&!current.media_url?'重试链接导入':'重新识别字幕';
  $('export').disabled=!current?.cues?.length;
}
function updateSummary(item){const summary={...item,word_count:item.words?.length??item.word_count??0,cue_count:item.cues?.length??item.cue_count??0};const idx=library.findIndex(v=>v.id===item.id);if(idx<0)library.unshift(summary);else library[idx]=summary;renderLibrary();}
function renderLibrary(){
  $('totalVideos').textContent=library.length;$('libraryCount').textContent=library.length;$('totalWords').textContent=library.reduce((sum,v)=>sum+(v.word_count??v.words?.length??0),0);$('library').replaceChildren();
  if(!library.length)$('library').append(textNode('p','添加第一个视频，开始积累你的学习记录。','empty-note'));
  for(const item of library){const row=textNode('div','','library-entry'),b=textNode('button','','library-item'+(current?.id===item.id?' active':''));b.type='button';b.setAttribute('aria-current',String(current?.id===item.id));const d=new Date(item.created_at),date=Number.isNaN(d.getTime())?'':d.toLocaleDateString('zh-CN',{month:'short',day:'numeric'});b.append(textNode('strong',item.title||'未命名视频'),textNode('small',`${stateLabel[item.status]||'已保存'} · ${item.word_count??item.words?.length??0} 个词${date?' · '+date:''}`));b.onclick=()=>selectVideo(item.id);const remove=textNode('button','删除','delete-button');remove.type='button';remove.setAttribute('aria-label','删除视频：'+item.title);remove.onclick=()=>changeTrash(item.id,null,'trash',item.title);row.append(b,remove);$('library').append(row);}
}
function renderJob(){if(!current)return;const info=current.cues.length?`已保存 ${current.cues.length} 个字幕片段。`:'';if(current.status==='error')jobNotice((current.error||'识别未完成，请重试或手动导入字幕。')+(info?' '+info:''),true);else if(busyStates.includes(current.status))jobNotice(`${stateLabel[current.status]}。${current.job_stage||'可切换视频或关闭页面，程序运行期间任务会继续。'}${info?' '+info:''}`);else jobNotice(info?info+' 点击生词听原声；字幕和单词已存档。':'识别结束，未找到语音字幕。可导入匹配字幕，或检查视频是否有清晰人声。');}
function applyItem(item,force=false){
  if(!current||current.id!==item.id)return;current={...item,cues:item.cues||[],words:item.words||[]};if(selectedWord&&!current.words.some(w=>normalize(w.query)===normalize(selectedWord))){selectedWord='';stop();}$('title').textContent=current.title||'学习视频';updateSummary(current);renderJob();controls();
  if(current.media_url&&sourceId!==current.id){sourceId=current.id;video.src=current.media_url;video.load();const id=current.id,pos=current.position||0,rate=current.speed||1;video.onloadedmetadata=()=>{if(current?.id!==id)return;video.playbackRate=rate;if(pos>0&&pos<video.duration)video.currentTime=pos;};}
  const sig=JSON.stringify([current.id,current.cues,current.words]);if(force||sig!==renderedSignature){renderedSignature=sig;renderWords();renderResults();}
}
async function selectVideo(id){
  queuePosition();flushSettings();stop();const version=++selectionVersion;
  try{const item=await api('/api/videos/'+id);if(version!==selectionVersion)return;current={...item,cues:item.cues||[],words:item.words||[]};selectedWord=current.selected_word||current.words[0]?.query||'';rememberVideo(id);sourceId=null;video.removeAttribute('src');video.load();$('title').textContent=current.title||'学习视频';$('query').value='';$('offset').value=current.offset||0;$('speed').value=String(current.speed||1);$('saveState').textContent='已载入存档';applyItem(current,true);}catch(e){notice(e.message,true);}
}
function renderWords(){
  $('words').replaceChildren();$('wordCount').textContent=current?.words.length||0;
  for(const w of current?.words||[]){const id=current.id,row=textNode('span','','word-entry'),active=normalize(w.query)===normalize(selectedWord),b=textNode('button','','word-chip'+(active?' active':''));b.type='button';b.setAttribute('aria-pressed',String(active));b.append(document.createTextNode(w.query),textNode('small',current.cues.filter(c=>matches(c.text,w.query)).length));b.onclick=()=>{stop();selectedWord=w.query;queueSettings(current.id,{selected_word:w.query});flushSettings();renderWords();renderResults();};const remove=textNode('button','×','delete-button');remove.type='button';remove.setAttribute('aria-label','删除生词：'+w.query);remove.onclick=()=>changeTrash(id,w.id,'trash',w.query);row.append(b,remove);$('words').append(row);}
}
function renderResults(){
  $('results').replaceChildren();if(!current||!selectedWord){$('count').textContent=current?'存入一个单词，开始精听。':'先添加视频，再存下没听懂的词。';const e=textNode('div','','empty');e.append(textNode('span','⌁'),textNode('strong','先听原声，再揭晓答案。'),textNode('p','每条句子默认隐藏，听两遍后再点击显示。'));$('results').append(e);return;}
  const found=current.cues.filter(c=>matches(c.text,selectedWord));$('count').textContent=`${selectedWord} · ${found.length} 个片段 · 记录已保存`;
  if(!found.length){$('results').append(textNode('div',!current.cues.length?'单词已存档。字幕识别完成或导入后，会自动查找对应片段。':'单词已存档，但字幕中没有匹配项。可尝试其他词形；识别文字也可能存在误差。','empty'));return;}
  const id=current.id;
  for(const cue of found){const card=textNode('article','','result'),time=textNode('div',`${timestamp(cue.start)} — ${timestamp(cue.end)}`,'time'),mask=textNode('div','','concealed'),sentence=textNode('div','','sentence');mask.setAttribute('aria-label','句子已隐藏');sentence.hidden=true;const row=textNode('div','','row'),play=textNode('button','▶ 听原声','primary'),reveal=textNode('button','显示文本');play.type=reveal.type='button';reveal.setAttribute('aria-expanded','false');play.onclick=()=>{if(current?.id===id)playCue(cue);};reveal.onclick=()=>{sentence.hidden=!sentence.hidden;sentence.textContent=sentence.hidden?'':cue.text;mask.hidden=!sentence.hidden;reveal.textContent=sentence.hidden?'显示文本':'隐藏文本';reveal.setAttribute('aria-expanded',String(!sentence.hidden));};row.append(play,reveal);card.append(time,mask,sentence,row);addClipEditor(card,cue,id,time,play);$('results').append(card);}
}

function addClipEditor(card,cue,id,time,play){
  const index=current.cues.indexOf(cue),original={start:cue.start,end:cue.end,text:cue.text};
  const editor=textNode('div','','clip-editor'),heading=textNode('strong','调整原声片段'),length=textNode('span','','clip-length');
  const startRange=document.createElement('input'),endRange=document.createElement('input'),startNumber=document.createElement('input'),endNumber=document.createElement('input');
  const fields=[startRange,endRange,startNumber,endNumber];
  for(const [input,label,type] of [[startRange,'起点进度条','range'],[endRange,'终点进度条','range'],[startNumber,'起点秒数','number'],[endNumber,'终点秒数','number']]){input.type=type;input.step='0.05';input.min='0';input.setAttribute('aria-label',label);}
  const startRow=textNode('label','','clip-control'),endRow=textNode('label','','clip-control');
  startRow.append(textNode('span','起点'),startRange,startNumber,textNode('span','秒'));endRow.append(textNode('span','终点'),endRange,endNumber,textNode('span','秒'));
  const buttons=textNode('div','','row'),save=textNode('button','保存范围'),reset=textNode('button','恢复原范围'),status=textNode('div','','micro');save.type=reset.type='button';status.setAttribute('role','status');
  buttons.append(save,reset);editor.append(heading,length,startRow,endRow,buttons,status,textNode('p','拖动起点或终点可延长、缩短片段；也可输入秒数。先试听，再保存。调整只影响这条字幕的原声播放，不改动隐藏文本。','micro'));card.append(editor);
  let start=0,end=0,dirty=false,saving=false,initialized=false;
  const duration=()=>current?.id===id&&sourceId===id&&video.readyState>=1&&Number.isFinite(video.duration)?video.duration:0;
  const valid=()=>duration()>=.05&&Number.isFinite(start)&&Number.isFinite(end)&&start>=0&&end<=duration()&&end-start>=.049999;
  function draw(activeInput=null){
    const available=duration()>=.05;
    fields.forEach(input=>{input.disabled=!available||saving;input.max=String(duration());});
    save.disabled=!valid()||!dirty||saving;reset.disabled=!available||saving;
    play.disabled=!valid()||saving;
    if(!available){status.textContent='等待视频加载后即可调整片段。';return;}
    startRange.value=start;endRange.value=end;if(activeInput!==startNumber)startNumber.value=Number.isFinite(start)?start:'';if(activeInput!==endNumber)endNumber.value=Number.isFinite(end)?end:'';
    if(valid()){time.textContent=`${timestamp(start)} — ${timestamp(end)}`;length.textContent=`时长 ${(end-start).toFixed(2)} 秒`;}
    else length.textContent='范围无效';
  }
  function initialize(){if(initialized||!duration())return;initialized=true;const offset=Number($('offset').value)||0;start=Math.max(0,Math.min(duration(),cue.clip_start??(cue.start+offset)));end=Math.max(0,Math.min(duration(),cue.clip_end??(cue.end+offset)));status.textContent=cue.clip_start!==undefined?'已保存自定义范围（视频时间，不再叠加字幕偏移）。':'原字幕范围；保存后将固定到视频时间。';draw();}
  for(const [input,side] of [[startRange,'start'],[endRange,'end'],[startNumber,'start'],[endNumber,'end']]){
    input.oninput=()=>{if(saving)return;stop();let value=input.value.trim()===''?NaN:Number(input.value);if(input.type==='range')value=side==='start'?Math.min(value,end-.05):Math.max(value,start+.05);value=Number.isFinite(value)?Math.round(value*1000)/1000:value;if(side==='start')start=value;else end=value;dirty=true;status.textContent=valid()?'尚未保存，可以点击「听原声」试听。':'起点需早于终点，至少保留 0.05 秒，且不能超出视频。';draw(input);};
  }
  play.onclick=()=>{if(current?.id===id&&valid())playCue({...cue,clip_start:start,clip_end:end});};
  async function persistRange(restore){
    if(saving||current?.id!==id||(!restore&&!valid()))return;
    saving=true;stop();draw();status.textContent='保存中…';
    try{const item=await post(`/api/videos/${id}/clip`,{index,original,...(restore?{reset:true}:{start,end,duration:duration()})});if(current?.id===id){applyItem(item,true);}notice(restore?'已恢复字幕原范围。':'片段范围已保存，下次播放与循环会使用新范围。');}
    catch(e){status.textContent='保存失败：'+e.message;}
    finally{saving=false;draw();}
  }
  save.onclick=()=>persistRange(false);reset.onclick=()=>persistRange(true);
  // Metadata arrives after cards are built. A short-lived callback avoids rebuilding drafts during polling.
  const onReady=()=>{if(current?.id!==id||!card.isConnected)return;initialize();draw();};
  video.addEventListener('loadedmetadata',onReady,{once:true});initialize();draw();
}

async function playCue(cue){if(!current?.media_url||video.readyState<1)return jobNotice('请等待视频加载。如果加载失败，请检查媒体编码。',true);stop();const offset=Number($('offset').value)||0,start=Math.max(0,cue.clip_start??(cue.start+offset)),end=Math.min(cue.clip_end??(cue.end+offset),video.duration);if(end<=start||start>=video.duration)return jobNotice('片段超出视频范围，请调整字幕偏移或检查字幕。',true);segment={start,end};video.currentTime=start;try{await video.play();}catch{stop();jobNotice('播放未能开始，请检查媒体格式。',true);}}
function checkSegment(){if(segment&&video.currentTime>=segment.end-.03){if($('loop').checked){video.currentTime=segment.start;if(video.paused)video.play().catch(()=>stop());}else stop();}}
setInterval(checkSegment,50);
video.addEventListener('timeupdate',()=>{checkSegment();if(Date.now()-lastPositionAt>5000){lastPositionAt=Date.now();queuePosition();flushSettings();}});
video.addEventListener('pause',()=>{queuePosition();flushSettings();});
video.addEventListener('seeking',()=>{if(segment&&(video.currentTime<segment.start-.25||video.currentTime>segment.end+.25))segment=null;});
video.addEventListener('ended',()=>{if(segment&&$('loop').checked){video.currentTime=segment.start;video.play().catch(()=>stop());}else stop();});
video.onerror=()=>{if(current?.media_url)jobNotice('视频暂时无法播放，可能是编码不兼容。记录仍已保留；建议上传 MP4（H.264 / AAC）。',true);};
function queueSettings(id,changes){if(!id)return;pendingSettings.set(id,{...pendingSettings.get(id),...changes});if(current?.id===id)$('saveState').textContent='保存中…';}
function queuePosition(){if(current&&sourceId===current.id&&video.readyState>=1)queueSettings(current.id,{position:video.currentTime});}
async function flushSettings(){if(writingSettings||!connected)return;writingSettings=true;while(pendingSettings.size){const[id,changes]=pendingSettings.entries().next().value;pendingSettings.delete(id);try{await post(`/api/videos/${id}/settings`,changes);if(current?.id===id)$('saveState').textContent='已自动保存';}catch(e){if(e.status===404||e.stale)continue;pendingSettings.set(id,{...changes,...pendingSettings.get(id)});if(current?.id===id)$('saveState').textContent='保存失败，将重试';notice(e.message,true);break;}}writingSettings=false;}
$('stop').onclick=stop;
$('speed').onchange=()=>{if(!current)return;video.playbackRate=Number($('speed').value);queueSettings(current.id,{speed:video.playbackRate});flushSettings();};
$('offset').onchange=()=>{if(!current)return;stop();const offset=Number($('offset').value);if(!Number.isFinite(offset)||Math.abs(offset)>3600){$('offset').value=current.offset||0;return;}queueSettings(current.id,{offset});flushSettings();renderResults();};
document.addEventListener('visibilitychange',()=>{if(document.hidden){queuePosition();flushSettings();}});
window.addEventListener('pagehide',()=>{queuePosition();for(const[id,changes]of pendingSettings)fetch(`/api/videos/${id}/settings`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes),keepalive:true}).catch(()=>{});});
$('searchForm').onsubmit=async e=>{e.preventDefault();if(!current)return;const id=current.id,q=$('query').value.trim();if(!q)return;$('addWord').disabled=true;try{const item=await post(`/api/videos/${id}/words`,{query:q});updateSummary(item);if(current?.id===id){selectedWord=q;$('query').value='';applyItem(item,true);}}catch(e){notice('单词未保存：'+e.message,true);}finally{controls();}};
$('media').onchange=async e=>{const file=e.target.files[0];if(!file)return;if(file.size>500*1024*1024){notice('单个视频最多 500 MB，请先裁剪。',true);e.target.value='';return;}importBusy=true;controls();$('importStatus').textContent='正在将视频保存到本机学习库，请稍候…';try{const item=await api('/api/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file});updateSummary(item);await selectVideo(item.id);$('importStatus').textContent='视频已存档，字幕识别已安排。';}catch(e){$('importStatus').textContent='保存失败：'+e.message;}finally{importBusy=false;controls();e.target.value='';}};
$('urlForm').onsubmit=async e=>{e.preventDefault();importBusy=true;controls();$('importStatus').textContent='正在添加链接任务…';try{const item=await post('/api/import',{url:$('url').value.trim()});updateSummary(item);await selectVideo(item.id);$('url').value='';$('importStatus').textContent='已加入学习库，正在后台尝试下载并识别。';}catch(e){$('importStatus').textContent='链接未导入：'+e.message;}finally{importBusy=false;controls();}};
$('subs').onchange=async e=>{const file=e.target.files[0],id=current?.id;if(!file||!id)return;try{const cues=parseSubtitles(await file.text());if(!cues.length)throw new Error('未发现有效的 SRT / VTT 时间轴，请使用 UTF-8 字幕。');const item=await post(`/api/videos/${id}/cues`,{cues});updateSummary(item);if(current?.id===id)applyItem(item,true);}catch(e){notice('字幕未保存：'+e.message,true);}finally{e.target.value='';}};
$('transcribe').onclick=async()=>{if(!current)return;const id=current.id;$('transcribe').disabled=true;try{const item=await post(`/api/videos/${id}/transcribe`,{});if(current?.id===id)applyItem(item);updateSummary(item);}catch(e){notice(e.message,true);}finally{controls();}};
$('export').onclick=()=>{if(!current?.cues.length)return;const str='WEBVTT\n\n'+current.cues.map(c=>`${timestamp(c.start)} --> ${timestamp(c.end)}\n${c.text}\n`).join('\n');const u=URL.createObjectURL(new Blob([str],{type:'text/vtt'})),a=document.createElement('a');a.href=u;a.download='listen-lab.vtt';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);};
async function poll(){if(pollBusy||!connected||trashBusy)return;pollBusy=true;try{const data=await api('/api/library');library=data.items;renderLibrary();if(current&&!library.some(v=>v.id===current.id)){clearCurrent();if(library.length)await selectVideo(library[0].id);}const id=current?.id,version=selectionVersion;if(id){const item=await api('/api/videos/'+id);if(version===selectionVersion&&current?.id===id)applyItem(item);}if(!$('trashPanel').hidden)await loadTrash();flushSettings();}catch(e){if(e.stale)return;notice('本地服务连接中断。记录已保存在电脑上；请重新启动 start.bat，页面会自动重连。',true);connected=false;controls();}finally{pollBusy=false;}}
async function connect(){if(location.protocol==='file:'){notice('请先双击同文件夹的 start.bat，再在浏览器打开 http://127.0.0.1:8765。新版的自动识别和长期存档需要本地程序运行。',true);return;}try{const health=await api('/api/health');connected=true;controls();const data=await api('/api/library');library=data.items;renderLibrary();notice(health.transcription_available?'':'本地转写组件尚未安装。请运行 start.bat 完成安装；现在仍可保存视频并导入字幕。',!health.transcription_available);const id=current?.id||rememberedVideo();if(library.length)await selectVideo(library.some(v=>v.id===id)?id:library[0].id);else clearCurrent();}catch(e){connected=false;controls();notice('暂未连接到本地服务。请双击 start.bat 并保持运行，页面会自动重连。',true);}}

function clearCurrent(){
  ++selectionVersion;current=null;selectedWord='';sourceId=null;renderedSignature='';stop();video.removeAttribute('src');video.load();rememberVideo('');
  $('title').textContent='选择一段你想听懂的视频';$('query').value='';$('saveState').textContent='';$('offset').value=0;$('speed').value='1';jobNotice('添加视频开始练习，也可以从回收站恢复。');renderWords();renderResults();renderLibrary();controls();
}
async function loadTrash(){
  const data=await api('/api/trash');$('trashItems').replaceChildren();
  if(!data.items.length)$('trashItems').append(textNode('p','回收站是空的。','empty-note'));
  for(const item of data.items){
    const row=textNode('article','','trash-item'),info=textNode('div','','trash-info');
    info.append(textNode('strong',(item.kind==='video'?'视频 · ':'生词 · ')+item.title));
    const detail=(item.kind==='word'?'所属视频：'+item.video_title+' · ':'')+'删除于 '+new Date(item.deleted_at).toLocaleString('zh-CN')+' · '+(item.purge_requested?'等待释放文件后永久清理':item.days_left>0?'剩余 '+item.days_left+' 天':'已到期，等待清理');
    info.append(textNode('p',detail,'micro'));const actions=textNode('div','','row');
    const restore=textNode('button','恢复'),purge=textNode('button',item.purge_requested?'重试清理':'永久删除','delete-button');restore.type=purge.type='button';restore.disabled=item.purge_requested;
    restore.onclick=()=>changeTrash(item.video_id,item.kind==='word'?item.id:null,'restore',item.title);
    purge.onclick=()=>changeTrash(item.video_id,item.kind==='word'?item.id:null,'purge',item.title);
    actions.append(restore,purge);row.append(info,actions);$('trashItems').append(row);
  }
}
async function changeTrash(id,wordId,action,title){
  if(trashBusy)return;
  const kind=wordId?'生词':'视频';
  if(action==='trash'&&!window.confirm(`将${kind}「${title}」移入回收站？${wordId?'字幕和视频会保留。':'关联字幕、生词和播放记录一起保留，不影响原始文件。'}30 天内可恢复。`))return;
  if(action==='purge'&&!window.confirm(`永久删除${kind}「${title}」？${wordId?'此生词记录':'视频存档副本、字幕及全部生词'}将无法恢复。`))return;
  trashBusy=true;++stateEpoch;++selectionVersion;
  if(current?.id===id){stop();queuePosition();}
  await flushSettings();pendingSettings.delete(id);
  try{
    const result=await post(`/api/videos/${id}${wordId?'/words/'+wordId:''}/${action}`,{});++stateEpoch;
    const data=await api('/api/library');library=data.items;
    if(current?.id===id&&!wordId&&action!=='restore'){clearCurrent();if(library.length)await selectVideo(library[0].id);}
    else if(current?.id===id){const item=await api('/api/videos/'+id);selectedWord=item.words.some(w=>normalize(w.query)===normalize(selectedWord))?selectedWord:'';applyItem(item,true);}
    else if(!current&&library.length)await selectVideo(library[0].id);
    renderLibrary();await loadTrash();
    notice(result.pending?'文件正在使用，已安排永久清理；释放后自动重试，当前尚未删除完成。':action==='trash'?'已移入回收站，30 天内可以恢复。':action==='restore'?'已恢复。':'已永久删除。');
  }catch(e){notice('操作未完成：'+e.message,true);}finally{trashBusy=false;controls();}
}
$('openTrash').onclick=async()=>{$('trashPanel').hidden=false;$('openTrash').setAttribute('aria-expanded','true');try{await loadTrash();}catch(e){notice(e.message,true);}};
$('closeTrash').onclick=()=>{$('trashPanel').hidden=true;$('openTrash').setAttribute('aria-expanded','false');};

connect();setInterval(()=>connected?poll():connect(),3000);
