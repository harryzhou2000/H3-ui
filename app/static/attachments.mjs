const ROLE_LABELS = {
  first_frame: "First frame",
  last_frame: "Last frame",
  reference_image: "Reference image",
  reference_video: "Reference video",
  reference_audio: "Reference audio",
  base_video: "Base video",
};


export function attachmentLabel(items, index) {
  const item = items[index];
  if (!item) return "Media input";
  const base = ROLE_LABELS[item.role] || "Media input";
  if (!item.role?.startsWith("reference_")) return base;
  const ordinal = items
    .slice(0, index + 1)
    .filter((candidate) => candidate.role === item.role)
    .length;
  return `${base} ${ordinal}`;
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
