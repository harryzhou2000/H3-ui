# MiniMax H3 I2VA and FL2VA prompt templates

Use these templates for image-conditioned MiniMax H3 video-and-audio generation. I2VA uses one
first-frame image. FL2VA uses both a first-frame and a last-frame image. The structure is based on
successful H3 Context IR output retained locally by H3 Studio and the native H3 prompt builder.

## Frame order and labels

Frame labels are **1-based**:

| Mode | Media role | Prompt label | Target alignment |
| --- | --- | --- | --- |
| I2VA | First frame | `<Picture 1>` | `0.00` seconds |
| FL2VA | First frame | `<Picture 1>` | `0.00` seconds |
| FL2VA | Last frame | `<Picture 2>` | target video end |

H3 Studio sends one text item and the attached image items in workspace order, but the explicit
`first_frame` and `last_frame` roles determine endpoint meaning. Keep the first-frame attachment
before the last-frame attachment for readability and portability. Do not mix endpoint-frame roles
with `reference_image`, `reference_video`, or `reference_audio` roles in one request.

Renaming an image does not change its `<Picture N>` label. In FL2VA, do not swap the two endpoint
images without also swapping their roles and reviewing the alignment sentence.

Native ComfyUI injects `<Picture 1>: ` before the first image embedding and `<Picture 2>: ` before
the second. The alignment sentence in the prompt tells H3 where those pictures belong on the target
timeline. `[Shot N]` remains an ordinary textual narrative marker.

## I2VA copy-ready template

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] [Describe the subject and appearance retained from <Picture 1>. Describe the setting, action progression, camera framing and movement, lighting, and final state. Include timing when it matters.]

overall_soundscape: [Describe dialogue, ambience, Foley, action sounds, and synchronization.]

non_diegetic_music: N/A
```

The first sentence is part of the prompt. Keep it before all section headers.

## FL2VA copy-ready template

Replace `{duration}` with the requested target duration, such as `10.00`.

```text
How the reference pictures align with the target video — <Picture 1> (from [Shot 1]) aligns with the 0.00-second mark of the target video; <Picture 2> (from [Shot 2]) aligns with the {duration}-second mark of the target video.

integrated_multimodal_description: [Shot 1] At 0.00 seconds, [describe the opening state represented by <Picture 1>, then the subject action, environment, framing, and camera movement]. [Shot 2] By {duration} seconds, [describe the transition into the exact closing state represented by <Picture 2>].

overall_soundscape: [Describe continuous ambience, dialogue, Foley, transitions, and synchronization across the complete clip.]

non_diegetic_music: N/A
```

Use `<Picture 1>` and `<Picture 2>` consistently even if an IR result omits angle brackets in a
prose occurrence. The exact injected media labels use angle brackets.

## Authoring guidance

- Treat the endpoint pictures as boundary conditions, not general visual references.
- Describe a plausible continuous transition between the endpoints; do not merely describe the two
  still images independently.
- Put all visual, action, camera, and timing instructions inside
  `integrated_multimodal_description`.
- Put dialogue and synchronized sound in `overall_soundscape`, repeating critical timing alongside
  the relevant visual action when useful.
- Use `non_diegetic_music: N/A` when no score is wanted.
- Keep the output as plain text with the section names and ordering shown above.

## Related mode

For last-frame-only L2VA, the single image is `<Picture 1>` and aligns with the target duration, not
with `0.00` seconds. Do not call it `<Picture 2>` merely because it represents the end frame.
