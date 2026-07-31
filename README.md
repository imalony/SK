# SK2 Studio 使用说明

SK2 Studio 是一个本地图文生视频工作台：

- 前端使用 Svelte
- 后端使用 FastAPI
- 视频模型使用 Wan2.1-VACE-1.3B
- 自然语言修改请求使用本地 Ollama Qwen 解析
- 每次生成或修改都会创建独立版本，不会覆盖原视频

## 服务地址

服务启动后，在浏览器访问：

- 前端工作台：`http://127.0.0.1:5173`
- 后端健康检查：`http://127.0.0.1:8000/api/health`
- ComfyUI：`http://127.0.0.1:8188`

## 目录说明

```text
SK2/
├─ apps/
│  ├─ api/                 FastAPI 后端
│  └─ web/                 Svelte 前端
├─ data/
│  ├─ uploads/             上传的参考图
│  ├─ media/               已完成的视频文件
│  └─ sk2.db               生成记录和版本关系
├─ workflows/              ComfyUI API 工作流
├─ providers.json          视频模型 Provider 配置
├─ comfy-models.yaml       ComfyUI 的外部模型目录配置
└─ DESIGN.md               系统设计说明
```

模型与运行时位于 F 盘：

```text
F:\SK2-models
├─ diffusion_models\wan2.1-vace-1.3b.safetensors
├─ text_encoders\umt5_xxl_fp8_e4m3fn_scaled.safetensors
├─ vae\Wan2.1_VAE.safetensors
└─ ollama\                 Qwen 模型文件

F:\SK2-runtime\ComfyUI_windows_portable
```

## 视频 Provider 配置

视频模型 Provider 集中配置在根目录的 `providers.json`。

当前默认 Provider：

```json
{
  "id": "local-wan-vace",
  "kind": "comfyui",
  "model": "Wan2.1-VACE-1.3B"
}
```

其中包含 ComfyUI 地址、输入输出目录和图生/文生/编辑工作流名称。前端“生成参数”中的 Provider 下拉框会读取 `GET /api/providers` 返回的可用 Provider。

每个任务都会把以下字段保存到生成配置中：

```json
{
  "provider_id": "local-wan-vace",
  "provider_model": "Wan2.1-VACE-1.3B"
}
```

因此后续切换默认模型不会改变历史任务的 Provider 归属。

### 添加 API Provider

`providers.json` 已包含禁用的 `external-video-api` 模板。密钥只应通过环境变量提供，例如：

```powershell
$env:SK2_VIDEO_API_KEY = "your-api-key"
```

不同视频服务的提交、轮询和下载协议不统一，因此该模板默认禁用。接入具体服务时：

1. 复制或修改该 Provider，设置唯一 `id`、`label`、`model` 和能力列表。
2. 配置 API 地址及密钥环境变量名称，不要把密钥写入 `providers.json`。
3. 为该服务实现对应执行适配器后，再将 `enabled` 改为 `true`。
4. 重启 API 服务，Provider 会出现在前端下拉框中。

## 启动方式

### 一键启动

在根目录执行：

```powershell
.\start-sk2.ps1
```

启动后访问 `http://127.0.0.1:5173`。如需自动打开浏览器：

```powershell
.\start-sk2.ps1 -OpenBrowser
```

脚本会检查 `11434`、`8188`、`8000` 和 `5173` 端口；已经运行的服务不会重复启动。后台日志会写入 `.runtime\`。

### 一键停止

在根目录执行：

```powershell
.\stop-sk2.ps1
```

脚本会先终止当前生成与编辑任务、释放模型，再关闭前端、API、ComfyUI 和 Ollama 服务。

预览将停止的服务但不执行：

```powershell
.\stop-sk2.ps1 -WhatIf
```

### 手动启动

按顺序打开三个 PowerShell 窗口。

### 1. 启动 ComfyUI

```powershell
F:\SK2-runtime\ComfyUI_windows_portable\python_embeded\python.exe `
  F:\SK2-runtime\ComfyUI_windows_portable\ComfyUI\main.py `
  --listen 127.0.0.1 `
  --port 8188 `
  --lowvram `
  --disable-pinned-memory `
  --extra-model-paths-config C:\Users\18391\Desktop\Work\SK2\comfy-models.yaml
