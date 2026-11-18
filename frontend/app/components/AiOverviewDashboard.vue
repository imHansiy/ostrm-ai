<template>
  <div class="space-y-5">
    <section class="card p-5 sm:p-7" aria-label="整体运行状态">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span class="text-xs tracking-wide text-white/45">运行概况</span>
        <div class="flex rounded-lg border border-white/10 p-1 gap-1" aria-label="统计范围">
          <button v-for="entry in scopes" :key="entry.all.toString()" :aria-pressed="all===entry.all" class="px-3 py-1.5 text-xs rounded-md" :class="all===entry.all?'bg-white/10 text-white':'text-white/45 hover:text-white'" @click="all=entry.all">{{ entry.label }}</button>
        </div>
      </div>
      <div class="flex items-start gap-3 mt-6">
        <span class="mt-2 h-2.5 w-2.5 shrink-0 rounded-full" :class="state.tone==='active'?'bg-blue-400':state.tone==='warning'?'bg-amber-400':'bg-white/40'" aria-hidden="true"></span>
        <div class="min-w-0"><h2 class="text-xl sm:text-2xl font-semibold text-white leading-snug">{{ state.title }}</h2><p class="text-sm text-white/55 mt-3 leading-7 max-w-3xl">{{ state.detail }}</p></div>
      </div>
      <div class="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-white/10 pt-4">
        <p class="text-xs text-white/40 leading-6">{{ progressError || (stale ? '已超过 20 秒未取得新数据' : '每 5 秒刷新 · 最近更新 '+(updatedAt ? new Date(updatedAt).toLocaleTimeString() : '读取中')) }}<span v-if="overview.last_success"> · 全库最近成功 {{ date(overview.last_success.created) }}</span></p>
        <button class="text-xs text-blue-300 hover:text-blue-200" @click="$emit('navigate','queue')">查看任务详情 →</button>
      </div>
    </section>

    <section class="card p-5 sm:p-7" aria-label="识别结果与统计范围">
      <div class="flex flex-wrap justify-between items-center gap-2"><h2 class="text-base font-semibold text-white">{{ all ? '全库识别结果' : '当前处理范围' }}</h2><span class="text-xs text-white/40">已发现 {{ ready ? number(state.total) : '—' }} 个视频</span></div>
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-5 mt-6">
        <div><p class="text-xs text-white/45">已识别成功</p><strong class="block text-3xl font-semibold text-white mt-2">{{ ready ? number(state.done) : '—' }}</strong><button class="text-xs text-blue-300 mt-3" @click="$emit('navigate','library')">浏览识别资料 →</button></div>
        <div><p class="text-xs text-white/45">仍需处理</p><strong class="block text-3xl font-semibold mt-2" :class="state.remaining?'text-amber-100':'text-white'">{{ ready ? number(state.remaining) : '—' }}</strong><p class="text-xs text-white/40 leading-5 mt-3">失败 {{ number(state.videos.failed) }} · 其他未成功 {{ number(state.remaining-(state.videos.failed||0)) }}</p></div>
        <div class="col-span-2 sm:col-span-1 border-t border-white/10 pt-4 sm:border-0 sm:pt-0"><p class="text-xs text-white/45">恢复计划暂缓范围</p><strong class="block text-3xl font-semibold text-white/65 mt-2">{{ ready ? number(state.deferred) : '—' }}</strong><p class="text-xs text-white/40 leading-5 mt-3">{{ all ? '已包含在全库统计中' : '未计入左侧处理范围' }}，保留原记录</p></div>
      </div>
      <div v-if="ready && state.total" class="mt-6"><div class="flex justify-between text-xs text-white/45 mb-2"><span>已发现视频的成功占比</span><span>{{ state.ratio }}%</span></div><div class="h-1.5 bg-white/10 rounded-full overflow-hidden"><div class="h-full bg-blue-400 rounded-full" :style="{width:state.ratio+'%'}"></div></div></div>
      <p class="text-xs text-white/35 leading-6 mt-4 break-all">{{ !all && overview.recovery?.exclude_roots?.length ? '当前排除：'+overview.recovery.exclude_roots.join('、')+'。' : '' }}目录发现尚可能增加视频数量；成功占比不代表全盘扫描完成。</p>
    </section>

    <section aria-label="后台各阶段" class="grid md:grid-cols-3 gap-4">
      <article class="card p-5"><h3 class="text-sm font-semibold text-white">视频准备与识别</h3><p class="text-lg text-white mt-4">{{ state.running ? state.running+' 个处理中' : state.queue ? '等待领取任务' : '当前空闲' }}</p><p class="text-xs text-white/45 leading-6 mt-2">已入队 {{ number(state.queue) }} 个 · 未入队的失败项另计</p><button class="text-xs text-blue-300 mt-4" @click="$emit('navigate','queue')">查看识别任务 →</button></article>
      <article class="card p-5"><h3 class="text-sm font-semibold text-white">目录发现与复查</h3><p class="text-lg text-white mt-4">{{ state.directoryRunning ? state.directoryRunning+' 个标记为检查中' : state.directoryQueue ? '等待目录检查' : '暂无待检查目录' }}</p><p class="text-xs text-white/45 leading-6 mt-2">待检查 {{ number(state.directoryQueue) }} 个 · 检查失败 {{ number(state.dirs.failed) }} 个<span v-if="state.dirs.paused"> · 已暂停 {{ number(state.dirs.paused) }} 个</span></p><p class="text-xs text-white/35 leading-6 mt-3">统计当前运行或暂停的扫描；实时活动以任务详情为准。</p></article>
      <article class="card p-5"><h3 class="text-sm font-semibold text-white">封面与协议发布</h3><p class="text-lg text-white mt-4">{{ overview.background ? overview.background.pending ? overview.background.pending+' 个后台任务' : '暂无积压' : '读取中' }}</p><p class="text-xs text-white/45 leading-6 mt-2">等待重试 {{ number(overview.background?.retrying) }} 个 · 独立于识别队列</p><button class="text-xs text-blue-300 mt-4" @click="$emit('navigate','publications')">查看外部协议 →</button></article>
    </section>

    <AiTaskActivity v-if="state.active.length" :activity="{...overview.activity,active:state.active}" :last-success="overview.last_success" :queued="state.queue" :running="state.running" :paused="overview.recovery?.state==='paused'" :updated-at="updatedAt" />
    <details v-if="overview.recovery && overview.recovery.state!=='disabled'" class="card p-5">
      <summary class="cursor-pointer text-sm text-white/70 leading-6">恢复计划详情 <span class="text-white/40 ml-2">第 {{ overview.recovery.batches||0 }} 批 · 本轮尚未投放 {{ number(state.future) }} 个历史任务</span></summary>
      <div class="mt-4"><AiRecoveryProgress :plan="overview.recovery" /></div>
    </details>
    <p v-if="overview.last_success" class="text-xs text-white/40 break-all leading-6 px-1">全库最近成功视频：{{ overview.last_success.path.split('/').pop() }} · {{ date(overview.last_success.created) }}。成功时间不是服务心跳。</p>
  </div>
</template>

<script setup lang="ts">
import { dashboard } from '~/utils/ai-overview.mjs'
const props=defineProps<{overview:Record<string,any>,updatedAt:number,progressError:string}>()
defineEmits<{navigate:[tab:string]}>()
const all=ref(false),clock=ref(Date.now())
const scopes=[{all:false,label:'当前处理范围'},{all:true,label:'全库'}]
const stale=computed(()=>Boolean(props.progressError)||(props.updatedAt>0 && clock.value-props.updatedAt>20000))
const ready=computed(()=>Boolean(props.overview.videos))
const state=computed(()=>dashboard(props.overview,all.value,stale.value))
const number=(value:unknown)=>Math.max(0,Number(value)||0).toLocaleString()
const date=(value:number)=>new Date(value*1000).toLocaleString()
let timer:ReturnType<typeof setInterval>|undefined
onMounted(()=>{timer=setInterval(()=>clock.value=Date.now(),1000)})
onBeforeUnmount(()=>{if(timer)clearInterval(timer)})
</script>
