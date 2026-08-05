export const ACTIVE_STATUSES = new Set(["queued", "running"]);

const ATTEMPT_STORAGE_KEY = "h3-studio.billable-attempts.v1";


function taskPresentation(status, operation, task) {
  const content = task?.content && typeof task.content === "object" ? task.content : {};
  const error = task?.error && typeof task.error === "object" ? task.error : {};
  return JSON.stringify({
    status: status || task?.status || null,
    operation: task?.task_type || operation || null,
    resolution: task?.resolution || null,
    duration: task?.duration ?? null,
    ratio: task?.ratio || null,
    modality: task?.modality || null,
    prompt: content.prompt || null,
    error: error.message || null,
  });
}


export function taskPresentationChanged(job, refreshResponse) {
  const currentTask = job?.response?.task;
  const refreshedTask = refreshResponse?.task;
  if (!job || !currentTask || !refreshedTask || typeof refreshedTask !== "object") return true;
  return taskPresentation(job.status, job.operation, currentTask)
    !== taskPresentation(refreshedTask.status, job.operation, refreshedTask);
}


function validRequestId(value, prefix) {
  return typeof value === "string"
    && value.startsWith(`${prefix}-`)
    && value.length >= 8
    && value.length <= 128;
}


function browserStorage() {
  try {
    // sessionStorage survives reloads while keeping independent tabs from
    // overwriting one another's ambiguous in-flight attempt IDs.
    return globalThis.sessionStorage || null;
  } catch {
    return null;
  }
}


function storedAttempts(storage) {
  if (!storage) return { attempts: {}, durable: false };
  try {
    const raw = storage.getItem(ATTEMPT_STORAGE_KEY);
    if (raw === null) return { attempts: {}, durable: true };
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || value.version !== 1) {
      return { attempts: {}, durable: false };
    }
    const regenerationMapIsValid = value.regenerationIds
      && typeof value.regenerationIds === "object"
      && !Array.isArray(value.regenerationIds);
    const regenerationEntries = Object.entries(
      regenerationMapIsValid ? value.regenerationIds : {},
    );
    const schemaIsValid = regenerationMapIsValid
      && regenerationEntries.length <= 10000
      && validRequestId(value.generationId, "generation")
      && validRequestId(value.contextId, "context-ir")
      && regenerationEntries.every(
        ([taskId, requestId]) => /^[A-Za-z0-9_-]{1,128}$/.test(taskId)
          && validRequestId(requestId, "regeneration"),
      );
    if (!schemaIsValid) return { attempts: {}, durable: false };
    return { attempts: value, durable: true };
  } catch {
    return { attempts: {}, durable: false };
  }
}


export function createAttemptLedger(
  randomUUID = () => globalThis.crypto.randomUUID(),
  storage = browserStorage(),
) {
  const next = (prefix) => `${prefix}-${randomUUID()}`;
  const loaded = storedAttempts(storage);
  const restored = loaded.attempts;
  let durable = loaded.durable;
  let generationId = validRequestId(restored.generationId, "generation")
    ? restored.generationId
    : next("generation");
  let contextId = validRequestId(restored.contextId, "context-ir")
    ? restored.contextId
    : next("context-ir");
  const regenerationIds = new Map(
    Object.entries(restored.regenerationIds || {})
      .filter(([taskId, requestId]) => taskId && validRequestId(requestId, "regeneration")),
  );

  const serialize = () => JSON.stringify({
    version: 1,
    generationId,
    contextId,
    regenerationIds: Object.fromEntries(regenerationIds),
  });

  const persist = () => {
    if (!storage || !durable) {
      durable = false;
      return;
    }
    try {
      const serialized = serialize();
      storage.setItem(ATTEMPT_STORAGE_KEY, serialized);
      if (storage.getItem(ATTEMPT_STORAGE_KEY) !== serialized) durable = false;
    } catch {
      // Billable calls fail closed when retry IDs cannot survive a reload.
      durable = false;
    }
  };
  const verifyDurable = () => {
    if (!storage || !durable) return false;
    try {
      if (storage.getItem(ATTEMPT_STORAGE_KEY) !== serialize()) durable = false;
    } catch {
      durable = false;
    }
    return durable;
  };
  persist();

  return {
    generation() {
      return generationId;
    },
    context() {
      return contextId;
    },
    regeneration(taskId) {
      if (!regenerationIds.has(taskId)) {
        regenerationIds.set(taskId, next("regeneration"));
        persist();
      }
      return regenerationIds.get(taskId);
    },
    isDurable() {
      return verifyDurable();
    },
    rotateScene() {
      generationId = next("generation");
      contextId = next("context-ir");
      persist();
    },
    markSucceeded(operation, taskId = null) {
      if (operation === "generation") generationId = next("generation");
      if (operation === "context-ir") contextId = next("context-ir");
      if (operation === "regeneration" && taskId) regenerationIds.delete(taskId);
      persist();
    },
  };
}


export async function pollActiveTaskPool({
  jobs,
  enabled,
  hidden,
  nextPollAt,
  inFlight,
  cycle = { running: false },
  refresh,
  now = Date.now(),
  concurrency = 4,
  maxTasksPerCycle = 8,
  pollIntervalMs = 8000,
}) {
  if (!enabled || hidden || cycle.running) return false;
  cycle.running = true;
  const selected = jobs
    .filter(
      (job) => ACTIVE_STATUSES.has(job.status)
        && (nextPollAt.get(job.task_id) || 0) <= now
        && !inFlight.has(job.task_id),
    )
    .sort(
      (left, right) => (nextPollAt.get(left.task_id) || 0)
        - (nextPollAt.get(right.task_id) || 0),
    )
    .slice(0, Math.max(1, maxTasksPerCycle));

  if (!selected.length) {
    cycle.running = false;
    return false;
  }

  for (const job of selected) {
    inFlight.add(job.task_id);
    nextPollAt.set(job.task_id, now + pollIntervalMs);
  }

  let cursor = 0;
  let changed = false;
  const errors = [];
  async function worker() {
    while (cursor < selected.length) {
      const job = selected[cursor];
      cursor += 1;
      try {
        if (await refresh(job.task_id)) changed = true;
      } catch (error) {
        errors.push(error);
      } finally {
        inFlight.delete(job.task_id);
      }
    }
  }

  try {
    const workerCount = Math.min(Math.max(1, concurrency), selected.length);
    await Promise.all(Array.from({ length: workerCount }, () => worker()));
  } finally {
    cycle.running = false;
  }
  if (errors.length) throw errors[0];
  return changed;
}