```

看到 `To see the GUI go to: http://127.0.0.1:8188` 即表示启动成功。

### 2. 启动后端

```powershell
cd C:\Users\18391\Desktop\Work\SK2\apps\api
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

可通过以下命令确认模型服务是否可用：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

正常情况下，`comfyui` 和 `ollama` 都应为 `true`。

### 3. 启动前端

```powershell
cd C:\Users\18391\Desktop\Work\SK2\apps\web
npm.cmd run dev -- --host 127.0.0.1 --port 5173
```

然后访问 `http://127.0.0.1:5173`。

## 使用流程

### 文生视频

1. 在左侧“场景描述”输入视频提示词。
2. 选择画幅、帧数和帧率。
3. 点击“生成视频”。
4. 在中间预览区查看生成结果。

提示词应尽量描述主体、动作、环境、光线和镜头运动。例如：

```text
雨后的夜市小巷，暖色灯笼倒映在湿润石板路上，
一位穿风衣的人缓慢向前行走，电影感跟拍镜头。
```

### 参考图生视频

1. 点击“添加参考图”并上传 PNG、JPG 或 WebP 图片。
2. 填写希望画面产生的运动、镜头和环境变化。
3. 点击“生成视频”。

参考图用于固定主体、构图和整体风格。提示词应重点描述“如何动”，例如人物动作、风吹、镜头推进、光线变化等。

### 自然语言修改视频

1. 在右侧版本记录中选择一个“已完成”的视频。
2. 在“修改当前视频”输入框描述要修改的内容。
3. 明确说明需要保留的主体、构图或镜头。
4. 点击“生成修改版”。

示例：

```text
保持人物、纸船和缓慢跟拍镜头不变。
把雨夜改成有薄雾的清晨，移除暖色商店灯光，
地面保留积水和倒影。
```

系统会先用本地 Qwen 提取“修改项”和“保持项”，再将父视频作为 VACE 的条件输入，生成一个新的子版本。

### 停止全部任务

页面顶栏的“停止全部”会终止当前所有视频生成和编辑解析操作：

1. 标记排队中、准备中和生成中的任务为已终止。
2. 中断 ComfyUI 当前推理并清空其队列。
3. 取消后端中的生成任务和 Qwen 编辑解析请求。
4. 卸载 ComfyUI 模型，并停止本地 Qwen 模型。

已完成的视频不会删除；被终止的任务会在版本记录中显示为失败，错误信息为 `Stopped by user`。

## 版本规则

