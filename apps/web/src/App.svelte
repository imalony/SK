<script lang="ts">
  import { onMount } from 'svelte'
  import {
    Clapperboard,
    CircleStop,
    FastForward,
    ImagePlus,
    LoaderCircle,
    RefreshCw,
    Send,
    Settings2,
    Sparkles,
    Upload,
    Video,
    WandSparkles,
  } from '@lucide/svelte'

  type ServiceHealth = {
    services: { comfyui: boolean; ollama: boolean }
    providers: Record<string, boolean>
    queue_active: boolean
    model_runtime: {
      busy: boolean
      activity: string
    }
  }

  type VideoProvider = {
    id: string
    label: string
    kind: 'comfyui' | 'http-api' | 'agnes-video' | 'wanx-video'
    model: string
    enabled: boolean
    available: boolean
    capabilities: string[]
    requires_payment_confirmation: boolean
    resolution_options: string[]
    default_resolution: string
  }

  type Generation = {
    id: string
    parent_generation_id?: string | null
    mode: 'text' | 'image' | 'edit' | 'continue'
    prompt: string
    status: 'queued' | 'preparing' | 'running' | 'succeeded' | 'failed'
    progress: number
    output_url?: string
    error_message?: string
    config: {
      width: number
      height: number
      length: number
      fps: number
      seed: number
      provider_id?: string
      provider_model?: string
    }
    edit_spec?: {
      requested_changes: string[]
      preserve_constraints: string[]
    }
  }

  let prompt = 'A quiet night market alley after rain, warm lanterns reflected in wet stone pavement, slow cinematic camera movement.'
  let editInstruction = ''
  let continuationPrompt = ''
  let continuationTailFrames = 8
  let selectedFile: File | null = null
  let assetId: string | null = null
  let dimensions = '512x288'
  let frameCount = 49
  let fps = 8
  let videoResolution = ''
  let providerId = 'local-wan-vace'
  let health: ServiceHealth = {
    services: { comfyui: false, ollama: false },
    providers: {},
    queue_active: false,
    model_runtime: { busy: false, activity: 'idle' },
  }
  let providers: VideoProvider[] = []
  let generations: Generation[] = []
  let selectedId: string | null = null
  let isSubmitting = false
  let isStopping = false
  let error = ''
  let fileInput: HTMLInputElement

  $: selected = generations.find((generation) => generation.id === selectedId) ?? generations[0]
  $: [width, height] = dimensions.split('x').map(Number)
  $: selectedProvider = providers.find((provider) => provider.id === providerId)
  $: createCapability = selectedFile ? 'image_to_video' : 'text_to_video'
  $: providerCanCreate = !!selectedProvider
    && selectedProvider.enabled
    && selectedProvider.available
    && selectedProvider.capabilities.includes(createCapability)
  $: providerCanEdit = !!selectedProvider
    && selectedProvider.enabled
    && selectedProvider.available
    && selectedProvider.capabilities.includes('video_edit')
  $: providerCanContinue = !!selectedProvider
    && selectedProvider.enabled
    && selectedProvider.available
    && (
      selectedProvider.capabilities.includes('video_edit')
      || selectedProvider.capabilities.includes('video_continue')
    )
  $: resolutionOptions = selectedProvider?.resolution_options ?? []
  $: if (resolutionOptions.length && !resolutionOptions.includes(videoResolution)) {
    videoResolution = selectedProvider?.default_resolution || resolutionOptions[0]
  }
  $: if (!resolutionOptions.length && videoResolution) {
    videoResolution = ''
  }
  $: hasActiveOperations = health.queue_active
    || health.model_runtime.busy
    || generations.some((generation) => ['queued', 'preparing', 'running'].includes(generation.status))

  const statusLabel: Record<Generation['status'], string> = {
    queued: '排队中',
    preparing: '准备中',
    running: '生成中',
    succeeded: '已完成',
    failed: '失败',
  }

  function relativeTime(timestamp?: number) {
    if (!timestamp) return '刚刚'
    const minutes = Math.max(1, Math.round((Date.now() / 1000 - timestamp) / 60))
    return `${minutes} 分钟前`
  }

  async function refresh() {
    try {
      const [healthResponse, generationResponse, providerResponse] = await Promise.all([
        fetch('/api/health'),
        fetch('/api/generations'),
        fetch('/api/providers'),
      ])
      health = await healthResponse.json()
      generations = await generationResponse.json()
      const providerPayload = await providerResponse.json()
      providers = providerPayload.providers
      if (!providers.some((provider) => provider.id === providerId)) {
        providerId = providerPayload.default_provider_id
      }
      if (!selectedId && generations[0]) selectedId = generations[0].id
    } catch {
      error = '无法连接本地 API 服务。'
    }
  }

  async function uploadReference() {
    if (!selectedFile) return null
    const form = new FormData()
    form.append('file', selectedFile)
    const response = await fetch('/api/assets', { method: 'POST', body: form })
    if (!response.ok) throw new Error('参考图上传失败')
    const asset = await response.json()
    return asset.id as string
  }

  function confirmPaidGeneration(): boolean {
    if (!selectedProvider?.requires_payment_confirmation) return true
    return window.confirm(
      `${selectedProvider.label} is a paid cloud provider. Submit this paid video generation task?`
    )
  }

  function continuationLabel(provider: VideoProvider): string {
    return (
      provider.capabilities.includes('video_continue')
      || provider.capabilities.includes('video_edit')
    )
      ? '支持接续视频'
      : '不支持接续视频'
  }

  async function createVideo() {
    if (!prompt.trim()) return
    if (!confirmPaidGeneration()) return
    error = ''
    isSubmitting = true
    try {
      assetId = selectedFile ? await uploadReference() : null
      const response = await fetch('/api/generations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt,
          reference_asset_id: assetId,
          provider_id: providerId,
          width,
          height,
          length: frameCount,
          fps,
          resolution: videoResolution || null,
          payment_confirmed: Boolean(selectedProvider?.requires_payment_confirmation),
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      const generation = await response.json()
      selectedId = generation.id
      await refresh()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '创建任务失败'
    } finally {
      isSubmitting = false
    }
  }

  async function editVideo() {
    if (!selected || selected.status !== 'succeeded' || !editInstruction.trim()) return
    if (!confirmPaidGeneration()) return
    error = ''
    isSubmitting = true
    try {
      const response = await fetch(`/api/generations/${selected.id}/edits`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          instruction: editInstruction,
          provider_id: providerId,
          payment_confirmed: Boolean(selectedProvider?.requires_payment_confirmation),
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      const generation = await response.json()
      selectedId = generation.id
      editInstruction = ''
      await refresh()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '编辑任务创建失败'
    } finally {
      isSubmitting = false
    }
  }

  async function continueVideo() {
    if (!selected || selected.status !== 'succeeded') return
    if (!confirmPaidGeneration()) return
    error = ''
    isSubmitting = true
    try {
      const response = await fetch(`/api/generations/${selected.id}/continuations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: continuationPrompt.trim() || selected.prompt,
          provider_id: providerId,
          length: frameCount,
          fps,
          tail_frames: continuationTailFrames,
          payment_confirmed: Boolean(selectedProvider?.requires_payment_confirmation),
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      const generation = await response.json()
      selectedId = generation.id
      continuationPrompt = ''
      await refresh()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '创建接续任务失败'
    } finally {
      isSubmitting = false
    }
  }

  async function stopAllOperations() {
    error = ''
    isStopping = true
    try {
      const response = await fetch('/api/stop', { method: 'POST' })
      if (!response.ok) throw new Error(await response.text())
      await refresh()
    } catch (cause) {
      error = cause instanceof Error ? cause.message : '停止任务失败'
    } finally {
      isStopping = false
    }
  }

  function chooseFile() {
    fileInput?.click()
  }

  function handleFile(event: Event) {
    const input = event.currentTarget as HTMLInputElement
    selectedFile = input.files?.[0] ?? null
    assetId = null
  }

  onMount(() => {
    refresh()
    const refreshTimer = window.setInterval(refresh, 4000)
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const events = new WebSocket(`${protocol}//${location.host}/api/events`)
    events.onmessage = () => refresh()
    return () => {
      window.clearInterval(refreshTimer)
      events.close()
    }
  })
</script>

<main>
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark"><Clapperboard size={20} strokeWidth={2.2} /></div>
      <span>SK2 Studio</span>
      <span class="workspace-label">本地视频工作台</span>
    </div>
    <div class="topbar-actions">
      <div class="service-health" aria-label="本地服务状态">
        <span class:online={health.services.comfyui} class="service-dot"></span>
        <span>视频 Worker</span>
        <span class:online={health.services.ollama} class="service-dot"></span>
        <span>编辑助手</span>
      </div>
      <button
        class="stop-all"
        type="button"
        title="停止当前所有生成和编辑任务"
        onclick={stopAllOperations}
        disabled={isStopping || !hasActiveOperations}
      >
        <CircleStop size={16} />
        <span>{isStopping ? '正在停止' : '停止全部'}</span>
      </button>
    </div>
  </header>

  <section class="studio">
    <aside class="create-panel">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">新建版本</p>
          <h1>图文生视频</h1>
        </div>
        <Sparkles size={19} />
      </div>

      <label class="field-label" for="prompt">场景描述</label>
      <textarea id="prompt" bind:value={prompt} rows="8" placeholder="描述人物、动作、场景、光线和镜头。"></textarea>

      <div class="upload-block">
        <input bind:this={fileInput} type="file" accept="image/png,image/jpeg,image/webp" onchange={handleFile} />
        <button class:has-file={!!selectedFile} class="upload-zone" type="button" onclick={chooseFile}>
          <ImagePlus size={19} />
          <span>{selectedFile ? selectedFile.name : '添加参考图'}</span>
          <Upload size={16} />
        </button>
      </div>

      <div class="settings">
        <div class="settings-title"><Settings2 size={16} /> <span>生成参数</span></div>
        <label>
          <span>Provider</span>
          <select bind:value={providerId}>
            {#each providers as provider}
              <option value={provider.id} disabled={!provider.enabled || !provider.available}>
                {provider.label} · {continuationLabel(provider)}{provider.available ? '' : '（不可用）'}
              </option>
            {/each}
          </select>
        </label>
        {#if resolutionOptions.length}
          <label>
            <span>分辨率</span>
            <select bind:value={videoResolution}>
              {#each resolutionOptions as option}
                <option value={option}>{option}</option>
              {/each}
            </select>
          </label>
        {:else}
          <label>
            <span>画幅</span>
            <select bind:value={dimensions}>
              <option value="512x288">16:9 预览</option>
              <option value="384x384">1:1 预览</option>
              <option value="288x512">9:16 预览</option>
            </select>
          </label>
        {/if}
        <label>
          <span>帧数</span>
          <input bind:value={frameCount} type="number" min="5" max="81" step="1" />
        </label>
        <label>
          <span>帧率</span>
          <input bind:value={fps} type="number" min="4" max="24" step="1" />
        </label>
      </div>

      <button class="primary-action" type="button" onclick={createVideo} disabled={isSubmitting || !prompt.trim() || !providerCanCreate}>
        {#if isSubmitting}
          <LoaderCircle size={18} class="spin" />
        {:else}
          <WandSparkles size={18} />
        {/if}
        <span>生成视频</span>
      </button>
    </aside>

    <section class="preview-panel">
      <div class="preview-header">
        <div>
          <p class="eyebrow">当前版本</p>
          <h2>{selected ? `版本 ${generations.findIndex((item) => item.id === selected.id) + 1}` : '等待生成'}</h2>
        </div>
        {#if selected}
          <span class:success={selected.status === 'succeeded'} class:failed={selected.status === 'failed'} class="status">
            {statusLabel[selected.status]}
          </span>
        {/if}
      </div>

      <div class="player-frame">
        {#if selected?.output_url}
          <!-- svelte-ignore a11y_media_has_caption -->
          <video controls src={selected.output_url} aria-label="已生成视频"></video>
        {:else if selected}
          <div class="generation-state">
            <LoaderCircle size={30} class={selected.status !== 'failed' ? 'spin' : ''} />
            <strong>{statusLabel[selected.status]}</strong>
            <span>{Math.round(selected.progress * 100)}%</span>
          </div>
        {:else}
          <div class="empty-state">
            <Video size={34} />
            <span>首个视频会显示在这里</span>
          </div>
        {/if}
      </div>

      {#if selected?.status === 'failed'}
        <div class="error-banner">{selected.error_message || '本次生成未完成'}</div>
      {/if}

      <div class="video-meta">
        {#if selected}
          <span>{selected.config.width} x {selected.config.height}</span>
          <span>{selected.config.length} 帧</span>
          <span>{selected.config.fps} FPS</span>
          {#if selected.config.provider_model}
            <span>{selected.config.provider_model}</span>
          {/if}
        {:else}
          <span>低显存预览配置</span>
        {/if}
      </div>
    </section>

    <aside class="version-panel">
      <div class="panel-heading compact">
        <div>
          <p class="eyebrow">版本记录</p>
          <h2>生成历史</h2>
        </div>
        <button class="icon-button" type="button" aria-label="刷新版本记录" onclick={refresh}><RefreshCw size={17} /></button>
      </div>

      <div class="version-list">
        {#each generations as generation, index}
          <button
            type="button"
            class:selected={generation.id === selected?.id}
            class="version-row"
            onclick={() => (selectedId = generation.id)}
          >
            <span class:success={generation.status === 'succeeded'} class:failed={generation.status === 'failed'} class="version-status"></span>
            <span class="version-copy">
              <strong>V{generations.length - index}</strong>
              <small>{generation.mode === 'edit' ? '自然语言编辑' : generation.mode === 'image' ? '参考图生成' : '文本生成'}</small>
            </span>
            <span class="version-time">{relativeTime((generation as any).created_at)}</span>
          </button>
        {:else}
          <div class="history-empty">尚无视频版本</div>
        {/each}
      </div>

      <div class="edit-box">
        <div class="continuation-box">
          <div class="edit-heading"><FastForward size={17} /><span>接续当前视频</span></div>
          <textarea
            bind:value={continuationPrompt}
            rows="4"
            placeholder="描述接下来发生的动作；留空则沿用当前提示词"
            disabled={selected?.status !== 'succeeded'}
          ></textarea>
          <label class="continuation-tail">
            <span>尾帧数量</span>
            <input bind:value={continuationTailFrames} type="number" min="2" max="24" step="1" />
          </label>
          <button class="continuation-action" type="button" onclick={continueVideo} disabled={isSubmitting || selected?.status !== 'succeeded' || !providerCanContinue}>
            <FastForward size={17} />
            <span>接续生成</span>
          </button>
        </div>

        <div class="edit-heading"><WandSparkles size={17} /><span>修改当前视频</span></div>
        <textarea
          bind:value={editInstruction}
          rows="5"
          placeholder="例如：保持人物和镜头运动不变，把白天街景改为雨夜，地面有积水反光。"
          disabled={selected?.status !== 'succeeded'}
        ></textarea>
        {#if selected?.edit_spec}
          <div class="edit-summary">
            <span>已解析修改</span>
            <p>{selected.edit_spec.requested_changes.join('，')}</p>
          </div>
        {/if}
        <button class="edit-action" type="button" onclick={editVideo} disabled={isSubmitting || selected?.status !== 'succeeded' || !editInstruction.trim() || !providerCanEdit}>
          <Send size={17} />
          <span>生成修改版</span>
        </button>
      </div>
    </aside>
  </section>

  {#if error}
    <div class="toast error-toast">{error}</div>
  {/if}
</main>
