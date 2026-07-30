# 广告成片页总体设计

## 1. 目标

在现有 SK2 视频模型测试工作台之外，新增面向非技术用户的广告成片页。

用户输入少量商品、门店或人物图片，填写简短说明和目标时长，系统生成可下载的抖音广告视频。用户不需要理解视频模型、提示词、帧数、Provider、FFmpeg 或接续逻辑。

当前测试工作台保留在 `/`。广告成片页使用 `/ad`。

## 2. 用户输入和输出

### 输入

- 1 到 8 张图片素材。
- 一段简短说明，包括商品或门店名称、卖点、活动信息和希望表达的风格。
- 目标时长：15、30 或 45 秒。
- 配音开关。
- 配音音色选择与试听。
- 字幕开关。
- 背景音乐开关。

### 输出

- 竖屏抖音兼容 MP4。
- 成片预览和下载。
- 最终口播稿。
- 抖音发布文案和话题标签。

成片输出为 H.264、AAC、`yuv420p`、`faststart` 的 MP4。视频将统一导出为 1080x1920；本地视频模型的实际原始清晰度受当前显存限制。

## 3. 双阶段工作流

```text
素材和说明
  -> 总体规划
  -> 用户多轮修改和确认
  -> 用户确认开始生成
  -> 系统自动生成、反思、接续、配音和合成
  -> 成片下载
```

### 阶段 A：总体规划和用户确认

在用户明确确认之前，系统不得调用视频生成接口。

系统使用 `gpt-5.5` 生成以下内容：

- 广告目标、受众、风格和核心卖点。
- 分镜表，包括镜头顺序、引用素材、镜头目的、时长和动作描述。
- 每个镜头的初始视频生成提示词。
- 口播稿、字幕文本、发布文案和话题标签。
- 总生成时长估计。

用户可以多轮反馈，例如：

- “更突出价格优惠”
- “第二张图作为开场”
- “口播更轻松一点”
- “减少门店环境，多展示产品细节”

系统按反馈创建新的规划版本。用户点击“确认方案并开始生成”后，当前规划版本被冻结。

### 阶段 B：确认后的自动执行

用户确认后，不需要逐镜头参与。系统按固定编排链路完成所有生成和合成步骤。

系统只有在达到重试上限、素材无法使用或基础服务不可用时，才将项目变为需要用户处理的状态。

## 4. 状态机

```text
draft
-> planning
-> waiting_user_confirmation
-> approved
-> generating_segments
-> reviewing_segments
-> composing_audio_video
-> completed

异常状态：
-> waiting_user_action
-> failed
-> cancelled
```

后端创建任何视频生成任务前，必须校验：

- 项目状态为 `approved` 或之后的自动执行状态。
- 当前规划版本已被用户确认。
- 该规划版本的确认时间存在。

这项校验必须在后端实现，不能只依赖前端按钮状态。

## 5. 模型职责

| 能力 | 实现 |
| --- | --- |
| 总体广告规划 | `gpt-5.5` |
| 分镜、视频提示词、接续提示词 | `gpt-5.5` |
| 关键帧反思和自动重试决策 | `gpt-5.5` |
| 口播稿、发布文案和话题标签 | `gpt-5.5` |
| 图生视频和尾帧接续 | 本地 Wan VACE 或其他视频 Provider |
| 配音 | Edge TTS，后续可扩展本地 HTTP TTS |
| 字幕、混音、转场、拼接和导出 | FFmpeg |

除视频生成模型外，默认模型配置为：

```text
base_url: https://fjbigmodel.fjdac.cn/v1
api: responses
api_key_env: OPENAI_API_KEY2
model: gpt-5.5
```

本地 Qwen 仅作为云端模型不可用时的降级方案。

## 6. 自动分镜与接续

### 单镜头生成

1. 系统根据总体规划、当前分镜和指定素材生成视频模型专用提示词。
2. Wan VACE 使用分镜指定的参考图片生成无声短视频。
3. FFmpeg 抽取首帧、中间帧、尾帧等关键帧。
4. 系统将关键帧、当前分镜、总体规划和已用提示词发送给 `gpt-5.5`。
5. `gpt-5.5` 输出结构化反思结果：
   - 是否符合总体规划和分镜目标。
   - 必须保持的商品、人物、构图、色调和镜头运动。
   - 是否需要重试当前片段。
   - 下一段的接续提示词。

### 接续生成

当单个镜头需要的时长超过视频模型单段时长时：

1. FFmpeg 从当前片段末尾截取若干尾帧，生成短控制视频。
2. `gpt-5.5` 基于总体规划、反思结果和尾帧生成接续提示词。
3. Wan VACE 使用尾帧短片作为 `control_video` 生成下一段。
4. FFmpeg 去除重叠尾帧后将下一段拼接到当前镜头。
5. 系统重复抽帧、反思和接续，直到达到该镜头计划时长。

### 镜头切换

不同分镜使用各自对应的参考图片生成。镜头之间由 FFmpeg 添加短转场并拼接，不强行用尾帧接续跨越完全不同的商品或场景。

### 自动重试

每段设置固定最大重试次数。反思结果可请求：

- 保持当前片段。
- 使用修订提示词重试当前片段。
- 继续生成下一片段。
- 标记为失败并进入 `waiting_user_action`。

