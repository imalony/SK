<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import {
    AlertTriangle, Check, Download, Film, ImagePlus, LoaderCircle, MessageSquareText,
    History, Music2, Pencil, Play, Send, Settings2, Sparkles, Square, Subtitles, Trash2, Video, Volume2, X
  } from '@lucide/svelte'

  const apiBase = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'
  const voices = [
    { id: 'zh-CN-XiaoxiaoNeural', label: '晓晓 · 女声 / 亲和自然' },
    { id: 'zh-CN-XiaoyiNeural', label: '晓伊 · 女声 / 活力促销' },
    { id: 'zh-CN-YunxiNeural', label: '云希 · 男声 / 年轻清晰' },
    { id: 'zh-CN-YunjianNeural', label: '云健 · 男声 / 稳重专业' }
  ]

  type Plan = {
    id: string
    version: number
    approved_at?: number | null
    created_at?: number
    plan: {
      title: string
      strategy: string
      visual_bible?: {
        product_identity?: string[]
        art_direction?: string
        lighting_and_palette?: string
        continuity_rules?: string[]
        negative_constraints?: string[]
      }
      voiceover_script: string
      post_caption: string
      hashtags: string[]
      segments: Array<{ asset_index: number; duration_seconds: number; purpose: string; motion: string; prompt: string; voiceover_beat?: string }>
      warning?: string
    }
  }

  type Project = {
    id: string
    brief: string
    status: string
    video_provider_id: string
    video_resolution?: string | null
    video_fps: number
    video_provider?: { id: string; label: string; model: string; kind: string }
    llm_base_url?: string | null
    llm_model?: string | null
    llm_api?: string | null
    llm_trace?: Array<{ base_url: string; model: string; api: string; stage: string; recorded_at: number }>
    target_duration_seconds: number
    voice_enabled: boolean
    subtitle_enabled: boolean
    bgm_enabled: boolean
    bgm_id: string
    voice_id: string
    output_url?: string
    master_output_url?: string
    error_message?: string
    reference_video_url?: string
    reference_analysis?: {
      visual_style?: string
      shot_structure?: string[]
      camera_language?: string
      editing_rhythm?: string
      color_lighting?: string
      sound_mood?: string
      generation_prompt?: string
      negative_prompt?: string
      adaptation_notes?: string
    }
    plans: Plan[]
    assets: Array<{ id: string; filename: string; url: string }>
    segments: Array<{
      sequence_number: number
      status: string
      target_duration_seconds: number
      prompt?: string
      review?: Record<string, unknown> | null
      retry_count?: number
      output_url?: string
      generation?: { status: string; progress: number; error_message?: string }
    }>
    runs: Array<{ stage: string; progress: number; status: string; details?: { sequence?: number; total?: number } }>
    final_versions: Array<{
      version: number
      output_url: string
      voiceover_script: string
      post_caption: string
      hashtags: string[]
      voice_enabled: boolean
      subtitle_enabled: boolean
      bgm_enabled: boolean
      bgm_id: string
      voice_id: string
      created_at: number
    }>
  }

  type VideoProvider = {
    id: string
    label: string
    model: string
    enabled: boolean
    available: boolean
    capabilities: string[]
    requires_payment_confirmation: boolean
    resolution_options: string[]
    default_resolution: string
    supports_custom_fps: boolean
    default_fps: number
    min_fps: number
    max_fps: number
  }

  type AdModelSettings = {
    video_provider_id: string
    llm_base_url: string
    llm_model: string
    llm_api_key_configured: boolean
    llm_api: string
  }

  type HistoryProject = {
    id: string
    brief: string
    title: string
    status: string
    error_message?: string
    target_duration_seconds: number
    output_url?: string
    master_output_url?: string
    segment_count: number
    completed_segment_count: number
    final_version_count: number
    created_at: number
    completed_at?: number
    video_provider_id: string
    video_provider_label: string
    video_provider_model: string
    video_resolution?: string | null
    video_fps: number
    llm_model?: string | null
    llm_api?: string | null
  }

  const activeProjectStorageKey = 'sk2-ad-active-project-id'
  const adDraftStorageKey = 'sk2-ad-composer-draft'
  let files: File[] = []
  let previews: string[] = []
  let referenceVideo: File | null = null
  let brief = ''
  let duration = 15
  let voiceEnabled = true
  let subtitleEnabled = true
  let bgmEnabled = true
  let voiceId = voices[0].id
  let project: Project | null = null
  let feedback = ''
  let replanFromSegment = 2
  let replanFeedback = ''
  let loading = false
  let savingShotPrompts = false
  let rewritingAllShotPrompts = false
  let rewritingShotIndex: number | null = null
  let shotRewriteInstructions: string[] = []
  let previewing = false
  let error = ''
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let editorPlanId = ''
  let finalVoiceover = ''
  let finalCaption = ''
  let finalHashtags = ''
  let finalVoiceEnabled = true
  let finalSubtitleEnabled = true
  let finalBgmEnabled = true
  let finalVoiceId = voices[0].id
  let finalBgmId = 'default/ambient'
  let finalCopyInstruction = ''
  let rewritingFinalCopy = false
  let videoProviders: VideoProvider[] = []
  let videoProviderId = 'local-wan-vace'
  let videoResolution = ''
  let videoFps = 8
  let showModelSettings = false
  let settingsBaseUrl = 'https://fjbigmodel.fjdac.cn/v1'
  let settingsModel = 'gpt-5.5'
  let settingsApiKey = ''
  let llmApiKeyConfigured = false
  let savingModelSettings = false
  let showHistory = false
  let historyLoading = false
  let historyItems: HistoryProject[] = []
  let historyError = ''
  let deletingHistoryProjectId: string | null = null
  let showPaymentConfirmation = false
  let paymentConfirmingPlanVersion: number | null = null
  let draftStorageReady = false

  const bgmOptions = [
    { id: 'default/ambient', label: '环境氛围' },
    { id: 'gym/energetic', label: '动感节奏' },
    { id: 'ktv/upbeat', label: '轻快活力' },
    { id: 'restaurant/mellow', label: '温和舒缓' }
  ]

  $: latestPlan = project?.plans?.[0] ?? null
  $: activeVideoProvider = videoProviders.find(
    (provider) => provider.id === (project?.video_provider_id ?? videoProviderId)
  )
  $: videoResolutionOptions = activeVideoProvider?.resolution_options ?? []
  $: if (!project && videoResolutionOptions.length && !videoResolutionOptions.includes(videoResolution)) {
    videoResolution = activeVideoProvider?.default_resolution || videoResolutionOptions[0]
  }
  $: if (!project && !videoResolutionOptions.length && videoResolution) {
    videoResolution = ''
  }
  $: running = Boolean(project && ['approved', 'generating_segments', 'reviewing_segments', 'composing_audio_video'].includes(project.status))
  $: completed = project?.status === 'completed'
  $: activeSegment = project?.segments.find((segment) => segment.generation && !['succeeded', 'failed'].includes(segment.generation.status))
  $: activeRun = project?.runs.find((run) => run.status === 'running')
  $: progressPercent = Math.round(((activeSegment?.generation?.progress ?? activeRun?.progress ?? 0) * 100))
  $: if (latestPlan && latestPlan.id !== editorPlanId) {
    editorPlanId = latestPlan.id
    finalVoiceover = latestPlan.plan.voiceover_script
    finalCaption = latestPlan.plan.post_caption
    finalHashtags = latestPlan.plan.hashtags.join(' ')
    finalVoiceEnabled = project?.voice_enabled ?? true
    finalSubtitleEnabled = project?.subtitle_enabled ?? true
    finalBgmEnabled = project?.bgm_enabled ?? true
    finalVoiceId = project?.voice_id ?? voices[0].id
    finalBgmId = project?.bgm_id ?? 'default/ambient'
  }
  $: if (project?.id && typeof localStorage !== 'undefined') {
    localStorage.setItem(activeProjectStorageKey, project.id)
  }
  $: if (draftStorageReady && typeof localStorage !== 'undefined') {
    localStorage.setItem(adDraftStorageKey, JSON.stringify({
      brief,
      duration,
      voiceEnabled,
      subtitleEnabled,
      bgmEnabled,
      voiceId,
      videoProviderId,
      videoResolution,
      videoFps
    }))
  }

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${apiBase}${path}`, init)
    if (!response.ok) {
      const data = await response.json().catch(() => null)
      throw new Error(data?.detail || `请求失败 (${response.status})`)
    }
    return response.json()
  }

  function continuationLabel(provider: VideoProvider): string {
    return (
      provider.capabilities.includes('video_continue')
      || provider.capabilities.includes('video_edit')
    )
      ? '支持接续视频'
      : '不支持接续视频'
  }

  function updatePreviews(next: File[]) {
    previews.forEach((url) => URL.revokeObjectURL(url))
    files = next
    previews = next.map((file) => URL.createObjectURL(file))
  }

  function selectFiles(event: Event) {
    const chosen = Array.from((event.currentTarget as HTMLInputElement).files ?? [])
    updatePreviews([...files, ...chosen].slice(0, 8))
  }

  function removeFile(index: number) {
    updatePreviews(files.filter((_, itemIndex) => itemIndex !== index))
  }

  function selectReferenceVideo(event: Event) {
    referenceVideo = (event.currentTarget as HTMLInputElement).files?.[0] ?? null
  }

  function hydrateComposer(projectToOpen: Project) {
    brief = projectToOpen.brief || brief
    duration = projectToOpen.target_duration_seconds || duration
    voiceEnabled = projectToOpen.voice_enabled
    subtitleEnabled = projectToOpen.subtitle_enabled
    bgmEnabled = projectToOpen.bgm_enabled
    voiceId = projectToOpen.voice_id || voiceId
    videoProviderId = projectToOpen.video_provider_id || videoProviderId
    videoResolution = projectToOpen.video_resolution || ''
    videoFps = projectToOpen.video_fps || videoFps
  }

  async function createPlan() {
    error = ''
    if (brief.trim().length < 3) return error = '请填写简短的产品或活动说明。'
    loading = true
    try {
      const created = project?.status === 'draft'
        ? project
        : await request<Project>('/api/ad-projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            brief, target_duration_seconds: duration, voice_enabled: voiceEnabled,
            subtitle_enabled: subtitleEnabled, bgm_enabled: bgmEnabled, voice_id: voiceId,
            video_provider_id: videoProviderId,
            video_resolution: videoResolution || null,
            video_fps: videoFps
          })
        })
      if (files.length > 0 && created.assets.length === 0) {
        const form = new FormData()
        files.forEach((file) => form.append('files', file))
        await request<Project>(`/api/ad-projects/${created.id}/assets`, { method: 'POST', body: form })
      }
      if (referenceVideo) {
        const form = new FormData()
        form.append('file', referenceVideo)
        await request<Project>(`/api/ad-projects/${created.id}/reference-video`, { method: 'POST', body: form })
      }
      project = await request<Project>(`/api/ad-projects/${created.id}/plan`, { method: 'POST' })
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '生成方案失败'
    } finally {
      loading = false
    }
  }

  async function saveModelSettings() {
    error = ''
    savingModelSettings = true
    try {
      const settings = await request<AdModelSettings>('/api/ad-settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_provider_id: videoProviderId,
          llm_base_url: settingsBaseUrl,
          llm_model: settingsModel,
          llm_api_key: settingsApiKey || null
        })
      })
      videoProviderId = settings.video_provider_id
      settingsBaseUrl = settings.llm_base_url
      settingsModel = settings.llm_model
      llmApiKeyConfigured = settings.llm_api_key_configured
      settingsApiKey = ''
      showModelSettings = false
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Unable to save model settings'
    } finally {
      savingModelSettings = false
    }
  }

  async function revisePlan() {
    if (!project || feedback.trim().length < 2) return
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/plan-feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feedback })
      })
      feedback = ''
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '修改方案失败'
    } finally {
      loading = false
    }
  }

  async function revisePlanFromSegment() {
    if (!project || !latestPlan || replanFeedback.trim().length < 2) return
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/plan-from-segment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          from_segment: replanFromSegment,
          feedback: replanFeedback
        })
      })
      replanFeedback = ''
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '重新分镜失败'
    } finally {
      loading = false
    }
  }

  async function approvePlan() {
    if (!project || !latestPlan) return
    loading = true
    error = ''
    try {
      await persistShotPrompts()
      if (activeVideoProvider?.requires_payment_confirmation) {
        paymentConfirmingPlanVersion = latestPlan.version
        showPaymentConfirmation = true
        return
      }
      await submitPlanApproval(false)
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '确认方案失败'
    } finally {
      loading = false
    }
  }

  async function submitPlanApproval(paymentConfirmed: boolean) {
    if (!project || !latestPlan) return
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/plan-approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version: paymentConfirmingPlanVersion ?? latestPlan.version,
          payment_confirmed: paymentConfirmed
        })
      })
      showPaymentConfirmation = false
      paymentConfirmingPlanVersion = null
      startPolling()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '确认方案失败'
    } finally {
      loading = false
    }
  }

  function cancelPaymentConfirmation() {
    showPaymentConfirmation = false
    paymentConfirmingPlanVersion = null
  }

  async function persistShotPrompts() {
    if (!project || !latestPlan) return
    project = await request<Project>(`/api/ad-projects/${project.id}/plan-prompts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        version: latestPlan.version,
        prompts: latestPlan.plan.segments.map((segment) => segment.prompt)
      })
    })
  }

  async function saveShotPrompts() {
    if (!project || !latestPlan) return
    savingShotPrompts = true
    error = ''
    try {
      await persistShotPrompts()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '保存镜头提示词失败'
    } finally {
      savingShotPrompts = false
    }
  }

  async function rewriteShotPrompt(index: number, prompt: string) {
    if (!project || !latestPlan) return
    rewritingShotIndex = index
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/plan-prompt-rewrite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version: latestPlan.version,
          segment_index: index,
          current_prompt: prompt,
          instruction: shotRewriteInstructions[index] ?? ''
        })
      })
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'AI 改写镜头提示词失败'
    } finally {
      rewritingShotIndex = null
    }
  }

  async function rewriteAllShotPrompts() {
    if (!project || !latestPlan) return
    rewritingAllShotPrompts = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/plan-prompts-rewrite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version: latestPlan.version,
          instruction: feedback
        })
      })
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'AI 总体改写镜头提示词失败'
    } finally {
      rewritingAllShotPrompts = false
    }
  }

  async function refreshProject() {
    if (!project) return
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}`)
      if (['completed', 'failed', 'cancelled'].includes(project.status)) stopPolling()
    } catch {
      // Temporary network errors should not erase an active project on screen.
    }
  }

  function startPolling() {
    stopPolling()
    pollTimer = setInterval(refreshProject, 3000)
    void refreshProject()
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer)
    pollTimer = null
  }

  async function stopProject() {
    if (!project) return
    loading = true
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/stop`, { method: 'POST' })
      stopPolling()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '停止失败'
    } finally {
      loading = false
    }
  }

  async function resumeFailedProject() {
    if (!project) return
    if (
      activeVideoProvider?.requires_payment_confirmation
      && !window.confirm('将从失败镜头继续提交付费视频任务，已成功的前段不会重新生成。确认继续？')
    ) return
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          payment_confirmed: Boolean(activeVideoProvider?.requires_payment_confirmation)
        })
      })
      startPolling()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '恢复生成失败'
    } finally {
      loading = false
    }
  }

  async function returnToPlan() {
    if (!project) return
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/return-to-plan`, { method: 'POST' })
      stopPolling()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '返回方案失败'
    } finally {
      loading = false
    }
  }

  async function recomposeFinal() {
    if (!project) return
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${project.id}/final-edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          voiceover_script: finalVoiceover,
          post_caption: finalCaption,
          hashtags: finalHashtags.split(/\s+/).filter(Boolean),
          voice_enabled: finalVoiceEnabled,
          subtitle_enabled: finalSubtitleEnabled,
          bgm_enabled: finalBgmEnabled,
          bgm_id: finalBgmId,
          voice_id: finalVoiceId
        })
      })
      startPolling()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '重新合成失败'
    } finally {
      loading = false
    }
  }

  async function rewriteFinalCopy() {
    if (!project) return
    rewritingFinalCopy = true
    error = ''
    try {
      const result = await request<{
        voiceover_script: string
        post_caption: string
        hashtags: string[]
      }>(`/api/ad-projects/${project.id}/final-copy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction: finalCopyInstruction })
      })
      finalVoiceover = result.voiceover_script
      finalCaption = result.post_caption
      finalHashtags = result.hashtags.join(' ')
      finalVoiceEnabled = true
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'AI 重写配音与配文失败'
    } finally {
      rewritingFinalCopy = false
    }
  }

  async function previewVoice() {
    if (!brief.trim()) return error = '先填写简短说明，再试听配音。'
    previewing = true
    error = ''
    try {
      if (!project) {
        project = await request<Project>('/api/ad-projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            brief, target_duration_seconds: duration, voice_enabled: voiceEnabled,
            subtitle_enabled: subtitleEnabled, bgm_enabled: bgmEnabled, voice_id: voiceId,
            video_provider_id: videoProviderId,
            video_resolution: videoResolution || null,
            video_fps: videoFps
          })
        })
      }
      const result = await request<{ url: string }>(`/api/ad-projects/${project.id}/voice-preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice_id: voiceId })
      })
      const audio = new Audio(`${apiBase}${result.url}`)
      await audio.play()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '试听失败'
    } finally {
      previewing = false
    }
  }

  function reset() {
    stopPolling()
    project = null
    localStorage.removeItem(activeProjectStorageKey)
    feedback = ''
    replanFeedback = ''
    replanFromSegment = 2
    error = ''
  }

  function historyDate(timestamp?: number) {
    if (!timestamp) return ''
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    }).format(new Date(timestamp * 1000))
  }

  function projectStatusLabel(status: string): string {
    return ({
      draft: '草稿',
      planning: '正在规划',
      waiting_user_confirmation: '等待确认',
      approved: '等待执行',
      generating_segments: '生成分镜中',
      reviewing_segments: '复盘分镜中',
      composing_audio_video: '正在合成',
      interrupted: '服务中断，可继续',
      failed: '失败，可继续',
      cancelled: '已停止，可继续',
      completed: '已完成'
    } as Record<string, string>)[status] ?? status
  }

  function projectProgress(item: HistoryProject): string {
    if (!item.segment_count) return item.status === 'waiting_user_confirmation' ? '方案待确认' : '尚未生成分镜'
    return `${item.completed_segment_count}/${item.segment_count} 段已完成`
  }

  function llmStageLabel(stage: string): string {
    return ({
      reference_video_analysis: '参考视频拆解',
      plan_generation: '总体规划与分镜',
      segment_prompt_rewrite: '单镜提示词改写',
      plan_prompt_rewrite: '整体提示词改写',
      segment_transition: '镜头衔接决策',
      segment_review: '镜头审片',
      final_copy_rewrite: '成片配文改写'
    } as Record<string, string>)[stage] ?? stage
  }

  function isActiveProjectStatus(status: string): boolean {
    return ['approved', 'generating_segments', 'reviewing_segments', 'composing_audio_video'].includes(status)
  }

  async function openHistory() {
    showHistory = true
    historyLoading = true
    historyError = ''
    try {
      const result = await request<{ items: HistoryProject[] }>('/api/ad-projects/history')
      historyItems = result.items
    } catch (cause) {
      historyError = cause instanceof Error ? cause.message : '无法加载历史作品'
    } finally {
      historyLoading = false
    }
  }

  async function openHistoryProject(projectId: string) {
    loading = true
    error = ''
    try {
      project = await request<Project>(`/api/ad-projects/${projectId}`)
      hydrateComposer(project)
      editorPlanId = ''
      showHistory = false
      if (isActiveProjectStatus(project.status)) {
        startPolling()
      } else {
        stopPolling()
      }
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '无法打开历史作品'
    } finally {
      loading = false
    }
  }

  async function deleteHistoryProject(projectId: string) {
    if (!window.confirm('删除后会移除该任务的素材、分镜、视频及后期版本，且无法恢复。是否继续？')) return
    deletingHistoryProjectId = projectId
    historyError = ''
    try {
      await request<{ id: string; deleted: boolean }>(`/api/ad-projects/${projectId}`, { method: 'DELETE' })
      historyItems = historyItems.filter((item) => item.id !== projectId)
      if (project?.id === projectId) reset()
    } catch (cause) {
      historyError = cause instanceof Error ? cause.message : '删除任务失败'
    } finally {
      deletingHistoryProjectId = null
    }
  }

  onDestroy(() => {
    stopPolling()
    previews.forEach((url) => URL.revokeObjectURL(url))
  })

  onMount(async () => {
    try {
      const savedDraft = JSON.parse(localStorage.getItem(adDraftStorageKey) || '{}')
      if (typeof savedDraft.brief === 'string') brief = savedDraft.brief
      if (typeof savedDraft.duration === 'number') duration = savedDraft.duration
      if (typeof savedDraft.voiceEnabled === 'boolean') voiceEnabled = savedDraft.voiceEnabled
      if (typeof savedDraft.subtitleEnabled === 'boolean') subtitleEnabled = savedDraft.subtitleEnabled
      if (typeof savedDraft.bgmEnabled === 'boolean') bgmEnabled = savedDraft.bgmEnabled
      if (typeof savedDraft.voiceId === 'string') voiceId = savedDraft.voiceId
      if (typeof savedDraft.videoProviderId === 'string') videoProviderId = savedDraft.videoProviderId
      if (typeof savedDraft.videoResolution === 'string') videoResolution = savedDraft.videoResolution
      if (typeof savedDraft.videoFps === 'number') videoFps = savedDraft.videoFps
    } catch {
      localStorage.removeItem(adDraftStorageKey)
    }
    draftStorageReady = true
    try {
      const data = await request<{ default_provider_id: string; providers: VideoProvider[] }>('/api/providers')
      videoProviders = data.providers.filter((provider) => provider.enabled)
      videoProviderId = data.default_provider_id
    } catch {
      // The default local provider remains selected when the availability check is unavailable.
    }
    try {
      const settings = await request<AdModelSettings>('/api/ad-settings')
      videoProviderId = settings.video_provider_id
      settingsBaseUrl = settings.llm_base_url
      settingsModel = settings.llm_model
      llmApiKeyConfigured = settings.llm_api_key_configured
    } catch {
      // The built-in defaults remain usable until settings can be loaded.
    }
    const savedProjectId = localStorage.getItem(activeProjectStorageKey)
    if (savedProjectId) {
      await openHistoryProject(savedProjectId)
    }
  })
</script>

<main class="ad-page">
  <header class="ad-header">
    <a class="ad-brand" href="/"><Film size={21} /> 广告成片</a>
    <div class="header-actions">
      <button class="header-history-command" on:click={openHistory}><History size={17} /> 任务与历史</button>
      <button class="header-icon-command" title="模型设置" aria-label="模型设置" on:click={() => showModelSettings = true}>
        <Settings2 size={18} />
      </button>
      <a class="test-link" href="/test">技术测试台</a>
    </div>
  </header>

  <section class="ad-shell">
    {#if !project || project.status === 'draft'}
      <div class="intro">
        <p class="eyebrow">抖音竖屏广告</p>
        <h1>用几张图片做一支广告视频</h1>
        <p>可上传商品、门店或人物素材，也可只填写说明。系统先给出整体方案，确认后才会开始生成视频。</p>
      </div>

      <div class="maker-grid">
        <section class="form-section">
          <label class="section-label" for="ad-images">图片素材</label>
          <label class="drop-zone" for="ad-images">
            <ImagePlus size={24} />
            <span>添加图片素材（可选）</span>
            <small>支持 PNG、JPG、WEBP，最多 8 张</small>
            <input id="ad-images" type="file" accept="image/png,image/jpeg,image/webp" multiple on:change={selectFiles} />
          </label>
          {#if previews.length}
            <div class="image-grid">
              {#each previews as src, index}
                <figure>
                  <img src={src} alt={`素材 ${index + 1}`} />
                  <button aria-label={`删除素材 ${index + 1}`} on:click={() => removeFile(index)}>×</button>
                </figure>
              {/each}
            </div>
          {/if}

          <label class="section-label" for="reference-video">参考视频（可选）</label>
          <label class="reference-upload" for="reference-video">
            <Video size={20} />
            <span>{referenceVideo ? referenceVideo.name : '上传想模仿节奏和画面语言的视频'}</span>
            <small>系统会拆解镜头、运镜、色彩和剪辑节奏，不会复制原视频内容。</small>
            <input id="reference-video" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska" on:change={selectReferenceVideo} />
          </label>

          <label class="section-label" for="brief">产品或活动说明</label>
          <textarea id="brief" bind:value={brief} placeholder="例如：夏季新品冰咖啡，主打低糖、现萃和第二杯半价，风格清爽有活力。"></textarea>
        </section>

        <aside class="settings-section">
          <label class="duration-field">
            <span class="section-label">成片时长</span>
            <div>
              <input type="number" bind:value={duration} min="5" max="120" step="1" aria-label="成片时长（秒）" />
              <span>秒</span>
            </div>
            <small>5 至 120 秒，系统会自动拆成短镜头生成。</small>
          </label>

          <label class="toggle-row"><Volume2 size={18} /><span>配音</span><input type="checkbox" bind:checked={voiceEnabled} /></label>
          {#if voiceEnabled}
            <div class="voice-row">
              <select bind:value={voiceId}>
                {#each voices as voice}<option value={voice.id}>{voice.label}</option>{/each}
              </select>
              <button class="icon-command" title="试听音色" aria-label="试听音色" disabled={previewing} on:click={previewVoice}>
                {#if previewing}<span class="spin"><LoaderCircle size={18} /></span>{:else}<Play size={18} />{/if}
              </button>
            </div>
          {/if}
          <label class="toggle-row"><Subtitles size={18} /><span>添加字幕</span><input type="checkbox" bind:checked={subtitleEnabled} /></label>
          <label class="toggle-row"><Music2 size={18} /><span>背景音乐</span><input type="checkbox" bind:checked={bgmEnabled} /></label>
          <button class="primary-command" disabled={loading} on:click={createPlan}>
            {#if loading}<span class="spin"><LoaderCircle size={18} /></span> 正在生成方案{:else}<MessageSquareText size={18} /> 生成广告方案{/if}
          </button>
        </aside>
      </div>
    {:else if project.status === 'waiting_user_confirmation' && latestPlan}
      <div class="stage-header">
        <div><p class="eyebrow">第 {latestPlan.version} 版方案</p><h1>{latestPlan.plan.title}</h1></div>
        <button class="text-command" on:click={reset}>重新开始</button>
      </div>
      <section class="plan-layout">
          <div class="plan-content">
            <div class="strategy"><span>创意方向</span><p>{latestPlan.plan.strategy}</p></div>
            {#if latestPlan.plan.visual_bible}
              <section class="reference-analysis">
                <span>全片视觉圣经</span>
                <p>{latestPlan.plan.visual_bible.art_direction}</p>
                <div class="reference-notes">
                  <p><strong>主体锚点：</strong>{latestPlan.plan.visual_bible.product_identity?.join('；')}</p>
                  <p><strong>光色：</strong>{latestPlan.plan.visual_bible.lighting_and_palette}</p>
                  <p><strong>连续性：</strong>{latestPlan.plan.visual_bible.continuity_rules?.join('；')}</p>
                  <p><strong>禁止变化：</strong>{latestPlan.plan.visual_bible.negative_constraints?.join('；')}</p>
                </div>
              </section>
            {/if}
          {#if project.reference_analysis}
            <section class="reference-analysis">
              <span>参考视频拆解</span>
              <p>{project.reference_analysis.visual_style}</p>
              <div class="reference-notes">
                <p><strong>运镜与节奏：</strong>{project.reference_analysis.camera_language} {project.reference_analysis.editing_rhythm}</p>
                <p><strong>光色：</strong>{project.reference_analysis.color_lighting}</p>
                <p><strong>适配建议：</strong>{project.reference_analysis.adaptation_notes}</p>
              </div>
              <p class="prompt-label">参考视频风格提示词</p>
              <code>{project.reference_analysis.generation_prompt}</code>
            </section>
          {/if}
          <div class="shot-list">
            {#each latestPlan.plan.segments as segment, index}
              <article class="shot">
                <span class="shot-index">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <strong>{segment.purpose}</strong>
                  <p>{segment.motion}</p>
                  <label class="shot-prompt-editor">
                    <span>视频模型提示词</span>
                    <textarea bind:value={segment.prompt} maxlength="3000"></textarea>
                  </label>
                  {#if voiceEnabled && segment.voiceover_beat}
                    <p class="shot-voiceover"><strong>该镜配音：</strong>{segment.voiceover_beat}</p>
                  {/if}
                  <div class="shot-ai-rewrite">
                    <input bind:value={shotRewriteInstructions[index]} maxlength="1000" placeholder="AI 改写要求（可选）" />
                    <button class="secondary-command" disabled={loading || savingShotPrompts || rewritingAllShotPrompts || rewritingShotIndex !== null} on:click={() => rewriteShotPrompt(index, segment.prompt)}>
                      {#if rewritingShotIndex === index}<span class="spin"><LoaderCircle size={16} /></span> 正在改写{:else}<Sparkles size={16} /> AI 改写{/if}
                    </button>
                  </div>
                </div>
                <time>{segment.duration_seconds} 秒</time>
              </article>
            {/each}
          </div>
          {#if voiceEnabled}
            <div class="script"><span>配音文案</span><p>{latestPlan.plan.voiceover_script}</p></div>
          {/if}
        </div>
        <aside class="confirm-panel">
          {#if latestPlan.plan.warning}<p class="plan-warning">{latestPlan.plan.warning}</p>{/if}
          <label class="section-label" for="feedback">整体调整或重新分镜要求</label>
          <textarea id="feedback" bind:value={feedback} placeholder="例如：开场更快进入卖点，增加产品特写，结尾保留行动召唤。"></textarea>
          <button class="secondary-command" disabled={loading || feedback.trim().length < 2} on:click={revisePlan}>
            <Sparkles size={17} /> AI 重新分镜
          </button>
          {#if latestPlan.plan.segments.length > 1}
            <div class="replan-from-segment">
              <label><span>从第几镜开始重新分镜</span><input bind:value={replanFromSegment} type="number" min="2" max={latestPlan.plan.segments.length} /></label>
              <textarea bind:value={replanFeedback} placeholder="说明后半段需要如何调整；此前镜头会被冻结并复用已生成的视频。"></textarea>
              <button class="secondary-command" disabled={loading || replanFeedback.trim().length < 2} on:click={revisePlanFromSegment}>
                <Sparkles size={17} /> 从此镜重新分镜
              </button>
            </div>
          {/if}
          <button class="secondary-command" disabled={loading || savingShotPrompts || rewritingAllShotPrompts} on:click={rewriteAllShotPrompts}>
            {#if rewritingAllShotPrompts}<span class="spin"><LoaderCircle size={17} /></span> 正在总体改写{:else}<Sparkles size={17} /> AI 总体改写镜头提示词{/if}
          </button>
          <button class="secondary-command" disabled={loading || savingShotPrompts || rewritingAllShotPrompts} on:click={saveShotPrompts}>
            {#if savingShotPrompts}<span class="spin"><LoaderCircle size={17} /></span> 正在保存提示词{:else}<Pencil size={17} /> 保存镜头提示词{/if}
          </button>
          <button class="approve-command" disabled={loading || savingShotPrompts || rewritingAllShotPrompts || rewritingShotIndex !== null} on:click={approvePlan}>
            <Check size={18} /> 确认方案并开始生成
          </button>
          <small>确认后系统会自动完成分镜、续接、配音和合成。</small>
        </aside>
      </section>
    {:else if completed && (project.master_output_url || project.output_url)}
      <div class="stage-header">
        <div><p class="eyebrow">已完成</p><h1>广告视频已生成</h1></div>
        <button class="text-command" on:click={reset}>制作新视频</button>
      </div>
      <section class="result-layout">
        <video controls playsinline src={`${apiBase}${project.master_output_url ?? project.output_url}`}><track kind="captions" srclang="zh" label="中文字幕" /></video>
        <div class="result-copy">
          <span>视频母版</span>
          <p>仅包含已拼接的视频画面，不含配音、字幕和背景音乐。</p>
          <a class="download-command" href={`${apiBase}${project.master_output_url ?? project.output_url}`} download><Download size={18} /> 下载视频母版</a>
        </div>
      </section>
      <section class="final-versions">
        <div><p class="eyebrow">配音配文版本</p><h2>已导出的成片版本</h2></div>
        {#if project.final_versions.length}
          <div class="final-version-list">
            {#each project.final_versions as version}
              <article class="final-version">
                <video controls playsinline src={`${apiBase}${version.output_url}`}><track kind="captions" srclang="zh" label="中文字幕" /></video>
                <div>
                  <strong>版本 {version.version}</strong>
                  <p>{version.post_caption}</p>
                  <p class="version-voice">{version.voice_enabled ? version.voiceover_script : '未启用配音'}</p>
                  <small>{version.voice_enabled ? '已配音' : '无配音'} · {version.subtitle_enabled ? '含字幕' : '无字幕'} · {version.bgm_enabled ? '含背景音乐' : '无背景音乐'}</small>
                </div>
                <a class="secondary-command" href={`${apiBase}${version.output_url}`} download><Download size={17} /> 下载版本</a>
              </article>
            {/each}
          </div>
        {:else}
          <p class="no-final-versions">此项目尚未按新版结构保存后期版本。</p>
        {/if}
      </section>
      <section class="artifact-panel">
        <details open>
          <summary>模型来源与调用记录</summary>
          <div class="artifact-body">
            <p><strong>视频模型：</strong>{project.video_provider?.label ?? project.video_provider_id} · {project.video_provider?.model ?? '配置已不在当前列表'} · {project.video_resolution ?? '原生分辨率'} · {project.video_fps} FPS</p>
            <p><strong>规划与审片模型：</strong>{project.llm_model ? `${project.llm_model} · ${project.llm_api ?? 'responses'} · ${project.llm_base_url ?? ''}` : '尚未调用规划模型或该记录创建于模型追踪上线前'}</p>
            {#if project.llm_trace?.length}
              <div class="model-trace">
                {#each project.llm_trace as trace}
                  <span>{historyDate(trace.recorded_at)} · {llmStageLabel(trace.stage)} · {trace.model} · {trace.api}</span>
                {/each}
              </div>
            {/if}
          </div>
        </details>
        <details>
          <summary>总体方案、分镜与提示词（{project.plans.length} 个版本）</summary>
          <div class="artifact-body artifact-plan-list">
            {#each project.plans as plan}
              <article class="artifact-plan">
                <strong>方案 {plan.version}{plan.approved_at ? '（已确认）' : ''}</strong>
                <p>{plan.plan.strategy}</p>
                {#if plan.plan.visual_bible}
                  <p class="artifact-muted">视觉方向：{plan.plan.visual_bible.art_direction}</p>
                  <p class="artifact-muted">光色：{plan.plan.visual_bible.lighting_and_palette}</p>
                {/if}
                <p><strong>配音文案：</strong>{plan.plan.voiceover_script || '未启用配音'}</p>
                <p><strong>发布文案：</strong>{plan.plan.post_caption}</p>
                <p><strong>话题：</strong>{plan.plan.hashtags.join(' ')}</p>
                <div class="artifact-shots">
                  {#each plan.plan.segments as segment, index}
                    <div>
                      <strong>镜头 {String(index + 1).padStart(2, '0')} · {segment.duration_seconds} 秒</strong>
                      <p>{segment.purpose} · {segment.motion}</p>
                      {#if segment.voiceover_beat}<p class="artifact-muted">配音节拍：{segment.voiceover_beat}</p>{/if}
                      <pre class="artifact-prompt">{segment.prompt}</pre>
                    </div>
                  {/each}
                </div>
              </article>
            {/each}
          </div>
        </details>
        <details>
          <summary>实际生成镜头与审片记录（{project.segments.length} 条）</summary>
          <div class="artifact-body artifact-segment-list">
            {#each project.segments as segment}
              <article class="artifact-segment">
                <strong>镜头 {String(segment.sequence_number).padStart(2, '0')} · {segment.status} · 第 {Number(segment.retry_count ?? 0) + 1} 次尝试</strong>
                {#if segment.prompt}<pre class="artifact-prompt">{segment.prompt}</pre>{/if}
                {#if segment.review}<pre class="artifact-review">{JSON.stringify(segment.review, null, 2)}</pre>{/if}
              </article>
            {/each}
          </div>
        </details>
      </section>
      <section class="post-edit">
        <div><p class="eyebrow">后期调整</p><h2>AI 重写配音与配文</h2></div>
        <p>AI 会复盘当前成片的关键画面并重写文案，不会重新生成视频画面。确认后仅重新配音、字幕、背景音乐和导出成片。</p>
        <div class="copy-rewrite">
          <label><span>调整要求（可选）</span><input bind:value={finalCopyInstruction} maxlength="1000" placeholder="例如：语气更有冲击力，突出第二杯半价，适合晚间发布。" /></label>
          <button class="secondary-command" disabled={rewritingFinalCopy || loading} on:click={rewriteFinalCopy}>
            {#if rewritingFinalCopy}<span class="spin"><LoaderCircle size={17} /></span> 正在复盘成片{:else}<Sparkles size={17} /> AI 重写配音与配文{/if}
          </button>
        </div>
        <div class="post-edit-grid">
          <label><span>配音文案</span><textarea bind:value={finalVoiceover} disabled={!finalVoiceEnabled}></textarea></label>
          <label><span>发布文案</span><textarea bind:value={finalCaption}></textarea></label>
          <label><span>话题标签</span><input bind:value={finalHashtags} placeholder="#好物推荐 #品质生活" /></label>
          <label><span>音色</span><select bind:value={finalVoiceId} disabled={!finalVoiceEnabled}>{#each voices as voice}<option value={voice.id}>{voice.label}</option>{/each}</select></label>
          <label><span>背景音乐</span><select bind:value={finalBgmId} disabled={!finalBgmEnabled}>{#each bgmOptions as track}<option value={track.id}>{track.label}</option>{/each}</select></label>
        </div>
        <div class="post-edit-options">
          <label><input type="checkbox" bind:checked={finalVoiceEnabled} /> 配音</label>
          <label><input type="checkbox" bind:checked={finalSubtitleEnabled} /> 字幕</label>
          <label><input type="checkbox" bind:checked={finalBgmEnabled} /> 背景音乐</label>
          <button class="secondary-command" disabled={loading || rewritingFinalCopy || (finalVoiceEnabled && !finalVoiceover.trim())} on:click={recomposeFinal}><Pencil size={17} /> 应用并重新合成</button>
        </div>
      </section>
    {:else if project.status === 'failed' || project.status === 'cancelled' || project.status === 'interrupted'}
      <section class="run-screen terminal-screen">
        <AlertTriangle size={36} color="#b54f42" />
        <p class="eyebrow">{project.status === 'interrupted' ? '服务中断' : project.status === 'failed' ? '生成失败' : '任务已停止'}</p>
        <h1>{project.status === 'interrupted' ? '可从已完成的步骤继续' : project.status === 'failed' ? '广告视频未能生成完成' : '广告视频制作已停止'}</h1>
        <p class="run-error">{project.error_message || '任务已停止，未继续执行。'}</p>
        <p>不会继续调用视频模型或消耗本地资源。</p>
        {#if project.plans.length}
          <button class="secondary-command return-plan-command" disabled={loading} on:click={returnToPlan}>返回已确认文案</button>
        {/if}
        <button class="approve-command retry-command" disabled={loading} on:click={resumeFailedProject}>从未完成步骤继续</button>
        <button class="primary-command retry-command" on:click={reset}>重新制作</button>
      </section>
    {:else}
      <section class="run-screen">
        <span class="spin run-spinner"><LoaderCircle size={34} /></span>
        <p class="eyebrow">正在制作</p>
        <h1>{project.status === 'composing_audio_video' ? '正在合成配音和成片' : '正在自动生成广告视频'}</h1>
        <p>系统正在按已确认的方案生成分镜，并根据画面自动续接和调整。</p>
        <div class="generation-progress">
          <div><span>{project.status === 'composing_audio_video' ? '正在合成' : activeSegment ? `正在生成第 ${activeSegment.sequence_number} 段` : '正在准备任务'}</span><strong>{progressPercent}%</strong></div>
          <i><b style={`width: ${progressPercent}%`}></b></i>
          {#if activeSegment?.generation?.status}<small>模型状态：{activeSegment.generation.status}</small>{/if}
        </div>
        <div class="progress-list">
          {#each project.segments as segment}
            <div class:done={segment.status === 'succeeded'}><span>{String(segment.sequence_number).padStart(2, '0')}</span><strong>{segment.status === 'succeeded' ? '分镜已完成' : segment.generation?.status === 'running' ? '模型生成中' : '等待处理'}</strong><small>{segment.generation ? `${Math.round(segment.generation.progress * 100)}%` : `${Math.round(segment.target_duration_seconds)} 秒`}</small></div>
          {/each}
        </div>
        <button class="secondary-command return-plan-command" disabled={loading} on:click={returnToPlan}>返回已确认文案</button>
        <button class="stop-command" disabled={loading} on:click={stopProject}><Square size={16} /> 停止并释放资源</button>
      </section>
    {/if}

    {#if error}<p class="error-message">{error}</p>{/if}
  </section>

  {#if showHistory}
    <div class="modal-backdrop" role="presentation" on:click={(event) => event.target === event.currentTarget && (showHistory = false)}>
      <div class="history-modal" role="dialog" aria-modal="true" aria-labelledby="history-title">
        <div class="modal-header">
          <div>
            <p class="eyebrow">持久化任务</p>
            <h2 id="history-title">全部任务与作品</h2>
          </div>
          <button class="header-icon-command" title="关闭" aria-label="关闭" on:click={() => showHistory = false}><X size={18} /></button>
        </div>
        {#if historyLoading}
          <div class="history-empty"><span class="spin"><LoaderCircle size={24} /></span><p>正在加载任务</p></div>
        {:else if historyError}
          <div class="history-empty"><AlertTriangle size={24} color="#b54f42" /><p>{historyError}</p></div>
        {:else if !historyItems.length}
          <div class="history-empty"><Film size={26} /><p>暂无任务记录</p></div>
        {:else}
          <div class="history-list">
            {#each historyItems as item}
              <article class="history-item">
                {#if item.output_url}
                  <video muted preload="metadata" playsinline src={`${apiBase}${item.output_url}`}></video>
                {:else}
                  <div class="history-placeholder"><Film size={24} /></div>
                {/if}
                <div>
                  <strong>{item.title} <span class:complete-status={item.status === 'completed'} class:recoverable-status={['failed', 'cancelled', 'interrupted'].includes(item.status)} class="task-status">{projectStatusLabel(item.status)}</span></strong>
                  <p>{item.brief}</p>
                  <small>{Math.round(item.target_duration_seconds)} 秒 · {projectProgress(item)} · {item.final_version_count} 个后期版本 · {historyDate(item.completed_at ?? item.created_at)}</small>
                  <small class="history-models">视频：{item.video_provider_label} · {item.video_provider_model || '未记录型号'} · {item.video_resolution ?? '原生分辨率'} · {item.video_fps} FPS</small>
                  <small class="history-models">规划/审片：{item.llm_model ? `${item.llm_model} · ${item.llm_api ?? 'responses'}` : '尚未调用或历史记录未追踪'}</small>
                </div>
                <div class="history-actions">
                  <button class="secondary-command" disabled={loading || deletingHistoryProjectId !== null} on:click={() => openHistoryProject(item.id)}>{item.status === 'completed' ? '打开并调整' : '打开任务'}</button>
                  <button class="header-icon-command delete-history-command" title="删除任务" aria-label="删除任务" disabled={loading || deletingHistoryProjectId !== null} on:click={() => deleteHistoryProject(item.id)}>
                    {#if deletingHistoryProjectId === item.id}<span class="spin"><LoaderCircle size={16} /></span>{:else}<Trash2 size={16} />{/if}
                  </button>
                </div>
              </article>
            {/each}
          </div>
        {/if}
      </div>
    </div>
  {/if}

  {#if showModelSettings}
    <div class="modal-backdrop" role="presentation" on:click={(event) => event.target === event.currentTarget && !savingModelSettings && (showModelSettings = false)}>
      <div class="model-settings-modal" role="dialog" aria-modal="true" aria-labelledby="model-settings-title" tabindex="-1">
        <div class="modal-header">
          <div>
            <p class="eyebrow">生成配置</p>
            <h2 id="model-settings-title">模型设置</h2>
          </div>
          <button class="header-icon-command" title="关闭" aria-label="关闭" disabled={savingModelSettings} on:click={() => showModelSettings = false}>
            <X size={18} />
          </button>
        </div>
        <div class="modal-fields">
          <label>
            <span>视频模型</span>
            <select bind:value={videoProviderId}>
              {#each videoProviders as provider}
                <option value={provider.id}>
                  {provider.label} · {provider.model} · {continuationLabel(provider)}{provider.available ? '' : '（当前不可用）'}
                </option>
              {/each}
            </select>
            {#if videoProviders.length === 0}<small>暂未读取到可用的视频模型配置。</small>{/if}
          </label>
          {#if videoResolutionOptions.length}
            <label>
              <span>视频分辨率</span>
              <select bind:value={videoResolution}>
                {#each videoResolutionOptions as option}
                  <option value={option}>{option}</option>
                {/each}
              </select>
            </label>
          {/if}
          <label>
            <span>视频帧率 (FPS)</span>
            <input
              type="number"
              min={activeVideoProvider?.min_fps ?? 4}
              max={activeVideoProvider?.max_fps ?? 24}
              step="1"
              bind:value={videoFps}
              disabled={!activeVideoProvider?.supports_custom_fps}
            />
            <small>
              {#if activeVideoProvider?.supports_custom_fps}
                本地模型可设置 {activeVideoProvider.min_fps}-{activeVideoProvider.max_fps} FPS。更高帧率会增加显存占用和生成时间。
              {:else}
                此云端模型按原生帧率输出（{activeVideoProvider?.default_fps ?? 8} FPS），无需设置。
              {/if}
            </small>
          </label>
          <label>
            <span>规划与提示词大模型地址</span>
            <input type="url" bind:value={settingsBaseUrl} placeholder="https://.../v1" />
          </label>
          <label>
            <span>规划与提示词模型</span>
            <input bind:value={settingsModel} placeholder="gpt-5.5" />
          </label>
          <label>
            <span>大模型 API Key</span>
            <input type="password" bind:value={settingsApiKey} placeholder={llmApiKeyConfigured ? '已配置；留空不会修改' : '输入 API Key'} autocomplete="off" />
            <small>接口类型固定为 Responses API。密钥仅保存在后端，不会返回到页面。</small>
          </label>
        </div>
        <div class="modal-actions">
          <button class="modal-cancel" disabled={savingModelSettings} on:click={() => showModelSettings = false}>取消</button>
          <button class="approve-command modal-save" disabled={savingModelSettings || !videoProviderId || !settingsBaseUrl.trim() || !settingsModel.trim()} on:click={saveModelSettings}>
            {#if savingModelSettings}<span class="spin"><LoaderCircle size={17} /></span> 正在保存{:else}保存设置{/if}
          </button>
        </div>
      </div>
    </div>
  {/if}

  {#if showPaymentConfirmation}
    <div class="modal-backdrop" role="presentation" on:click={(event) => event.target === event.currentTarget && !loading && cancelPaymentConfirmation()}>
      <div class="model-settings-modal payment-confirmation-modal" role="dialog" aria-modal="true" aria-labelledby="payment-confirmation-title">
        <div class="modal-header">
          <div>
            <p class="eyebrow">付费任务确认</p>
            <h2 id="payment-confirmation-title">确认提交视频生成？</h2>
          </div>
          <button class="header-icon-command" title="关闭" aria-label="关闭" disabled={loading} on:click={cancelPaymentConfirmation}>
            <X size={18} />
          </button>
        </div>
        <div class="payment-confirmation-copy">
          <AlertTriangle size={22} />
          <p>将使用 <strong>{activeVideoProvider?.label ?? '所选云端视频模型'}</strong> 提交 {latestPlan?.plan.segments.length ?? 0} 个视频生成任务。该 Provider 按量计费，提交后会产生云端费用。</p>
          <small>确认后系统才会调用视频接口。取消会保留当前总体规划和全部镜头提示词。</small>
        </div>
        <div class="modal-actions">
          <button class="modal-cancel" disabled={loading} on:click={cancelPaymentConfirmation}>返回修改</button>
          <button class="approve-command modal-save" disabled={loading} on:click={() => submitPlanApproval(true)}>
            {#if loading}<span class="spin"><LoaderCircle size={17} /></span> 正在提交{:else}确认付费并生成{/if}
          </button>
        </div>
      </div>
    </div>
  {/if}
</main>

<style>
  :global(body) { background: #f5f7f6; color: #15201b; }
  .ad-page { min-height: 100vh; background: #f5f7f6; }
  .ad-header { height: 62px; display: flex; align-items: center; justify-content: space-between; padding: 0 max(24px, calc((100vw - 1180px) / 2)); border-bottom: 1px solid #d9e1dc; background: #fff; }
  .ad-brand { display: inline-flex; align-items: center; gap: 9px; color: #123e37; font-size: 17px; font-weight: 800; text-decoration: none; }
  .header-actions { display: flex; align-items: center; gap: 9px; }
  .header-history-command { min-height: 34px; display: inline-flex; align-items: center; gap: 6px; padding: 0 10px; border: 1px solid #cad8d1; border-radius: 5px; color: #386055; background: #fff; font-size: 13px; font-weight: 750; }
  .header-history-command:hover { color: #176b5c; border-color: #82a99c; background: #f3f8f5; }
  .header-icon-command { width: 34px; height: 34px; display: grid; place-items: center; padding: 0; border: 1px solid #cad8d1; border-radius: 5px; color: #386055; background: #fff; }
  .header-icon-command:hover:not(:disabled) { color: #176b5c; border-color: #82a99c; background: #f3f8f5; }
  .test-link, .text-command { color: #587169; font-size: 13px; text-decoration: none; border: 0; background: transparent; }
  .ad-shell { width: min(1180px, calc(100% - 40px)); margin: 0 auto; padding: 62px 0 80px; }
  .intro { max-width: 680px; margin-bottom: 40px; }
  .eyebrow { margin: 0 0 9px; color: #25836f; font-size: 12px; font-weight: 800; letter-spacing: 0; }
  h1 { margin: 0; color: #15201b; font-size: 32px; line-height: 1.2; letter-spacing: 0; }
  .intro > p:last-child, .run-screen > p:not(.eyebrow), .strategy p, .script p { color: #61746c; line-height: 1.7; }
  .maker-grid, .plan-layout, .result-layout { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(290px, .85fr); gap: 32px; }
  .form-section, .settings-section, .plan-content, .confirm-panel, .result-copy { min-width: 0; }
  .section-label { display: block; margin: 0 0 10px; color: #374c44; font-size: 13px; font-weight: 750; }
  .drop-zone { min-height: 168px; display: grid; place-content: center; justify-items: center; gap: 7px; border: 1px dashed #91afa4; border-radius: 7px; color: #3d7568; background: #fcfefd; cursor: pointer; }
  .drop-zone small { color: #83958e; font-size: 12px; } .drop-zone input { display: none; }
  .reference-upload { min-height: 66px; display: grid; grid-template-columns: 22px 1fr; column-gap: 9px; align-items: center; padding: 10px 12px; border: 1px dashed #9eb8af; border-radius: 6px; color: #456a60; background: #fbfdfc; cursor: pointer; } .reference-upload span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; } .reference-upload small { grid-column: 2; color: #82938c; font-size: 11px; line-height: 1.4; } .reference-upload input { display: none; }
  .image-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin: 12px 0 24px; }
  figure { position: relative; aspect-ratio: 1; margin: 0; overflow: hidden; border-radius: 5px; background: #e2e9e4; }
  figure img { width: 100%; height: 100%; object-fit: cover; } figure button { position: absolute; top: 5px; right: 5px; width: 22px; height: 22px; padding: 0; border: 0; border-radius: 50%; color: #fff; background: rgba(20,31,27,.72); font-size: 17px; line-height: 1; }
  textarea { width: 100%; min-height: 130px; padding: 12px; border: 1px solid #cbd7d0; border-radius: 6px; color: #1e2d27; background: #fff; resize: vertical; outline: none; line-height: 1.55; }
  textarea:focus, select:focus { border-color: #167461; box-shadow: 0 0 0 3px rgba(22,116,97,.12); }
  .settings-section { align-self: start; display: grid; gap: 20px; padding-left: 28px; border-left: 1px solid #d9e1dc; }
  .duration-field { display: grid; gap: 7px; }
  .duration-field > div { display: flex; align-items: center; gap: 8px; }
  .duration-field input { width: 94px; height: 37px; padding: 0 9px; border: 1px solid #cbd7d0; border-radius: 5px; color: #1e2d27; background: #fff; font: inherit; }
  .duration-field input:focus { border-color: #167461; box-shadow: 0 0 0 3px rgba(22,116,97,.12); outline: none; }
  .duration-field > div span { color: #40544c; font-size: 14px; }
  .duration-field small { color: #718278; font-size: 12px; line-height: 1.45; }
  .toggle-row { display: grid; grid-template-columns: 24px 1fr auto; align-items: center; color: #40544c; font-size: 14px; } input[type="checkbox"] { width: 17px; height: 17px; accent-color: #176b5c; }
  .voice-row { display: grid; grid-template-columns: 1fr 38px; gap: 8px; margin-top: -10px; } select { height: 37px; min-width: 0; padding: 0 8px; border: 1px solid #cbd7d0; border-radius: 5px; background: #fff; }
  .icon-command { display: grid; place-items: center; border: 1px solid #9fbab0; border-radius: 5px; color: #176b5c; background: #fff; }
  .primary-command, .approve-command, .secondary-command, .stop-command, .download-command { min-height: 43px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; border-radius: 5px; font-weight: 750; text-decoration: none; }
  .primary-command { width: 100%; margin-top: 8px; border: 0; color: #fff; background: #176b5c; } .primary-command:hover, .approve-command:hover { background: #105649; }
  .stage-header { display: flex; align-items: start; justify-content: space-between; gap: 20px; margin-bottom: 32px; }
  .strategy, .script { padding: 18px 0; border-top: 1px solid #d8e1dc; } .strategy span, .script span, .result-copy > span { color: #4e655c; font-size: 13px; font-weight: 800; }
  .strategy p, .script p { margin: 7px 0 0; white-space: pre-wrap; }
  .reference-analysis { padding: 18px 0; border-top: 1px solid #d8e1dc; } .reference-analysis > span { color: #4e655c; font-size: 13px; font-weight: 800; } .reference-analysis > p { margin: 7px 0; color: #50645b; line-height: 1.6; } .reference-notes { display: grid; gap: 5px; color: #6a7c74; font-size: 13px; line-height: 1.5; } .reference-notes p { margin: 0; } .prompt-label { margin: 14px 0 0; color: #4e655c; font-size: 12px; font-weight: 800; } .reference-analysis code { display: block; margin-top: 7px; padding: 10px; overflow-wrap: anywhere; border-left: 3px solid #3a8b77; color: #35564b; background: #eef6f1; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; line-height: 1.5; white-space: pre-wrap; }
  .shot-list { border-top: 1px solid #d8e1dc; } .shot { display: grid; grid-template-columns: 42px 1fr auto; gap: 12px; padding: 17px 0; border-bottom: 1px solid #d8e1dc; } .shot-index { color: #2c8c76; font-size: 12px; font-weight: 800; } .shot strong { font-size: 15px; } .shot p { margin: 5px 0 0; color: #71827a; font-size: 13px; line-height: 1.45; } .shot time { color: #5c7068; font-size: 13px; white-space: nowrap; }
  .shot-prompt-editor { display: grid; gap: 6px; margin-top: 11px; } .shot-prompt-editor span { color: #4e655c; font-size: 12px; font-weight: 800; } .shot-prompt-editor textarea { min-height: 96px; padding: 9px; color: #426157; border-color: #c6ddd3; background: #f5faf7; font-size: 12px; line-height: 1.55; }
  .shot-ai-rewrite { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; margin-top: 8px; } .shot-ai-rewrite input { min-width: 0; height: 36px; padding: 0 9px; border: 1px solid #cbd7d0; border-radius: 5px; color: #29453b; background: #fff; outline: none; font-size: 12px; } .shot-ai-rewrite input:focus { border-color: #167461; box-shadow: 0 0 0 3px rgba(22,116,97,.12); } .shot-ai-rewrite .secondary-command { width: auto; min-width: 108px; min-height: 36px; margin: 0; padding: 0 10px; font-size: 12px; }
  .confirm-panel { align-self: start; padding-left: 28px; border-left: 1px solid #d9e1dc; } .confirm-panel textarea { min-height: 118px; } .plan-warning { padding: 10px; color: #85611f; background: #fff7e3; font-size: 12px; line-height: 1.5; }
  .replan-from-segment { display: grid; gap: 9px; margin-top: 14px; padding-top: 14px; border-top: 1px solid #d8e1dc; } .replan-from-segment label { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: #4b6158; font-size: 12px; font-weight: 750; } .replan-from-segment input { width: 68px; height: 34px; padding: 0 8px; border: 1px solid #cbd7d0; border-radius: 5px; background: #fff; } .replan-from-segment textarea { min-height: 82px; }
  .secondary-command { width: 100%; margin-top: 10px; border: 1px solid #a6b9b0; color: #365248; background: #fff; } .approve-command { width: 100%; margin-top: 12px; border: 0; color: #fff; background: #176b5c; } .confirm-panel small { display: block; margin-top: 12px; color: #7a8d84; font-size: 12px; line-height: 1.5; }
  .run-screen { max-width: 620px; margin: 72px auto; text-align: center; } .run-spinner { color: #23816e; margin-bottom: 15px; } .generation-progress { margin: 24px 0 0; text-align: left; } .generation-progress > div { display: flex; justify-content: space-between; color: #52685e; font-size: 13px; font-weight: 750; } .generation-progress i { display: block; height: 7px; margin-top: 8px; overflow: hidden; border-radius: 4px; background: #dce8e1; } .generation-progress b { display: block; height: 100%; min-width: 3px; border-radius: inherit; background: #23816e; transition: width .3s ease; } .generation-progress small { display: block; margin-top: 7px; color: #7a8d84; font-size: 12px; } .progress-list { margin: 30px 0; border-top: 1px solid #d8e1dc; text-align: left; } .progress-list > div { display: grid; grid-template-columns: 35px 1fr auto; padding: 13px 2px; border-bottom: 1px solid #d8e1dc; color: #63756d; } .progress-list .done { color: #25836f; } .progress-list span { font-size: 12px; font-weight: 800; } .progress-list strong { font-size: 13px; } .progress-list small { font-size: 12px; }
  .return-plan-command { width: auto; min-width: 154px; margin: 0 10px 0 0; padding: 0 14px; } .stop-command { padding: 0 16px; border: 1px solid #d7a69d; color: #a54236; background: #fff; } .run-error, .error-message { color: #aa3e32; } .error-message { margin-top: 22px; padding: 10px 12px; border: 1px solid #edc7c0; background: #fff4f1; border-radius: 5px; font-size: 13px; }
  .terminal-screen .run-error { max-width: 620px; margin: 20px auto 8px; padding: 12px; border: 1px solid #edc7c0; border-radius: 5px; background: #fff4f1; line-height: 1.6; } .terminal-screen > p:not(.eyebrow):not(.run-error) { color: #61746c; } .retry-command { width: auto; min-width: 154px; margin-top: 12px; padding: 0 16px; }
  .result-layout video { width: min(100%, 480px); max-height: 720px; background: #111; border-radius: 6px; } .result-copy { padding-top: 10px; } .download-command { width: 100%; margin-bottom: 26px; color: #fff; background: #176b5c; } .result-copy p { color: #52665d; line-height: 1.65; }
  .final-versions { margin-top: 42px; padding-top: 26px; border-top: 1px solid #d8e1dc; }
  .final-versions h2 { margin: 0; font-size: 20px; }
  .final-version-list { display: grid; gap: 14px; margin-top: 18px; }
  .final-version { display: grid; grid-template-columns: 124px minmax(0, 1fr) auto; gap: 16px; align-items: center; padding: 12px 0; border-top: 1px solid #e0e8e3; }
  .final-version:first-child { border-top: 0; }
  .final-version video { width: 124px; aspect-ratio: 9 / 16; max-height: 178px; border-radius: 5px; background: #17211d; }
  .final-version strong { display: block; color: #24372f; font-size: 15px; }
  .final-version p { margin: 6px 0; color: #61746c; font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
  .final-version .version-voice { color: #406157; }
  .final-version small { color: #809087; font-size: 12px; }
  .final-version .secondary-command { width: auto; min-width: 112px; margin: 0; padding: 0 12px; }
  .no-final-versions { margin: 16px 0 0; color: #74857d; font-size: 13px; }
  .artifact-panel { margin-top: 42px; border-top: 1px solid #d8e1dc; }
  .artifact-panel details { border-bottom: 1px solid #d8e1dc; }
  .artifact-panel summary { padding: 16px 0; color: #29473d; cursor: pointer; font-size: 15px; font-weight: 800; }
  .artifact-body { padding: 0 0 18px; color: #53685f; font-size: 13px; line-height: 1.6; }
  .artifact-body > p { margin: 7px 0; }
  .artifact-muted { color: #74877e; }
  .model-trace { display: grid; gap: 5px; margin-top: 12px; }
  .model-trace span { display: block; padding: 7px 9px; border-left: 3px solid #67a48f; color: #4b655a; background: #f2f8f4; font-size: 12px; }
  .artifact-plan-list, .artifact-segment-list { display: grid; gap: 16px; }
  .artifact-plan, .artifact-segment { padding: 13px 0; border-top: 1px solid #e0e8e3; }
  .artifact-plan:first-child, .artifact-segment:first-child { border-top: 0; }
  .artifact-plan > p { margin: 7px 0; white-space: pre-wrap; }
  .artifact-shots { display: grid; gap: 10px; margin-top: 12px; }
  .artifact-shots > div { padding: 10px; border-left: 3px solid #c5ddd2; background: #f8fbf9; }
  .artifact-shots p { margin: 5px 0; }
  .artifact-prompt, .artifact-review { margin: 8px 0 0; padding: 9px; overflow: auto; border: 1px solid #d9e6df; border-radius: 4px; color: #3d5e52; background: #f5faf7; font: 12px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
  .artifact-review { color: #4b5f57; background: #fbfcfb; }
  .post-edit { display: grid; grid-template-columns: minmax(0, .85fr) minmax(0, 1.4fr); gap: 18px 32px; margin-top: 48px; padding-top: 26px; border-top: 1px solid #d8e1dc; } .post-edit h2 { margin: 0; font-size: 20px; } .post-edit > p { margin: 0; color: #667970; line-height: 1.6; } .post-edit-grid { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; } .post-edit-grid label { display: grid; gap: 7px; color: #4b6158; font-size: 13px; font-weight: 750; } .post-edit-grid textarea { min-height: 92px; } .post-edit-grid input { height: 38px; padding: 0 10px; border: 1px solid #cbd7d0; border-radius: 5px; background: #fff; outline: none; } .post-edit-options { grid-column: 1 / -1; display: flex; align-items: center; flex-wrap: wrap; gap: 16px; } .post-edit-options label { display: inline-flex; align-items: center; gap: 6px; color: #52665d; font-size: 13px; } .post-edit-options .secondary-command { width: auto; min-width: 178px; margin: 0 0 0 auto; padding: 0 14px; }
  .copy-rewrite { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: end; padding: 14px; border: 1px solid #d9e6df; border-radius: 6px; background: #f7fbf8; }
  .copy-rewrite label { display: grid; gap: 7px; color: #4b6158; font-size: 13px; font-weight: 750; }
  .copy-rewrite input { width: 100%; height: 38px; padding: 0 10px; border: 1px solid #cbd7d0; border-radius: 5px; color: #1e2d27; background: #fff; outline: none; }
  .copy-rewrite input:focus { border-color: #167461; box-shadow: 0 0 0 3px rgba(22,116,97,.12); }
  .copy-rewrite .secondary-command { width: auto; min-width: 182px; margin: 0; padding: 0 14px; }
  .spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }
  .modal-backdrop { position: fixed; z-index: 20; inset: 0; display: grid; place-items: center; padding: 24px; background: rgba(20, 35, 29, .42); }
  .model-settings-modal { width: min(100%, 560px); max-height: min(720px, calc(100vh - 48px)); overflow: auto; padding: 24px; border: 1px solid #cad8d1; border-radius: 7px; background: #fff; box-shadow: 0 18px 48px rgba(16, 42, 33, .22); }
  .history-modal { width: min(100%, 820px); max-height: min(760px, calc(100vh - 48px)); overflow: auto; padding: 24px; border: 1px solid #cad8d1; border-radius: 7px; background: #fff; box-shadow: 0 18px 48px rgba(16, 42, 33, .22); }
  .history-empty { min-height: 220px; display: grid; place-content: center; justify-items: center; gap: 10px; color: #667970; text-align: center; }
  .history-empty p { margin: 0; font-size: 14px; }
  .history-list { display: grid; gap: 12px; padding-top: 18px; }
  .history-item { display: grid; grid-template-columns: 118px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 11px; border: 1px solid #dce5e0; border-radius: 6px; }
  .history-item video { width: 118px; aspect-ratio: 9 / 16; max-height: 150px; object-fit: cover; border-radius: 4px; background: #17211d; }
  .history-placeholder { width: 118px; aspect-ratio: 9 / 16; display: grid; place-items: center; border-radius: 4px; color: #779087; background: #e8eeea; }
  .history-item strong { display: block; color: #24372f; font-size: 15px; }
  .history-item p { display: -webkit-box; margin: 5px 0 8px; overflow: hidden; color: #667970; font-size: 13px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; }
  .history-item small { display: block; color: #809087; font-size: 12px; }
  .history-models { margin-top: 4px; color: #5d756b !important; }
  .history-item .secondary-command { width: auto; min-width: 108px; margin: 0; padding: 0 12px; }
  .history-actions { display: flex; align-items: center; gap: 7px; }
  .history-actions .secondary-command { min-width: 108px; }
  .delete-history-command { flex: 0 0 34px; color: #a54236; border-color: #dfb8b1; }
  .delete-history-command:hover:not(:disabled) { color: #8e3328; border-color: #c98880; background: #fff4f1; }
  .task-status { display: inline-flex; align-items: center; margin-left: 7px; padding: 2px 6px; border-radius: 4px; color: #5f7169; background: #edf1ef; font-size: 11px; font-weight: 750; vertical-align: middle; }
  .task-status.complete-status { color: #1d735f; background: #e4f3ec; }
  .task-status.recoverable-status { color: #9b5925; background: #fff0df; }
  .modal-header { display: flex; align-items: start; justify-content: space-between; gap: 18px; padding-bottom: 20px; border-bottom: 1px solid #dce5e0; }
  .modal-header .eyebrow { margin-bottom: 5px; } .modal-header h2 { margin: 0; color: #15201b; font-size: 22px; }
  .modal-fields { display: grid; gap: 16px; padding: 20px 0; }
  .modal-fields label { display: grid; gap: 7px; color: #40544c; font-size: 13px; font-weight: 750; }
  .modal-fields input { width: 100%; height: 38px; padding: 0 10px; border: 1px solid #cbd7d0; border-radius: 5px; color: #1e2d27; background: #fff; outline: none; }
  .modal-fields input:focus { border-color: #167461; box-shadow: 0 0 0 3px rgba(22,116,97,.12); }
  .modal-fields small { color: #7a8d84; font-size: 12px; font-weight: 400; line-height: 1.5; }
  .modal-actions { display: flex; justify-content: end; gap: 10px; padding-top: 18px; border-top: 1px solid #dce5e0; }
  .payment-confirmation-copy { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 10px; margin: 20px 0; padding: 14px; border: 1px solid #e6c98d; border-radius: 6px; color: #76521d; background: #fff9eb; }
  .payment-confirmation-copy p { margin: 0; line-height: 1.65; }
  .payment-confirmation-copy small { grid-column: 2; color: #876a38; line-height: 1.5; }
  .modal-cancel { min-height: 40px; padding: 0 16px; border: 1px solid #a6b9b0; border-radius: 5px; color: #365248; background: #fff; font-weight: 750; }
  .modal-save { width: auto; min-width: 106px; margin: 0; padding: 0 16px; }
  @media (max-width: 760px) { .ad-header { padding: 0 20px; } .header-history-command { width: 34px; justify-content: center; padding: 0; font-size: 0; } .ad-shell { width: min(100% - 32px, 1180px); padding-top: 38px; } h1 { font-size: 26px; } .maker-grid, .plan-layout, .result-layout, .post-edit { grid-template-columns: 1fr; gap: 28px; } .settings-section, .confirm-panel { padding-left: 0; border-left: 0; border-top: 1px solid #d9e1dc; padding-top: 24px; } .image-grid, .post-edit-grid { grid-template-columns: 1fr; } .shot-ai-rewrite { grid-template-columns: 1fr; } .shot-ai-rewrite .secondary-command { width: 100%; } .copy-rewrite { grid-template-columns: 1fr; } .copy-rewrite .secondary-command { width: 100%; } .post-edit-options .secondary-command, .return-plan-command { width: 100%; margin: 0 0 10px; } .final-version { grid-template-columns: 82px minmax(0, 1fr); } .final-version video, .history-placeholder { width: 82px; max-height: 120px; } .final-version .secondary-command { grid-column: 1 / -1; width: 100%; } .history-item { grid-template-columns: 76px minmax(0, 1fr); } .history-item video, .history-item .history-placeholder { width: 76px; max-height: 108px; } .history-actions { grid-column: 1 / -1; } .history-actions .secondary-command { width: 100%; } .history-item video, .history-item .history-placeholder { width: 76px; max-height: 108px; } .modal-backdrop { padding: 14px; } .model-settings-modal, .history-modal { padding: 18px; } }
</style>
