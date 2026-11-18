export function dashboard(data, all = false, stale = false) {
  const scope = !all && data.scopes ? data.scopes.included : data
  const videos = scope?.videos || {}, tasks = scope?.tasks || {}, dirs = scope?.directories || {}
  const total = Object.values(videos).reduce((sum, value) => sum + Number(value), 0)
  const done = Number(videos.done || 0), remaining = total - done
  const roots = data.recovery?.exclude_roots || []
  const active = (data.activity?.active || []).filter(job => all || !roots.some(root => root === '/' || job.path === root || job.path?.startsWith(root.replace(/\/$/, '') + '/')))
  const videoActive = active.filter(job => job.kind !== 'directory')
  const directoryActive = active.filter(job => job.kind === 'directory')
  const queue = Number(tasks.queued || 0), running = Number(tasks.running || 0)
  const directoryQueue = Number(dirs.queued || 0), directoryRunning = Number(dirs.running || 0)
  const plan = data.recovery || {}, future = Math.max(0, (plan.total_tasks || 0) - (plan.task_cursor || 0))
  let state = { code: 'idle', title: '等待启动扫描', detail: '配置扫描目录后，发现的视频会进入识别队列。', tone: 'neutral' }
  if (!data.videos) state = {code:'loading',title:'正在读取运行状态',detail:'尚未取得统计数据，暂不显示为零。',tone:'neutral'}
  else if (stale) state = {code:'stale',title:'状态更新中断',detail:'以下为上次取得的数据，无法确认后台此刻是否仍在推进。',tone:'warning'}
  else if (videoActive.length || running) state = {code:'processing',title:'视频识别进行中',detail:'视频准备和模型处理可以重叠进行；封面与协议资料在后台独立发布。',tone:'active'}
  else if (queue) state = {code:'queued',title:'视频已排队，等待领取',detail:`当前已有 ${queue} 个视频入队；尚未收到实际处理活动上报。`,tone:'warning'}
  else if (directoryQueue || directoryRunning || directoryActive.length) state = {
    code:'directories',title:directoryActive.length ? '正在检查目录，视频识别暂时空闲' : '等待目录检查推进',
    detail:`视频队列${queue ? '中还有 '+queue+' 个任务' : '为空'}；仍有 ${directoryQueue} 个目录待检查、${directoryRunning} 个标记为检查中。${plan.state === 'running' ? '当前恢复调度会等待目录检查结束，再投放下一批视频。' : '目录记录不等于正在调用模型。'}`,
    tone:directoryActive.length ? 'active' : 'warning'}
  else if (plan.state==='paused' || data.scans?.paused) state = {code:'paused',title:'任务已暂停',detail:'原进度与待处理记录已保留，恢复后继续执行。',tone:'neutral'}
  else if (plan.state==='running' && (future || remaining)) state = {code:'batch',title:'等待恢复计划投放下一批',detail:`当前视频队列为空，本轮还有 ${future} 个历史任务尚未调度。计划标记为运行中，不等于模型正在处理。`,tone:'warning'}
  else if (remaining) state = {code:'unresolved',title:'本轮暂未继续处理，仍有未成功视频',detail:'失败和未识别记录仍然保留；当前队列为空不代表这些视频已完成。',tone:'warning'}
  else if (total) state = {code:'complete',title:'当前范围已发现的视频均已识别',detail:'这表示现有视频记录已成功；不保证尚未扫描到的目录也已检查完毕。',tone:'neutral'}
  return { ...state, videos, total, done, remaining, queue, running, dirs, directoryQueue, directoryRunning, active,
    future, deferred:Object.values(data.scopes?.deferred?.videos || {}).reduce((sum,value)=>sum+Number(value),0),
    ratio:total ? Math.round(done/total*1000)/10 : 0 }
}