- 初始文生视频和图生视频是根版本。
- 对某个视频进行修改会产生新版本。
- 父视频始终保留。
- 已完成视频文件位于 `data\media\`。
- 生成历史、参数、提示词和父子关系保存在 `data\sk2.db`。

## 推荐参数

当前设备为 RTX 5050 Laptop GPU，显存约 8GB，系统内存 16GB。

建议先用低分辨率完成构图和修改验证：

| 用途 | 推荐画幅 | 帧数 | 帧率 |
| --- | --- | --- | --- |
| 快速预览 | `320x192` 或 `512x288` | 25 | 8 FPS |
| 较长预览 | `512x288` | 49 | 8 FPS |
| 竖屏预览 | `288x512` | 25 | 8 FPS |

在当前配置下，低分辨率 25 帧视频通常需要约 1.5 到 2 分钟。任务会串行执行，生成期间请不要同时在 ComfyUI 中提交大型工作流。

## 内存与显存机制

视频编辑时，后端会：

1. 使用统一模型锁，视频生成与 Qwen 解析不会并发占用硬件资源。
2. 在视频任务开始前清理 ComfyUI 已加载的模型。
3. 编辑时先卸载 ComfyUI，再调用 Qwen 解析自然语言修改请求。
4. Qwen 使用 `keep_alive: 0`，响应后立即释放。
5. 视频任务完成或失败后，后端再次调用 ComfyUI `/free` 卸载模型。

这样避免 Qwen 与 UMT5 文本编码器同时占用系统内存。`/api/health` 的 `model_runtime` 会显示当前模型状态。不要修改该卸载逻辑，否则在 16GB 内存设备上可能导致 ComfyUI Worker 退出。

## 常见问题

### 前端显示“无法连接本地 API 服务”

确认后端已启动，并检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

### 健康检查中 ComfyUI 为 `false`

确认 ComfyUI 已在 `127.0.0.1:8188` 启动，并且启动命令中包含：

```text
--lowvram
--disable-pinned-memory
--extra-model-paths-config C:\Users\18391\Desktop\Work\SK2\comfy-models.yaml
```

### 健康检查中 Ollama 为 `false`

确认 Ollama 服务已启动，且本地模型存在：

```powershell
ollama list
```

列表中应包含 `qwen3:4b-instruct`。

### 生成失败或 ComfyUI 退出

1. 关闭占用 GPU 或大量内存的应用。
2. 重启 ComfyUI。
3. 使用 `320x192`、25 帧、8 FPS 重新验证。
4. 不要在编辑任务解析期间手动加载其他 Ollama 模型。

## 开发检查

前端检查和构建：

```powershell
cd C:\Users\18391\Desktop\Work\SK2\apps\web
npm.cmd run check
npm.cmd run build
```

后端语法检查：

```powershell
cd C:\Users\18391\Desktop\Work\SK2\apps\api
.\.venv\Scripts\python.exe -m py_compile main.py
```

## Agnes Cloud Provider

`agnes-video-v2` is an optional cloud text-to-video provider. It does not load
the local GPU model, so it can be used when local VRAM or system memory is
limited.

The API key is read from the root `.env` file:

```text
SK2_AGNES_API_KEY=your-api-key
```

The `.env` file is ignored by version control. Do not put the key in
`providers.json`, source code, screenshots, or shared documents.

Current capability mapping:

- Agnes Video V2.0: text-to-video only.
- Local Wan2.1 VACE: text-to-video, image-to-video, and natural-language video
  editing.

### Continue a video

For a completed video, select the local Wan provider and use the continuation
section in the version panel. The backend uses FFmpeg to extract the final
frames from the current video, passes that tail clip to Wan VACE as the control
video, then joins the new segment onto the parent video. The resulting child
version is a longer, self-contained MP4.

Frame count and FPS are numeric inputs. For continuation, set the tail-frame
count lower than the new segment frame count. FFmpeg is required; the backend
uses `SK2_FFMPEG_EXECUTABLE` when it is configured, otherwise it searches the
standard Windows installation locations.

After changing `.env` or `providers.json`, run `.\start-sk2.ps1` again. The
Provider selector lists Agnes when the key is present. Cloud video results are
downloaded to `data\media\` and remain available in the normal generation
history.

## Alibaba Wanxiang Provider

Alibaba trial providers are ordered from lower to higher capability in the
model selector:

- HappyHorse 1.1 T2V: text-to-video.
- HappyHorse 1.1 I2V: image-to-video and image-based continuation.
- HappyHorse 1.1 R2V: reference-image video and continuation.
- Wan 2.7 T2V: higher-quality text-to-video.
- Wan 2.7 R2V: higher-quality reference-image video and continuation.

Continuation extracts the last frame of the preceding local video and uses it
as the image-to-video starting frame before FFmpeg joins the result. The
models expose only their configured resolution options in the UI. Seeds are
generated randomly by the backend and are not shown in the UI.

Add the DashScope key to the root `.env` file:

```text
ALI_API_KEY=your-dashscope-api-key
```

Do not put this key in `providers.json`. Restart the API after editing `.env`.
The provider remains visible but unavailable until the key is configured.

The advertising workflow always requires two confirmations when this provider
is selected: first approve the plan, then explicitly confirm the paid task
submission. The API enforces the second confirmation, so a direct request
without `payment_confirmed: true` is rejected before any Alibaba task is
created.

### Numeric launcher menu

Double-click `start-sk2.cmd` and enter one or more service numbers:

| Number | Service |
| --- | --- |
| `1` | SK2 FastAPI backend |
| `2` | Svelte frontend |
| `3` | ComfyUI local Wan provider |
| `4` | Ollama local planning model |

The default is `12`, which starts only the backend and frontend. For local
Wan, use `123`. The API serializes video jobs with one shared lock.

`stop-sk2.cmd` stops the frontend, API, ComfyUI, and Ollama. It
also asks the API to cancel current tasks and release local model resources
before terminating the managed services.
