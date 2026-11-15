/* OpenList native UI adapter. No model/admin service credentials belong here. */
(() => {
  'use strict';
  if (window.__openlistAITags) return;
  window.__openlistAITags = true;
  const asset = new URL(document.currentScript?.src || '/ai/assets/ai.js', location.href);
  const base = asset.pathname.replace(/\/assets\/ai\.js$/, '');
  const css = document.createElement('link');
  css.rel = 'stylesheet'; css.href = `${base}/assets/ai.css`; document.head.append(css);
  const state = { admin: false, cache: new Map(), context: null, token: null, userName: null, error: '', refreshed: 0 };
  const statuses = { pending:'未识别', running:'识别中', done:'已识别', failed:'识别失败', cancelled:'已取消' };
  let videoRE = /\.(mp4|mkv|avi|mov|webm|m4v|ts|flv)$/i;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  function icon(name) {
    const paths={
      film:'M4 3h16v18H4z M4 8h16 M4 16h16 M8 3v18 M16 3v18',
      library:'M3 4h7v7H3z M14 4h7v7h-7z M3 15h7v6H3z M14 15h7v6h-7z',
      settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
      jobs:'M9 5h12 M9 12h12 M9 19h12 M2 5l2 2 3-4 M2 12l2 2 3-4 M2 19l2 2 3-4',
      tags:'M3 3h8l10 10-8 8L3 11z M7 7h.01',
      history:'M3 11a9 9 0 1 1 2 7 M3 4v7h7 M12 7v5l3 2',
      chevron:'m8 4 8 8-8 8',play:'m8 4 12 8-12 8z',
      list:'M9 5h12 M9 12h12 M9 19h12 M3 5h.01 M3 12h.01 M3 19h.01',
      file:'M5 3h9l5 5v13H5z M14 3v6h5',
      download:'M12 3v12 m-5-5 5 5 5-5 M4 15v6h16v-6',
      close:'m6 6 12 12 M6 18 18 6',expand:'M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5'
    };
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',
      'stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))svg.setAttribute(key,value);
    svg.classList.add('ai-icon');
    const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name] || paths.film);
    svg.append(path);return svg;
  }
  const fileName=path=>path.split('/').at(-1);
  const videoTitle=path=>fileName(path).replace(/\.[^.]+$/,'');
  function fileLink(path){return '/'+path.replace(/^\/+/,'').split('/').map(encodeURIComponent).join('/');}
  function clockTime(seconds){
    if(!Number.isFinite(Number(seconds)) || seconds<0)return '';
    const value=Math.floor(seconds),hours=Math.floor(value/3600);
    return (hours ? hours+':' : '')+String(Math.floor(value/60)%60).padStart(2,'0')+':'+String(value%60).padStart(2,'0');
  }
  function fileSize(bytes){
    if(bytes==null || !Number.isFinite(Number(bytes)))return '';
    const units=['B','KB','MB','GB','TB'];let value=Number(bytes),unit=0;
    while(value>=1024 && unit<units.length-1){value/=1024;unit++;}
    return (unit?value.toFixed(1):value)+' '+units[unit];
  }
  function button(text, fn, cls='') {
    const b = el('button', text, `ai-button ${cls}`); b.type='button';
    b.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); Promise.resolve().then(fn).catch(error => notify(error.message)); });
    return b;
  }
  async function api(path, options={}) {
    const r = await fetch(`${base}/api${path}`, { ...options, headers:{ 'Authorization':localStorage.getItem('token') || '', 'Content-Type':'application/json', ...options.headers } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(typeof body.detail === 'string' ? body.detail : `AI 服务请求失败 (${r.status})`);
    return body;
  }
  const post = (path,body={}) => api(path,{method:'POST',body:JSON.stringify(body)});
  function notify(message) {
    let n=document.querySelector('#ai-toast');
    if (!n) { n=el('div',null,'ai-toast'); n.id='ai-toast'; n.setAttribute('role','status'); document.body.append(n); }
    n.textContent=message; n.hidden=false; setTimeout(()=>n.hidden=true,6500);
  }
  function password() {
    const pair=document.cookie.split('; ').find(v=>v.startsWith('browser-password='));
    try { return pair ? decodeURIComponent(pair.slice(pair.indexOf('=')+1)) : ''; } catch { return ''; }
  }
  async function nativeCall(route,body){
    const response=await fetch('/api/'+route,{method:'POST',
      headers:{Authorization:localStorage.getItem('token') || '','Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok || data.code!==200)throw Error('文件暂时无法访问');
    return data.data;
  }
  async function setCover(image,path,signal){
    try{
      const response=await fetch(base+'/api/library/cover',{method:'POST',signal,
        headers:{Authorization:localStorage.getItem('token') || '','Content-Type':'application/json'},
        body:JSON.stringify({paths:[path],password:password()})});
      if(!response.ok)return;
      if(!response.headers.get('Content-Type')?.startsWith('image/'))return;
      const blob=await response.blob();
      if(!image.isConnected || signal?.aborted)return;
      const url=URL.createObjectURL(blob);
      image.addEventListener('load',()=>{image.hidden=false;URL.revokeObjectURL(url);},{once:true});
      image.addEventListener('error',()=>{URL.revokeObjectURL(url);image.hidden=true;},{once:true});
      image.src=url;
    }catch{}
  }
  async function downloadVideo(path,copy=false){
    const obj=await nativeCall('fs/get',{path,password:password()});
    if(!obj.raw_url)throw Error('暂时无法取得下载地址');
    const url=new URL(obj.raw_url,location.href);
    if(!['http:','https:'].includes(url.protocol))throw Error('下载地址协议不受支持');
    if(copy){await navigator.clipboard.writeText(url.href);notify('文件链接已复制');return;}
    const link=el('a');link.href=url.href;link.download=fileName(path);
    link.target='_blank';link.rel='noopener noreferrer';link.click();
  }
  async function directoryFiles(path){
    const parent=path.slice(0,path.lastIndexOf('/')) || '/';
    const data=await nativeCall('fs/list',{path:parent,password:password(),page:1,per_page:200,refresh:false});
    const files=(data.content || []).filter(obj=>!obj.is_dir && obj.name && !/[\/\\]/.test(obj.name))
      .map(obj=>({...obj,path:(parent==='/'?'':parent)+'/'+obj.name}));
    return {files,hasMore:data.total>200,parent};
  }
  function decoded(path) { try { return decodeURIComponent(path); } catch { return path; } }
  const currentPath = () => decoded(location.pathname);
  const managePath = () => /^\/@manage(?:\/|$)/.test(location.pathname);
  let initialCatalog=null;
  const initialParams=new URLSearchParams(location.search);
  if(managePath() && initialParams.get('ai')==='video-tags' && ['library','overview'].includes(initialParams.get('view') || 'library') &&
     localStorage.getItem('token')){
    // Prefetch one bounded batch; the mounted grid chooses how many fill its rows.
    const body=JSON.stringify({keywords:'',tags:[],category:'',year:'',sort:'newest',content:'all',password:password(),page:1,page_size:60});
    initialCatalog={token:localStorage.getItem('token'),url:location.href,body,
      result:api('/library',{method:'POST',body}).catch(()=>null)};
  }
  const manageTabs = {library:'识别资料',settings:'模型与扫描配置',jobs:'识别任务',tags:'标签规则',history:'历史与错误日志'};
  function manageLocation(tab='settings') {
    return `/@manage?${new URLSearchParams({ai:'video-tags',view:tab})}`;
  }
  function openManage(tab='settings') {
    state.manageExpanded=true;
    window.history.pushState({},'',manageLocation(tab));
    window.dispatchEvent(new PopStateEvent('popstate'));
  }
  function manageNavigation() {
    const nav=el('nav',null,'ai-manage-nav');
    nav.setAttribute('aria-label','AI 插件管理分区');
    Object.entries(manageTabs).forEach(([tab,label])=>{
      const link=el('a',null,'ai-manage-sub-link');
      link.append(icon(tab),el('span',label));
      link.href=manageLocation(tab);
      link.dataset.view=tab;
      link.addEventListener('click',event=>{
        if(event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button!==0)return;
        event.preventDefault();
        event.stopPropagation();
        link.closest('.hope-drawer__content')?.querySelector('.hope-modal__close-button')?.click();
        openManage(tab);
      });
      nav.append(link);
    });
    return nav;
  }
  function clearManagePanel() {
    document.querySelector('#ai-manage-view')?.aiDispose?.();
    document.querySelector('#ai-manage-panel')?.remove();
    document.querySelectorAll('.ai-manage-host').forEach(n=>n.classList.remove('ai-manage-host'));
  }
  function restoreManageNavigation() {
    if(typeof state.manageTitle==='string' && document.title==='AI 视频标签 | OpenList 管理')document.title=state.manageTitle;
    state.manageTitle=undefined;
    document.querySelectorAll('.ai-manage-native-active').forEach(n=>n.classList.remove('ai-manage-native-active'));
    document.querySelectorAll('[data-ai-current]').forEach(a=>{
      a.removeAttribute('data-ai-current');
      if(new URL(a.href,location.href).pathname===location.pathname)a.setAttribute('aria-current','page');
    });
  }
  async function ensureManage() {
    if(!managePath() || !state.admin) {
      clearManagePanel();
      restoreManageNavigation();
      document.querySelectorAll('.ai-manage-menu-row,.ai-manage-nav,#ai-manage-shortcut').forEach(n=>n.remove());
      return;
    }
    const query=new URLSearchParams(location.search);
    const active=query.get('ai')==='video-tags';
    const tab=Object.hasOwn(manageTabs,query.get('view')) ? query.get('view') : 'settings';
    if(active && query.get('view')==='overview'){
      const previous=location.href;
      window.history.replaceState({},'',manageLocation());
      if(initialCatalog?.url===previous)initialCatalog.url=location.href;
    }
    const profiles=[...document.querySelectorAll('a[href="/@manage"]')];
    for(const profile of profiles) {
      const menu=profile.parentElement;
      if(menu.querySelectorAll('a[href^="/@manage"]').length<2)continue;
      menu.classList.toggle('ai-manage-native-active',active);
      if(active)menu.querySelectorAll('a[aria-current="page"]:not(.ai-manage-link):not(.ai-manage-sub-link)').forEach(a=>{
        a.setAttribute('data-ai-current','page');a.removeAttribute('aria-current');
      });
      let link=menu.querySelector('.ai-manage-link');
      if(!link) {
        const template=menu.querySelector('a[href="/@manage/about"]') || profile;
        link=el('a',null,`${template.className} ai-manage-link`);
        link.href=manageLocation();
        link.append(icon('film'),el('span','AI 视频标签'));
        link.addEventListener('click',e=>{
          if(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button!==0)return;
          e.preventDefault();e.stopPropagation();
          link.closest('.hope-drawer__content')?.querySelector('.hope-modal__close-button')?.click();
          openManage();
        });
        const row=el('div',null,'ai-manage-menu-row');
        const toggle=button('',()=>{
          if(new URLSearchParams(location.search).get('ai')!=='video-tags'){openManage();return;}
          state.manageExpanded=state.manageExpanded===false;
          schedule();
        },'ai-manage-chevron');
        toggle.setAttribute('aria-label','展开或收起 AI 视频标签菜单');toggle.append(icon('chevron'));
        row.append(link,toggle);menu.append(row);
      }
      let nav=menu.querySelector('.ai-manage-nav');
      if(!nav){nav=manageNavigation();link.closest('.ai-manage-menu-row').after(nav);}
      nav.hidden=!active || state.manageExpanded===false;
      link.closest('.ai-manage-menu-row').querySelector('.ai-manage-chevron').setAttribute('aria-expanded',String(!nav.hidden));
      nav.querySelectorAll('a').forEach(item=>{
        const current=active && item.dataset.view===tab;
        if(current && item.getAttribute('aria-current')!=='page')item.setAttribute('aria-current','page');
        if(!current)item.removeAttribute('aria-current');
      });
    }
    const header=[...document.querySelectorAll('header')].find(h=>h.nextElementSibling?.children.length===2);
    if(header && !document.querySelector('#ai-manage-shortcut')) {
      const shortcut=button('AI 视频标签',()=>openManage(),'ai-manage-shortcut');shortcut.id='ai-manage-shortcut';
      (header.firstElementChild?.lastElementChild || header).prepend(shortcut);
    }
    if(!active) {clearManagePanel();restoreManageNavigation();return;}
    if(state.manageTitle===undefined)state.manageTitle=document.title;
    if(document.title!=='AI 视频标签 | OpenList 管理')document.title='AI 视频标签 | OpenList 管理';
    const host=document.querySelector('[data-openlist-manage-content]') || header?.nextElementSibling?.lastElementChild;
    if(!host)return;
    const old=document.querySelector('#ai-manage-panel');
    if(old?.dataset.view===tab && old.aiSession===state.token)return;
    clearManagePanel();host.classList.add('ai-manage-host');
    const panel=el('section',null,'ai-manage-page');panel.id='ai-manage-panel';panel.dataset.view=tab;
    // Compare session changes without placing a token in DOM attributes.
    panel.aiSession=state.token;
    const heading=el('h1',manageTabs[tab]);
    const headingRow=el('div',null,'ai-manage-heading');headingRow.append(heading);
    const mobileMenu=el('details',null,'ai-manage-mobile-nav');
    const mobileNav=manageNavigation();
    mobileNav.querySelector(`[data-view="${tab}"]`)?.setAttribute('aria-current','page');
    mobileMenu.append(el('summary','插件页面'),mobileNav);
    const view=el('div',null,'ai-dialog-content');view.id='ai-manage-view';
    view.append(el('p','正在加载插件管理选项…','ai-muted'));
    panel.append(mobileMenu,headingRow,view);host.append(panel);
    try {
      view.replaceChildren();
      await ({library,settings,jobs,tags:tagManager,history})[tab](view);
    } catch(e) {
      if(view.isConnected)view.replaceChildren(el('p',e.message,'ai-error'),button('重新加载',()=>{clearManagePanel();return ensureManage();}));
    }
  }
  function rowPath(row) {
    const a=row.matches('a[href]') ? row : row.querySelector('a[href]');
    return a ? decoded(new URL(a.getAttribute('href'),location.href).pathname) : null;
  }
  function modal(title) {
    document.querySelector('#ai-dialog')?.remove();
    const d=el('dialog',null,'ai-dialog'); d.id='ai-dialog';
    const head=el('div',null,'ai-dialog-head'); head.append(el('h2',title),button('关闭',()=>d.close()));
    const content=el('div',null,'ai-dialog-content'); d.append(head,content); document.body.append(d);
    d.addEventListener('close',()=>d.remove());
    d.addEventListener('click',e=>{if(e.target===d && (e.clientX<d.getBoundingClientRect().left || e.clientX>d.getBoundingClientRect().right)) d.close();});
    d.showModal(); return { d,content };
  }
  async function refreshAuth() {
    const token=localStorage.getItem('token') || '';
    if(state.token!==token) {
      if(state.token!==null){initialCatalog=null;clearNativeResults();sessionStorage.removeItem('ai-native-search-pending');}
      closeHoverCard();if(state.token!==null){directoryListing=null;document.querySelector('#ai-image-videos')?.remove();}state.cache.clear();state.refreshed=0;state.token=token;document.querySelectorAll('.ai-badges').forEach(n=>n.remove());document.querySelector('#ai-toolbar')?.remove();
      state.authChecked=0;state.admin=false;state.userName=null;
    }
    if(Date.now()-(state.authChecked || 0)<30000)return;
    try {
      const me=await api('/me');
      if(token!==(localStorage.getItem('token') || '')){state.admin=false;schedule();return;}
      state.admin=me.admin;
      state.userName=me.username || '';
      const extensions=me.extensions?.filter(x=>/^[a-z0-9]+$/i.test(x));
      if(extensions?.length)videoRE=new RegExp(`\\.(${extensions.join('|')})$`,'i');
      state.error='';
      state.authChecked=Date.now();
    }
    catch(e) { state.admin=false;state.userName=null;closeHoverCard();clearNativeResults();state.error=e.message; }
  }
  async function recognize(path,contentType='auto',useSubtitles=false) {
    await post('/recognize',{paths:[path],content_type:contentType,use_subtitles:useSubtitles}); state.refreshed=0; notify('已加入真实视频识别队列'); await jobs();
  }
  async function details(path) {
    const {d,content:root}=modal('视频详情');d.classList.add('ai-movie-dialog');
    const content=el('div',null,'ai-movie-analysis');
    const items=await post('/videos/batch',{paths:[path],password:password()});
    const item=items[0];
    if(!root.isConnected)return;
    const hero=el('section',null,'ai-movie-hero'),poster=el('div',null,'ai-movie-poster');
    poster.append(icon('film'));
    const image=el('img');image.alt='视频封面';image.hidden=true;poster.append(image);
    const info=el('div',null,'ai-movie-info');
    const metadata=[statuses[item?.status] || '未识别',item?.file?.size!=null?fileSize(item.file.size):'',
      Number(item?.result?.sampling?.duration)>0?clockTime(item.result.sampling.duration):'',fileName(path).split('.').at(-1)?.toUpperCase()].filter(Boolean);
    info.append(el('h2',videoTitle(path)),el('p',metadata.join(' · '),'ai-muted'),
      el('p',item?.result?.summary || '尚无视频内容简介。','ai-movie-intro'));
    const actions=el('div',null,'ai-movie-actions'),play=el('a',null,'ai-button ai-library-primary');
    play.href=fileLink(path);play.target='_blank';play.rel='noopener noreferrer';play.append(icon('play'),el('span','播放视频'));
    actions.append(play,button('下载',()=>downloadVideo(path)),button('复制链接',()=>downloadVideo(path,true)));
    info.append(actions);hero.append(poster,info);root.append(hero);
    if(state.admin)setCover(image,path);
    else if(item?.file?.thumb){image.src=item.file.thumb;image.hidden=false;}
    const navigation=el('nav',null,'ai-movie-tabs');
    navigation.setAttribute('aria-label','视频详情分区');
    const files=el('section',null,'ai-movie-files');files.hidden=true;
    let loadedFiles;
    async function showFiles(subtitlesOnly){
      files.replaceChildren(el('p','正在读取同目录文件…','ai-muted'));
      try{
        loadedFiles=loadedFiles || await directoryFiles(path);
        if(!files.isConnected)return;
        const list=loadedFiles.files.filter(obj=>subtitlesOnly?/\.(srt|vtt|ass|ssa)$/i.test(obj.name):videoRE.test(obj.name));
        files.replaceChildren(el('h3',subtitlesOnly?'同目录字幕':'同目录视频'));
        for(const obj of list){
          const row=el('div',null,'ai-file-entry'),link=el('a',obj.name);
          link.href=fileLink(obj.path);link.target='_blank';link.rel='noopener noreferrer';
          row.append(icon(subtitlesOnly?'file':'film'),link,el('span',fileSize(obj.size),'ai-muted'),
            button('下载',()=>downloadVideo(obj.path)));
          files.append(row);
        }
        if(!list.length)files.append(el('p',subtitlesOnly?'同目录暂无可用字幕':'当前目录暂无其他视频','ai-muted'));
        if(loadedFiles.hasMore){
          const open=el('a','在原目录查看全部文件');open.href=fileLink(loadedFiles.parent);files.append(open);
        }
      }catch(error){files.replaceChildren(el('p',error.message,'ai-error'));}
    }
    for(const [label,mode] of [['影片信息','info'],['视频文件','video'],['字幕文件','subtitles']]){
      const tab=button(label,()=>{
        content.hidden=mode!=='info';files.hidden=mode==='info';
        for(const control of navigation.children)control.setAttribute('aria-pressed',String(control===tab));
        if(mode!=='info')return showFiles(mode==='subtitles');
      });
      tab.setAttribute('aria-pressed',String(mode==='info'));navigation.append(tab);
    }
    root.append(navigation,content,files);
    root.append(el('p',path,'ai-movie-path'));
    if(state.admin) content.append(button('按电影剧情识别',()=>recognize(path,'movie')));
    if(state.admin) content.append(button('字幕与电影联合识别',()=>recognize(path,'movie',true)));
    if (!item) {
      content.append(el('p','此视频尚未识别，或当前账户无权读取结果。'));
      if(state.admin) content.append(button('AI 识别',()=>recognize(path)));
      return;
    }
    if(item.status!=='done' && item.result.summary) content.append(el('p','以下为上一次识别结果，当前任务尚未完成。','ai-muted'));
    const tags=el('div',null,'ai-tags');
    item.ai_tags.forEach(t=>tags.append(button(t,()=>filterTag(t),'ai-tag')));
    item.manual_tags.forEach(t=>tags.append(button(`${t} · 人工`,()=>filterTag(t),'ai-tag ai-manual'))); info.append(tags);
    if(item.error) content.append(el('p',item.error,'ai-error'));
    if(item.result.visible_text?.length) content.append(el('h3','画面文字'),el('p',item.result.visible_text.join('；')));
    if(item.result.characters?.length) {
      content.append(el('h3','主要人物与关系'));
      item.result.characters.forEach(c=>{
        content.append(el('p',`${c.name}：${c.role}${c.relationships?.length ? '；'+c.relationships.join('；') : ''}`));
        if(c.evidence) content.append(el('p',c.evidence,'ai-muted'));
      });
    }
    if(item.result.segments?.length) {
      content.append(el('h3',item.result.verification ? '画面与对白引用时间' : '时间段内容'));
      item.result.segments.forEach(s=>content.append(el('p',`${s.start}–${s.end} 秒：${s.description}`)));
    }
    if(item.result.events?.length) {
      content.append(el('h3','关键事件顺序'));
      item.result.events.forEach((s,i)=>content.append(el('p',`${i+1}. ${s}`)));
    }
    if(item.result.uncertainty) content.append(el('p',item.result.uncertainty,'ai-muted'));
    if(item.result.sampling) {
      const s=item.result.sampling;
      const input=s.method==='compressed-video-v1'
        ? `整段压缩视频 ${(s.video_bytes/1048576).toFixed(2)} MiB`
        : `抽样 ${s.timestamps.length} 帧`;
      const fallback=s.fallback_reason ? ` · ${s.fallback_reason}，已使用抽帧` : '';
      content.append(el('p',`模型：${s.model} · ${input} · 读取 ${(s.network_bytes/1048576).toFixed(1)} MiB${fallback}`,'ai-muted'));
      if(s.context_timestamps?.length) content.append(el('p',`补充清晰画面 ${s.context_timestamps.length} 张，用于核对字幕和人物。`,'ai-muted'));
      if(s.thinking) content.append(el('p','已启用模型推理模式。','ai-muted'));
      if(s.evidence_cache_hit) content.append(el('p','本次复用了仍与原文件信息匹配的压缩视频证据。','ai-muted'));
      if(s.subtitles) {
        content.append(el('p',`字幕证据 ${s.subtitles.count} 条 · ${s.subtitles.source==='burned-in-ocr'?'画面 OCR，可能漏字或错字':'文本字幕轨'}`,'ai-muted'));
        content.append(button('下载提取的字幕',async()=>{
          const data=await post('/subtitles',{paths:[path],password:password()});
          const url=URL.createObjectURL(new Blob([data.srt],{type:'application/x-subrip;charset=utf-8'}));
          const link=el('a'); link.href=url; link.download='video-subtitles.srt'; link.click();
          setTimeout(()=>URL.revokeObjectURL(url),1000);
        }));
      }
    }
    if(state.admin) {
      content.append(el('hr'),el('h3','人工标签（重新识别后保留）'));
      const input=el('input'); input.value=item.manual_tags.join('，'); input.placeholder='使用逗号分隔'; input.setAttribute('aria-label','人工标签'); content.append(input);
      content.append(el('p','取消勾选可隐藏 AI 标签；重新识别后仍然保留此选择。','ai-muted'));
      const checks=[];
      (item.result.tags || []).forEach(tag=>{ const label=el('label',null,'ai-check'); const c=el('input'); c.type='checkbox'; c.checked=!item.removed.includes(tag); label.append(c,document.createTextNode(tag)); content.append(label); checks.push([tag,c]); });
      content.append(button('保存标签',async()=>{
        await post('/videos/edit',{path,manual_tags:input.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),removed:checks.filter(x=>!x[1].checked).map(x=>x[0])});
        state.refreshed=0; notify('标签已保存'); await details(path);
      }),button('重新识别',()=>recognize(path)));
    }
  }
  let nativeSearch=null;
  let pendingSearch=false;
  function clearNativeResults() {
    if(!nativeSearch)return;
    nativeSearch.controller?.abort();nativeSearch.seq++;nativeSearch.items=[];
    nativeSearch.body=null;nativeSearch.cursor=null;nativeSearch.signature='';nativeSearch.message='标签和视频简介也会参与这次搜索。';
    nativeSearch.section.replaceChildren();
    nativeSearch.dialog.querySelectorAll('.ai-native-note').forEach(n=>n.remove());
    nativeSearch.dialog.querySelectorAll('[data-ai-native-empty]').forEach(n=>{n.hidden=false;n.removeAttribute('data-ai-native-empty');});
  }
  function renderNativeResults() {
    const n=nativeSearch;
    if(!n || !n.input.isConnected)return;
    const root=n.input.parentElement.parentElement;
    const list=[...root.children].find(x=>x!==n.input.parentElement && x.classList.contains('hope-stack') && x.id!=='ai-native-search-results');
    if(!list)return;
    const existing=new Map();
    list.querySelectorAll('a[href]').forEach(a=>{
      if(a.closest('#ai-native-search-results'))return;
      try{existing.set(decoded(new URL(a.href,location.href).pathname),a);}catch{}
    });
    if(n.items.length && !existing.size) {
      [...root.children].filter(x=>x.tagName==='P').forEach(x=>{x.hidden=true;x.setAttribute('data-ai-native-empty','true');});
    } else root.querySelectorAll('[data-ai-native-empty]').forEach(x=>{x.hidden=false;x.removeAttribute('data-ai-native-empty');});
    const signature=JSON.stringify([n.message,n.items,n.cursor,[...existing.keys()]]);
    const needsNote=n.items.some(x=>existing.has(x.path) && !existing.get(x.path).querySelector('.ai-native-note'));
    if(n.signature===signature && n.section.isConnected && !needsNote)return;
    n.signature=signature;
    n.dialog.querySelectorAll('.ai-native-note').forEach(x=>x.remove());
    n.section.replaceChildren(el('h3','视频标签与简介匹配'));
    if(n.message)n.section.append(el('p',n.message,'ai-muted'));
    n.items.forEach(item=>{
      if(existing.has(item.path)) {
        const a=existing.get(item.path);const note=el('div',null,'ai-native-note');
        note.append(el('p',item.tags.join(' · '),'ai-muted'),el('p',(item.result.summary || '').slice(0,300)));
        (a.querySelector('.hope-stack') || a).append(note);return;
      }
      const card=el('article',null,'ai-native-result');
      const a=el('a',item.path.split('/').at(-1));a.href=item.path.split('/').map(encodeURIComponent).join('/')+'?from=search';
      card.append(a,el('p',item.path,'ai-muted'),el('p',(item.result.summary || '').slice(0,300)));
      const tags=el('div',null,'ai-tags');item.tags.forEach(t=>tags.append(button(t,()=>filterTag(t),'ai-tag')));
      card.append(tags,button('查看 AI 结果',()=>details(item.path)));n.section.append(card);
    });
    if(n.cursor!==null)n.section.append(button('继续查找标签与简介匹配',()=>submitNativeSearch(n.body,true)));
    if(n.items.length && !existing.size) {
      [...root.children].filter(x=>x.tagName==='P').forEach(x=>{x.hidden=true;x.setAttribute('data-ai-native-empty','true');});
    } else {
      root.querySelectorAll('[data-ai-native-empty]').forEach(x=>{x.hidden=false;x.removeAttribute('data-ai-native-empty');});
    }
    list.append(n.section);
  }
  function ensureNativeSearch() {
    const input=document.querySelector('#search-input');const dialog=input?.closest('[role="dialog"]');
    if(!input || !dialog || !input.getClientRects().length)return;
    if(nativeSearch?.input!==input) {
      clearNativeResults();
      const section=el('section',null,'ai-native-search');section.id='ai-native-search-results';section.setAttribute('aria-live','polite');
      nativeSearch={input,dialog,section,seq:0,items:[],cursor:null,body:null,message:'同时查找当前目录下的视频标签和简介；#动画 #亲情 表示组合标签。'};
      input.placeholder='搜索文件名、AI 标签或视频简介';
      input.addEventListener('input',()=>{clearNativeResults();renderNativeResults();});
    }
    renderNativeResults();
  }
  async function submitNativeSearch(body,more=false) {
    ensureNativeSearch();const n=nativeSearch;
    if(!n || !n.input.isConnected || n.input.value!==body.keywords)return;
    const key=JSON.stringify([body.parent,body.keywords,body.scope,body.password]);
    if(!more && n.key===key && n.body && body.page>1)return;
    n.controller?.abort();n.controller=new AbortController();const seq=++n.seq;
    const token=localStorage.getItem('token') || '';
    if(!more){n.items=[];n.cursor=null;}
    n.body=body;n.key=key;n.message='正在匹配视频标签与简介…';renderNativeResults();
    try {
      const data=await api('/native-search',{method:'POST',body:JSON.stringify({parent:body.parent,keywords:body.keywords,password:body.password,scope:body.scope,cursor:more?n.cursor || 0:0,limit:30}),signal:n.controller.signal});
      if(nativeSearch!==n || seq!==n.seq || !n.input.isConnected || token!==(localStorage.getItem('token') || ''))return;
      const items=new Map(n.items.map(x=>[x.path,x]));data.items.forEach(x=>items.set(x.path,x));n.items=[...items.values()];n.cursor=data.next_cursor;
      n.message=body.scope===1?'文件夹范围不检索视频内容。':n.items.length?`已找到 ${n.items.length} 个可访问视频。`:n.cursor!==null?'当前批次没有匹配，可继续查找。':'未找到标签或简介匹配的视频。';
    } catch(e) {
      if(e.name==='AbortError' || nativeSearch!==n || seq!==n.seq)return;
      n.cursor=null;n.message='标签与简介检索暂不可用，OpenList 文件名搜索仍可使用。';
    }
    if(nativeSearch===n && seq===n.seq)renderNativeResults();
  }
  function observeSearch(method,url,body) {
    try {
      const u=new URL(url,location.href);
      if(method.toUpperCase()!=='POST' || u.origin!==location.origin || u.pathname!=='/api/fs/search' || typeof body!=='string' || body.length>65536)return;
      const q=JSON.parse(body);
      if(typeof q.parent==='string' && typeof q.keywords==='string')submitNativeSearch({parent:q.parent,keywords:q.keywords,password:q.password || '',scope:q.scope || 0,page:Number(q.page) || 1}).catch(()=>{});
    } catch{}
  }
  // Observe the native request; never alter, delay, cancel, or replace its response.
  const xhrRequests=new WeakMap();
  let directoryListing=null;
  function rememberListing(url,body,response){
    try{if(new URL(url,location.href).pathname==='/api/fs/list' && response.code===200){const q=JSON.parse(body);directoryListing={path:q.path,items:response.data.content || []};document.querySelector('#ai-image-videos')?.remove();schedule();}}catch{}
  }
  const originalOpen=XMLHttpRequest.prototype.open,originalSend=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...args){xhrRequests.set(this,{method,url});return originalOpen.call(this,method,url,...args);};
  XMLHttpRequest.prototype.send=function(body){const q=xhrRequests.get(this);if(q && new URL(q.url,location.href).pathname==='/api/fs/list')this.addEventListener('load',()=>{try{rememberListing(q.url,body,this.responseType==='json'?this.response:JSON.parse(this.responseText));}catch{}},{once:true});const value=originalSend.call(this,body);if(q)observeSearch(q.method,q.url,body);return value;};
  const originalFetch=window.fetch;
  window.fetch=function(input,options){
    const response=originalFetch.apply(this,arguments);
    if(typeof input==='string' || input instanceof URL){observeSearch(options?.method || 'GET',String(input),options?.body);if(new URL(String(input),location.href).pathname==='/api/fs/list')response.then(r=>r.clone().json()).then(data=>rememberListing(String(input),options?.body,data)).catch(()=>{});}
    else if(input instanceof Request && new URL(input.url).pathname==='/api/fs/search')input.clone().text().then(body=>observeSearch(options?.method || input.method,input.url,options?.body || body)).catch(()=>{});
    return response;
  };
  function ensureImageVideoCards(){
    const host=document.querySelector('.image-folders')?.parentElement || document.querySelector('.image-images')?.parentElement;
    if(!host){document.querySelector('#ai-image-videos')?.remove();return;}
    if(document.querySelector('#ai-image-videos') || directoryListing?.path!==currentPath())return;
    const videos=directoryListing.items.filter(x=>!x.is_dir && videoRE.test(x.name));if(!videos.length)return;
    const section=el('section',null,'ai-image-videos');section.id='ai-image-videos';section.setAttribute('aria-label','视频');
    videos.forEach(item=>{
      const a=el('a',null,'grid-item viselect-item ai-image-video');a.href=(currentPath().replace(/\/$/,'')+'/'+item.name).split('/').map(encodeURIComponent).join('/');
      const thumb=el('div',null,'ai-video-thumbnail');
      if(item.thumb){const img=el('img');img.src=item.thumb;img.loading='lazy';img.alt='';thumb.append(img);}else thumb.append(el('span','▶'));
      a.append(thumb,el('span',item.name,'name'));section.append(a);
    });host.append(section);
  }
  async function openNativeSearch(query,redirect=true) {
    document.querySelector('#ai-dialog')?.close();
    let input=document.querySelector('#search-input');
    if(!input?.getClientRects().length)document.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,bubbles:true}));
    for(let i=0;i<20;i++) {
      input=document.querySelector('#search-input');if(input?.getClientRects().length)break;
      await new Promise(resolve=>setTimeout(resolve,80));
    }
    if(!input?.getClientRects().length) {
      if(redirect && videoRE.test(currentPath())) {
        const parent=currentPath().slice(0,currentPath().lastIndexOf('/')) || '/';
        sessionStorage.setItem('ai-native-search-pending',JSON.stringify({parent,query,user:state.userName,expires:Date.now()+120000}));
        location.assign(parent.split('/').map(encodeURIComponent).join('/') || '/');
      } else if(redirect)notify('请打开 OpenList 原生搜索（Ctrl+K）后输入标签或简介关键词。');
      return false;
    }
    // A tag targets videos even if the previous native search selected folders.
    const controls=input.parentElement,select=controls.querySelector('select');
    if(select){select.value='2';select.dispatchEvent(new Event('change',{bubbles:true}));}
    else {
      const combo=controls.querySelector('[role="combobox"]');
      if(combo){
        combo.click();
        for(let i=0;i<10;i++){
          const options=document.getElementById(combo.getAttribute('aria-controls'))?.querySelectorAll('[role="option"]');
          if(options?.length===3){options[2].click();break;}
          await new Promise(resolve=>setTimeout(resolve,30));
        }
        if(combo.getAttribute('aria-expanded')==='true')combo.click();
      }
    }
    input.value=query;input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();
    input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return true;
  }
  function filterTag(t) {return openNativeSearch('#'+(t.includes(' ')?JSON.stringify(t):t));}
  async function resumeNativeSearch() {
    if(pendingSearch)return;
    try {
      const pending=JSON.parse(sessionStorage.getItem('ai-native-search-pending') || 'null');
      if(!pending)return;
      if(pending.user!==state.userName){sessionStorage.removeItem('ai-native-search-pending');return;}
      if(pending.expires<Date.now()){sessionStorage.removeItem('ai-native-search-pending');return;}
      if(pending.parent!==currentPath())return;
      pendingSearch=true;
      if(await openNativeSearch(pending.query,false))sessionStorage.removeItem('ai-native-search-pending');
    }catch{}finally{pendingSearch=false;}
  }
  function ensureToolbar() {
    const body=document.querySelector('.body');
    if(!body || location.pathname.startsWith('/@') || location.pathname.startsWith('/login'))return;
    if(!videoRE.test(currentPath())){document.querySelector('#ai-toolbar')?.remove();return;}
    if(document.querySelector('#ai-toolbar'))return;
    const bar=el('section',null,'ai-toolbar');bar.id='ai-toolbar';bar.setAttribute('aria-label','AI 视频标签');
    const tools=el('div',null,'ai-tools');
    if(state.error)tools.append(el('span',state.error,'ai-error'));
    bar.append(tools);body.prepend(bar);
    bar.append(button('查看此视频 AI 结果',()=>details(currentPath())));
  }
  async function jobs(target) {
    const {content,d}=target ? {content:target,d:target} : modal('AI 扫描任务');
    const render=async()=>{
      if(!d.isConnected) return;
      const data=await api('/jobs'); if(!d.isConnected) return;
      content.replaceChildren(button('扫描全部已配置目录',async()=>{const s=await api('/settings');await post('/scan',{paths:s.settings.roots});await render();}),el('p','暂停后停止领取新任务；正在解码或推理的任务可能继续完成。取消后结果不会提交。','ai-muted'));
      data.forEach(job=>{
        const card=el('article',null,'ai-job'); const t=job.tasks;
        card.append(el('strong',`#${job.id} ${job.root}`),el('p',`${job.state} · 总数 ${Object.values(t).reduce((a,b)=>a+b,0)} · 完成 ${t.done || 0} · 失败 ${t.failed || 0} · 等待 ${t.queued || 0} · 识别中 ${t.running || 0}`));
        const progress=el('progress'); progress.max=Math.max(1,Object.values(t).reduce((a,b)=>a+b,0)); progress.value=(t.done||0)+(t.failed||0)+(t.cancelled||0); card.append(progress);
        if(job.error) card.append(el('p',job.error,'ai-error'));
        const actions=job.state==='running' ? [['pause','暂停'],['cancel','取消']] : job.state==='paused' ? [['resume','恢复'],['cancel','取消']] : ['failed','done'].includes(job.state) ? [['retry','重试失败任务']] : [];
        actions.forEach(([a,label])=>card.append(button(label,async()=>{await post(`/jobs/${job.id}/${a}`);await render();}))); content.append(card);
      });
      if(!data.length)content.append(el('p','尚无扫描任务。'));
    };
    await render(); const timer=setInterval(()=>{
      if(!d.isConnected){clearInterval(timer);return;}
      render().catch(()=>{});
    },3000); d.addEventListener('close',()=>clearInterval(timer),{once:true});
  }
  async function library(target) {
    target.classList.add('ai-library-page');
    const form=el('form',null,'ai-library-filters');
    const query=el('input');
    query.type='search';query.maxLength=200;
    query.placeholder='搜索文件名、标签或简介';
    query.setAttribute('aria-label','视频库搜索');
    const category=el('select'),year=el('select'),sort=el('select');
    category.setAttribute('aria-label','视频类型');year.setAttribute('aria-label','视频年份');sort.setAttribute('aria-label','排序方式');
    category.title='按已有标签及电影识别类型归类';year.title='文件名中明确标注的年份';
    const allTypes=el('option','全部类型');allTypes.value='';category.append(allTypes);
    const allYears=el('option','全部年份');allYears.value='';year.append(allYears);
    for(const [value,label] of [['newest','最新收录'],['oldest','最早收录'],['name','名称排序']]){
      const option=el('option',label);option.value=value;sort.append(option);
    }
    form.append(query,category,year,sort,button('搜索',()=>load(true),'ai-library-primary'));
    form.addEventListener('submit',event=>{event.preventDefault();load(true).catch(error=>notify(error.message));});
    for(const control of [category,year,sort])control.addEventListener('change',()=>load(true).catch(error=>notify(error.message)));
    const selected=new Set();
    const filters=el('div',null,'ai-library-tags');
    const grid=el('div',null,'ai-library-grid');
    const hint=el('p',null,'ai-muted');hint.setAttribute('role','status');
    const pagination=el('nav',null,'ai-library-pagination');pagination.setAttribute('aria-label','视频库分页');
    let currentPage=1,totalPages=0,pageSize=24;
    let asOf=0;
    const tools=el('div',null,'ai-library-tools');
    let content='all';
    const contentMenu=el('div',null,'ai-library-content-menu');
    const contentSwitch=el('div',null,'ai-library-content-switch');contentSwitch.hidden=true;
    contentSwitch.id='ai-library-content-options';
    contentSwitch.setAttribute('role','group');contentSwitch.setAttribute('aria-label','内容分区');
    contentSwitch.title='依据已有成人、NSFW、色情或裸露标签分类；未标记内容归入非成人';
    const contentTrigger=button('',()=>{
      contentSwitch.hidden=!contentSwitch.hidden;
      contentTrigger.setAttribute('aria-expanded',String(!contentSwitch.hidden));
      if(!contentSwitch.hidden)contentSwitch.querySelector('[aria-pressed=true]')?.focus();
    },'ai-button ai-library-content-trigger');
    contentTrigger.append(icon('settings'));contentTrigger.title='内容分区：全部';
    contentTrigger.setAttribute('aria-label','切换内容分区');contentTrigger.setAttribute('aria-expanded','false');
    contentTrigger.setAttribute('aria-controls',contentSwitch.id);
    function closeContentMenu(){contentSwitch.hidden=true;contentTrigger.setAttribute('aria-expanded','false');}
    const outsideContent=event=>{if(!contentMenu.contains(event.target))closeContentMenu();};
    const contentKey=event=>{
      if(event.key==='Escape' && !contentSwitch.hidden){closeContentMenu();contentTrigger.focus();}
    };
    document.addEventListener('pointerdown',outsideContent,true);
    contentMenu.addEventListener('keydown',contentKey);
    for(const [value,label] of [['all','全部'],['general','非成人'],['adult','成人']]){
      const control=button(label,()=>{
        content=value;
        for(const option of contentSwitch.children)option.setAttribute('aria-pressed',String(option===control));
        contentTrigger.title='内容分区：'+label;
        contentTrigger.classList.toggle('ai-is-filtered',value!=='all');
        closeContentMenu();contentTrigger.focus();
        return load(true);
      });
      control.setAttribute('aria-pressed',String(value===content));contentSwitch.append(control);
    }
    contentMenu.append(contentTrigger,contentSwitch);
    (target.closest('.ai-manage-page')?.querySelector('.ai-manage-heading') || target).append(contentMenu);
    const views=el('div',null,'ai-library-views');
    views.setAttribute('aria-label','视频库视图');
    const layout=el('div',null,'ai-library-layout'),side=el('aside',null,'ai-library-side');side.hidden=true;
    let selectedPath=null;
    const viewButtons=[];
    let view=localStorage.getItem('ai-library-view')==='list'?'list':'poster';
    function setView(value){
      view=value;localStorage.setItem('ai-library-view',value);grid.dataset.view=value;
      side.hidden=true;selectedPath=null;layout.classList.remove('ai-has-selection');
      for(const control of viewButtons)control.setAttribute('aria-pressed',String(control.dataset.view===value));
    }
    for(const [value,label,glyph] of [['poster','海报','library'],['list','列表','list']]){
      const control=button('',()=>{setView(value);schedulePageLayout();});
      control.dataset.view=value;control.append(icon(glyph),el('span',label));
      viewButtons.push(control);views.append(control);
    }
    setView(view);tools.append(views);layout.append(grid,side);
    target.append(form,filters,tools,layout,hint,pagination);

    let criteria;
    let request;
    let pageLayoutTimer;
    function gridPageSize(){
      if(view==='list')return 24;
      const tracks=getComputedStyle(grid).gridTemplateColumns.trim().split(/\s+/);
      const columns=tracks[0]==='none'?1:Math.min(60,tracks.length);
      return Math.min(60,columns*Math.ceil(24/columns));
    }
    function schedulePageLayout(){
      clearTimeout(pageLayoutTimer);
      pageLayoutTimer=setTimeout(()=>{
        if(target.isConnected && criteria && gridPageSize()!==pageSize)load(false,1);
      },150);
    }
    const pageLayoutObserver=new ResizeObserver(schedulePageLayout);
    pageLayoutObserver.observe(grid);
    const facetsRequest=new AbortController();
    let checkTimer;
    let checkRequest;
    const displayed=new Map();
    let preview=null,previewAnchor=null,previewTimer,previewCloseTimer;
    function closePreview(){
      clearTimeout(previewTimer);clearTimeout(previewCloseTimer);
      preview?.remove();preview=null;previewAnchor=null;
    }
    function positionPreview(){
      if(!preview || !previewAnchor?.isConnected){closePreview();return;}
      const card=previewAnchor.getBoundingClientRect(),box=preview.getBoundingClientRect();
      let left=card.right+12;
      if(left+box.width>innerWidth-12)left=card.left-box.width-12;
      preview.style.left=Math.max(12,Math.min(left,innerWidth-box.width-12))+'px';
      preview.style.top=Math.max(12,Math.min(card.top-12,innerHeight-box.height-12))+'px';
    }
    function startPreview(item,anchor){
      clearTimeout(previewCloseTimer);clearTimeout(previewTimer);
      if(previewAnchor===anchor)return;
      previewTimer=setTimeout(()=>{
        if(!anchor.isConnected || grid.getAttribute('aria-busy')==='true')return;
        closePreview();previewAnchor=anchor;
        preview=el('section',null,'ai-library-preview');preview.id='ai-library-preview';
        preview.setAttribute('role','region');preview.setAttribute('aria-label','视频详细预览');
        const media=el('div',null,'ai-library-preview-cover');media.append(icon('film'));
        const original=anchor.querySelector('.ai-library-play img');
        function paint(){
          if(!preview?.isConnected || previewAnchor!==anchor || !original?.naturalWidth)return;
          const canvas=el('canvas');canvas.width=Math.min(480,original.naturalWidth);
          canvas.height=Math.round(canvas.width*original.naturalHeight/original.naturalWidth);
          canvas.getContext('2d').drawImage(original,0,0,canvas.width,canvas.height);
          media.replaceChildren(canvas);
        }
        const body=el('div',null,'ai-library-preview-body');
        body.append(el('h2',videoTitle(item.path)),el('p',[
          ...(item.categories || []),item.year && item.year!=='unknown'?item.year:'',
          fileSize(item.size),Number(item.duration)>0?clockTime(item.duration):''
        ].filter(Boolean).join(' · '),'ai-muted'),
        el('p',item.summary || '暂无内容简介','ai-library-preview-intro'));
        const tags=el('div',null,'ai-library-tags');
        for(const tag of item.tags)tags.append(button(tag,()=>{closePreview();return toggleTag(tag);},'ai-tag'));
        const actions=el('div',null,'ai-movie-actions'),play=el('a',null,'ai-button ai-library-primary');
        play.href=fileLink(item.path);play.target='_blank';play.rel='noopener noreferrer';
        play.append(icon('play'),el('span','播放视频'));
        actions.append(play,button('查看完整详情',()=>{closePreview();return details(item.path);}),
          button('关闭',closePreview));
        body.append(tags,actions);preview.append(media,body);document.body.append(preview);
        // An image without a src is also "complete" while its authenticated fetch is pending.
        if(original?.complete && original.naturalWidth)paint();
        else original?.addEventListener('load',paint,{once:true});
        preview.addEventListener('pointerenter',()=>clearTimeout(previewCloseTimer));
        preview.addEventListener('pointerleave',()=>{previewCloseTimer=setTimeout(closePreview,240);});
        positionPreview();
      },180);
    }
    const outsidePreview=event=>{
      if(preview && !preview.contains(event.target) && !previewAnchor?.contains(event.target))closePreview();
    };
    const previewKey=event=>{if(event.key==='Escape')closePreview();};
    const previewScroll=event=>{if(!preview?.contains(event.target))closePreview();};
    document.addEventListener('pointerdown',outsidePreview,true);
    document.addEventListener('keydown',previewKey);
    window.addEventListener('resize',positionPreview);
    window.addEventListener('scroll',previewScroll,true);
    let images=new AbortController();
    let activeCovers=0;
    let pendingCovers=[];
    function pumpCovers(){
      while(activeCovers<4 && pendingCovers.length){
        const {image,signal}=pendingCovers.shift();
        if(signal.aborted || !image.isConnected)continue;
        activeCovers++;
        loadCover(image,signal).finally(()=>{activeCovers--;pumpCovers();});
      }
    }
    const observer=new IntersectionObserver(entries=>{
      for(const entry of entries){
        if(!entry.isIntersecting)continue;
        observer.unobserve(entry.target);
        pendingCovers.push({image:entry.target.aiCover,signal:images.signal});
      }
      pumpCovers();
    },{rootMargin:'160px'});
    target.aiDispose=()=>{
      document.removeEventListener('pointerdown',outsideContent,true);contentMenu.remove();
      clearTimeout(pageLayoutTimer);pageLayoutObserver.disconnect();
      request?.abort();images.abort();observer.disconnect();pendingCovers=[];
      clearTimeout(checkTimer);checkRequest?.abort();displayed.clear();
      facetsRequest.abort();closePreview();
      document.removeEventListener('pointerdown',outsidePreview,true);
      document.removeEventListener('keydown',previewKey);
      window.removeEventListener('resize',positionPreview);
      window.removeEventListener('scroll',previewScroll,true);
    };
    const availabilityLabels={checking:'后台核验中',unavailable:'目录暂时无法访问',
      missing:'原文件已不存在',changed:'视频已变化，请重新扫描'};
    function paintAvailability(root,state,manualTags=[]){
      if(state==='checking' && root.aiAvailability && root.aiAvailability!=='checking')return;
      root.aiAvailability=state;
      root.aiAvailabilityText.textContent=availabilityLabels[state] || '';
      root.aiAvailabilityText.hidden=!availabilityLabels[state];
      if(state==='changed'){
        if(previewAnchor===root)closePreview();
        root.querySelector('.ai-library-summary')?.remove();
        root.querySelector('.ai-library-tags')?.replaceChildren(
          ...manualTags.map(tag=>button(tag,()=>toggleTag(tag),'ai-tag ai-manual')));
        root.querySelector('.ai-library-play img')?.remove();
        root.aiRecognitionText.textContent=statuses.pending;
        root.aiRecognitionText.hidden=false;
      }
    }
    function scheduleChecks(){
      clearTimeout(checkTimer);
      if(displayed.size){
        const checking=[...displayed.values()].some(root=>{
          const rect=root.getBoundingClientRect();
          return root.aiAvailability==='checking' && rect.bottom>=-160 && rect.top<=innerHeight+160;
        });
        checkTimer=setTimeout(checkAvailability,checking?1500:15000);
      }
    }
    async function checkAvailability(){
      if(!target.isConnected)return;
      if(document.hidden){scheduleChecks();return;}
      const pending=new AbortController();checkRequest=pending;
      const paths=[...displayed.entries()].filter(([,root])=>{
        const rect=root.getBoundingClientRect();
        return rect.bottom>=-160 && rect.top<=innerHeight+160;
      }).map(([path])=>path).slice(0,48);
      try{
        if(paths.length){
          const data=await api('/library/availability',{method:'POST',signal:pending.signal,
            body:JSON.stringify({paths,password:password()})});
          if(pending.signal.aborted || !target.isConnected)return;
          for(const item of data.items){
            const root=displayed.get(item.path);
            if(root)paintAvailability(root,item.availability,item.manual_tags);
          }
        }
      }catch{ /* A failed background check leaves the indexed cards available. */ }
      finally{if(!pending.signal.aborted)scheduleChecks();}
    }
    async function loadCover(image,signal){
      return setCover(image,image.dataset.path,signal);
    }
    function select(item){
      closePreview();
      if(view!=='list'){return details(item.path);}
      selectedPath=item.path;side.hidden=false;layout.classList.add('ai-has-selection');
      for(const [path,root] of displayed)root.classList.toggle('ai-selected',path===selectedPath);
      const head=el('div',null,'ai-library-side-head');
      const close=button('',()=>{side.hidden=true;selectedPath=null;layout.classList.remove('ai-has-selection');});
      close.setAttribute('aria-label','关闭文件详情');close.append(icon('close'));
      head.append(el('h2','文件详情'),close);
      const poster=el('div',null,'ai-movie-poster');poster.append(icon('film'));
      const image=el('img');image.alt='视频封面';image.hidden=true;poster.append(image);
      side.replaceChildren(head,poster,el('h3',videoTitle(item.path)),
        el('p',[fileSize(item.size),Number(item.duration)>0?clockTime(item.duration):''].filter(Boolean).join(' · '),'ai-muted'),
        el('p',item.summary || '尚无视频简介','ai-library-summary'));
      if(item.cover)setCover(image,item.path,images.signal);
      const play=el('a',null,'ai-button ai-library-primary');
      play.href=fileLink(item.path);play.target='_blank';play.rel='noopener noreferrer';
      play.append(icon('play'),el('span','播放视频'));
      const actions=el('div',null,'ai-movie-actions');
      actions.append(play,button('下载',()=>downloadVideo(item.path)),button('复制链接',()=>downloadVideo(item.path,true)));
      side.append(actions,button('查看完整详情',()=>details(item.path)));
    }
    function toggleTag(tag){
      if(selected.has(tag))selected.delete(tag);
      else if(selected.size<12)selected.add(tag);
      else return;
      return load(true);
    }
    function card(item){
      const name=videoTitle(item.path);
      const root=el('article',null,'ai-library-card');
      const link=el('a',null,'ai-library-play');
      link.href=fileLink(item.path);
      link.target='_blank';link.rel='noopener noreferrer';link.setAttribute('aria-label',`播放 ${fileName(item.path)}（新标签页）`);
      const playIcon=el('span',null,'ai-library-play-icon');playIcon.append(icon('play'));link.append(playIcon);
      if(item.cover){
        const image=el('img');image.alt='';image.hidden=true;image.dataset.path=item.path;
        link.append(image);
        link.aiCover=image;
        observer.observe(link);
      }
      const details=el('div',null,'ai-library-card-body');
      root.aiRecognitionText=el('p',statuses[item.status] || item.status,'ai-muted');
      root.aiRecognitionText.hidden=item.status==='done';
      const title=button(name,()=>select(item),'ai-library-title');title.title=fileName(item.path);
      details.append(title,el('p',[fileSize(item.size),Number(item.duration)>0?clockTime(item.duration):''].filter(Boolean).join(' · '),'ai-library-file-meta'),
        root.aiRecognitionText);
      root.aiAvailabilityText=el('p',null,'ai-muted');
      details.append(root.aiAvailabilityText);
      if(item.summary)details.append(el('p',item.summary,'ai-library-summary'));
      const tags=el('div',null,'ai-library-tags');
      for(const tag of item.tags.slice(0,3))tags.append(button(tag,()=>toggleTag(tag),'ai-tag'));
      const open=button('详情',()=>select(item),'ai-library-details');
      details.append(open);
      details.append(tags);root.append(link,details);
      displayed.set(item.path,root);paintAvailability(root,item.availability,item.tags);
      root.addEventListener('pointerenter',event=>{if(event.pointerType!=='touch')startPreview(item,root);});
      root.addEventListener('pointerleave',()=>{clearTimeout(previewTimer);previewCloseTimer=setTimeout(closePreview,240);});
      root.addEventListener('focusin',()=>startPreview(item,root));
      return root;
    }
    function renderPagination(total){
      const summary=el('span','共 '+total+' 个视频','ai-muted');
      const controls=el('div',null,'ai-library-page-buttons');
      const previous=button('上一页',()=>load(false,currentPage-1));
      previous.disabled=currentPage<=1;
      controls.append(previous);
      const numbers=[...new Set([1,totalPages,currentPage-2,currentPage-1,currentPage,currentPage+1,currentPage+2])]
        .filter(value=>value>=1 && value<=totalPages).sort((a,b)=>a-b);
      let last=0;
      for(const number of numbers){
        if(last && number-last>1)controls.append(el('span','…','ai-muted'));
        const control=button(String(number),()=>load(false,number));
        control.setAttribute('aria-label','第 '+number+' 页');
        if(number===currentPage)control.setAttribute('aria-current','page');
        controls.append(control);last=number;
      }
      const next=button('下一页',()=>load(false,currentPage+1));
      next.disabled=currentPage>=totalPages;
      controls.append(next);pagination.replaceChildren(summary,controls);
    }
    async function load(reset,page=1) {
      request?.abort();clearTimeout(checkTimer);checkRequest?.abort();closePreview();
      pageSize=gridPageSize();
      const requestedSize=pageSize;
      if(reset){
        asOf=0;
        criteria={keywords:query.value,tags:[...selected],category:category.value,
          year:year.value,sort:sort.value,content,password:password()};
      }
      filters.replaceChildren(...[...selected].map(tag=>button(`${tag} ×`,()=>toggleTag(tag),'ai-tag')));
      const pending=new AbortController();request=pending;
      const sessionToken=localStorage.getItem('token') || '';
      grid.setAttribute('aria-busy','true');hint.textContent='正在加载视频…';
      try {
        const body=JSON.stringify({...criteria,page,page_size:requestedSize,...(asOf?{as_of:asOf}:{})});
        const prefetched=initialCatalog;initialCatalog=null;
        let data;
        if(prefetched && prefetched.token===(localStorage.getItem('token') || '') &&
           prefetched.url===location.href &&
           JSON.stringify({...JSON.parse(prefetched.body),page_size:requestedSize})===body){
          const first=await prefetched.result;
          if(first)data={...first,items:first.items.slice(0,requestedSize),page_size:requestedSize,
            total_pages:Math.ceil(first.total/requestedSize)};
        }
        if(!data)data=await api('/library',{method:'POST',signal:pending.signal,body});
        if(!target.isConnected || pending.signal.aborted || sessionToken!==(localStorage.getItem('token') || ''))return;
        if(data.total_pages && page>data.total_pages){return load(false,data.total_pages);}
        observer.disconnect();images.abort();pendingCovers=[];images=new AbortController();
        displayed.clear();selectedPath=null;side.hidden=true;layout.classList.remove('ai-has-selection');
        grid.replaceChildren(...data.items.map(card));
        currentPage=data.page || page;totalPages=data.total_pages || 0;
        asOf=data.as_of || asOf;
        renderPagination(data.total ?? data.items.length);
        scheduleChecks();
        hint.textContent=data.items.length?'':'未找到匹配的已识别视频';
        if(!reset)target.scrollIntoView({block:'start'});
      } catch(error) {
        if(!pending.signal.aborted){hint.textContent=error.message;scheduleChecks();}
      } finally {
        if(request===pending)grid.setAttribute('aria-busy','false');
      }
    }
    await load(true);
    api('/library/facets',{signal:facetsRequest.signal}).then(data=>{
      if(!target.isConnected || facetsRequest.signal.aborted)return;
      for(const [control,values] of [[category,data.types],[year,data.years]]){
        for(const entry of values){
          const label=entry.value==='unknown'?'未标注年份':entry.value;
          const option=el('option',label+' ('+entry.count+')');option.value=entry.value;control.append(option);
        }
      }
    }).catch(()=>{});
  }
  async function settings(target) {
    const content=target || modal('AI 插件管理').content;
    const data=await api('/settings');
    content.classList.add('ai-settings-page');
    const form=el('form',null,'ai-settings'); const fields={};
    const model=el('section',null,'ai-settings-card');
    model.append(el('h2','模型服务'));
    const scan=el('section',null,'ai-settings-card');
    scan.append(el('h2','扫描范围与计划'));
    const coverSettings=el('section',null,'ai-settings-card');
    coverSettings.append(el('h2','视频封面'));
    const coverState=el('p',data.cover_backfill?'后台正在补齐封面':'封面补齐已暂停','ai-settings-hint');
    const coverToggle=button(data.cover_backfill?'暂停补齐封面':'开始补齐封面',async()=>{
      coverToggle.disabled=true;
      try{
        const next=await api('/library/covers',{method:'PUT',body:JSON.stringify({enabled:!data.cover_backfill})});
        data.cover_backfill=next.enabled;
        coverToggle.textContent=next.enabled?'暂停补齐封面':'开始补齐封面';
        coverState.textContent=next.enabled?'后台正在补齐封面':'封面补齐已暂停';
      }finally{coverToggle.disabled=false;}
    });
    coverSettings.append(coverState,coverToggle);
    const advanced=el('details',null,'ai-settings-card');
    advanced.append(el('summary','高级选项'));
    const maintenance=el('details',null,'ai-settings-card');
    maintenance.append(el('summary','密钥与数据管理'));
    const groups=[
      [model,['model_url','model_name']],
      [scan,['roots','exclude','interval_minutes','extensions']],
      [advanced,[]],
    ];
    const labels={model_url:'模型服务基础地址',model_name:'模型规格',roots:'扫描目录（每行一个）',exclude:'排除目录（每行一个）',extensions:'视频扩展名（逗号分隔）',interval_minutes:'定时扫描间隔（分钟，0 关闭）',concurrency:'识别并发（1–4）',frames:'每个视频抽帧数量（2–48）',max_video_bytes:'视频大小上限（字节）',max_network_bytes:'每个视频读取流量上限（字节）',media_timeout:'解码总超时（秒）',model_timeout:'模型超时（秒）',page_size:'目录分页大小',max_depth:'最大目录深度',max_files:'最大视频索引数量',scan_delay:'扫描请求间隔（秒）'};
    Object.assign(labels,{model_thinking:'官方 MiniCPM 推理模式（增加计算量）',video_num_frames:'官方接口视频采样帧数（0 使用服务默认，最多 128）',context_frames:'字幕关系补充画面（0–8 张）'});
    groups[2][1]=Object.keys(data.settings).filter(key=>!groups[0][1].includes(key) && !groups[1][1].includes(key));
    for (const [section,keys] of groups) for (const key of keys) {
      if (!Object.hasOwn(data.settings,key)) continue;
      const value=data.settings[key];
      const label=el('label',null,'ai-settings-row');
      label.append(el('span',labels[key] || key));
      const input=el(Array.isArray(value) && key!=='extensions'?'textarea':'input');
      input.value=Array.isArray(value)?value.join(key==='extensions'?',':'\n'):String(value); input.name=key;
      if(typeof value==='number') {input.type='number'; input.step='any';}
      if(typeof value==='boolean') {input.type='checkbox'; input.checked=value;}
      if(key==='model_name') { input.setAttribute('list','ai-models'); const list=el('datalist'); list.id='ai-models'; ['MiniCPM-V-4.7-1B','MiniCPM-V-4.7-35B-A3B','openbmb/MiniCPM-V-4.7-1B','openbmb/MiniCPM-V-4.7-35B-A3B'].forEach(v=>{const o=el('option');o.value=v;list.append(o);});label.append(list); }
      label.append(input);section.append(label);fields[key]=input;
    }
    const keyStatus=el('p',data.model_key_configured?'模型密钥已配置':'模型密钥未配置','ai-settings-hint');
    const key=el('input');key.type='password';key.autocomplete='new-password';key.placeholder='输入新模型密钥';key.setAttribute('aria-label','新模型密钥');
    const keyRow=el('div',null,'ai-settings-actions');
    keyRow.append(key,button('更新模型密钥',async()=>{
      await api('/model/key',{method:'PUT',body:JSON.stringify({key:key.value})});
      key.value='';keyStatus.textContent='模型密钥已配置';notify('模型密钥已保存');
    }));
    maintenance.append(keyStatus,keyRow,el('p',`媒体主机白名单 · ${data.media_allowed_hosts.join(', ') || '仅 OpenList 本站'}`,'ai-settings-hint'));
    form.addEventListener('submit',e=>e.preventDefault());
    const actions=el('div',null,'ai-settings-actions ai-settings-save');
    actions.append(button('保存配置',async()=>{
      const s={}; Object.entries(data.settings).forEach(([k,v])=>s[k]=Array.isArray(v)?fields[k].value.split(k==='extensions'?/[,，]/:/\n/).map(x=>x.trim()).filter(Boolean):typeof v==='boolean'?fields[k].checked:typeof v==='number'?Number(fields[k].value):fields[k].value);
      await api('/settings',{method:'PUT',body:JSON.stringify(s)});notify('配置已保存');
    },'ai-settings-primary'),button('测试已保存模型（真实视觉请求）',async()=>{notify('正在发送测试图片…'); const r=await post('/model/test');notify(r.summary);}));
    maintenance.append(button('下载数据备份',async()=>{
      const r=await fetch(`${base}/api/backup`,{method:'POST',headers:{Authorization:localStorage.getItem('token')||''}}); if(!r.ok)throw Error('备份失败');
      const url=URL.createObjectURL(await r.blob()); const a=el('a'); a.href=url;a.download=`aitags-${new Date().toISOString().slice(0,10)}.sqlite3`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }));
    form.append(actions,model,scan,coverSettings,advanced,maintenance);
    content.append(form);
  }
  async function tagManager(target) {
    const content=target || modal('同义标签与常用标签').content;const data=await api('/tags');
    content.append(el('p','同义标签每行填写“别名=标准标签”，不允许循环或多级映射。'));
    const aliases=el('textarea');aliases.value=data.aliases.map(a=>`${a.alias}=${a.canonical}`).join('\n'); aliases.setAttribute('aria-label','同义标签');
    const favorites=el('input');favorites.value=data.favorites.join('，');favorites.setAttribute('aria-label','常用标签');content.append(aliases,el('p','常用标签，逗号分隔'),favorites);
    data.favorites.forEach(t=>content.append(target?el('span',t,'ai-tag'):button(t,()=>filterTag(t),'ai-tag')));
    content.append(button('保存',async()=>{
      await api('/tags',{method:'PUT',body:JSON.stringify({aliases:aliases.value.split('\n').filter(x=>x.trim()).map(x=>{const [alias,canonical]=x.split('=');return {alias:alias?.trim(),canonical:canonical?.trim()};}),favorites:favorites.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)})});state.refreshed=0;notify('标签规则已保存');
    }));
  }
  async function history(target) {
    const content=target || modal('识别历史与错误日志').content;let before='';
    const load=async()=>{const rows=await api(`/history${before?`?before=${before}`:''}`); rows.forEach(r=>content.append(el('p',`${new Date(r.created*1000).toLocaleString()} · ${r.path} · ${r.state} · ${r.message}`))); before=rows.at(-1)?.id;more.hidden=rows.length<100;};
    const more=button('加载较早记录',load);content.append(more);await load();
  }
  let hoverCard=null,hoverAnchor=null,hoverTimer;
  function closeHoverCard(){clearTimeout(hoverTimer);hoverCard?.remove();hoverCard=null;hoverAnchor?.setAttribute('aria-expanded','false');hoverAnchor=null;}
  function positionHoverCard(){
    if(!hoverCard || !hoverAnchor?.isConnected){closeHoverCard();return;}
    const r=(hoverAnchor.closest('.ai-video-row') || hoverAnchor).getBoundingClientRect();
    const below=innerHeight-r.bottom-22,above=r.top-22,down=below>=above;
    hoverCard.style.maxHeight=Math.max(100,Math.min(520,down?below:above))+'px';
    const c=hoverCard.getBoundingClientRect();
    const left=Math.max(12,Math.min(r.right-c.width,innerWidth-c.width-12));
    let top=down?r.bottom+10:Math.max(12,r.top-c.height-10);
    hoverCard.style.left=left+'px';hoverCard.style.top=top+'px';
  }
  function showHoverCard(anchor,path){
    clearTimeout(hoverTimer);if(hoverAnchor===anchor && hoverCard)return;
    closeHoverCard();hoverAnchor=anchor;anchor.setAttribute('aria-expanded','true');
    const data=state.cache.get(path),card=el('section',null,'ai-hover-card');hoverCard=card;card.id='ai-hover-card';card.setAttribute('role','region');card.setAttribute('aria-label','视频 AI 信息');
    const head=el('div',null,'ai-hover-head');head.append(el('span',statuses[data?.status] || '未识别',`ai-status ai-${data?.status||'pending'}`),button('关闭',closeHoverCard));
    const summary=el('p',data?.result?.summary || (data?.status==='failed'?'识别失败，可打开完整结果查看原因。':'尚无视频简介。'),'ai-hover-summary');
    card.append(head,el('h3',path.split('/').at(-1)),summary);
    if((data?.result?.summary || '').length>150){
      const expand=button('展开简介',()=>{const expanded=summary.classList.toggle('ai-expanded');expand.textContent=expanded?'收起简介':'展开简介';expand.setAttribute('aria-expanded',String(expanded));positionHoverCard();});expand.setAttribute('aria-expanded','false');card.append(expand);
    }
    const tags=el('div',null,'ai-tags');(data?.tags || []).forEach(t=>tags.append(button(t,()=>{closeHoverCard();return filterTag(t);},'ai-tag'+(data.manual_tags?.includes(t)?' ai-manual':''))));card.append(tags);
    card.append(button('查看完整 AI 结果',()=>{closeHoverCard();return details(path);}));
    if(state.admin)card.append(button(data?'重新识别':'AI 识别',()=>{closeHoverCard();return recognize(path);}));
    card.addEventListener('pointerenter',()=>clearTimeout(hoverTimer));card.addEventListener('pointerleave',()=>{hoverTimer=setTimeout(closeHoverCard,240);});
    card.addEventListener('focusout',()=>setTimeout(()=>{if(hoverCard && !hoverCard.contains(document.activeElement) && document.activeElement!==hoverAnchor)closeHoverCard();},0));
    document.body.append(card);positionHoverCard();
  }
  document.addEventListener('pointerdown',e=>{if(hoverCard && !hoverCard.contains(e.target) && !e.target.closest?.('.ai-badges'))closeHoverCard();},true);
  document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHoverCard();});
  window.addEventListener('resize',positionHoverCard);window.addEventListener('scroll',e=>{if(hoverCard && !hoverCard.contains(e.target))closeHoverCard();},true);
  function paintRow(row,path) {
    const status=state.cache.get(path)?.status || 'pending';
    const old=row.querySelector('.ai-badges'); if(old && old.aiPath===path && old.aiStatus===status)return;
    old?.remove();const badges=el('span',null,'ai-badges');badges.aiPath=path;badges.aiStatus=status;
    const trigger=button('AI',()=>{showHoverCard(trigger,path);},`ai-status ai-${status}`);
    trigger.setAttribute('aria-label',statuses[status] || '未识别');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-controls','ai-hover-card');
    trigger.title='查看视频简介与标签';
    trigger.addEventListener('focus',()=>showHoverCard(trigger,path));
    trigger.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault();showHoverCard(trigger,path);hoverCard?.querySelector('button')?.focus();}});
    row.onpointerenter=e=>{if(e.pointerType!=='touch')showHoverCard(trigger,path);};
    row.onpointerleave=()=>{hoverTimer=setTimeout(closeHoverCard,240);};
    badges.append(trigger);row.append(badges);row.classList.add('ai-video-row');
    if(row.classList.contains('list-item'))row.classList.add('ai-list-row');
  }
  let busy=false;
  function ensureCinemaLink(){
    if(managePath() || document.querySelector('#ai-cinema-link'))return;
    const actions=document.querySelector('.header .header-right');
    if(!actions)return;
    const link=el('a',null,'ai-cinema-entry');link.id='ai-cinema-link';link.href='/ai/exports';
    link.setAttribute('aria-label','打开 AI 资料与协议');link.title='AI 资料与协议';
    link.addEventListener('click',event=>{
      if(event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button!==0)return;
      event.preventDefault();event.stopPropagation();location.assign(link.href);
    });
    link.append(icon('film'),el('span','AI 资料'));actions.prepend(link);
  }
  async function update() {
    if(busy || document.hidden)return; busy=true;
    try {
      ensureCinemaLink();await refreshAuth(); await ensureManage(); ensureToolbar();ensureNativeSearch();resumeNativeSearch().catch(()=>{});
      if(managePath())return;
      ensureImageVideoCards();
      const rows=[...document.querySelectorAll('.list-item[href],.grid-item[href]')].map(row=>[row,rowPath(row)]).filter(([,path])=>path && videoRE.test(path));
      const missing=rows.map(([,p])=>p).filter(p=>!state.cache.has(p));
      const paths=[...new Set(missing.length?missing:Date.now()-state.refreshed>20000?rows.map(([,p])=>p):[])].slice(0,missing.length ? 16 : 60);
      if(paths.length) {
        const token=state.token, page=location.href, secret=password();
        const data=await post('/videos/batch',{paths,password:secret,preview:true});
        if(token!==(localStorage.getItem('token') || '') || page!==location.href || secret!==password()){schedule();return;}
        paths.forEach(p=>state.cache.set(p,null));data.forEach(x=>state.cache.set(x.path,x));state.refreshed=Date.now();
        if(missing.length>paths.length)schedule();
      }
      rows.forEach(([row,path])=>paintRow(row,path));
      // Extend the existing OpenList context menu without replacing its original actions.
      if(state.context) {
        const menu=[...document.querySelectorAll('[role="menu"],.solid-contextmenu,.contexify')].find(n=>n.getBoundingClientRect().width>0);
        if(menu && !menu.querySelector('.ai-context')) {
          const actions=el('div',null,'ai-context');actions.append(button('查看 AI 结果',()=>details(state.context)));
          if(state.admin) actions.append(button('AI 识别 / 重新识别',()=>recognize(state.context)),button('编辑 AI / 人工标签',()=>details(state.context)));menu.append(actions);
        }
      }
    } catch(e) { state.error=e.message; }
    finally {busy=false;}
  }
  let debounce;
  const schedule=()=>{
    if(debounce)return;
    debounce=setTimeout(()=>{debounce=null;update();},120);
  };
  document.addEventListener('click',e=>{
    if(!managePath() || new URLSearchParams(location.search).get('ai')!=='video-tags' ||
       e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button!==0)return;
    const a=e.target.closest?.('a[href]');
    if(!a || a.matches('.ai-manage-link,.ai-manage-sub-link'))return;
    const url=new URL(a.href,location.href);
    if(url.origin!==location.origin || !/^\/@manage(?:\/|$)/.test(url.pathname))return;
    const before=location.pathname+location.search;
    // Solid can change only the query without rendering or emitting popstate.
    setTimeout(()=>{
      if(before!==location.pathname+location.search){
        oldPath=location.pathname+location.search;clearManagePanel();restoreManageNavigation();schedule();
      }
    },0);
  },true);
  document.addEventListener('contextmenu',e=>{const row=e.target.closest('.list-item,.grid-item');const path=row&&rowPath(row);state.context=path && videoRE.test(path)?path:null;document.querySelectorAll('.ai-context').forEach(n=>n.remove());schedule();},true);
  window.addEventListener('popstate',()=>{closeHoverCard();oldPath=location.pathname+location.search;state.refreshed=0;document.querySelector('#ai-toolbar')?.remove();clearManagePanel();schedule();});
  window.addEventListener('storage',e=>{if(e.key==='token'){clearManagePanel();clearNativeResults();sessionStorage.removeItem('ai-native-search-pending');schedule();}});
  let oldPath=location.pathname+location.search;
  new MutationObserver(records=>{
    if(oldPath!==location.pathname+location.search){closeHoverCard();oldPath=location.pathname+location.search;state.refreshed=0;document.querySelector('#ai-toolbar')?.remove();clearManagePanel();}
    const pluginNode=node=>node.nodeType===1 && node.matches('[id^="ai-"],.ai-badges,.ai-context');
    if(records.some(r=>!r.target.closest?.('[id^="ai-"],.ai-badges,.ai-context') &&
       [...r.addedNodes,...r.removedNodes].some(node=>!pluginNode(node))))schedule();
  }).observe(document.documentElement,{childList:true,subtree:true});
  setInterval(update,8000);schedule();
})();
