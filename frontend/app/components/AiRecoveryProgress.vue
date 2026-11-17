<template>
  <section class="card p-5 sm:p-6" aria-label="失败恢复计划">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-lg font-semibold text-white">失败恢复计划</h2>
      <span class="text-xs text-blue-200">{{ stateLabel }} · 第 {{ plan.round || 1 }} / {{ plan.max_rounds || 1 }} 轮 · 已投放 {{ number(plan.batches) }} 批</span>
    </div>
    <dl class="grid grid-cols-2 lg:grid-cols-4 gap-5 mt-5">
      <div><dt class="text-xs text-white/50">纳入恢复的视频</dt><dd class="text-2xl text-white mt-2">{{ number(plan.total_tasks) }}</dd></div>
      <div><dt class="text-xs text-white/50">本轮尚未调度的视频</dt><dd class="text-2xl text-white mt-2">{{ number(Math.max(0,(plan.total_tasks||0)-(plan.task_cursor||0))) }}</dd></div>
      <div><dt class="text-xs text-white/50">本轮尚未调度的目录</dt><dd class="text-2xl text-white mt-2">{{ number(Math.max(0,(plan.total_directories||0)-(plan.directory_cursor||0))) }}</dd></div>
      <div><dt class="text-xs text-white/50">暂缓的视频</dt><dd class="text-2xl text-white mt-2">{{ number(plan.deferred_tasks) }}</dd></div>
    </dl>
    <p class="text-sm text-white/50 leading-6 mt-5">每批最多 {{ plan.batch_size || 0 }} 个视频、{{ plan.directory_batch_size || 0 }} 个目录。当前批次及其新发现的任务处理完后，自动投放后续批次；“已入队待识别”只统计当前队列。调度数量不代表识别成功数量。</p>
    <p v-if="plan.exclude_roots?.length" class="text-xs text-white/40 leading-5 mt-2 break-all">暂缓范围：{{ plan.exclude_roots.join('、') }}。失败记录保留，等待外部访问条件恢复。</p>
  </section>
</template>

<script setup lang="ts">
const props=defineProps<{plan:Record<string,any>}>()
const number=(value:unknown)=>Math.max(0,Number(value)||0).toLocaleString()
const stateLabels:Record<string,string>={running:'恢复中',paused:'已暂停',stopped:'已停止',done:'本计划已结束'}
const stateLabel=computed(()=>stateLabels[props.plan.state] || '恢复计划')
</script>
