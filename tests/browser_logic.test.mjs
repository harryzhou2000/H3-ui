import assert from "node:assert/strict";
import test from "node:test";

import {
  createAttemptLedger,
  pollActiveTaskPool,
  taskPresentationChanged,
} from "../app/static/pool.mjs";
import {
  generationCharges,
  regenerationCharges,
} from "../app/static/billing.mjs";
import {
  attachmentLabel,
  reorderAttachedItems,
} from "../app/static/attachments.mjs";


test("reference media labels use exact H3 tags and independent one-based ordinals", () => {
  const items = [
    { assetId: "image-a", role: "reference_image" },
    { assetId: "video-a", role: "reference_video" },
    { assetId: "image-b", role: "reference_image" },
    { assetId: "audio-a", role: "reference_audio" },
    { assetId: "frame-a", role: "first_frame" },
  ];
  assert.deepEqual(
    items.map((_, index) => attachmentLabel(items, index)),
    [
      "<Picture 1>",
      "<Video 1>",
      "<Picture 2>",
      "<Audio 1>",
      "<Picture 1>",
    ],
  );
});


test("endpoint frame tags follow I2VA, FL2VA, and L2VA numbering", () => {
  const firstAndLast = [
    { assetId: "opening", role: "first_frame" },
    { assetId: "closing", role: "last_frame" },
  ];
  assert.equal(attachmentLabel(firstAndLast, 0), "<Picture 1>");
  assert.equal(attachmentLabel(firstAndLast, 1), "<Picture 2>");
  assert.equal(
    attachmentLabel([{ assetId: "closing", role: "last_frame" }], 0),
    "<Picture 1>",
  );
});


test("attached media can be reordered without mutating the original array", () => {
  const items = [{ assetId: "a" }, { assetId: "b" }, { assetId: "c" }];
  const reordered = reorderAttachedItems(items, 0, 2);
  assert.deepEqual(reordered.map((item) => item.assetId), ["b", "c", "a"]);
  assert.deepEqual(items.map((item) => item.assetId), ["a", "b", "c"]);
  assert.equal(reorderAttachedItems(items, 1, 1), items);
});


test("billable request IDs stay stable until a scene changes or submission succeeds", () => {
  let sequence = 0;
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  };
  const randomUUID = () => `uuid-${++sequence}`;
  const ledger = createAttemptLedger(randomUUID, storage);
  assert.equal(ledger.isDurable(), true);

  const first = ledger.generation();
  assert.equal(ledger.generation(), first, "a timeout/retry must reuse the same ID");
  ledger.rotateScene();
  assert.notEqual(ledger.generation(), first, "editing the scene starts a new intent");

  const reviewed = ledger.generation();
  ledger.markSucceeded("generation");
  assert.notEqual(ledger.generation(), reviewed, "a deliberate repeat after success is new");

  const context = ledger.context();
  assert.equal(ledger.context(), context, "Context IR retries also reuse their ID");
  ledger.markSucceeded("context-ir");
  assert.notEqual(ledger.context(), context);

  const regen = ledger.regeneration("source-1");
  assert.equal(ledger.regeneration("source-1"), regen);

  const restored = createAttemptLedger(randomUUID, storage);
  assert.equal(restored.generation(), ledger.generation(), "a reload preserves generation retry safety");
  assert.equal(restored.context(), ledger.context(), "a reload preserves Context IR retry safety");
  assert.equal(restored.regeneration("source-1"), regen, "a reload preserves regeneration retry safety");

  ledger.markSucceeded("regeneration", "source-1");
  assert.notEqual(ledger.regeneration("source-1"), regen);
});


test("attempt persistence is tab-isolated and storage failures disable billable safety", () => {
  let sequence = 0;
  const randomUUID = () => `tab-uuid-${++sequence}`;
  const makeStorage = (initialValues = []) => {
    const values = new Map(initialValues);
    return {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
      snapshot: () => [...values.entries()],
    };
  };
  const tabAStorage = makeStorage();
  const tabA = createAttemptLedger(randomUUID, tabAStorage);
  const ambiguousId = tabA.generation();
  const tabBStorage = makeStorage(tabAStorage.snapshot());
  const tabB = createAttemptLedger(randomUUID, tabBStorage);
  tabB.rotateScene();

  const reloadedTabA = createAttemptLedger(randomUUID, tabAStorage);
  assert.equal(reloadedTabA.generation(), ambiguousId, "another tab cannot replace this tab's retry ID");

  const blockedStorage = {
    getItem: () => { throw new Error("storage blocked"); },
    setItem: () => { throw new Error("storage blocked"); },
  };
  const unsafe = createAttemptLedger(randomUUID, blockedStorage);
  assert.equal(unsafe.isDurable(), false, "the UI must fail closed when IDs cannot persist");

  const corruptStorage = makeStorage();
  corruptStorage.setItem(
    "h3-studio.billable-attempts.v1",
    JSON.stringify({ version: 1, generationId: "wrong", contextId: "wrong" }),
  );
  const corrupt = createAttemptLedger(randomUUID, corruptStorage);
  assert.equal(corrupt.isDurable(), false, "corrupt retry state must not be silently replaced");

  const clearedStorage = makeStorage();
  const cleared = createAttemptLedger(randomUUID, clearedStorage);
  clearedStorage.setItem("h3-studio.billable-attempts.v1", "");
  assert.equal(cleared.isDurable(), false, "storage is re-verified immediately before billing");

  const manyStorage = makeStorage();
  const many = createAttemptLedger(randomUUID, manyStorage);
  const oldestRegeneration = many.regeneration("source-0");
  for (let index = 1; index <= 101; index += 1) many.regeneration(`source-${index}`);
  assert.equal(
    many.regeneration("source-0"),
    oldestRegeneration,
    "ambiguous regeneration IDs are never evicted merely because the pool grew",
  );
});