系统不得无限重试。

## 7. 配音、音色和字幕

视频 Provider 返回的音轨不作为最终成片依赖。所有视频片段按无声素材处理。

### 音色选择

用户在确认总体方案前可：

- 开启或关闭配音。
- 在音色下拉框选择音色。
- 按女声、男声、亲和、稳重、促销等标签筛选。
- 试听 5 到 10 秒的示例口播。

确认时锁定 `tts_provider`、`voice_id` 和默认语速。

默认使用 Edge TTS，默认音色为 `zh-CN-XiaoxiaoNeural`。后续可接入本地 HTTP TTS Provider。

### 时长适配

参考 `SK` 项目的配音逻辑：

1. `gpt-5.5` 根据最终目标时长控制口播字数。
2. 配音偏长时先在合理范围内调整语速。
3. 仍偏长时，由 `gpt-5.5` 自动精简口播稿，再重新生成配音。
4. 配音偏短时，视频结尾保留 BGM 垫底。

### 字幕与 BGM

- 依据最终配音时序生成 ASS 字幕并烧录。
- BGM 循环至成片时长，在结尾淡出。
- 人声存在时对 BGM 做压低或侧链闪避。
- 最终由 FFmpeg 混合视频、配音、BGM 和字幕。

## 8. 内置提示词

提示词为应用内置、版本化资源，不对普通用户开放编辑：

```text
apps/api/prompts/ad/
  planner_v1.md
  video_prompt_writer_v1.md
  segment_reviewer_v1.md
  continuation_director_v1.md
  voiceover_writer_v1.md
  publish_copy_writer_v1.md
```

每个项目记录：

- 使用的提示词版本。
- 规划版本。
- 实际视频提示词。
- 关键帧反思结果。
- 自动重试原因。
- 最终口播和音色配置。

这保证任务可追溯、可复现且易于排查。

## 9. 数据模型

### ad_projects

- `id`
- `brief`
- `target_duration_seconds`
- `voice_enabled`
- `subtitle_enabled`
- `bgm_enabled`
- `tts_provider`
- `voice_id`
- `status`
- `approved_plan_version`
- `plan_approved_at`
- `final_output_path`
- `error_message`
- `created_at`
- `completed_at`

### ad_assets

- `id`
- `project_id`
- `filename`
- `stored_path`
- `sort_order`
- `created_at`

### ad_plans

- `id`
- `project_id`
- `version`
- `plan_json`
- `voiceover_script`
- `post_caption`
- `hashtags_json`
- `prompt_bundle_version`
- `created_at`
- `approved_at`

### ad_segments

- `id`
- `project_id`
- `plan_id`
- `sequence_number`
- `asset_id`
- `target_duration_seconds`
- `parent_segment_id`
- `generation_id`
- `prompt`
- `review_json`
- `retry_count`
- `output_path`
- `status`

### ad_runs

- `id`
- `project_id`
- `stage`
- `status`
- `progress`
- `details_json`
- `error_message`
- `started_at`
- `completed_at`

## 10. API

```text
POST /api/ad-projects
POST /api/ad-projects/{id}/assets
GET  /api/ad-projects/{id}
POST /api/ad-projects/{id}/plan
POST /api/ad-projects/{id}/plan-feedback
POST /api/ad-projects/{id}/plan-approve
GET  /api/ad-projects/{id}/voices
POST /api/ad-projects/{id}/voice-preview
POST /api/ad-projects/{id}/generate
POST /api/ad-projects/{id}/stop
GET  /api/ad-projects/{id}/download
```

`POST /api/ad-projects/{id}/generate` 必须要求已确认的规划版本，否则返回 `409 Conflict`。

## 11. 页面结构

广告页只包含三个工作区域：

1. 素材和说明：图片上传、简短说明、时长、配音和音色。
2. 广告方案：总体规划、分镜、口播稿和用户反馈输入。
3. 生成结果：自动执行进度、成片预览、下载、发布文案和话题标签。

确认前，主操作为“生成方案”和“确认方案并开始生成”。

确认后，主区域显示自动执行进度。用户不需要逐镜头确认。

## 12. 实施顺序

1. 新增广告项目数据库表、项目 API 和 `/ad` 页面骨架。
2. 接入 `gpt-5.5` Responses Provider 和内置提示词。
3. 实现总体规划版本、用户反馈和后端确认门禁。
4. 实现分镜任务编排、关键帧抽取、反思和尾帧接续。
5. 迁移 `SK` 的 TTS、时长适配、字幕和混音能力。
6. 实现 FFmpeg 镜头拼接、转场、竖屏导出和下载。
7. 验证确认前不会发起视频任务，确认后可自动完成 15 秒、30 秒广告项目。

## 13. 验收标准

- 用户可以只凭图片、说明和时长完成广告制作。
- 用户确认前，后端无任何视频 Provider 调用。
- 用户可以多轮修改总体规划。
- 用户确认后，系统自动完成分镜、反思、接续、配音和合成。
- 用户可选择并试听音色。
- 最终成片包含独立生成的配音，可选字幕和 BGM。
- 最终视频可在浏览器预览和下载。
- 视频、规划、分镜、提示词、反思和音色配置均可追溯。
