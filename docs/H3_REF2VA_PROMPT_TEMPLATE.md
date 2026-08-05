# MiniMax H3 REF2VA prompt template

Use this template for MiniMax H3 reference-to-video-and-audio requests. It is based on successful
H3 Context IR results retained locally by H3 Studio and on the model-facing label presentation in
ComfyUI's native MiniMax H3 integration.

## Media order and labels

Reference labels are **1-based** and numbered independently for each media type:

- the first reference image is `<Picture 1>`, the second is `<Picture 2>`, and so on;
- the first reference video is `<Video 1>`, the second is `<Video 2>`, and so on;
- the first reference audio clip is `<Audio 1>`, the second is `<Audio 2>`, and so on.

H3 Studio sends the text prompt first and then sends attached media in the order shown in the
workspace. Dragging an attachment changes that provider `content` order. Within that order, count
each reference role separately. For example:

| Workspace order | Role | Prompt label |
| ---: | --- | --- |
| 1 | Reference image | `<Picture 1>` |
| 2 | Reference video | `<Video 1>` |
| 3 | Reference image | `<Picture 2>` |
| 4 | Reference audio | `<Audio 1>` |
| 5 | Reference video | `<Video 2>` |

Renaming a source-shelf item changes its human-facing name only. It does not change the H3 label.
Reordering an item can change its ordinal among items of the same type, so update every affected
prompt reference after reordering.

When reproducing a request directly in native ComfyUI, note one implementation difference: the
`MiniMaxH3ReferenceToVideo` node assembles its presentation as images first, then videos, then
standalone audio. A soundtrack paired with a reference video receives its own `<Audio N>` label
immediately before that `<Video N>` presentation. Wire the ports and write the labels shown by that
node; do not infer ComfyUI labels from an interleaved H3 Studio workspace order.

`<Subject N>` is not a media slot. It is a semantic alias defined by the prompt author and linked to
one or more media labels. `[Shot N]` is a narrative marker, not an injected multimodal token.

## Copy-ready template

Replace bracketed instructions but retain the six section names and their order.

```text
subject_definitions:
<Subject 1> is [identity and stable appearance], shown in <Picture 1>.
<Subject 2> is [identity and stable appearance], shown in <Video 1>.
<Picture 2> provides [wardrobe, prop, environment, composition, or style].
<Audio 1> provides [voice, dialogue, sound effect, ambience, or rhythm].

summary:
[reference generation + audio reference] [One sentence stating the intended target video and the references it uses.]

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - [features that must remain unchanged].
<Video 1> (referenced in [Shot 1]): motion_reference - [motion or camera behavior to transfer].
<Picture 2> (referenced in [Shot 2]): attribute_transfer - [attributes to transfer].
<Audio 1> (used in [Shot 1]): fully_preserved - [audio content and synchronization requirement].

detailed_description:
[Overall medium, visual style, lighting, color, and continuity requirements.]

[Shot 1] [Time range.] [Framing and camera.] <Subject 1> [action]. [Environment and interactions.] [How <Picture 1>, <Video 1>, or <Audio 1> conditions the shot.]

[Shot 2] [Time range.] [Transition, action, camera, environment, and continuity.]

overall_soundscape:
[Dialogue, ambience, Foley, referenced audio, timing, and audiovisual synchronization.]

non_diegetic_music:
N/A
```

Delete unused definition and retention lines. Never mention a label for which no corresponding
media item is attached.

## Authoring guidance

- Define subjects once, then use the same `<Subject N>` consistently across shots.
- State separately what is preserved, what is transferred, and what is intentionally changed.
- Put target action, framing, camera motion, and timing in `detailed_description`; do not rely on a
  source filename to communicate intent.
- Describe dialogue and important synchronization in both the relevant shot and
  `overall_soundscape`.
- Use `non_diegetic_music: N/A` when no score is wanted.
- Keep the output as plain text. Do not wrap it in JSON, XML, or a chat message.

## Model-facing presentation

The open-weight ComfyUI path presents precise text labels adjacent to media embeddings, followed
by this prompt. Images receive `<Picture N>: ` plus vision tokens. Videos receive `<Video N>: `
plus timestamped vision blocks. Audio receives `<Audio N>: ` in text while its latent conditioning
travels through H3's audio/reference path. This is why exact spelling, capitalization, angle
brackets, and 1-based numbering matter.
