const REFERENCE_TAGS = {
  reference_image: "Picture",
  reference_video: "Video",
  reference_audio: "Audio",
};


export function attachmentLabel(items, index) {
  const item = items[index];
  if (!item) return "Media input";
  if (item.role === "first_frame") return "<Picture 1>";
  if (item.role === "last_frame") {
    const ordinal = items.some((candidate) => candidate.role === "first_frame") ? 2 : 1;
    return `<Picture ${ordinal}>`;
  }
  const tag = REFERENCE_TAGS[item.role];
  if (!tag) return item.role === "base_video" ? "Base video" : "Media input";
  const ordinal = items
    .slice(0, index + 1)
    .filter((candidate) => candidate.role === item.role)
    .length;
  return `<${tag} ${ordinal}>`;
}


export function reorderAttachedItems(items, fromIndex, toIndex) {
  if (
    fromIndex < 0
    || fromIndex >= items.length
    || toIndex < 0
    || toIndex >= items.length
    || fromIndex === toIndex
  ) return items;
  const reordered = [...items];
  const [moved] = reordered.splice(fromIndex, 1);
  reordered.splice(toIndex, 0, moved);
  return reordered;
}
