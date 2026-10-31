(function(root){
'use strict';
function timestamp(value){const ms=Math.round(Math.max(0,Number(value)||0)*1000);return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;}
function parseSubtitles(raw){const blocks=raw.replace(/^\uFEFF/,'').replace(/\r/g,'').trim().split(/\n\s*\n/),out=[];const seconds=s=>s.replace(',','.').split(':').map(Number).reduce((a,v)=>a*60+v,0);
for(const block of blocks){if(/^(WEBVTT|NOTE|STYLE|REGION)(?:\s|$)/.test(block))continue;const lines=block.split('\n'),idx=lines.findIndex(l=>l.includes('-->'));if(idx<0)continue;
const m=lines[idx].match(/^((?:\d{2,}:)?[0-5]\d:[0-5]\d[.,]\d{3})\s*-->\s*((?:\d{2,}:)?[0-5]\d:[0-5]\d[.,]\d{3})(?:\s|$)/);if(!m)continue;const start=seconds(m[1]),end=seconds(m[2]),text=lines.slice(idx+1).join(' ').replace(/<[^>]*>/g,'').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&nbsp;/g,' ').trim();if(Number.isFinite(start)&&Number.isFinite(end)&&end>start&&text)out.push({start,end,text});}return out.sort((a,b)=>a.start-b.start);}
const normalize=s=>s.normalize('NFKC').toLowerCase().replace(/[’‘]/g,"'").trim().replace(/\s+/g,' ');
function matches(text,query){const q=normalize(query);if(!q)return false;const escaped=q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&').replace(/ /g,'\\s+');return new RegExp('(^|[^a-z0-9])'+escaped+'(?=$|[^a-z0-9])','i').test(normalize(text));}
const core={timestamp,parseSubtitles,matches,normalize};if(typeof module==='object'&&module.exports)module.exports=core;else root.ListenCore=core;
})(globalThis);
