const GENERATION_RATES = Object.freeze({ "768P": 0.50, "2K": 0.80 });


export function generationCharges({ resolution, duration, imageCount, videoCount }) {
  const outputRate = GENERATION_RATES[resolution];
  if (!outputRate) throw new Error(`Unsupported generation resolution: ${resolution}`);
  const excessImageCount = Math.max(0, imageCount - 5);
  const outputCost = outputRate * duration;
  const excessImageCost = excessImageCount * 0.20;
  return {
    outputRate,
    outputCost,
    excessImageCount,
    excessImageCost,
    inputVideoRate: outputRate,
    videoCount,
    knownCost: outputCost + excessImageCost,
  };
}


export function regenerationCharges({ duration, imageCount = 0, videoCount = 0 }) {
  const excessImageCount = Math.max(0, imageCount - 5);
  const outputCost = duration * 0.30;
  const excessImageCost = excessImageCount * 0.15;
  return {
    outputRate: 0.30,
    outputCost,
    excessImageCount,
    excessImageCost,
    inputVideoRate: 0.30,
    videoCount,
    knownCost: outputCost + excessImageCost,
  };
}