test("the active pool polls every eligible task without replacing or cancelling any", async () => {
  const jobs = [
    { task_id: "queued-1", status: "queued" },
    { task_id: "running-1", status: "running" },
    { task_id: "running-2", status: "running" },
    { task_id: "done-1", status: "succeeded" },
  ];
  const seen = [];
  let concurrent = 0;
  let maximumConcurrent = 0;
  const changed = await pollActiveTaskPool({
    jobs,
    enabled: true,
    hidden: false,
    nextPollAt: new Map(),
    inFlight: new Set(),
    cycle: { running: false },
    concurrency: 2,
    refresh: async (taskId) => {
      seen.push(taskId);
      concurrent += 1;
      maximumConcurrent = Math.max(maximumConcurrent, concurrent);
      await new Promise((resolve) => setTimeout(resolve, 2));
      concurrent -= 1;
      return true;
    },
  });

  assert.equal(changed, true);
  assert.deepEqual(new Set(seen), new Set(["queued-1", "running-1", "running-2"]));
  assert.ok(maximumConcurrent <= 2, "polling honors the provider-friendly concurrency cap");
  assert.equal(jobs.length, 4, "pool membership is additive and unchanged by polling");
});


test("hidden, backed-off, and already in-flight tasks are not polled", async () => {
  const jobs = [
    { task_id: "backoff", status: "queued" },
    { task_id: "busy", status: "running" },
  ];
  const seen = [];
  await pollActiveTaskPool({
    jobs,
    enabled: true,
    hidden: false,
    now: 100,
    nextPollAt: new Map([["backoff", 101]]),
    inFlight: new Set(["busy"]),
    cycle: { running: false },
    refresh: async (taskId) => { seen.push(taskId); return true; },
  });
  assert.deepEqual(seen, []);
});


test("unchanged task refreshes do not request a task-list rerender", () => {
  const job = {
    task_id: "task-1",
    operation: "generation",
    status: "running",
    response: {
      task: {
        id: "task-1",
        task_type: "generation",
        status: "running",
        resolution: "768P",
        duration: 4,
        ratio: "16:9",
      },
    },
  };
  assert.equal(
    taskPresentationChanged(job, { task: { ...job.response.task, provider_trace: "new" } }),
    false,
    "provider metadata that is not rendered must not replace focused task controls",
  );
  assert.equal(
    taskPresentationChanged(job, { task: { ...job.response.task, status: "succeeded" } }),
    true,
    "a visible status transition must rerender the task pool",
  );
  assert.equal(
    taskPresentationChanged(job, {
      task: { ...job.response.task, error: { message: "provider failed" } },
    }),
    true,
    "new visible task details must rerender the task pool",
  );
});


test("overlapping timer cycles are serialized and each cycle has a fair request budget", async () => {
  const jobs = Array.from({ length: 6 }, (_, index) => ({
    task_id: `task-${index + 1}`,
    status: "running",
  }));
  const nextPollAt = new Map();
  const inFlight = new Set();
  const cycle = { running: false };
  const seen = [];
  let releaseFirst;
  const firstRefresh = new Promise((resolve) => { releaseFirst = resolve; });
  const refresh = async (taskId) => {
    seen.push(taskId);
    if (taskId === "task-1") await firstRefresh;
    return true;
  };
  const options = {
    jobs,
    enabled: true,
    hidden: false,
    now: 100,
    nextPollAt,
    inFlight,
    cycle,
    refresh,
    concurrency: 1,
    maxTasksPerCycle: 2,
    pollIntervalMs: 10,
  };

  const firstCycle = pollActiveTaskPool(options);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(await pollActiveTaskPool(options), false, "an overlapping interval is ignored");
  releaseFirst();
  assert.equal(await firstCycle, true);
  assert.deepEqual(seen, ["task-1", "task-2"], "one cycle cannot drain an unbounded pool");

  await pollActiveTaskPool({ ...options, now: 101 });
  assert.deepEqual(seen, ["task-1", "task-2", "task-3", "task-4"], "unseen tasks are selected next");
  assert.equal(inFlight.size, 0);
  assert.equal(cycle.running, false);
});


test("published MiniMax generation and regeneration charges are represented exactly", () => {
  const generation = generationCharges({
    resolution: "2K",
    duration: 4,
    imageCount: 7,
    videoCount: 1,
  });
  assert.deepEqual(generation, {
    outputRate: 0.80,
    outputCost: 3.20,
    excessImageCount: 2,
    excessImageCost: 0.40,
    inputVideoRate: 0.80,
    videoCount: 1,
    knownCost: 3.60,
  });

  const regeneration = regenerationCharges({ duration: 4, imageCount: 7, videoCount: 1 });
  assert.deepEqual(regeneration, {
    outputRate: 0.30,
    outputCost: 1.20,
    excessImageCount: 2,
    excessImageCost: 0.30,
    inputVideoRate: 0.30,
    videoCount: 1,
    knownCost: 1.50,
  });
});
