<template>
  <main class="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
    <div class="flex flex-wrap items-center justify-between gap-4 mb-7">
      <div><h1 class="text-3xl font-bold text-white">AI 识别与外部协议</h1><p class="mt-2 text-sm text-white/50">理解视频内容，生成媒体资料，交给你的播放器。</p></div>
      <button class="btn-secondary" :disabled="busy" @click="refreshCurrent">刷新数据</button>
    </div>
    <div v-if="message" role="status" class="mb-5 rounded-xl border border-blue-400/20 bg-blue-400/10 p-4 text-sm text-blue-100">{{ message }}</div>
    <div v-if="error" role="alert" class="mb-5 rounded-xl border border-red-400/20 bg-red-400/10 p-4 text-sm text-red-100">{{ error }}</div>
    <nav aria-label="AI 中心分区" class="flex gap-2 overflow-x-auto border-b border-white/10 pb-4 mb-7">
      <button v-for="entry in tabs" :key="entry.id" class="px-4 py-2 rounded-lg whitespace-nowrap text-sm" :class="tab === entry.id ? 'bg-blue-500/20 text-blue-200' : 'text-white/50 hover:bg-white/5'" :aria-pressed="tab === entry.id" @click="changeTab(entry.id)">{{ entry.label }}</button>
    </nav>

    <section v-if="tab === 'overview'" class="space-y-6">
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div v-for="stat in stats" :key="stat.label" class="stat-card"><p class="text-sm text-white/50">{{ stat.label }}</p><strong class="block text-3xl text-white mt-3">{{ stat.value }}</strong></div>
      </div>
      <div class="card p-6"><h2 class="text-xl text-white font-semibold">从识别到入库</h2><div class="grid sm:grid-cols-3 gap-6 mt-6 text-sm"><div><b class="text-blue-300">01 配置与扫描</b><p class="text-white/50 mt-2 leading-7">连接在线 MiniCPM-V，设置目录、资源上限和定时扫描。</p></div><div><b class="text-blue-300">02 校对资料</b><p class="text-white/50 mt-2 leading-7">查看真实画面生成的简介与标签；人工标签在重新识别后保留。</p></div><div><b class="text-blue-300">03 导出与订阅</b><p class="text-white/50 mt-2 leading-7">生成 STRM/NFO、M3U、RSS、TVBox 或 Jellyfin 接入信息。</p></div></div></div>
      <div class="card p-6 text-sm text-white/60"><p>媒体输出目录：<code>{{ exportDirectory || '读取中…' }}</code></p><p class="mt-3">OpenList：{{ openlistUrl || '请先配置 AI 服务的 OpenList 连接' }}</p><p class="mt-3">原有 OStrm 任务可继续生成 STRM。启用自动 AI 入队后，新视频将进入持久化识别队列。</p></div>
    </section>

    <section v-if="tab === 'settings'" class="space-y-6">
      <form class="card p-6" @submit.prevent="saveSettings">
        <h2 class="text-xl font-semibold text-white mb-6">模型与扫描设置</h2>
        <div class="grid sm:grid-cols-2 gap-5">
          <label class="field">模型服务地址<input v-model="settings.model_url" required type="url" class="input" placeholder="https://api.modelbest.ai/v1"></label>
          <label class="field">模型规格<select v-model="settings.model_name" class="input"><option>MiniCPM-V-4.7-1B</option><option>MiniCPM-V-4.7-35B-A3B</option></select></label>
          <label class="field">扫描目录，每行一个<textarea v-model="rootText" required rows="4" class="input" placeholder="/网盘/OneDrive/Videos"></textarea></label>
          <label class="field">排除目录，每行一个<textarea v-model="excludeText" rows="4" class="input"></textarea></label>
          <label class="field">视频格式，逗号分隔<input v-model="extensionText" class="input" required></label>
          <label v-for="field in numericFields" :key="field.key" class="field">{{ field.label }}<input v-model.number="settings[field.key]" type="number" :min="field.min" :max="field.max" required class="input"></label>
        </div>
        <label class="flex items-center gap-2 text-sm text-white/70 mt-5"><input v-model="settings.model_thinking" type="checkbox">启用模型思考模式</label>
        <label class="flex items-center gap-2 text-sm text-white/70 mt-4"><input v-model="coverBackfill" type="checkbox">后台补齐已有视频封面（新识别视频优先复用压缩文件生成封面）</label>
        <div class="flex flex-wrap gap-3 mt-6"><button class="btn-primary" :disabled="busy">保存设置</button><button type="button" class="btn-secondary" :disabled="busy" @click="testModel">真实模型连接测试</button></div>
      </form>
      <form class="card p-6" @submit.prevent="saveKey"><h2 class="text-xl font-semibold text-white mb-4">模型密钥</h2><p class="text-white/50 text-sm mb-4">{{ keyConfigured ? '已配置。现有密钥不会返回到浏览器。' : '尚未配置。' }}</p><label class="field">更新密钥<input v-model="modelKey" class="input" type="password" autocomplete="new-password" required maxlength="2000"></label><button class="btn-primary mt-5" :disabled="busy">保存新密钥</button></form>
    </section>

    <section v-if="tab === 'queue'" class="space-y-6">
      <form class="card p-6" @submit.prevent="scan"><h2 class="text-xl font-semibold text-white mb-4">目录识别任务</h2><label class="field">本次扫描目录，每行一个<textarea v-model="scanText" rows="3" class="input" required></textarea></label><div class="flex flex-wrap gap-4 mt-4 items-center"><label class="text-sm text-white/60"><input v-model="useSubtitles" type="checkbox"> 结合字幕识别</label><button class="btn-primary" :disabled="busy">开始扫描</button></div></form>
      <div class="card overflow-x-auto"><table class="w-full text-sm text-left"><thead class="text-white/40"><tr><th class="p-4">任务</th><th class="p-4">目录</th><th class="p-4">状态与进度</th><th class="p-4">操作</th></tr></thead><tbody class="text-white/75"><tr v-for="job in jobs" :key="job.id" class="border-t border-white/5"><td class="p-4">#{{ job.id }}</td><td class="p-4 break-all max-w-sm">{{ job.root }}</td><td class="p-4">{{ job.state }}<pre class="text-xs mt-1 text-white/40">{{ job.counts ? JSON.stringify(job.counts) : '' }}</pre></td><td class="p-4"><div class="flex gap-2"><button v-for="action in actions(job.state)" :key="action.id" class="text-blue-300 text-xs" @click="jobAction(job.id, action.id)">{{ action.label }}</button></div></td></tr></tbody></table><p v-if="!jobs.length" class="p-6 text-white/40">暂无任务。</p></div>
    </section>

    <section v-if="tab === 'library'" class="space-y-5">
      <form class="flex flex-wrap gap-3" @submit.prevent="loadLibrary(1)"><input v-model="query" aria-label="搜索识别结果" class="input flex-1 min-w-48" placeholder="搜索文件名、标签或简介"><select v-model="content" aria-label="内容分区" class="input w-36" @change="loadLibrary(1)"><option value="all">全部内容</option><option value="general">非成人</option><option value="adult">成人</option></select><button class="btn-primary">搜索</button><button type="button" class="btn-secondary" :disabled="!selected.length" @click="publicationFromSelection">为所选视频创建订阅</button></form>
      <p class="text-sm text-white/40">共 {{ libraryTotal }} 条识别结果 · 已选 {{ selected.length }} 条</p>
      <article v-for="item in library" :key="item.path" class="card p-5">
        <div class="flex items-start gap-3"><input v-model="selected" :value="item.path" type="checkbox" :aria-label="'选择 '+item.path" class="mt-1"><div class="flex-1"><h3 class="font-semibold text-white break-all">{{ item.path.split('/').pop() }}</h3><p class="text-xs text-white/30 mt-1 break-all">{{ item.path }}</p><p class="mt-3 text-sm leading-7 text-white/65">{{ item.summary }}</p><div class="flex flex-wrap gap-2 mt-3"><span v-for="tag in item.tags" :key="tag" class="px-2 py-1 rounded bg-blue-400/10 text-blue-200 text-xs">{{ tag }}</span></div><div class="flex flex-wrap gap-4 mt-4 text-xs"><button class="text-blue-300" @click="editTags(item)">编辑人工标签</button><button class="text-white/50" @click="recognize(item.path)">重新识别</button><button class="text-white/50" @click="showDetail(item.path)">完整识别资料</button></div></div></div>
      </article>
      <div class="flex items-center justify-center gap-3"><button class="btn-secondary" :disabled="libraryPage <= 1 || busy" @click="loadLibrary(libraryPage-1)">上一页</button><span class="text-white/50 text-sm">{{ libraryPage }} / {{ libraryPages || 1 }}</span><button class="btn-secondary" :disabled="libraryPage >= libraryPages || busy" @click="loadLibrary(libraryPage+1)">下一页</button></div>
    </section>

    <section v-if="tab === 'publications'" class="space-y-6">
      <form class="card p-6" @submit.prevent="createPublication"><h2 class="text-xl font-semibold text-white mb-5">创建外部媒体库订阅</h2><div class="grid sm:grid-cols-2 gap-5"><label class="field">订阅名称<input v-model="publicationForm.name" required maxlength="150" class="input"></label><label class="field">公开服务地址<input v-model="publicationForm.public_base_url" required type="url" class="input" placeholder="https://media.example.com"></label><label class="field sm:col-span-2">授权目录或视频路径，每行一个<textarea v-model="publicationRoots" rows="3" class="input" required></textarea></label><label class="field">内容范围<select v-model="publicationForm.content" class="input"><option value="all">全部内容</option><option value="general">非成人</option><option value="adult">成人</option></select></label><label class="field">最多输出条目<input v-model.number="publicationForm.max_items" type="number" min="1" max="50000" class="input" required></label><label class="field">视频播客作者（可选）<input v-model="publicationForm.author" class="input"></label><label class="field">播客联系人邮箱（可选）<input v-model="publicationForm.owner_email" type="email" class="input"></label></div><label class="flex items-center gap-2 mt-5 text-sm text-white/60"><input v-model="publicationForm.sync_enabled" type="checkbox">自动增量更新共享输出目录</label><p class="text-xs leading-6 text-white/40 mt-4">订阅链接本身是限定这些目录的只读访问凭据，可随时撤销。Jellyfin 接口提供直放与观看状态；WebDAV 继续使用原 OpenList 账户和 /dav 地址。</p><button class="btn-primary mt-5" :disabled="busy">创建并生成接入信息</button></form>
      <article v-for="publication in publications" :key="publication.id" class="card p-5"><div class="flex flex-wrap justify-between gap-4"><div><h3 class="text-lg font-semibold text-white">{{ publication.name }} <span class="text-xs text-white/40">{{ publication.revoked ? '已撤销' : '有效' }}</span></h3><p class="text-xs text-white/40 mt-2 break-all">{{ publication.roots.join(' · ') }}</p></div><div class="flex gap-3 text-xs items-start"><button class="text-blue-300" @click="loadLinks(publication.id)">接入信息</button><button class="text-blue-300" :disabled="publication.revoked" @click="sync(publication.id)">同步资料</button><button class="text-white/50" @click="rotate(publication.id)">更换凭据</button><button class="text-red-300" :disabled="publication.revoked" @click="revoke(publication.id)">撤销</button></div></div><div v-if="linkSets[publication.id]" class="mt-5 space-y-3"><label v-for="(value,key) in linkSets[publication.id]" :key="key" class="field text-xs">{{ linkLabels[key] || key }}<div class="flex gap-2"><input :value="value" readonly :aria-label="String(key)" class="input min-w-0 flex-1 text-xs font-mono" @focus="selectText"><button class="btn-secondary text-xs" @click="copy(value)">复制</button></div></label></div></article>
      <div class="card p-5"><h2 class="text-lg font-semibold text-white mb-4">资料同步记录</h2><div v-for="job in exportJobs" :key="job.id" class="py-3 border-t border-white/5 text-sm text-white/65"><span>{{ job.state }}</span><pre class="mt-2 text-xs whitespace-pre-wrap break-all text-white/40">{{ job.result }}</pre><button v-if="job.state==='failed'" class="text-blue-300 mt-2 text-xs" @click="sync(job.publication_id)">重新同步</button></div><p v-if="!exportJobs.length" class="text-sm text-white/40">暂无同步记录。</p></div>
    </section>

    <section v-if="tab === 'tags'" class="space-y-6">
      <form class="card p-6" @submit.prevent="saveTagRules"><h2 class="text-xl font-semibold text-white mb-4">同义标签与常用标签</h2><p class="text-sm text-white/50 mb-5">每行一个「别名=规范标签」。显示与导出使用规范标签；视频的人工修改仍然保留。</p><label class="field">同义标签<textarea v-model="aliasText" rows="8" class="input" placeholder="教程=教学"></textarea></label><label class="field mt-5">常用标签，逗号分隔<input v-model="favoriteText" class="input" placeholder="电影,教学,旅行"></label><button class="btn-primary mt-6" :disabled="busy">保存标签规则</button></form>
    </section>

    <section v-if="tab === 'history'" class="card p-6"><div class="flex flex-wrap gap-4 items-center justify-between"><div><h2 class="text-xl font-semibold text-white">识别历史与错误日志</h2><p class="text-sm text-white/40 mt-2">最近 100 条记录</p></div><label class="text-sm text-white/60"><input v-model="onlyErrors" type="checkbox"> 只看错误与警告</label></div><div v-for="record in visibleHistory" :key="record.id" class="border-t border-white/10 mt-5 pt-5 text-sm"><div class="flex flex-wrap gap-3 text-white/40"><time>{{ new Date(record.created*1000).toLocaleString() }}</time><span :class="record.state==='failed'?'text-red-300':'text-blue-300'">{{ record.state }}</span></div><p class="mt-3 text-white/70 break-all">{{ record.path }}</p><p class="mt-2 text-white/50 break-all">{{ record.message }}</p></div><p v-if="!visibleHistory.length" class="text-sm text-white/40 mt-6">暂无匹配记录。</p></section>

    <dialog ref="detailDialog" class="ai-detail"><div class="flex justify-between items-center gap-4 mb-5"><h2 class="text-xl font-semibold text-white">识别资料</h2><button class="btn-secondary" @click="detailDialog?.close()">关闭</button></div><pre class="whitespace-pre-wrap break-all text-xs leading-7 text-white/70">{{ detail }}</pre></dialog>
    <dialog ref="tagDialog" class="ai-detail"><form @submit.prevent="saveTags"><h2 class="text-xl font-semibold text-white mb-5">人工标签</h2><p class="text-xs text-white/40 mb-4 break-all">{{ editingPath }}</p><label class="field">新增标签，逗号分隔<input v-model="manualTags" class="input"></label><label class="field mt-4">移除的 AI 标签，逗号分隔<input v-model="removedTags" class="input"></label><div class="flex gap-3 mt-6"><button class="btn-primary">保存标签</button><button type="button" class="btn-secondary" @click="tagDialog?.close()">取消</button></div></form></dialog>
  </main>
