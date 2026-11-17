<template>
  <section class="card p-5 sm:p-6" aria-label="实时处理状态">
    <div class="flex flex-wrap justify-between items-center gap-3">
      <h2 class="text-lg font-semibold text-white">实时处理状态</h2>
      <span class="text-xs" :class="stale?'text-amber-200':'text-blue-200'">{{ stale ? '监测数据暂未刷新' : (activity.active?.length || 0)+' 项活动 · 随页面每 5 秒更新' }}</span>
    </div>
    <p v-if="stale" class="text-xs text-amber-200/80 leading-6 mt-3">以下为上次上报状态，不能据此确认任务此刻是否仍在处理。请检查页面与服务的连接。</p>
    <article v-for="job in activity.active || []" :key="job.kind+':'+job.id" class="rounded-xl border border-white/10 p-4 mt-4">
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs text-white/50">
        <span>{{ job.kind==='directory' ? '目录复查' : '视频任务' }} #{{ job.id }} · 扫描 #{{ job.run_id }}</span>
        <span class="text-blue-200">{{ stages[job.stage] || '正在处理' }}</span>
      </div>
      <h3 class="text-sm font-medium text-white break-all mt-3">{{ job.name }}</h3>
      <p v-if="job.kind!=='directory'" class="text-xs text-white/40 mt-2">源文件 {{ bytes(job.size) }}<span v-if="job.duration"> · 全片 {{ duration(job.duration) }}</span><span v-if="job.cache_hit"> · 已复用压缩证据缓存</span></p>
      <dl class="grid grid-cols-2 lg:grid-cols-4 gap-4 mt-5 text-xs">
        <div><dt class="text-white/40">任务已耗时</dt><dd class="text-white/80 mt-2">{{ duration(job.elapsed_seconds) }}</dd></div>
        <div><dt class="text-white/40">本阶段已耗时</dt><dd class="text-white/80 mt-2">{{ duration(job.stage_elapsed_seconds) }}</dd></div>
        <div><dt class="text-white/40">累计源数据读取</dt><dd class="text-white/80 mt-2" title="包含视频定位与重复读取，可能大于源文件大小">{{ bytes(job.network_bytes) }}</dd></div>
        <div><dt class="text-white/40">最近实际进展</dt><dd class="text-white/80 mt-2">{{ ago(job.progress_age_seconds) }}</dd></div>
      </dl>
      <div v-if="job.stage_percent !== null && job.stage_percent !== undefined" class="mt-5">
        <div class="flex flex-wrap justify-between gap-2 text-xs text-white/60 mb-2">
          <span>{{ job.kind==='directory' ? '本页目录项处理' : '当前阶段视频时轴进度' }} {{ Number(job.stage_percent).toFixed(1) }}%</span>
          <span>{{ job.kind==='directory' ? (job.processed_entries||0)+' / '+(job.total_entries||0)+' 项' : duration(job.out_seconds)+' / '+duration(job.duration) }}</span>
        </div>
        <div role="progressbar" :aria-label="job.kind==='directory'?'本页目录项处理':'当前阶段视频时轴进度'" :aria-valuenow="Number(job.stage_percent)" aria-valuemin="0" aria-valuemax="100" class="h-2 rounded-full bg-white/10 overflow-hidden"><div class="h-full rounded-full bg-blue-400 transition-all motion-reduce:transition-none" :style="{width:Number(job.stage_percent)+'%'}"></div></div>
        <p class="text-xs text-white/40 mt-2">这是当前阶段的进度；压缩完成后还需要模型识别和结果保存。</p>
      </div>
      <p v-if="job.process" class="text-xs text-white/50 leading-6 mt-3">
        {{ job.process==='subtitle_ocr' ? '字幕识别' : (job.process.toLowerCase().startsWith('ffmpeg') ? '视频编码器' : '视频解析器') }}状态检查：{{ ago(job.heartbeat_age_seconds) }}
        <span v-if="job.output_bytes"> · 当前输出 {{ bytes(job.output_bytes) }}</span>
        <span v-if="job.frame"> · 已输出 {{ Math.round(job.frame).toLocaleString() }} 帧</span>
        <span v-if="job.fps"> · {{ Number(job.fps).toFixed(1) }} 帧/秒</span>
        <span v-if="job.speed"> · {{ Number(job.speed).toFixed(2) }} 倍速</span>
      </p>
      <p v-if="job.stage?.startsWith('model_')" class="text-xs text-blue-200/80 leading-6 mt-3">正在等待远端模型响应，期间不提供百分比。第 {{ job.model_request || 1 }} 次模型调用，当前第 {{ job.model_attempt || 1 }} 次尝试<span v-if="job.compressed_bytes">，压缩视频 {{ bytes(job.compressed_bytes) }}</span><span v-if="job.timeout_seconds">；本次读取超时设置 {{ job.timeout_seconds }} 秒</span>。</p>
      <p v-else-if="['reading','listing','probe'].includes(job.stage)" class="text-xs text-white/50 leading-6 mt-3">正在等待网盘数据或解析视频信息；长时间没有新增数据时，会按配置的超时结束本次请求并记录原因。</p>
      <p v-else-if="job.progress_age_seconds>60" class="text-xs text-amber-200/80 leading-6 mt-3">近 {{ Math.floor(job.progress_age_seconds) }} 秒未观察到新增数据或画面进度，可能正在等待网络或处理当前帧；这不等同于已卡死。</p>
    </article>
    <p v-if="!activity.active?.length" class="text-sm text-white/50 leading-7 mt-4">{{ running ? '队列记录中有识别中任务，尚未收到详细状态。' : (paused ? '队列已暂停，正在保留原有进度。' : (queued ? '队列正在等待领取下一条任务。' : '当前没有视频正在识别，后台会按恢复计划继续投放任务或复查目录。')) }}</p>
    <p v-if="lastSuccess" class="text-xs text-white/50 leading-6 mt-4 break-all">最近识别成功：{{ date(lastSuccess.created) }} · {{ lastSuccess.path.split('/').pop() }}</p>
    <details v-if="activity.recent?.length" class="mt-4 text-xs text-white/50">
      <summary class="cursor-pointer">最近处理记录</summary>
      <ul class="mt-3 space-y-3"><li v-for="item in activity.recent" :key="item.kind+':'+item.id+':'+item.finished_at" class="leading-6 break-all">{{ date(item.finished_at) }} · {{ outcomes[item.state] || item.state }} · {{ item.name }} · 耗时 {{ duration(item.elapsed_seconds) }}<span v-if="item.error" class="block text-white/40">{{ item.error }}</span></li></ul>
    </details>
  </section>
