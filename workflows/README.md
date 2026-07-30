# Wan VACE API Workflows

These ComfyUI API prompt templates cover the two product capabilities:

- `wan_vace_text_to_video_api.json`: create a root video from a text prompt.
- `wan_vace_image_to_video_api.json`: create a root video from text plus a reference image.
- `wan_vace_video_edit_api.json`: create a child video version from a generated source video and an edit prompt.

## Input substitution

The backend must replace these placeholders before submitting the JSON to
`POST http://127.0.0.1:8188/prompt`:

- `__POSITIVE_PROMPT__`
- `__EDIT_PROMPT__`
- `__NEGATIVE_PROMPT__`
- `__REFERENCE_IMAGE__`
- `__SOURCE_VIDEO__`

Images and videos must first be uploaded to ComfyUI's input storage. The placeholder
values are the resulting input filenames, not filesystem paths.

## Low-VRAM Defaults

The templates use 512x288, 49 frames, 8 FPS, FP8 diffusion weights, and the text
encoder on CPU. This is the initial RTX 5050 profile.

## Edit Semantics

The V2V template passes all frames from the selected source video into
`WanVaceToVideo`. The edit prompt should describe both the requested change and what
must remain consistent, for example:

```text
Change the daytime background to a rainy night with wet street reflections.
Keep the person's identity, clothing, framing, and camera motion unchanged.
```

For exact local changes, the backend will add a `control_masks` input to node `9`.
That requires a user-drawn mask or a separate segmentation workflow.