</template>

<script setup lang="ts">
import { authenticatedApiCall } from '~/core/api/client'
definePageMeta({ middleware: 'auth' })
type Row = Record<string, any>
const tabs=[{id:'overview',label:'总览'},{id:'settings',label:'模型与扫描'},{id:'queue',label:'识别任务'},{id:'library',label:'识别资料'},{id:'publications',label:'外部协议与导出'},{id:'tags',label:'标签规则'},{id:'history',label:'历史与错误日志'}]
const tab=ref('overview'),busy=ref(false),message=ref(''),error=ref(''),overview=ref<Row>({}),settings=reactive<Row>({}),jobs=ref<Row[]>([])
const keyConfigured=ref(false),modelKey=ref(''),rootText=ref(''),excludeText=ref(''),extensionText=ref(''),scanText=ref(''),useSubtitles=ref(true)
const coverBackfill=ref(false)
const aliasText=ref(''),favoriteText=ref(''),historyRecords=ref<Row[]>([]),onlyErrors=ref(false)
const visibleHistory=computed(()=>historyRecords.value.filter(row=>!onlyErrors.value||['failed','warning'].includes(row.state)))
async function refreshCurrent(){if(['tags','history'].includes(tab.value))await changeTab(tab.value);else await reload()}
const publications=ref<Row[]>([]),exportJobs=ref<Row[]>([]),linkSets=reactive<Record<string,Record<string,string>>>({}),exportDirectory=ref(''),openlistUrl=ref('')
const publicationForm=reactive({name:'我的视频资料库',public_base_url:'',content:'all',max_items:5000,author:'',owner_email:'',sync_enabled:false}),publicationRoots=ref('')
const library=ref<Row[]>([]),libraryTotal=ref(0),libraryPage=ref(1),libraryPages=ref(0),query=ref(''),content=ref('all'),selected=ref<string[]>([])
const detailDialog=ref<HTMLDialogElement|null>(null),tagDialog=ref<HTMLDialogElement|null>(null),detail=ref(''),editingPath=ref(''),manualTags=ref(''),removedTags=ref('')
const numericFields=[{key:'interval_minutes',label:'自动扫描间隔（分钟，0 关闭）',min:0,max:10080},{key:'concurrency',label:'识别并发',min:1,max:4},{key:'video_num_frames',label:'模型视频帧数',min:0,max:128},{key:'context_frames',label:'补充证据帧数',min:0,max:8},{key:'max_video_bytes',label:'视频大小上限（字节）',min:1048576,max:1099511627776},{key:'max_network_bytes',label:'单视频读取流量上限（字节）',min:1048576,max:17179869184},{key:'media_timeout',label:'压缩与取字幕超时（秒）',min:10,max:7200},{key:'model_timeout',label:'推理超时（秒）',min:10,max:1800}]
const linkLabels:Record<string,string>={strm_nfo_zip:'STRM＋NFO＋封面下载包',m3u:'扩展 M3U',rss:'RSS 2.0＋Media RSS',podcast:'视频播客 RSS',maccms:'苹果 CMS v10 JSON',tvbox:'TVBox 配置',libretv:'LibreTV 订阅',jellyfin_server:'Jellyfin 服务器地址',jellyfin_username:'Jellyfin 用户名',jellyfin_password:'Jellyfin 密码',webdav_server:'原 OpenList WebDAV 地址（使用原账户）',webdav_ai_server:'带 AI 资料的只读 WebDAV 地址',webdav_username:'只读 WebDAV 用户名',webdav_password:'只读 WebDAV 密码'}
const stats=computed(()=>[{label:'已识别',value:overview.value.videos?.done||0},{label:'等待识别',value:overview.value.tasks?.queued||0},{label:'正在识别',value:overview.value.tasks?.running||0},{label:'有效订阅',value:publications.value.filter(row=>!row.revoked).length}])
const lines=(value:string)=>value.split(/\r?\n/).map(row=>row.trim()).filter(Boolean)
const tags=(value:string)=>value.split(/[,，]/).map(row=>row.trim()).filter(Boolean)
const api=<T=any>(path:string,method='GET',body?:unknown)=>authenticatedApiCall<T>('/ai'+path,{method,body})
async function operation(action:()=>Promise<void>){busy.value=true;error.value='';message.value='';try{await action()}catch(cause:any){const value=cause?.data?.detail;error.value=Array.isArray(value)?value.map((entry:any)=>entry.msg).join('；'):typeof value==='string'?value:(cause.message||'操作失败，请重试')}finally{busy.value=false}}
async function reload(){await operation(async()=>{const [stats,data,records,profiles,outputs]=await Promise.all([api('/overview'),api('/settings'),api('/jobs'),api('/exports/publications'),api('/exports/jobs')]);overview.value=stats;coverBackfill.value=Boolean(data.cover_backfill);Object.assign(settings,data.settings);keyConfigured.value=data.model_key_configured;rootText.value=(settings.roots||[]).join('\n');excludeText.value=(settings.exclude||[]).join('\n');extensionText.value=(settings.extensions||[]).join(',');if(!scanText.value)scanText.value=rootText.value;if(!publicationRoots.value)publicationRoots.value=rootText.value;publications.value=profiles.items;exportDirectory.value=profiles.export_directory;openlistUrl.value=profiles.openlist_url;exportJobs.value=outputs;jobs.value=records})}
async function changeTab(value:string){tab.value=value;error.value='';message.value='';if(value==='library')await loadLibrary(1);else if(value==='queue'||value==='publications')await refreshQueues();else if(value==='tags')await operation(async()=>{const data=await api('/tags');aliasText.value=data.aliases.map((row:Row)=>row.alias+'='+row.canonical).join('\n');favoriteText.value=data.favorites.join(',')});else if(value==='history')await operation(async()=>{historyRecords.value=await api('/history')})}
async function saveTagRules(){await operation(async()=>{const aliases=lines(aliasText.value).map(value=>{const index=value.indexOf('=');if(index<1||index===value.length-1)throw Error('每行需要填写「别名=规范标签」');return {alias:value.slice(0,index).trim(),canonical:value.slice(index+1).trim()}});await api('/tags','PUT',{aliases,favorites:tags(favoriteText.value)});message.value='标签规则已保存'})}
async function refreshQueues(){try{const [tasks,profiles,outputs]=await Promise.all([api('/jobs'),api('/exports/publications'),api('/exports/jobs')]);jobs.value=tasks;publications.value=profiles.items;exportJobs.value=outputs}catch(cause:any){error.value=cause?.data?.detail||'数据暂时无法刷新'}}
async function saveSettings(){await operation(async()=>{await api('/settings','PUT',{...settings,roots:lines(rootText.value),exclude:lines(excludeText.value),extensions:tags(extensionText.value)});await api('/library/covers','PUT',{enabled:coverBackfill.value});message.value='设置已保存'})}
async function saveKey(){await operation(async()=>{await api('/model/key','PUT',{key:modelKey.value});modelKey.value='';keyConfigured.value=true;message.value='模型密钥已更新'})}
async function testModel(){await operation(async()=>{const result=await api('/model/test','POST',{});message.value='模型测试完成：'+JSON.stringify(result)})}
async function scan(){await operation(async()=>{await api('/scan','POST',{paths:lines(scanText.value),content_type:'auto',use_subtitles:useSubtitles.value});message.value='扫描已加入队列';await refreshQueues()})}
function actions(state:string){return state==='running'?[{id:'pause',label:'暂停'},{id:'cancel',label:'取消'}]:state==='paused'?[{id:'resume',label:'恢复'},{id:'cancel',label:'取消'}]:[{id:'retry',label:'重试失败'}]}
async function jobAction(id:number,action:string){await operation(async()=>{await api('/jobs/'+id+'/'+action,'POST',{});await refreshQueues()})}
async function loadLibrary(page:number){await operation(async()=>{const data=await api('/library','POST',{keywords:query.value,content:content.value,page,page_size:24});library.value=data.items;libraryTotal.value=data.total;libraryPage.value=data.page;libraryPages.value=data.total_pages})}
function publicationFromSelection(){publicationRoots.value=selected.value.join('\n');publicationForm.name='所选视频资料';tab.value='publications'}
async function createPublication(){await operation(async()=>{const result=await api('/exports/publications','POST',{...publicationForm,roots:lines(publicationRoots.value)});linkSets[result.id]=result.links;await refreshQueues();message.value='订阅已创建，下方可复制各协议接入信息'})}
async function loadLinks(id:string){await operation(async()=>{linkSets[id]=await api('/exports/publications/'+id+'/links')})}
async function sync(id:string){await operation(async()=>{await api('/exports/publications/'+id+'/sync','POST',{});message.value='资料同步已加入队列';await refreshQueues()})}
async function revoke(id:string){await operation(async()=>{await api('/exports/publications/'+id+'/revoke','POST',{});delete linkSets[id];await refreshQueues();message.value='订阅已撤销'})}
async function rotate(id:string){await operation(async()=>{const result=await api('/exports/publications/'+id+'/rotate','POST',{});linkSets[id]=result.links;await refreshQueues();message.value='凭据已更换，旧链接和旧 Jellyfin 会话已失效'})}
async function recognize(path:string){await operation(async()=>{await api('/recognize','POST',{paths:[path],force:true,content_type:'auto',use_subtitles:useSubtitles.value});message.value='重新识别已加入队列'})}
async function showDetail(path:string){await operation(async()=>{const records=await api('/videos/batch','POST',{paths:[path]});detail.value=JSON.stringify(records[0]||{},null,2);detailDialog.value?.showModal()})}
async function editTags(item:Row){await operation(async()=>{const records=await api('/videos/batch','POST',{paths:[item.path]});const full=records[0];if(!full)throw Error('当前视频不可读取');editingPath.value=item.path;manualTags.value=(full.manual_tags||[]).join(',');removedTags.value=(full.removed||[]).join(',');tagDialog.value?.showModal()})}
async function saveTags(){await operation(async()=>{await api('/videos/edit','POST',{path:editingPath.value,manual_tags:tags(manualTags.value),removed:tags(removedTags.value)});tagDialog.value?.close();message.value='人工标签已保存'});await loadLibrary(libraryPage.value)}
async function copy(value:string){try{await navigator.clipboard.writeText(value);message.value='已复制'}catch{message.value='请选中字段内容后复制'}}
function selectText(event:FocusEvent){(event.target as HTMLInputElement)?.select()}
let timer:ReturnType<typeof setInterval>|undefined
onMounted(()=>{publicationForm.public_base_url=window.location.origin;reload();timer=setInterval(()=>{if(!document.hidden&&!busy.value&&(tab.value==='queue'||tab.value==='publications'))refreshQueues()},5000)})
onBeforeUnmount(()=>{if(timer)clearInterval(timer)})
</script>

<style scoped>
.field{display:flex;flex-direction:column;gap:.5rem;font-size:.8rem;color:rgb(255 255 255 / .6)}
.input{width:100%;padding:.65rem .8rem;border:1px solid rgb(255 255 255 / .12);border-radius:.6rem;background:#111723;color:#e5edf9;outline:none;font:inherit}
.input:focus{border-color:#60a5fa}select.input{width:auto}textarea.input{resize:vertical}
.ai-detail{width:min(900px,calc(100vw - 32px));max-height:85dvh;padding:24px;border:1px solid #334155;border-radius:16px;background:#101722;color:white}
.ai-detail::backdrop{background:#000a;backdrop-filter:blur(5px)}
</style>