</template>

<script setup lang="ts">
type Row=Record<string,any>
const props=defineProps<{activity:Row,lastSuccess?:Row|null,queued:number,running:number,paused:boolean,updatedAt:number}>()
const clock=ref(Date.now())
const stale=computed(()=>props.updatedAt>0 && clock.value-props.updatedAt>20000)
let freshnessTimer:ReturnType<typeof setInterval>|undefined
onMounted(()=>{freshnessTimer=setInterval(()=>clock.value=Date.now(),1000)})
onBeforeUnmount(()=>{if(freshnessTimer)clearInterval(freshnessTimer)})
const stages:Record<string,string>={reading:'读取视频信息',listing:'读取网盘目录',indexing:'检查目录与登记文件',probe:'解析视频时长',decoding:'读取并生成视频中间文件',decode_subtitles:'读取视频并提取字幕',subtitle_layout:'探测画面字幕位置',encoding_analysis:'压缩分析（第一遍）',encoding:'压缩编码',validating:'校验完整视频时间轴',context_frames:'提取补充证据画面',sampling:'提取画面证据',cached:'复用已有证据',cover:'生成视频封面',model_video:'等待模型理解视频',model_text:'等待模型分析与整理结果',saving:'复核源文件并保存结果',publishing:'识别完成，保存封面与同步资料'}
const outcomes:Record<string,string>={done:'成功',failed:'失败',page_done:'已检查一页目录',queued:'等待继续',cancelled:'已取消'}
function duration(value:unknown){const seconds=Math.max(0,Math.floor(Number(value)||0));return seconds>=3600?`${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分 ${seconds%60} 秒`:`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`}
function bytes(value:unknown){const count=Math.max(0,Number(value)||0);return count>=1024**3?`${(count/1024**3).toFixed(2)} GiB`:`${(count/1024**2).toFixed(2)} MiB`}
function ago(value:unknown){return Number(value)<2?'刚刚':duration(value)+'前'}
function date(value:unknown){return new Date(Number(value)*1000).toLocaleString()}
</script>
